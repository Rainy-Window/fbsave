import json
import os
import time
import logging
import sqlite3
import shutil
import tempfile
import hashlib
import re
import requests
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(BASE_DIR, 'static', 'images')
os.makedirs(IMAGES_DIR, exist_ok=True)

_RTL_RE = re.compile(
    r"[\u0590-\u05FF\u0600-\u06FF\u0700-\u074F\u0750-\u077F"
    r"\u08A0-\u08FF\uFB1D-\uFDFD\uFE70-\uFEFC]"
)

TRACKING_PARAMS = {
    'fbclid', '__cft__', '__tn__', 'mibextid', 'refsrc', '_rdc', '_rdr',
    'sfnsn', 'extid', 'ref', 'acontext', 'action_history', 'source',
    '__xts__', 'hc_ref', 'fref', 'pnref', 'ref_component', 'ref_page',
    'ref_feature', 'notif_id', 'notif_t', 'ref_notif_type',
}


def strip_tracking(url: str) -> str:
    try:
        p = urlparse(url)
        qs = parse_qs(p.query, keep_blank_values=True)
        clean = {k: v for k, v in qs.items() if k.lower() not in TRACKING_PARAMS}
        new_query = urlencode(clean, doseq=True)
        return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, p.fragment))
    except Exception:
        return url


def detect_rtl(text: str) -> bool:
    return bool(_RTL_RE.search(text or ""))


def download_image(src: str, session: requests.Session) -> str:
    try:
        url_path = src.split("?")[0]
        ext = url_path.rsplit(".", 1)[-1].lower()
        ext = ext if ext in ("jpg", "jpeg", "png", "webp", "gif") else "jpg"
        filename   = hashlib.md5(url_path.encode()).hexdigest() + "." + ext
        local_path = os.path.join(IMAGES_DIR, filename)
        if os.path.exists(local_path):
            return f"/static/images/{filename}"
        r = session.get(src, timeout=15, stream=True)
        if r.status_code == 200:
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            logger.info(f"  ↓ saved: {filename}")
            return f"/static/images/{filename}"
    except Exception as e:
        logger.warning(f"  ⚠ image download failed ({e}), keeping CDN URL")
    return src


class FBScraper:
    def __init__(self, user_data_dir, status_dict, status_lock):
        self.user_data_dir = user_data_dir
        self.status_dict   = status_dict
        self.status_lock   = status_lock
        self.results_path  = os.path.join(BASE_DIR, 'data', 'results.json')
        os.makedirs(os.path.dirname(self.results_path), exist_ok=True)
        if not os.path.exists(self.results_path):
            with open(self.results_path, 'w') as f:
                json.dump([], f)

    def _update_status(self, message, current=None, total=None):
        with self.status_lock:
            self.status_dict["message"] = message
            if current is not None:
                self.status_dict["current"] = current
            if total is not None:
                self.status_dict["total"] = total

    def _load_results(self):
        try:
            with open(self.results_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _save_results(self, results):
        with open(self.results_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

    def _get_facebook_cookies(self):
        cookies_db = os.path.join(self.user_data_dir, 'cookies.sqlite')
        if not os.path.exists(cookies_db):
            raise FileNotFoundError(f"cookies.sqlite not found in: {self.user_data_dir}")

        tmp = tempfile.NamedTemporaryFile(suffix='.sqlite', delete=False)
        tmp.close()
        shutil.copy2(cookies_db, tmp.name)

        playwright_cookies = []
        requests_cookies   = {}

        try:
            conn   = sqlite3.connect(tmp.name)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT name, value, host, path, expiry, isSecure, isHttpOnly, sameSite
                FROM moz_cookies
                WHERE host LIKE '%facebook.com%'
            """)
            rows = cursor.fetchall()
            conn.close()

            same_site_map = {0: "None", 1: "Lax", 2: "Strict"}

            for name, value, host, path, expiry, is_secure, is_http_only, same_site in rows:
                try:
                    exp = int(expiry)
                except (TypeError, ValueError):
                    exp = -1
                if exp <= 0:
                    exp = -1

                ck = {
                    "name":     name,
                    "value":    value,
                    "domain":   host,
                    "path":     path,
                    "secure":   bool(is_secure),
                    "httpOnly": bool(is_http_only),
                    "sameSite": same_site_map.get(same_site, "None"),
                }
                if exp > 0:
                    ck["expires"] = float(exp) / 1000.0
                playwright_cookies.append(ck)
                requests_cookies[name] = value

        finally:
            os.unlink(tmp.name)

        if not playwright_cookies:
            raise ValueError("No Facebook cookies found. Make sure you are logged into Facebook in Firefox.")

        logger.info(f"Extracted {len(playwright_cookies)} Facebook cookies.")
        return playwright_cookies, requests_cookies

    def extract(self, urls, url_to_collection=None):
        url_to_collection = url_to_collection or {}
        results    = self._load_results()
        existing   = {r['url'] for r in results}
        to_process = [u for u in urls if u not in existing]
        total      = len(to_process)
        skipped    = len(urls) - total

        if skipped:
            logger.info(f"Skipping {skipped} already-extracted URLs.")

        self._update_status("Extracting cookies from Firefox...", current=0, total=total)
        playwright_cookies, requests_cookies = self._get_facebook_cookies()

        clean_cookies = []
        for ck in playwright_cookies:
            if not ck.get("name") or not ck.get("domain"):
                continue
            try:
                exp = float(ck.get("expires", -1))
            except (TypeError, ValueError):
                exp = -1
            # Firefox stores expiry in milliseconds, Playwright needs seconds
            if exp > 0:
                ck["expires"] = exp / 1000.0
            else:
                ck["expires"] = -1
            clean_cookies.append(ck)

        logger.info(f"Clean cookies ready: {len(clean_cookies)}")

        img_session = requests.Session()
        img_session.cookies.update(requests_cookies)
        img_session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Referer":    "https://www.facebook.com/",
        })

        self._update_status(f"Starting… {total} URLs to process.", current=0, total=total)

        with sync_playwright() as p:
            browser = p.firefox.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"
            )

            added = 0
            for ck in clean_cookies:
                try:
                    context.add_cookies([ck])
                    added += 1
                except Exception as e:
                    logger.warning(f"Skipped bad cookie '{ck.get('name')}': {e} | expires={ck.get('expires')} type={type(ck.get('expires')).__name__}")

            logger.info(f"Added {added}/{len(clean_cookies)} cookies to Playwright context.")

            page = context.new_page()

            self._update_status("Verifying Facebook session…", current=0, total=total)
            page.goto("https://www.facebook.com", wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            if "login" in page.url or "checkpoint" in page.url:
                context.close()
                browser.close()
                raise ValueError("Facebook session expired. Please re-login in Firefox and try again.")

            logger.info("Facebook session verified OK.")

            for i, url in enumerate(to_process):
                self._update_status(f"Extracting {i+1} of {total}…", current=i+1, total=total)
                logger.info(f"({i+1}/{total}) {url}")
                try:
                    data = self._scrape_url(page, url, img_session)
                    if data:
                        coll = url_to_collection.get(url, '')
                        data['collection'] = coll
                        data['url'] = strip_tracking(data['url'])
                        # Auto-add collection name as a tag so it appears in tag bar
                        # and can be edited/removed like any other tag
                        if coll and coll not in data.get('tags', []):
                            data.setdefault('tags', []).append(coll)
                        results.append(data)
                        self._save_results(results)
                except PlaywrightTimeoutError:
                    logger.warning(f"Timeout — skipping: {url}")
                except Exception as e:
                    logger.error(f"Error on {url}: {e}")
                finally:
                    time.sleep(2)

            context.close()
            browser.close()

    def _scrape_url(self, page, url, img_session):
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(3)

        if "/media/set/" in url or ("album" in url.lower() and "photo" not in url.lower()):
            return self._scrape_album(page, url, img_session)
        if "/events/" in url:
            return self._scrape_event(page, url, img_session)
        if "/videos/" in url or "/watch/" in url or "/reel/" in url:
            return self._scrape_video(page, url, img_session)
        if "photo.php" in url or "/photos/" in url:
            return self._scrape_photo(page, url, img_session)
        return self._scrape_post(page, url, img_session)

    def _check_unavailable(self, page) -> bool:
        try:
            main = page.query_selector('[role="main"]')
            text = (main.inner_text() if main else page.content()) or ""
        except Exception:
            text = page.inner_text('body') or ""
        markers = [
            "this content isn't available",
            "this content is not available",
            "content not available",
            "this page isn't available",
            "this post is no longer available",
        ]
        if not any(m in text.lower() for m in markers):
            return False
        article = page.query_selector('div[role="article"]')
        if article:
            try:
                if len((article.inner_text() or "").strip()) > 100:
                    return False
            except Exception:
                pass
        return True

    def _scrape_event(self, page, url, img_session):
        """Handler for Facebook event URLs."""
        data = {
            "url":          url,
            "author":       "Unknown",
            "author_url":   "",
            "author_thumb": "",
            "date":         "",
            "text":         "",
            "images":       [],
            "reactions":    "",
            "comments":     [],
            "collection":   "",
            "tags":         [],
            "type":         "event",
            "rtl":          False,
            "favorited":    False,
            "unavailable":  False,
        }

        if self._check_unavailable(page):
            data["unavailable"] = True
            return data

        # Title from og:title or h1
        title = page.evaluate("""
            () => {
                const og = document.querySelector('meta[property="og:title"]');
                if (og) { const c = og.getAttribute('content'); if (c) return c; }
                const h1 = document.querySelector('h1');
                return h1 ? h1.innerText.trim() : "";
            }
        """)
        data["text"] = title or ""

        # Cover image
        img_url = page.evaluate("""
            () => {
                const og = document.querySelector('meta[property="og:image"]');
                if (og) { const c = og.getAttribute('content'); if (c) return c; }
                const imgs = Array.from(document.querySelectorAll('img[src*="fbcdn.net"]'));
                let best = null, bestSize = 0;
                for (const img of imgs) {
                    const sz = img.naturalWidth * img.naturalHeight;
                    if (sz > bestSize && img.naturalWidth > 200) { bestSize = sz; best = img.src; }
                }
                return best || "";
            }
        """)
        if img_url:
            data["images"].append(download_image(img_url, img_session))

        # Date and organizer
        details = page.evaluate("""
            () => {
                const main = document.querySelector('[role="main"]') || document.body;
                let date = "";
                // Look for date-like text in divs
                for (const el of main.querySelectorAll('div[dir="auto"], span')) {
                    const t = (el.innerText||"").trim();
                    if (!t || t.length > 120) continue;
                    if (/\\d/.test(t) && (
                        /\\d{4}/.test(t) || /\\d+:\\d+/.test(t) ||
                        /(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|at\\s+\\d)/i.test(t)
                    )) { date = t; break; }
                }
                // Organizer: first profile-like link
                let author = "", authorUrl = "";
                for (const a of main.querySelectorAll('a[role="link"]')) {
                    const t = (a.innerText||"").trim();
                    const h = (a.href||"").toLowerCase();
                    if (!t || t.length < 2 || t.length > 80) continue;
                    if (h.includes('/events/') || h.includes('/login')) continue;
                    if (!/facebook\\.com/.test(h)) continue;
                    if (/^[0-9]+$/.test(t)) continue;
                    author = t; authorUrl = a.href; break;
                }
                return { date, author, authorUrl };
            }
        """)
        if details:
            data["date"]       = details.get("date", "")
            data["author"]     = details.get("author", "Unknown") or "Unknown"
            data["author_url"] = details.get("authorUrl", "")

        data["rtl"] = detect_rtl(data["text"] or data["author"])
        return data

    def _scrape_photo(self, page, url, img_session):
        """Dedicated handler for photo.php and /photos/ URLs."""
        data = {
            "url":          url,
            "author":       "Unknown",
            "author_url":   "",
            "author_thumb": "",
            "date":         "",
            "text":         "",
            "images":       [],
            "reactions":    "",
            "comments":     [],
            "collection":   "",
            "tags":         [],
            "type":         "photo",
            "rtl":          False,
            "favorited":    False,
            "unavailable":  False,
        }

        if self._check_unavailable(page):
            data["unavailable"] = True
            return data

        main_img = page.evaluate("""
            () => {
                const imgs = Array.from(document.querySelectorAll('img[src*="fbcdn.net"]'));
                let best = null, bestSize = 0;
                for (const img of imgs) {
                    const size = img.naturalWidth * img.naturalHeight;
                    if (size > bestSize && img.naturalWidth > 200) {
                        bestSize = size; best = img.src;
                    }
                }
                return best;
            }
        """)
        if main_img:
            data["images"].append(download_image(main_img, img_session))

        author_data = page.evaluate("""
            () => {
                const BAD = ["login","forgotten","signup","checkpoint","home","feed",
                             "watch","events","groups","photo.php","/photos/"];
                for (const sel of ['h2 a','h3 a','strong a','[role="main"] a[role="link"]']) {
                    for (const a of document.querySelectorAll(sel)) {
                        const t = (a.innerText||"").trim();
                        const h = (a.href||"").toLowerCase();
                        if (!t || t.length < 2 || t.length > 80) continue;
                        if (BAD.some(b => h.includes(b))) continue;
                        if (/^[0-9]+$/.test(t)) continue;
                        let thumb = "", el = a;
                        for (let i = 0; i < 10; i++) {
                            el = el.parentElement; if (!el) break;
                            const imgs = el.querySelectorAll('img[src*="fbcdn.net"]');
                            for (const img of imgs) {
                                const w = img.naturalWidth;
                                if (w === 0 || (w >= 20 && w <= 200)) { thumb = img.src; break; }
                            }
                            if (thumb) break;
                        }
                        return { name: t, url: a.href, thumb };
                    }
                }
                return null;
            }
        """)
        if author_data:
            data["author"]     = author_data.get("name", "Unknown")
            data["author_url"] = author_data.get("url", "")
            ts = author_data.get("thumb", "")
            if ts:
                data["author_thumb"] = download_image(ts, img_session)

        caption = page.evaluate("""
            () => {
                const sels = [
                    '[data-ad-preview="message"]',
                    '[data-ad-comet-preview="message"]',
                    'div[dir="auto"]'
                ];
                let best = "";
                for (const sel of sels) {
                    for (const el of document.querySelectorAll(sel)) {
                        const t = el.innerText.trim();
                        if (t && t.length > best.length) best = t;
                    }
                    if (best) break;
                }
                return best;
            }
        """)
        data["text"] = caption or ""
        data["rtl"]  = detect_rtl(data["text"] or data["author"])

        date_val = page.evaluate("""
            () => {
                const abbr = document.querySelector('abbr[data-utime]');
                if (abbr) return abbr.getAttribute('title') || abbr.innerText || "";
                for (const a of document.querySelectorAll('a[role="link"]')) {
                    const label = a.getAttribute('aria-label') || '';
                    if (label && /\\d/.test(label) && label.length < 80 &&
                        (/\\d{4}/.test(label) || /\\d+\\s+(hour|minute|day|week|month)/i.test(label)
                         || label.includes(' at '))) {
                        return label;
                    }
                }
                return "";
            }
        """)
        data["date"] = date_val.strip() if date_val else ""
        data["comments"] = self._extract_comments(page, img_session)
        return data

    def _scrape_video(self, page, url, img_session):
        """Handler for video/watch/reel URLs."""
        data = {
            "url":          url,
            "author":       "Unknown",
            "author_url":   "",
            "author_thumb": "",
            "date":         "",
            "text":         "",
            "video_title":  "",
            "images":       [],
            "reactions":    "",
            "comments":     [],
            "collection":   "",
            "tags":         [],
            "type":         "video",
            "rtl":          False,
            "favorited":    False,
            "unavailable":  False,
        }

        if self._check_unavailable(page):
            data["unavailable"] = True
            return data

        # Video pages sometimes load only comment articles; check for real content
        _has_main = page.query_selector('[role="main"]')
        if not _has_main:
            data["unavailable"] = True
            return data

        video_title = page.evaluate("""
            () => {
                const og = document.querySelector('meta[property="og:title"]');
                if (og) return og.getAttribute('content') || "";
                const h1 = document.querySelector('h1');
                return h1 ? h1.innerText.trim() : "";
            }
        """)
        data["video_title"] = video_title or ""

        thumb_url = page.evaluate("""
            () => {
                const og = document.querySelector('meta[property="og:image"]');
                if (og) return og.getAttribute('content') || "";
                const imgs = Array.from(document.querySelectorAll('img[src*="fbcdn.net"]'));
                let best = null, bestSize = 0;
                for (const img of imgs) {
                    const size = img.naturalWidth * img.naturalHeight;
                    if (size > bestSize && img.naturalWidth > 100) { bestSize = size; best = img.src; }
                }
                return best || "";
            }
        """)
        if thumb_url:
            data["images"].append(download_image(thumb_url, img_session))

        text = page.evaluate("""
            () => {
                const sels = [
                    'div[data-ad-preview="message"]',
                    'div[data-ad-comet-preview="message"]',
                    '[role="main"] div[dir="auto"]'
                ];
                let best = "";
                for (const sel of sels) {
                    for (const el of document.querySelectorAll(sel)) {
                        const t = el.innerText.trim();
                        if (t && t.length > best.length) best = t;
                    }
                    if (best) break;
                }
                return best;
            }
        """)
        data["text"] = text or ""
        data["rtl"]  = detect_rtl(data["text"] or data["video_title"])

        author_data = page.evaluate("""
            () => {
                const BAD = ["login","forgotten","signup","checkpoint","watch","videos","reels"];
                const article = document.querySelector('[role="main"]') || document.body;
                for (const sel of ['h2 a','h3 a','strong a','a[role="link"]']) {
                    for (const a of article.querySelectorAll(sel)) {
                        const t = (a.innerText||"").trim();
                        const h = (a.href||"").toLowerCase();
                        if (!t || t.length < 2 || t.length > 80) continue;
                        if (BAD.some(b => h.includes('/' + b + '/'))) continue;
                        if (/^[0-9]+$/.test(t)) continue;
                        return { name: t, url: a.href, thumb: "" };
                    }
                }
                return null;
            }
        """)
        if author_data:
            data["author"]     = author_data.get("name", "Unknown")
            data["author_url"] = author_data.get("url", "")

        date_val = page.evaluate("""
            () => {
                const abbr = document.querySelector('abbr[data-utime]');
                if (abbr) return abbr.getAttribute('title') || abbr.innerText || "";
                for (const a of document.querySelectorAll('a[role="link"]')) {
                    const label = a.getAttribute('aria-label') || '';
                    if (label && /\\d/.test(label) && label.length < 80 &&
                        (/\\d{4}/.test(label) || label.includes(' at '))) return label;
                }
                return "";
            }
        """)
        data["date"] = date_val.strip() if date_val else ""

        reactions = page.evaluate("""
            () => {
                const el = document.querySelector('[aria-label*="reaction"], [aria-label*="reacted"]');
                return el ? el.getAttribute('aria-label') || "" : "";
            }
        """)
        data["reactions"] = reactions or ""
        data["comments"]  = self._extract_comments(page, img_session)
        return data

    def _scrape_post(self, page, url, img_session):
        data = {
            "url":          url,
            "author":       "Unknown",
            "author_url":   "",
            "author_thumb": "",
            "date":         "",
            "text":         "",
            "images":       [],
            "reactions":    "",
            "comments":     [],
            "collection":   "",
            "tags":         [],
            "type":         "post",
            "rtl":          False,
            "favorited":    False,
            "unavailable":  False,
            "group_name":   "",
            "group_url":    "",
        }

        if self._check_unavailable(page):
            data["unavailable"] = True
            return data

        # If no real article exists (private/inaccessible group post), mark unavailable
        _article_check = page.query_selector('div[role="article"]:not([tabindex="-1"])')
        if not _article_check:
            data["unavailable"] = True
            return data

        # Primary author detection (h2/h3/strong — works for regular posts)
        author_data = page.evaluate("""
            () => {
                const BAD = ["login","forgotten","signup","checkpoint","home","feed",
                             "watch","events","groups"];
                const article = document.querySelector('div[role="article"]:not([tabindex="-1"])');
                if (!article) return null;
                for (const sel of ['h2 a','h3 a','strong a']) {
                    for (const a of article.querySelectorAll(sel)) {
                        const t = (a.innerText||"").trim();
                        const h = (a.href||"").toLowerCase();
                        if (!t || t.length < 2 || t.length > 80) continue;
                        if (BAD.some(b => h.includes('/' + b))) continue;
                        if (/^[0-9]+$/.test(t)) continue;
                        let thumb = "", el = a;
                        for (let i = 0; i < 10; i++) {
                            el = el.parentElement; if (!el) break;
                            const imgs = el.querySelectorAll('img[src*="fbcdn.net"]');
                            for (const img of imgs) {
                                const w = img.naturalWidth;
                                if (w === 0 || (w >= 20 && w <= 200)) { thumb = img.src; break; }
                            }
                            if (thumb) break;
                        }
                        return { name: t, url: a.href, thumb };
                    }
                }
                return null;
            }
        """)
        if author_data:
            data["author"]     = author_data.get("name", "Unknown")
            data["author_url"] = author_data.get("url", "")
            ts = author_data.get("thumb", "")
            if ts:
                data["author_thumb"] = download_image(ts, img_session)

        # Fallback for group posts: h4 + a[role="link"] scan
        if data["author"] == "Unknown":
            author_data = page.evaluate("""
                () => {
                    const BAD = ["login","forgotten","signup","checkpoint","home","feed",
                                 "watch","events","groups","permalink","photo","photos",
                                 "videos","reels","marketplace","notifications","friends",
                                 "saved","bookmarks"];
                    const article = document.querySelector('div[role="article"]:not([tabindex="-1"])');
                    if (!article) return null;
                    for (const sel of ['h4 a', 'a[role="link"]']) {
                        for (const a of article.querySelectorAll(sel)) {
                            const t = (a.innerText||"").trim();
                            const h = (a.href||"").toLowerCase();
                            if (!t || t.length < 2 || t.length > 80) continue;
                            if (BAD.some(b => h.includes('/' + b))) continue;
                            if (/^[0-9]+$/.test(t)) continue;
                            if (!h.includes('facebook.com')) continue;
                            if (h.includes('/groups/') || h.includes('/events/')) continue;
                            // Skip if the visible text is an external URL shared in the post
                            if (t.startsWith('http') || t.includes('://') || t.startsWith('www.')) continue;
                            let thumb = "", el = a;
                            for (let i = 0; i < 10; i++) {
                                el = el.parentElement; if (!el) break;
                                const imgs = el.querySelectorAll('img[src*="fbcdn.net"]');
                                for (const img of imgs) {
                                    const w = img.naturalWidth;
                                    if (w === 0 || (w >= 20 && w <= 200)) { thumb = img.src; break; }
                                }
                                if (thumb) break;
                            }
                            return { name: t, url: a.href, thumb };
                        }
                    }
                    return null;
                }
            """)
            if author_data:
                data["author"]     = author_data.get("name", "Unknown")
                data["author_url"] = author_data.get("url", "")
                ts = author_data.get("thumb", "")
                if ts:
                    data["author_thumb"] = download_image(ts, img_session)
        # Stage 3: non-member group post — poster name rendered as div[role="button"]
        # (happens when scraper is not a member; no href available)
        if data["author"] == "Unknown" and '/groups/' in url:
            author_data = page.evaluate("""
                () => {
                    const BAD = new Set(['join','follow','like','comment','share',
                                         'save','send','more','hide','report']);
                    const article = document.querySelector(
                        'div[role="article"]:not([tabindex="-1"])');
                    if (!article) return null;
                    // Find the date link so we only look before it (header area)
                    let dateLink = null;
                    for (const a of article.querySelectorAll('a[role="link"]')) {
                        const lbl = (a.getAttribute('aria-label') || '');
                        if (/\\d{4}|\\d+\\s*(hour|minute|day|week|month)/i.test(lbl)) {
                            dateLink = a; break;
                        }
                    }
                    for (const btn of article.querySelectorAll('div[role="button"]')) {
                        const t = (btn.innerText || '').trim();
                        if (!t || t.length < 2 || t.length > 80) continue;
                        if (BAD.has(t.toLowerCase())) continue;
                        if (/^\\d/.test(t)) continue;
                        // Must appear before the date link in DOM
                        if (dateLink &&
                            !(btn.compareDocumentPosition(dateLink) & 4)) continue; // 4 = Node.DOCUMENT_POSITION_FOLLOWING
                        return { name: t, url: '', thumb: '' };
                    }
                    return null;
                }
            """)
            if author_data:
                data["author"] = author_data.get("name", "Unknown")
                # No URL available for non-member view
                # Extract group name for group posts
        if '/groups/' in url:
            gdata = page.evaluate("""
                () => {
                    for (const a of document.querySelectorAll('a[href*="/groups/"]')) {
                        const t = (a.innerText||"").trim();
                        const h = a.href || "";
                        if (t && t.length > 2 && t.length < 100 && !/^\\d+$/.test(t))
                            return { name: t, url: h };
                    }
                    return { name: "", url: "" };
                }
            """)
            data['group_name'] = (gdata.get('name') or '').strip()
            data['group_url']  = (gdata.get('url')  or '').strip()
            
        date_val = page.evaluate("""
            () => {
                const article = document.querySelector('div[role="article"]:not([tabindex="-1"])');
                if (!article) return "";
                const abbr = article.querySelector('abbr[data-utime]');
                if (abbr) return abbr.getAttribute('title') || abbr.innerText || "";
                for (const a of article.querySelectorAll('a[role="link"]')) {
                    const label = a.getAttribute('aria-label') || '';
                    if (label && /\\d/.test(label) && label.length < 80 &&
                        (/\\d{4}/.test(label) || /\\d+\\s+(hour|minute|day|week|month)/i.test(label)
                         || label.includes(' at '))) {
                        return label;
                    }
                    const span = a.querySelector('span');
                    if (span) {
                        const t = span.innerText.trim();
                        if (t && t.length > 2 && t.length < 40 && /\\d/.test(t)
                            && !/^[0-9,]+$/.test(t)) return t;
                    }
                }
                return "";
            }
        """)
        data["date"] = date_val.strip() if date_val else ""

        reactions = page.evaluate("""
            () => {
                const article = document.querySelector('div[role="article"]:not([tabindex="-1"])');
                if (!article) return "";
                const el = article.querySelector('[aria-label*="reaction"], [aria-label*="reacted"]');
                return el ? el.getAttribute('aria-label') || "" : "";
            }
        """)
        data["reactions"] = reactions or ""

        text_val = page.evaluate("""
            () => {
                const article = document.querySelector('div[role="article"]:not([tabindex="-1"])');
                if (!article) return "";
                for (const sel of [
                    '[data-ad-preview="message"]',
                    '[data-ad-comet-preview="message"]',
                    'div[dir="auto"]'
                ]) {
                    let best = "";
                    for (const el of article.querySelectorAll(sel)) {
                        let p = el.parentElement, nested = false;
                        while (p && p !== article) {
                            if (p.getAttribute('role') === 'article') { nested = true; break; }
                            p = p.parentElement;
                        }
                        if (nested) continue;
                        const t = el.innerText.trim();
                        if (t && t.length > best.length) best = t;
                    }
                    if (best) return best;
                }
                return "";
            }
        """)
        if text_val:
            data["text"] = text_val

        data["rtl"] = detect_rtl(data["text"] or data["author"])

        for _ in range(4):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1)
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(1)

        imgs_data = page.evaluate("""
            () => {
                const article = document.querySelector('div[role="article"]:not([tabindex="-1"])');
                if (!article) return [];
                const allImgs = article.querySelectorAll('img[src*="fbcdn.net"]');
                const results = [];
                for (const img of allImgs) {
                    let el = img, inComment = false;
                    for (let i = 0; i < 15; i++) {
                        el = el.parentElement; if (!el) break;
                        const role = el.getAttribute('role');
                        const al   = (el.getAttribute('aria-label')||'').toLowerCase();
                        if (role === 'article' && i > 0) { inComment = true; break; }
                        if (al.includes('comment') || al.includes('reply')) { inComment = true; break; }
                    }
                    if (inComment) continue;
                    results.push({ src: img.src, w: img.naturalWidth, h: img.naturalHeight });
                }
                return results;
            }
        """)
        seen = set()
        thumb_path = data["author_thumb"].split("?")[0] if data["author_thumb"] else ""
        thumb_set = {thumb_path} if thumb_path else set()
        for img in imgs_data:
            src = img.get('src', '')
            w, h = img.get('w', 0), img.get('h', 0)
            if not src or "/cp0/" in src or "/safe_image" in src: continue
            if (w > 0 or h > 0) and (w < 100 or h < 100): continue
            src_path = src.split("?")[0]
            if src_path in seen or src_path in thumb_set: continue
            seen.add(src_path)
            data["images"].append(download_image(src, img_session))

        # Follow individual photo links to get images beyond the 5-image collage cap
        photo_links = page.eval_on_selector_all(
            'div[role="article"]:not([tabindex="-1"]) a[href*="/photo"]',
            'els => [...new Set(els.map(e => e.href))].filter(h => /\\/photos?[\\/\\?]|photo\\.php/.test(h))'
        )
        original_url = page.url
        for plink in photo_links[:30]:
            try:
                page.goto(plink, wait_until="domcontentloaded", timeout=20000)
                time.sleep(1)
                best = page.evaluate("""
                    () => {
                        const imgs = Array.from(document.querySelectorAll('img[src*="fbcdn.net"]'));
                        let best = null, bestSize = 0;
                        for (const img of imgs) {
                            const sz = (img.naturalWidth||0) * (img.naturalHeight||0);
                            if (sz > bestSize && (img.naturalWidth||0) > 200) {
                                bestSize = sz; best = img.src;
                            }
                        }
                        // fallback: largest img by rendered size
                        if (!best) {
                            for (const img of imgs) {
                                const sz = img.width * img.height;
                                if (sz > bestSize && img.width > 200) { bestSize = sz; best = img.src; }
                            }
                        }
                        return best || "";
                    }
                """)
                if best:
                    best_path = best.split("?")[0]
                    if best_path not in seen and best_path not in thumb_set:
                        seen.add(best_path)
                        data["images"].append(download_image(best, img_session))
            except Exception as e:
                logger.warning(f"Photo link failed ({plink}): {e}")
        if photo_links:
            try:
                page.goto(original_url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(1)
            except Exception:
                pass

        for _ in range(5):
            clicked = False
            for btn_text in ["View more comments", "Most relevant",
                             "View previous comments", "View more replies",
                             "View more", "See more comments"]:
                try:
                    btn = page.locator(f"text='{btn_text}'").first
                    if btn.is_visible(timeout=500):
                        btn.click()
                        time.sleep(0.8)
                        clicked = True
                except Exception:
                    pass
            if not clicked:
                break

        data["comments"] = self._extract_comments(page, img_session)
        return data

    def _extract_comments(self, page, img_session=None):
        """Shared comment extraction logic."""
        comments_raw = page.evaluate("""
            () => {
                const BADGES = new Set([
                    "author","top fan","admin","moderator","follow","following",
                    "like","reply","see translation","log in","forgotten account?",
                    "sign up","share","comment","most relevant"
                ]);
                const BAD_HREF = [
                    "login","forgotten","signup","register","checkpoint",
                    "/videos/","/watch/","/reels/","/events/","/groups/"
                ];
                const results = [];
                const seen = new Set();

                const root = document.querySelector('div[role="article"]:not([tabindex="-1"])') || document.querySelector('[role="feed"]') || document.body;
                const authorLinks = root.querySelectorAll('a[attributionsrc]');

                for (const a of authorLinks) {
                    try {
                        const href = (a.href||"").toLowerCase();
                        if (BAD_HREF.some(b => href.includes(b))) continue;

                        const name = (a.innerText||"").trim();
                        if (!name || name.length < 2 || name.length > 80) continue;
                        if (BADGES.has(name.toLowerCase())) continue;

                        // Walk up to find comment container
                        let container = a, textEl = null;
                        for (let depth = 0; depth < 8; depth++) {
                            container = container.parentElement;
                            if (!container) break;
                            for (const child of container.children) {
                                if (child.contains(a)) continue;
                                const t = (child.innerText||"").trim();
                                if (t && t.length >= 3 && !BADGES.has(t.toLowerCase())) {
                                    textEl = child; break;
                                }
                            }
                            if (textEl) break;
                        }
                        if (!textEl) continue;

                        const commentText = textEl.innerText.trim();
                        if (!commentText || commentText.length < 3) continue;
                        if (commentText === name) continue;

                        // Deduplicate by comment text alone — same text = same comment regardless
                        // of how many times Facebook renders the author link in the DOM
                        const key = commentText.slice(0, 120);
                        if (seen.has(key)) continue;
                        seen.add(key);

                        const avatarSrc = "";

                        // Replies: scan only the next 3 sibling elements of container
                        const replies = [];
                        const replySeen = new Set();
                        let siblingEl = container.nextElementSibling;
                        let sibCount = 0;
                        while (siblingEl && replies.length < 3 && sibCount < 3) {
                            const replyLinks = siblingEl.querySelectorAll('a[attributionsrc], a[role="link"]');
                            for (const ra of replyLinks) {
                                if (ra === a) continue;
                                const rHref = (ra.href||"").toLowerCase();
                                if (BAD_HREF.some(b => rHref.includes(b))) continue;
                                const rName = (ra.innerText||"").trim();
                                if (!rName || rName.length < 2 || BADGES.has(rName.toLowerCase())) continue;

                                let rContainer = ra, rTextEl = null;
                                for (let depth = 0; depth < 8; depth++) {
                                    rContainer = rContainer.parentElement;
                                    if (!rContainer || rContainer === siblingEl) break;
                                    for (const child of rContainer.children) {
                                        if (child.contains(ra)) continue;
                                        const t = (child.innerText||"").trim();
                                        if (t && t.length >= 3 && !BADGES.has(t.toLowerCase())) {
                                            rTextEl = child; break;
                                        }
                                    }
                                    if (rTextEl) break;
                                }
                                if (!rTextEl) continue;
                                const rText = rTextEl.innerText.trim();
                                if (!rText || rText === rName || rText.length < 3) continue;

                                const rKey = (ra.href || rName) + "|||" + rText.slice(0, 80);
                                if (replySeen.has(rKey) || seen.has(rKey)) continue;
                                replySeen.add(rKey);
                                seen.add(rKey);

                                const rAvatarSrc = "";

                                replies.push({ author: rName, author_url: ra.href, text: rText, avatar_src: rAvatarSrc });
                                if (replies.length >= 3) break;
                            }
                            siblingEl = siblingEl.nextElementSibling;
                            sibCount++;
                        }

                        results.push({ author: name, author_url: a.href, text: commentText, replies, avatar_src: avatarSrc });
                    } catch(e) {}
                    if (results.length >= 50) break;
                }
                return results;
            }
        """)

        comments = []
        for c in (comments_raw or []):
            ca, ct, cu = c.get('author', '').strip(), c.get('text', '').strip(), c.get('author_url', '')
            if ca and ct and ca != ct:
                # Store CDN URL directly — no download, avatars are small and display fine from CDN
                thumb = c.get('avatar_src', '')
                replies = []
                for r in c.get('replies', []):
                    ra, rt, ru = r.get('author', '').strip(), r.get('text', '').strip(), r.get('author_url', '')
                    if ra and rt and ra != rt:
                        rthumb = r.get('avatar_src', '')
                        replies.append({"author": ra, "author_url": ru, "text": rt, "rtl": detect_rtl(rt), "author_thumb": rthumb})
                comments.append({"author": ca, "author_url": cu, "text": ct, "rtl": detect_rtl(ct), "replies": replies, "author_thumb": thumb})
        return comments

    def _scrape_album(self, page, url, img_session):
        logger.info(f"Album detected: {url}")
        data = self._scrape_post(page, url, img_session)
        data["type"] = "album"

        for _ in range(5):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.5)
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(1)

        photo_links = list(set(page.eval_on_selector_all(
            'a[href*="/photo.php"], a[href*="/photos/"]',
            'els => els.map(e => e.href)'
        )))
        photo_links = [l for l in photo_links if "/photo.php" in l or "/photos/" in l]

        album_images = []
        for link in photo_links[:50]:
            try:
                page.goto(link, wait_until="domcontentloaded", timeout=30000)
                time.sleep(1)
                img_el = page.query_selector('img[class*="x1lliihq"], img[src*="fbcdn.net"][alt="Image"]')
                if img_el:
                    src = img_el.get_attribute("src") or ""
                    if src and "/cp0/" not in src and src not in album_images:
                        album_images.append(download_image(src, img_session))
            except Exception as e:
                logger.error(f"Album photo error on {link}: {e}")

        if album_images:
            # Merge and deduplicate
            data["images"] = list(dict.fromkeys(data["images"] + album_images))
        return data
