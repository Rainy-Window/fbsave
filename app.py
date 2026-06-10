from flask import Flask, render_template, request, jsonify, send_from_directory
import os
import re
import json
import threading
import pathlib
import logging
from collections import Counter
from werkzeug.utils import secure_filename
from scraper import FBScraper
from flask_wtf.csrf import CSRFProtect

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
csrf = CSRFProtect(app)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", os.urandom(24))
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DATA_FILE     = os.path.join(BASE_DIR, 'data', 'results.json')
BIN_FILE      = os.path.join(BASE_DIR, 'data', 'recycle_bin.json')
CONFIG_FILE   = os.path.join(BASE_DIR, 'data', 'config.json')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')

for d in ['data', 'static', 'templates', 'uploads']:
    os.makedirs(os.path.join(BASE_DIR, d), exist_ok=True)

status_lock = threading.Lock()
extraction_status = {"running": False, "message": "Idle", "current": 0, "total": 0}

# ── URL / encoding helpers ────────────────────────────────────────────────────

def _fix_mojibake(s):
    """Fix UTF-8 bytes that were misread as latin-1 and JSON-escaped.
    e.g. \\u00d9\\u0084 → ل"""
    if not isinstance(s, str) or not s:
        return s
    try:
        return s.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _clean_url(url):
    """Strip non-URL characters (e.g. Arabic annotations appended to URLs),
    return None if the result is not a usable Facebook post URL."""
    if not url:
        return None
    url = url.strip()
    # Keep only valid URL characters (stops before Arabic / non-ASCII text)
    m = re.match(r'(https?://[A-Za-z0-9\-._~:/?#\[\]@!$&\'()*+,;=%]+)', url)
    if not m:
        return None
    url = m.group(1)
    if 'facebook.com' not in url:
        return None
    # Must look like a scrapable content URL, not a bare profile page
    post_signals = [
        '/posts/', '/videos/', '/photo', '/photos/', '/permalink',
        '/groups/', '/reel/', '/watch/', '/media/set/',
        'story_fbid=', 'fbid=', '/events/',
    ]
    if not any(sig in url for sig in post_signals):
        logger.info(f"Skipping non-post URL: {url}")
        return None
    return url

# ── Core helpers ──────────────────────────────────────────────────────────────

def validate_profile_path(path):
    p = pathlib.Path(path).expanduser().resolve()
    if not p.exists() or not p.is_dir():
        raise ValueError("Invalid or inaccessible Firefox profile path.")
    if not (p / "prefs.js").exists():
        raise ValueError("Path does not appear to be a Firefox profile directory.")
    return str(p)


def load_json(path):
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_results():  return load_json(DATA_FILE)
def save_results(r): save_json(DATA_FILE, r)
def load_bin():      return load_json(BIN_FILE)
def save_bin(r):     save_json(BIN_FILE, r)


def save_config(profile_path):
    save_json(CONFIG_FILE, {"profile_path": profile_path})


def load_config():
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def extract_urls_from_dyi(data):
    url_collections = []

    if isinstance(data, dict) and "saves_v2" in data:
        for item in data["saves_v2"]:
            for att in item.get("attachments", []):
                for d in att.get("data", []):
                    url = d.get("external_context", {}).get("url", "")
                    url = _fix_mojibake(url)
                    url = _clean_url(url)
                    if url:
                        url_collections.append((url, "Saved Items"))

    elif isinstance(data, list):
        for collection in data:
            coll_name = "Saved Items"
            for lv in collection.get("label_values", []):
                if lv.get("title") in ("Name", "Collection Name"):
                    v = _fix_mojibake(lv.get("value", ""))
                    if v:
                        coll_name = v
                    break
            for lv in collection.get("label_values", []):
                if lv.get("title") == "Saves":
                    for si in lv.get("dict", []):
                        for field in si.get("dict", []):
                            if field.get("label") == "URL":
                                url = _fix_mojibake(field.get("value", ""))
                                url = _clean_url(url)
                                if url:
                                    url_collections.append((url, coll_name))

    # Deduplicate (first collection wins)
    seen = {}
    for url, coll in url_collections:
        if url not in seen:
            seen[url] = coll
    return list(seen.items())


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    results = load_results()
    with status_lock:
        current_status = extraction_status.copy()
    return render_template('index.html', results=results, status=current_status, config=load_config())


@app.route('/extract', methods=['POST'])
def extract():
    global extraction_status
    with status_lock:
        if extraction_status["running"]:
            return jsonify({"error": "Extraction already in progress"}), 409
        extraction_status = {"running": True, "message": "Starting...", "current": 0, "total": 0}

    data = request.get_json()
    urls = data.get('urls', [])
    profile_path = data.get('profile_path', '')
    if not urls or not profile_path:
        return jsonify({"error": "Missing URLs or profile path"}), 400

    try:
        vpp = validate_profile_path(profile_path)
        save_config(vpp)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    def run():
        global extraction_status
        try:
            FBScraper(vpp, extraction_status, status_lock).extract(urls)
            with status_lock:
                extraction_status = {"running": False, "message": "Extraction complete!", "current": len(urls), "total": len(urls)}
        except Exception as e:
            logger.error(f"Extraction failed: {e}")
            with status_lock:
                extraction_status["running"] = False
                extraction_status["message"] = "Extraction failed. Check server logs."

    threading.Thread(target=run).start()
    return jsonify({"status": "success"})


@app.route('/upload_dyi', methods=['POST'])
def upload_dyi():
    global extraction_status
    with status_lock:
        if extraction_status["running"]:
            return jsonify({"error": "Extraction already in progress"}), 409
        extraction_status = {"running": True, "message": "Parsing DYI file(s)...", "current": 0, "total": 0}

    profile_path = request.form.get('profile_path', '')
    if not profile_path:
        return jsonify({"error": "Firefox profile path is required"}), 400

    try:
        vpp = validate_profile_path(profile_path)
        save_config(vpp)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    uploaded_files = request.files.getlist('dyi_files')
    if not uploaded_files or all(f.filename == '' for f in uploaded_files):
        return jsonify({"error": "No file(s) selected"}), 400

    all_pairs = []
    saved_paths = []
    try:
        for file in uploaded_files:
            if file and file.filename.endswith(".json"):
                filename = secure_filename(file.filename)
                filepath = os.path.join(UPLOAD_FOLDER, filename)
                file.save(filepath)
                saved_paths.append(filepath)
                with open(filepath, 'r', encoding='utf-8') as f:
                    try:
                        pairs = extract_urls_from_dyi(json.load(f))
                        all_pairs.extend(pairs)
                    except json.JSONDecodeError:
                        return jsonify({"error": f"Invalid JSON: {filename}"}), 400

        seen = {}
        for url, coll in all_pairs:
            if url not in seen:
                seen[url] = coll
        pairs = list(seen.items())
        urls = [u for u, c in pairs]
        u2c  = {u: c for u, c in pairs}

        if not urls:
            return jsonify({"error": "No valid Facebook URLs found."}), 400

        def run():
            global extraction_status
            try:
                FBScraper(vpp, extraction_status, status_lock).extract(urls, url_to_collection=u2c)
                with status_lock:
                    extraction_status = {"running": False, "message": "Extraction complete!", "current": len(urls), "total": len(urls)}
            except Exception as e:
                logger.error(f"DYI extraction failed: {e}")
                with status_lock:
                    extraction_status["running"] = False
                    extraction_status["message"] = "Extraction failed."

        threading.Thread(target=run).start()
        return jsonify({"status": "success", "message": f"Started extraction of {len(urls)} URLs."})
    finally:
        for path in saved_paths:
            if os.path.exists(path):
                os.remove(path)


@app.route('/extraction_status')
def get_extraction_status():
    with status_lock:
        return jsonify(extraction_status)


@app.route('/reset_status', methods=['POST'])
@csrf.exempt
def reset_status():
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({"error": "Forbidden"}), 403
    with status_lock:
        extraction_status.update({"running": False, "message": "Idle", "current": 0, "total": 0})
    return jsonify({"status": "success"})


@app.route('/delete_post', methods=['POST'])
@csrf.exempt
def delete_post():
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    url  = data.get('url', '')
    if not url:
        return jsonify({"error": "Missing URL"}), 400

    results = load_results()
    original_index = next((i for i, r in enumerate(results) if r['url'] == url), None)
    post = next((r for r in results if r['url'] == url), None)
    if not post:
        return jsonify({"error": "Post not found"}), 404

    post = dict(post)
    if original_index is not None:
        post['_original_index'] = original_index

    bin_posts = load_bin()
    bin_posts.insert(0, post)
    save_bin(bin_posts)

    results = [r for r in results if r['url'] != url]
    save_results(results)
    return jsonify({"status": "success"})


@app.route('/restore_post', methods=['POST'])
@csrf.exempt
def restore_post():
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    url  = data.get('url', '')
    bin_posts = load_bin()
    post = next((r for r in bin_posts if r['url'] == url), None)
    if not post:
        return jsonify({"error": "Post not found in bin"}), 404

    original_index = post.pop('_original_index', None)

    results = load_results()
    if not any(r['url'] == url for r in results):
        if original_index is not None:
            results.insert(min(original_index, len(results)), post)
        else:
            results.append(post)
        save_results(results)

    bin_posts = [r for r in bin_posts if r['url'] != url]
    save_bin(bin_posts)
    return jsonify({"status": "success"})


@app.route('/empty_bin', methods=['POST'])
@csrf.exempt
def empty_bin():
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({"error": "Forbidden"}), 403
    bin_posts = load_bin()
    for post in bin_posts:
        for img_path in post.get('images', []):
            if img_path.startswith('/static/images/'):
                fp = os.path.join(BASE_DIR, img_path.lstrip('/'))
                if os.path.exists(fp):
                    os.remove(fp)
        thumb = post.get('author_thumb', '')
        if thumb.startswith('/static/images/'):
            fp = os.path.join(BASE_DIR, thumb.lstrip('/'))
            if os.path.exists(fp):
                os.remove(fp)
    save_bin([])
    return jsonify({"status": "success"})


@app.route('/get_bin')
def get_bin():
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({"error": "Forbidden"}), 403
    return jsonify(load_bin())


@app.route('/toggle_favorite', methods=['POST'])
@csrf.exempt
def toggle_favorite():
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    url  = data.get('url', '')
    results = load_results()
    for post in results:
        if post['url'] == url:
            post['favorited'] = not post.get('favorited', False)
            save_results(results)
            return jsonify({"status": "success", "favorited": post['favorited']})
    return jsonify({"error": "Post not found"}), 404


@app.route('/update_tags', methods=['POST'])
@csrf.exempt
def update_tags():
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    url  = data.get('url', '')
    tags = data.get('tags', [])
    results = load_results()
    for post in results:
        if post['url'] == url:
            post['tags'] = [t.strip() for t in tags if t.strip()]
            save_results(results)
            return jsonify({"status": "success", "tags": post['tags']})
    return jsonify({"error": "Post not found"}), 404


@app.route('/rescrape', methods=['POST'])
@csrf.exempt
def rescrape_post():
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({"error": "Forbidden"}), 403
    global extraction_status
    with status_lock:
        if extraction_status["running"]:
            return jsonify({"error": "Extraction already in progress"}), 409

    data = request.get_json()
    url  = data.get('url', '')
    if not url:
        return jsonify({"error": "Missing URL"}), 400

    config = load_config()
    profile_path = config.get('profile_path', '')
    if not profile_path:
        return jsonify({"error": "No profile path saved."}), 400

    try:
        vpp = validate_profile_path(profile_path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    all_results = load_results()
    original_index = next((i for i, r in enumerate(all_results) if r['url'] == url), None)
    original_urls = {r['url'] for r in all_results if r['url'] != url}

    results = [r for r in all_results if r['url'] != url]
    save_results(results)

    with status_lock:
        extraction_status = {"running": True, "message": "Re-scraping...", "current": 0, "total": 1}

    def run():
        global extraction_status
        try:
            FBScraper(vpp, extraction_status, status_lock).extract([url])
            if original_index is not None:
                current = load_results()
                new_idx = next((i for i, r in enumerate(current) if r['url'] not in original_urls), None)
                if new_idx is not None:
                    post = current.pop(new_idx)
                    current.insert(min(original_index, len(current)), post)
                    save_results(current)
            with status_lock:
                extraction_status = {"running": False, "message": "Re-scrape complete!", "current": 1, "total": 1}
        except Exception as e:
            logger.error(f"Re-scrape failed: {e}")
            with status_lock:
                extraction_status["running"] = False
                extraction_status["message"] = "Re-scrape failed."

    threading.Thread(target=run).start()
    return jsonify({"status": "success"})


@app.route('/get_stats')
def get_stats():
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({"error": "Forbidden"}), 403
    results = load_results()
    authors     = Counter(r.get('author', 'Unknown') for r in results)
    tags_c      = Counter(t for r in results for t in r.get('tags', []))
    collections = Counter(r.get('collection', '') for r in results if r.get('collection'))
    return jsonify({
        "total":         len(results),
        "with_images":   sum(1 for r in results if r.get('images')),
        "with_comments": sum(1 for r in results if r.get('comments')),
        "favorites":     sum(1 for r in results if r.get('favorited')),
        "unavailable":   sum(1 for r in results if r.get('unavailable')),
        "bin_count":     len(load_bin()),
        "top_authors":   authors.most_common(15),
        "top_tags":      tags_c.most_common(20),
        "collections":   collections.most_common(20),
    })


@app.route('/viewer')
def viewer():
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({"error": "Forbidden"}), 403
    return send_from_directory(os.path.join(BASE_DIR, 'static'), 'viewer.html')


@app.route('/data/results.json')
def get_results():
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({"error": "Forbidden"}), 403
    return send_from_directory(os.path.join(BASE_DIR, 'data'), 'results.json')


@app.route('/static/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(os.path.join(BASE_DIR, 'static', 'images'), filename)


if __name__ == '__main__':
    app.run(debug=False, host='127.0.0.1', port=5000)
