#!/usr/bin/env python3
"""
Export all saved items to a single self-contained offline HTML file.
Usage: python export_viewer.py
Output: fb_export_YYYYMMDD_HHMMSS.html  (no server needed to open)
"""
import json, os, base64, re
from datetime import datetime

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_FILE   = os.path.join(BASE_DIR, 'data', 'results.json')
IMAGES_DIR  = os.path.join(BASE_DIR, 'static', 'images')
VIEWER_FILE = os.path.join(BASE_DIR, 'static', 'viewer.html')

MIME = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
        'webp': 'image/webp', 'gif': 'image/gif'}


def to_b64(path):
    ext = path.rsplit('.', 1)[-1].lower()
    mime = MIME.get(ext, 'image/jpeg')
    try:
        with open(path, 'rb') as f:
            return f"data:{mime};base64,{base64.b64encode(f.read()).decode()}"
    except Exception:
        return ""


def main():
    print("Loading results...")
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        posts = json.load(f)
    print(f"Embedding images for {len(posts)} posts...")

    for post in posts:
        post['images'] = [
            to_b64(os.path.join(BASE_DIR, p.lstrip('/'))) if p.startswith('/static/images/') else p
            for p in post.get('images', [])
        ]
        if post.get('author_thumb', '').startswith('/static/images/'):
            b = to_b64(os.path.join(BASE_DIR, post['author_thumb'].lstrip('/')))
            if b:
                post['author_thumb'] = b

    print("Reading viewer template...")
    with open(VIEWER_FILE, 'r', encoding='utf-8') as f:
        html = f.read()

    inline = json.dumps(posts, ensure_ascii=False)

    # Replace loadResults to use inline data
    new_fn = f"""async function loadResults() {{
        try {{
            allPosts = {inline};
            updateStats();
            buildFilterBar();
            buildTagBar();
            applyFilters();
        }} catch(e) {{
            document.getElementById("feed").innerHTML = '<div class="post-card" style="padding:32px;text-align:center">Error loading data.</div>';
        }}
    }}"""

    html = re.sub(
        r'async function loadResults\(\)\s*\{.*?\}',
        new_fn,
        html,
        flags=re.DOTALL | re.MULTILINE
    )

    # Disable server calls (no server in offline mode)
    for route in ['/toggle_favorite', '/delete_post', '/update_tags', '/rescrape',
                  '/get_stats', '/get_bin', '/restore_post', '/empty_bin']:
        html = html.replace(
            f"fetch('{route}'",
            f"Promise.resolve({{ ok:false, json:()=>Promise.resolve({{error:'Offline mode'}}) }}) //fetch('{route}'"
        )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(BASE_DIR, f'fb_export_{timestamp}.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)

    size_mb = os.path.getsize(out) / 1024 / 1024
    print(f"Done! → {out}  ({size_mb:.1f} MB)")
    print("Open this file in any browser — no internet or server needed.")


if __name__ == '__main__':
    main()
