"""Visual proof: a labelled before/after/reference montage so you can SEE the match."""
from __future__ import annotations
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw

from .colorspace import encoded_to_lab
from .metrics import delta_e00


def _u8(enc: np.ndarray) -> np.ndarray:
    return (np.clip(enc, 0, 1) * 255 + 0.5).astype(np.uint8)


def _panel(enc: np.ndarray, label: str, pad_top: int = 26, max_w: int | None = None) -> Image.Image:
    im = Image.fromarray(_u8(enc))
    if max_w and im.width > max_w:
        im = im.resize((max_w, max(1, int(im.height * max_w / im.width))), Image.BILINEAR)
    w, h = im.size
    img = Image.new("RGB", (w, h + pad_top), (18, 18, 18))
    img.paste(im, (0, pad_top))
    d = ImageDraw.Draw(img)
    d.text((6, 6), label, fill=(235, 235, 235))
    return img


def make_comparison(reference: np.ndarray, before: np.ndarray, after: np.ndarray,
                    out_path: str | Path, de_before: float | None = None,
                    de_after: float | None = None, show_error: bool = True,
                    tf: str = "sRGB", max_w: int | None = None) -> None:
    """Write a [ reference | before | after ] montage plus a dE00 error heatmap.

    The error heatmap is per-pixel, so it is only meaningful for aligned/same-scene
    shots — pass show_error=False in distribution mode (different scenes)."""
    bl = "SOURCE (before)" + (f"   dE00 {de_before:.2f}" if de_before is not None else "")
    al = "MATCHED (after)" + (f"   dE00 {de_after:.2f}" if de_after is not None else "")
    panels = [_panel(reference, "REFERENCE (target look)", max_w=max_w),
              _panel(before, bl, max_w=max_w),
              _panel(after, al, max_w=max_w)]

    # error heatmap: per-pixel dE00 between matched and reference (0..6 -> dark..bright)
    if show_error and reference.shape == after.shape:
        de = delta_e00(encoded_to_lab(after, tf).reshape(-1, 3),
                       encoded_to_lab(reference, tf).reshape(-1, 3))
        de = de.reshape(reference.shape[:2])
        hm = np.clip(de / 6.0, 0, 1)
        heat = np.stack([hm, 1 - hm, np.zeros_like(hm)], axis=-1)  # red=error, green=match
        panels.append(_panel(heat, "ERROR MAP (green=perfect, red>=6 dE00)", max_w=max_w))

    gap = 8
    W = sum(p.width for p in panels) + gap * (len(panels) - 1)
    H = max(p.height for p in panels)
    canvas = Image.new("RGB", (W, H), (10, 10, 10))
    x = 0
    for p in panels:
        canvas.paste(p, (x, 0))
        x += p.width + gap
    if str(out_path).lower().endswith((".jpg", ".jpeg")):
        canvas.save(out_path, format="JPEG", quality=88)
    else:
        canvas.save(out_path, format="PNG")   # explicit: don't crash on an extensionless path
