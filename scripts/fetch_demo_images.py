"""Download the demo image materials listed in demo_materials/manifest.json from
Wikimedia Commons and fill in their source / license fields.

Only items with a `commons_title` (e.g. "File:Kelmscott Chaucer.jpg") are touched.
Images are stored as <= 1400px JPEGs so the repo stays small; the manifest keeps the
link to the original file page and its license (spec §0.2 license gate).

Usage:  python scripts/fetch_demo_images.py [--force]
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "demo_materials" / "manifest.json"
IMG_DIR = ROOT / "demo_materials" / "images"
API = "https://commons.wikimedia.org/w/api.php"
UA = "MagpieMVP/0.1 (demo material fetch; https://github.com/echo-222/Magpie)"
MAX_SIDE = 1400


def fetch(url: str) -> bytes:
    # curl is noticeably more robust than urllib on flaky networks; retry hard.
    return subprocess.run(
        ["curl", "-sS", "-L", "-m", "120", "--retry", "8", "--retry-all-errors", "--retry-delay", "2", "-A", UA, url],
        capture_output=True,
        check=True,
    ).stdout


def imageinfo(title: str) -> dict:
    params = {
        "action": "query",
        "titles": title,
        "prop": "imageinfo",
        "iiprop": "url|size|extmetadata|mime",
        "iiurlwidth": str(MAX_SIDE),
        "format": "json",
    }
    data = json.loads(fetch(API + "?" + urllib.parse.urlencode(params)))
    page = next(iter(data["query"]["pages"].values()))
    if "imageinfo" not in page:
        raise SystemExit(f"not found on Commons: {title}")
    ii = page["imageinfo"][0]
    em = ii.get("extmetadata", {})
    strip = lambda s: re.sub(r"<[^>]+>", "", s or "").strip()  # noqa: E731
    return {
        "thumburl": ii.get("thumburl") or ii["url"],
        "url": ii["url"],
        "descriptionurl": ii["descriptionurl"],
        "license": em.get("LicenseShortName", {}).get("value"),
        "artist": strip(em.get("Artist", {}).get("value"))[:120],
        "description": strip(em.get("ImageDescription", {}).get("value"))[:300],
    }


def to_jpeg(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82, optimize=True)
    return buf.getvalue()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    args = ap.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    for item in manifest["items"]:
        title = item.get("commons_title")
        if not title or item["modality"] != "image":
            continue
        out = IMG_DIR / f"{item['key']}.jpg"
        item["file"] = f"images/{out.name}"
        if out.exists() and not args.force and item.get("source", {}).get("license"):
            print(f"skip  {out.name}")
            continue
        info = imageinfo(title)
        print(f"fetch {out.name:<32} {info['license']:<14} {info['thumburl'][:80]}")
        out.write_bytes(to_jpeg(fetch(info["thumburl"])))
        src = item.setdefault("source", {})
        src.update(
            {
                "page_url": info["descriptionurl"],
                "resource_url": info["url"],
                "page_title": title.removeprefix("File:"),
                "license": info["license"],
            }
        )
        item["attribution"] = info["artist"] or None
        if info["description"] and not item.get("commons_description"):
            item["commons_description"] = info["description"]
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("manifest updated")


if __name__ == "__main__":
    sys.exit(main())
