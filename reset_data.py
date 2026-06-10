#!/usr/bin/env python3
"""
Wipe all extracted data and start completely fresh.
Usage:
  python reset_data.py               # clears data + deletes images
  python reset_data.py --keep-images # clears data, keeps images
"""
import os, json, sys, shutil

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(BASE_DIR, 'data')
IMAGES_DIR = os.path.join(BASE_DIR, 'static', 'images')

keep_images = '--keep-images' in sys.argv

print("=" * 50)
print("  Facebook Saved Items — FULL RESET")
print("=" * 50)
print("This will permanently erase:")
print("  • data/results.json")
print("  • data/recycle_bin.json")
if not keep_images:
    img_count = len(os.listdir(IMAGES_DIR)) if os.path.isdir(IMAGES_DIR) else 0
    print(f"  • static/images/ ({img_count} files)")
else:
    print("  • static/images/ — SKIPPED (--keep-images)")
print()

confirm = input("Type  YES  to confirm, anything else to abort: ").strip()
if confirm != 'YES':
    print("Aborted. Nothing was changed.")
    sys.exit(0)

os.makedirs(DATA_DIR, exist_ok=True)
for fname in ['results.json', 'recycle_bin.json']:
    path = os.path.join(DATA_DIR, fname)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump([], f)
    print(f"  Cleared: data/{fname}")

if not keep_images:
    if os.path.isdir(IMAGES_DIR):
        shutil.rmtree(IMAGES_DIR)
    os.makedirs(IMAGES_DIR, exist_ok=True)
    print(f"  Cleared: static/images/")

print()
print("Done. Run  python app.py  and start fresh.")
