"""Image understanding built on mature dependencies.

* Pillow            – format / size / EXIF / dominant colours (median-cut quantisation)
* RapidOCR (opt.)   – OCR (PP-OCR models on onnxruntime); falls back to the vision model
* vision LLM        – concise description, subjects, style keywords, mood, visible text
"""

from __future__ import annotations

import colorsys
import io
import logging
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from ..llm import LLM
from ..models import ColorSwatch

log = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = 80_000_000

VISION_SYSTEM = (
    "You are an image analyst for a designer's personal material library. "
    "Describe what is objectively in the image and how it looks as visual material. "
    "Be concrete and concise. Do not judge quality. Do not invent text that is not visible. "
    "Answer in the JSON schema requested; write `summary` in Chinese, keywords may be Chinese or short English design terms."
)

VISION_USER = """Analyse this image and return ONLY JSON:
{
  "summary": "1-2 sentences, Chinese: what this is and how it looks as design material",
  "subjects": ["main visible subjects, 1-5 short nouns"],
  "style": ["3-6 visual/style keywords: e.g. 粗糙纸张质感, 网格排版, 极简, 高对比, brutalist, letterpress"],
  "keywords": ["4-8 retrieval keywords covering medium, era/genre, material, technique, use-case"],
  "mood": ["1-4 mood words, e.g. 克制, 温暖, 冷峻"],
  "visible_text": "any clearly legible text in the image, verbatim, or empty string"
}"""


# --------------------------------------------------------------------------- file-derived


def load_image(path: Path) -> Image.Image:
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return img


def file_metadata(path: Path) -> dict:
    with Image.open(path) as img:
        info: dict = {
            "format": img.format,
            "width": img.width,
            "height": img.height,
            "mode": img.mode,
            "has_alpha": img.mode in ("RGBA", "LA") or "transparency" in img.info,
            "file_size": path.stat().st_size,
            "aspect_ratio": round(img.width / img.height, 3) if img.height else None,
        }
        try:
            exif = img.getexif()
            keep = {271: "make", 272: "model", 305: "software", 306: "datetime", 315: "artist"}
            ex = {name: str(exif[tag]) for tag, name in keep.items() if tag in exif}
            if ex:
                info["exif"] = ex
        except Exception:  # noqa: BLE001 - EXIF is best-effort
            pass
    return info


def _hue_family(r: int, g: int, b: int) -> str:
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    hue = h * 360
    if s < 0.10:
        if l > 0.88:
            return "white"
        if l < 0.14:
            return "black"
        return "grey"
    if s < 0.32 and 15 <= hue <= 65:
        return "warm neutral"  # paper, kraft, sand, linen
    if s < 0.22:
        return "muted " + _hue_name(hue)
    if 15 <= hue < 45 and l < 0.42:
        return "brown"
    return _hue_name(hue)


def _hue_name(hue: float) -> str:
    if hue < 15 or hue >= 345:
        return "red"
    if hue < 45:
        return "orange"
    if hue < 70:
        return "yellow"
    if hue < 160:
        return "green"
    if hue < 200:
        return "teal"
    if hue < 250:
        return "blue"
    if hue < 290:
        return "purple"
    return "pink"


def dominant_colors(img: Image.Image, n: int = 6) -> tuple[list[ColorSwatch], dict]:
    """Median-cut palette via Pillow plus a few cheap palette descriptors.

    Descriptors (saturation / lightness / hue families) are file-derived facts that
    make colour language ("低饱和", "暖色", "蓝紫") searchable without a model.
    """
    small = img.convert("RGB")
    small.thumbnail((160, 160))
    quant = small.quantize(colors=n, method=Image.Quantize.MEDIANCUT)
    palette = quant.getpalette()
    counts = quant.getcolors() or []
    total = sum(c for c, _ in counts) or 1
    swatches: list[ColorSwatch] = []
    for count, idx in sorted(counts, reverse=True):
        r, g, b = palette[idx * 3 : idx * 3 + 3]
        swatches.append(ColorSwatch(hex=f"#{r:02x}{g:02x}{b:02x}", ratio=round(count / total, 3), name=_hue_family(r, g, b)))

    # global stats on the thumbnail
    px = np.asarray(small, dtype=np.uint8).reshape(-1, 3)
    sat = lig = 0.0
    fam: dict[str, int] = {}
    step = max(1, len(px) // 4000)
    sample = [tuple(int(v) for v in row) for row in px[::step]]
    for r, g, b in sample:
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        sat += s
        lig += l
        f = _hue_family(r, g, b)
        fam[f] = fam.get(f, 0) + 1
    k = len(sample) or 1
    mean_s, mean_l = sat / k, lig / k
    fam_ratio = {f: round(c / k, 3) for f, c in sorted(fam.items(), key=lambda kv: -kv[1])}
    descriptors: list[str] = []
    descriptors.append("low saturation" if mean_s < 0.25 else "medium saturation" if mean_s < 0.5 else "high saturation")
    descriptors.append("dark" if mean_l < 0.35 else "light" if mean_l > 0.65 else "mid-tone")
    warm = sum(v for f, v in fam_ratio.items() if any(w in f for w in ("red", "orange", "yellow", "brown", "warm")))
    cool = sum(v for f, v in fam_ratio.items() if any(c in f for c in ("blue", "teal", "purple")))
    if warm > 0.35 and warm > cool * 1.5:
        descriptors.append("warm")
    elif cool > 0.35 and cool > warm * 1.5:
        descriptors.append("cool")
    if fam_ratio.get("blue", 0) + fam_ratio.get("purple", 0) > 0.45:
        descriptors.append("blue-purple dominant")
    if sum(v for f, v in fam_ratio.items() if f in ("white", "grey", "black", "warm neutral")) > 0.7:
        descriptors.append("near-monochrome")
    stats = {
        "mean_saturation": round(mean_s, 3),
        "mean_lightness": round(mean_l, 3),
        "hue_families": fam_ratio,
        "descriptors": descriptors,
    }
    return swatches, stats


def prepare_for_vision(img: Image.Image, max_side: int = 1024) -> bytes:
    rgb = img.convert("RGB")
    rgb.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def make_thumbnail(img: Image.Image, out_path: Path, max_side: int = 360) -> None:
    rgb = img.convert("RGB")
    rgb.thumbnail((max_side, max_side))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rgb.save(out_path, format="JPEG", quality=82)


# --------------------------------------------------------------------------- OCR

_ocr_engine = None
_ocr_unavailable = False


def ocr_text(path: Path) -> tuple[str | None, str | None]:
    """Returns (text, engine). Uses RapidOCR when installed; None when unavailable."""
    global _ocr_engine, _ocr_unavailable
    if _ocr_unavailable:
        return None, None
    try:
        if _ocr_engine is None:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore

            _ocr_engine = RapidOCR()
    except Exception as e:  # noqa: BLE001
        log.info("RapidOCR unavailable (%s); will rely on vision-model visible_text", e)
        _ocr_unavailable = True
        return None, None
    try:
        result, _ = _ocr_engine(str(path))
    except Exception as e:  # noqa: BLE001
        log.warning("OCR failed on %s: %s", path, e)
        return None, "rapidocr(error)"
    if not result:
        return "", "rapidocr"
    lines = [txt for _, txt, conf in result if float(conf) >= 0.5 and txt.strip()]
    return "\n".join(lines), "rapidocr"


# --------------------------------------------------------------------------- vision model


def describe_image(llm: LLM, jpeg_bytes: bytes) -> dict:
    data = llm.chat_json(VISION_SYSTEM, VISION_USER, images=[jpeg_bytes], purpose="image_analysis")
    return {
        "summary": _s(data.get("summary")),
        "subjects": _list(data.get("subjects")),
        "style": _list(data.get("style")),
        "keywords": _list(data.get("keywords")),
        "mood": _list(data.get("mood")),
        "visible_text": _s(data.get("visible_text")) or "",
    }


def _s(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        v = " ".join(str(x) for x in v)
    return str(v).strip() or None


def _list(v, limit: int = 10) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [p for p in (x.strip() for x in v.replace("，", ",").split(",")) if p]
    out: list[str] = []
    for x in v:
        s = str(x).strip().strip("#")
        if s and s not in out:
            out.append(s)
    return out[:limit]
