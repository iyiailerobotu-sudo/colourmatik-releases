"""Faces in a frame - OpenCV's YuNet detector (MIT licence, Shiqi Yu;
models/face_detection_yunet_2023mar.onnx, 230 KB, runs in OpenCV the engine
already ships).

skin.py finds skin by colour alone, and on real frames it also fires on wheat,
beige walls and statues (17-67% false positives), so it could only keep
skin-toned things on their hue. Inside a face the detector found, skin-coloured
pixels are a person's skin: their lightness is the exposure a colourist matches
between two shots, and their hue is what must not drift.
"""
from __future__ import annotations
import threading
from pathlib import Path
import numpy as np

_MODEL = Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx"
_tls = threading.local()          # one detector per thread: it is not thread-safe


def _detector():
    import cv2
    d = getattr(_tls, "det", None)
    if d is None:
        d = cv2.FaceDetectorYN.create(str(_MODEL), "", (320, 320), 0.75, 0.3, 50)
        _tls.det = d
    return d


def find_faces(frame_enc: np.ndarray, max_side: int = 640, min_frac: float = 0.03):
    """Faces as [(x0, y0, x1, y1, score)] in pixels of `frame_enc` (H x W x 3,
    encoded 0..1). Faces smaller than 3% of the frame's short side are left
    out: too few pixels to say anything about exposure."""
    import cv2
    if not _MODEL.exists() or frame_enc.ndim != 3:
        return []
    h, w = frame_enc.shape[:2]
    s = min(1.0, max_side / float(max(h, w)))
    img = (np.clip(frame_enc, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)[..., ::-1]
    if s < 1.0:
        img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    det = _detector()
    det.setInputSize((img.shape[1], img.shape[0]))
    _, found = det.detect(np.ascontiguousarray(img))
    out = []
    if found is None:
        return out
    for f in found:
        x, y, bw, bh = (float(v) / s for v in f[:4])
        if min(bw, bh) < min_frac * min(h, w):
            continue
        out.append((max(0.0, x), max(0.0, y), min(float(w), x + bw), min(float(h), y + bh), float(f[14])))
    return out


def face_mask(img_enc: np.ndarray, n_frames: int = 1, max_frames: int = 3):
    """H x W boolean mask of the middle of every found face (brow to chin,
    without hair and background), for a stack of `n_frames` frames one under
    another (as io.load_any pools them), looking at up to `max_frames` of them;
    None when there is no usable face. Not filtered by skin colour: under night
    or coloured light the colour test rejects real faces (0 of 7 faces passed
    it in two night clips), and the middle of a face box is skin anyway."""
    if img_enc.ndim != 3:
        return None
    H, W = img_enc.shape[:2]
    n = max(1, int(n_frames))
    fh = H // n
    if fh < 32:
        return None
    mask = np.zeros((H, W), dtype=bool)
    for k in sorted({int(round(i * (n - 1) / max(1, max_frames - 1))) for i in range(min(n, max_frames))}):
        fr = img_enc[k * fh:(k + 1) * fh]
        for x0, y0, x1, y1, _ in find_faces(fr):
            mx, my = (x1 - x0) * 0.20, (y1 - y0) * 0.15
            a0, a1, b0, b1 = int(y0 + my), int(y1 - my), int(x0 + mx), int(x1 - mx)
            if a1 - a0 >= 4 and b1 - b0 >= 4:
                mask[k * fh + a0:k * fh + a1, b0:b1] = True
    return mask if mask.sum() >= 100 else None
