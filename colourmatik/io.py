"""Image + video frame I/O. Video frames come out via ffmpeg — the system one if
installed, else the static binary bundled by the imageio-ffmpeg pip package, so a
fresh machine needs NO ffmpeg install."""
from __future__ import annotations
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
import numpy as np
import imageio.v3 as iio

_FFMPEG: str | None = None


def _works(exe: str) -> bool:
    """A binary on PATH may be broken (wrong arch, corrupt) — trust it only if
    `-version` actually runs."""
    try:
        return subprocess.run([exe, "-version"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def _ffmpeg_exe() -> str:
    global _FFMPEG
    if _FFMPEG is None:
        p = shutil.which("ffmpeg")
        if not (p and _works(p)):
            try:
                from imageio_ffmpeg import get_ffmpeg_exe
                p = get_ffmpeg_exe()
            except Exception:
                p = p or "ffmpeg"
        _FFMPEG = p
    return _FFMPEG

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
VIDEO_EXTS = {".mov", ".mp4", ".mxf", ".m4v", ".avi", ".mkv", ".mts", ".braw"}

# Frames only ever feed colour STATISTICS (300k sampled pixels), the preview
# (960 px) and thumbnails - never a full-resolution output. Decoding 4K/6K frames
# at native size cost up to 11.6 GB of RAM for a 7-frame pooled match (11
# candidate frames held as float64) and would swap or crash an 8-16 GB machine.
# Area-averaged down to 1920 px on the long side; HD and smaller are untouched.
MAX_SIDE = 1920


def _cap_size(arr: np.ndarray, max_side: int | None = MAX_SIDE) -> np.ndarray:
    """Area-downscale an (H,W,3) frame so its long side is <= max_side (no-op otherwise)."""
    if not max_side or arr.ndim != 3:
        return arr
    h, w = arr.shape[:2]
    if max(h, w) <= max_side:
        return arr
    k = max_side / float(max(h, w))
    nw, nh = max(1, int(round(w * k))), max(1, int(round(h * k)))
    try:
        import cv2
        return cv2.resize(np.ascontiguousarray(arr), (nw, nh), interpolation=cv2.INTER_AREA)
    except Exception:
        # no OpenCV: integer-step block mean (still bounded memory)
        f = int(np.ceil(max(h, w) / float(max_side)))
        hh, ww = (h // f) * f, (w // f) * f
        a = arr[:hh, :ww].reshape(hh // f, f, ww // f, f, 3).astype(np.float64).mean(axis=(1, 3))
        return a.astype(arr.dtype) if np.issubdtype(arr.dtype, np.floating) else np.round(a).astype(arr.dtype)


def load_image(path: str | Path) -> np.ndarray:
    """Load an image as display-encoded float RGB in [0,1], shape (H,W,3)."""
    try:
        arr = iio.imread(path)
    except Exception as e:
        name = Path(path).name
        raise ValueError(
            f"Couldn't read '{name}' as a video/image. Pick a plain video or still "
            f"clip — After Effects comps (.aep), nested sequences, titles/graphics and "
            f"offline clips can't be sampled directly."
        ) from e
    while arr.ndim > 3:              # animated PNG/GIF -> (N,H,W,C); take the first frame
        arr = arr[0]
    if arr.dtype == np.uint8:
        arr = arr.astype(np.float64) / 255.0
    elif arr.dtype == np.uint16:
        arr = arr.astype(np.float64) / 65535.0
    else:
        arr = arr.astype(np.float64)
    if arr.ndim == 2:                       # grayscale (H,W)
        arr = np.stack([arr] * 3, axis=-1)
    elif arr.shape[-1] == 1:                # single channel with axis (H,W,1)
        arr = np.repeat(arr, 3, axis=-1)
    elif arr.shape[-1] == 2:               # grayscale + alpha, Pillow "LA" (H,W,2)
        arr = np.repeat(arr[..., :1], 3, axis=-1)
    return np.ascontiguousarray(arr[..., :3])  # RGB / RGBA -> RGB


def save_image(path: str | Path, enc: np.ndarray) -> None:
    a = np.clip(enc, 0.0, 1.0)
    iio.imwrite(path, (a * 255.0 + 0.5).astype(np.uint8))


_PROBE_CACHE: dict = {}


def _parse_ffmpeg_banner(stderr: str) -> dict:
    """Pull duration + the video stream's pixel format / colour tags out of
    `ffmpeg -i` stderr (bundled-ffmpeg machines have no ffprobe). Handles
    'yuv420p(tv, bt709, progressive)', 'yuv420p10le(tv, bt2020nc/bt2020/arib-std-b67)',
    'yuv420p(pc, bt709/bt709/iec61966-2-1, progressive)' and plain 'yuv420p'."""
    info: dict = {}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if m:
        h, mnt, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        info["duration"] = h * 3600 + mnt * 60 + s
    m = re.search(r"Video:.*?,\s*([a-z0-9]+)(?:\(([^)]*)\))?,\s*(\d+)x(\d+)", stderr)
    if m:
        info["pix_fmt"] = m.group(1)
        info["width"], info["height"] = int(m.group(3)), int(m.group(4))
        for tok in (m.group(2) or "").split(","):
            tok = tok.strip()
            if tok in ("tv", "pc"):
                info["color_range"] = tok
            elif "/" in tok:
                parts = tok.split("/")
                info["color_space"] = parts[0]
                info["color_primaries"] = parts[1] if len(parts) > 1 else parts[0]
                info["color_transfer"] = parts[2] if len(parts) > 2 else parts[0]
            elif tok.startswith(("bt", "smpte", "fcc", "arib", "iec", "ycgco")):
                info["color_space"] = info["color_primaries"] = info["color_transfer"] = tok
    return info


def _probe_video(video: str | Path) -> dict:
    """Duration, size, pixel format and colour tags of the first video stream.
    Missing/unknown tags are simply absent. Cached per (path, mtime, size)."""
    p = Path(video)
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(p), 0, 0)
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    info: dict = {}
    if shutil.which("ffprobe"):
        try:
            import json
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height,pix_fmt,color_range,color_space,color_transfer,"
                 "color_primaries:format=duration", "-of", "json", str(video)],
                capture_output=True, text=True, check=True, timeout=30)
            j = json.loads(out.stdout or "{}")
            for k, v in (j.get("streams") or [{}])[0].items():
                if v not in (None, "", "unknown", "unspecified"):
                    info[k] = v
            d = (j.get("format") or {}).get("duration")
            if d not in (None, "", "N/A"):
                info["duration"] = float(d)
        except Exception:
            info = {}
    if "duration" not in info or "pix_fmt" not in info:
        try:
            out = subprocess.run([_ffmpeg_exe(), "-hide_banner", "-i", str(video)],
                                 capture_output=True, text=True, timeout=30)
            for k, v in _parse_ffmpeg_banner(out.stderr).items():
                info.setdefault(k, v)
        except Exception:
            pass
    _PROBE_CACHE[key] = info
    return info


def _probe_duration(video: str | Path) -> float | None:
    return _probe_video(video).get("duration")


# ffprobe colour_space tag -> the name libswscale's `scale` filter understands
_SWS_MATRIX = {"bt709": "bt709", "bt470bg": "bt470", "smpte170m": "smpte170m",
               "smpte240m": "smpte240m", "bt2020nc": "bt2020", "bt2020c": "bt2020",
               "fcc": "fcc"}
_HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}


def _decode_plan(info: dict) -> tuple[list, str | None]:
    """How to turn this clip's frames into the RGB Premiere itself shows.

    Returns (extra ffmpeg args, hdr_transfer). hdr_transfer is the HLG/PQ curve
    name when the clip is HDR (so the caller tone-maps the 16-bit output), else None.

    libswscale converts an UNTAGGED clip with BT.601 coefficients regardless of
    size, while Premiere (like every NLE) treats untagged HD as Rec.709 — measured
    on the AI-generated clips this tool is mostly used on (all untagged): mean
    1.8/255, up to 7/255 of hue/saturation error baked into every match. Pick the
    matrix the way Premiere does: the tag when present, else Rec.709 for HD and
    601 for SD. Only YUV sources get the filter; RGB sources have no matrix."""
    pix = str(info.get("pix_fmt", ""))
    is_yuv = pix.startswith(("yuv", "yuvj", "nv", "p0", "p2", "p4", "uyvy", "yuyv", "y4"))
    if not is_yuv:
        return [], None
    w, h = int(info.get("width", 0) or 0), int(info.get("height", 0) or 0)
    trc = str(info.get("color_transfer", ""))
    csp = str(info.get("color_space", ""))
    prim = str(info.get("color_primaries", ""))
    rng = "pc" if info.get("color_range") == "pc" or pix.startswith("yuvj") else "tv"
    hdr = trc in _HDR_TRANSFERS or csp.startswith("bt2020") or prim == "bt2020"
    if hdr:
        matrix = "bt2020"
        hdr_trc = trc if trc in _HDR_TRANSFERS else "arib-std-b67"
    else:
        matrix = _SWS_MATRIX.get(csp) or ("bt709" if (h >= 720 or w >= 1280) else "bt601")
        hdr_trc = None
    args = ["-vf", f"scale=in_color_matrix={matrix}:in_range={rng}:out_range=pc"]
    if hdr:
        args += ["-pix_fmt", "rgb48be"]        # keep the HDR signal's precision for tone-mapping
    return args, hdr_trc


# BT.2020 -> BT.709 primaries (linear light), colour-science matrix_RGB_to_RGB
_M_2020_TO_709 = np.array([[1.66049, -0.58764, -0.07285],
                           [-0.12455, 1.13290, -0.00835],
                           [-0.01815, -0.10058, 1.11873]])


def _hdr_to_sdr(rgb: np.ndarray, trc: str) -> np.ndarray:
    """Tone-map a BT.2020 HLG/PQ frame (non-linear [0,1]) to the SDR sRGB frame
    Premiere's automatic tone-mapping shows in a Rec.709 sequence.

    Without this an iPhone HLG clip enters the match as its raw flat, desaturated,
    green-tinted signal, so any match to or from it lands on colours that never
    appear on the user's screen. Reference white follows ITU-R BT.2408 (203 nits ->
    SDR 1.0); highlights above 80% roll off through a soft shoulder instead of
    clipping, and BT.2020 primaries are converted to Rec.709."""
    e = np.clip(np.asarray(rgb, dtype=np.float64), 0.0, 1.0)
    if trc == "smpte2084":                                  # PQ (ST 2084) EOTF -> nits
        m1, m2 = 2610.0 / 16384.0, 2523.0 / 4096.0 * 128.0
        c1, c2, c3 = 3424.0 / 4096.0, 2413.0 / 4096.0 * 32.0, 2392.0 / 4096.0 * 32.0
        ep = np.power(e, 1.0 / m2)
        nits = 10000.0 * np.power(np.clip(ep - c1, 0.0, None) / (c2 - c3 * ep), 1.0 / m1)
    else:                                                   # HLG (ARIB STD-B67 / BT.2100)
        a, b, c = 0.17883277, 0.28466892, 0.55991073
        scene = np.where(e <= 0.5, (e * e) / 3.0, (np.exp((e - c) / a) + b) / 12.0)
        y = scene[..., 0] * 0.2627 + scene[..., 1] * 0.6780 + scene[..., 2] * 0.0593
        nits = 1000.0 * np.power(np.clip(y, 1e-12, None), 0.2)[..., None] * scene
    lin = (nits / 203.0) @ _M_2020_TO_709.T
    lin = np.clip(lin, 0.0, None)
    k = 0.8
    over = lin > k
    lin[over] = k + (1.0 - k) * (1.0 - np.exp(-(lin[over] - k) / (1.0 - k)))
    from .colorspace import encode
    return np.clip(encode(np.clip(lin, 0.0, 1.0), "sRGB"), 0.0, 1.0)


def _strip_black_bars(img: np.ndarray) -> np.ndarray:
    """Crop letterbox/pillarbox bars off a frame before it enters the match.

    Hard black bars are not part of the footage's look, but they dominate a
    colour histogram (often 20-30% of all pixels) and drag every distribution
    method toward black. A bar row/column is near-black AND near-flat across the
    whole frame — real content (even a night sky) carries more variance. Caps at
    35% per side so a genuinely dark frame can never be cropped away."""
    h, w = img.shape[:2]
    if h < 32 or w < 32:
        return img
    luma = img.mean(axis=2)
    def run(means, stds, cap):
        k = 0
        for m, s in zip(means, stds):
            if m < 0.03 and s < 0.015:
                k += 1
            else:
                break
        return k if 2 <= k <= cap else 0
    top = run(luma.mean(axis=1), luma.std(axis=1), int(h * 0.35))
    bot = run(luma.mean(axis=1)[::-1], luma.std(axis=1)[::-1], int(h * 0.35))
    left = run(luma.mean(axis=0), luma.std(axis=0), int(w * 0.35))
    right = run(luma.mean(axis=0)[::-1], luma.std(axis=0)[::-1], int(w * 0.35))
    if top + bot >= h - 16 or left + right >= w - 16:
        return img
    return img[top:h - bot if bot else h, left:w - right if right else w]


def _keep_dominant(frames: list, n: int) -> list:
    """From a pool of candidate frames, keep the `n` that share the DOMINANT look.

    Seven uniform samples on a multi-shot clip land on different scenes; matching
    their mixed distribution fits none of them. Rank each frame's coarse colour
    histogram by distance to the pool's median look and keep the n closest —
    cuts to other scenes, white flashes and black leaders fall away. Temporal
    order is preserved for the stack."""
    if len(frames) <= n:
        return frames
    sigs = []
    for f in frames:
        small = f[::max(1, f.shape[0] // 90), ::max(1, f.shape[1] // 160)]
        hist, _ = np.histogramdd(small.reshape(-1, 3), bins=(8, 8, 8),
                                 range=((0, 1), (0, 1), (0, 1)))
        hist = hist.ravel()
        sigs.append(hist / (hist.sum() or 1.0))
    sigs = np.asarray(sigs)
    dist = np.abs(sigs - np.median(sigs, axis=0)).sum(axis=1)
    keep = np.sort(np.argsort(dist)[:n])
    return [frames[i] for i in keep]


def extract_frame(video: str | Path, t: float | None = None,
                  max_side: int | None = MAX_SIDE) -> np.ndarray:
    """Extract one representative frame (default: middle) as encoded float RGB,
    area-downscaled to max_side on the long side (None = native size)."""
    dur = _probe_duration(video)
    if t is None:
        t = (dur / 2.0) if dur else 0.5
    elif dur:
        # clamp into the clip, staying a frame's-worth clear of the end (seeking to
        # the very last millisecond yields no frame on most codecs)
        margin = 0.1 if dur > 1.0 else max(1e-3, dur * 0.1)
        t = max(0.0, min(float(t), dur - margin))
    else:
        t = max(0.0, float(t))
    info = _probe_video(video)
    extra, hdr_trc = _decode_plan(info)
    base = [_ffmpeg_exe(), "-y", "-loglevel", "error", "-ss", f"{t:.3f}",
            "-i", str(video), "-frames:v", "1"]
    # Fast path: the frame comes back through a pipe as a PPM (raw bytes with a
    # header that carries the REAL output size, so a phone clip that ffmpeg
    # auto-rotates cannot be misread) - no temp file, no PNG encode/decode.
    # 16-bit PPM for HDR, so the tone-mapping still gets the full signal.
    vf = [a for a in extra if a != "-pix_fmt" and a != "rgb48be"]
    pix = ["-pix_fmt", "rgb48be"] if hdr_trc else ["-pix_fmt", "rgb24"]
    try:
        r = subprocess.run(base + vf + pix + ["-f", "image2pipe", "-vcodec", "ppm", "-"],
                           capture_output=True, check=True)
        img = _parse_ppm(r.stdout, max_side=max_side)
        if img is not None:
            return _hdr_to_sdr(img, hdr_trc) if hdr_trc else img
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        pass
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "frame.png"
        try:
            try:
                subprocess.run(base + extra + [str(out)], check=True)
            except subprocess.CalledProcessError:
                if not extra:
                    raise
                # an exotic build without the scale options: plain decode beats no frame
                extra, hdr_trc = [], None
                subprocess.run(base + [str(out)], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            raise ValueError(
                f"Couldn't extract a frame from '{Path(video).name}' — the clip may be "
                f"corrupt, an unsupported codec, or offline."
            ) from e
        if not out.exists():                 # fast-seek past EOF writes nothing
            raise ValueError(
                f"Couldn't read a frame from '{Path(video).name}' at {t:.2f}s "
                f"(is the requested time past the end of the clip?)."
            )
        img = _cap_size(load_image(out), max_side)
        return _hdr_to_sdr(img, hdr_trc) if hdr_trc else img


def _parse_ppm(data: bytes, max_side: int | None = None) -> np.ndarray | None:
    """Binary PPM (P6, 8- or 16-bit) -> float RGB [0,1] (H,W,3); None if not a PPM.
    With max_side, the frame is area-downscaled while still 8/16-bit integers
    (a 4K frame never exists as float64)."""
    if not data.startswith(b"P6"):
        return None
    pos = 2
    fields = []
    while len(fields) < 3:
        while pos < len(data) and (data[pos:pos + 1].isspace() or data[pos:pos + 1] == b"#"):
            if data[pos:pos + 1] == b"#":          # header comment: skip to end of line
                nl = data.find(b"\n", pos)
                pos = len(data) if nl < 0 else nl + 1
            else:
                pos += 1
        start = pos
        while pos < len(data) and not data[pos:pos + 1].isspace():
            pos += 1
        if start == pos:
            return None
        fields.append(int(data[start:pos]))
    pos += 1                                   # the single whitespace after maxval
    w, h, maxval = fields
    if w <= 0 or h <= 0:
        return None
    n_val = w * h * 3                          # samples; frombuffer counts ELEMENTS
    if maxval < 256:
        if len(data) - pos < n_val:
            return None
        arr = np.frombuffer(data, dtype=np.uint8, count=n_val, offset=pos).reshape(h, w, 3)
        return _cap_size(arr, max_side).astype(np.float64) / float(maxval)
    if len(data) - pos < n_val * 2:
        return None
    arr = np.frombuffer(data, dtype=">u2", count=n_val, offset=pos).reshape(h, w, 3).astype(np.uint16)
    return _cap_size(arr, max_side).astype(np.float64) / float(maxval)


def extract_frames(video: str | Path, n: int = 3,
                   start: float | None = None, end: float | None = None,
                   robust: bool = True, max_side: int | None = MAX_SIDE) -> np.ndarray:
    """Extract `n` frames spread across the clip and stack them vertically.

    Pooling several frames gives a more representative colour distribution than a
    single frame (motion, exposure drift). Frames are stacked in the SAME temporal
    order for both clips, so pixel correspondence is preserved for aligned shots.

    `start`/`end` (seconds) restrict sampling to the SEGMENT actually used on the
    timeline — long source files often contain several unrelated scenes, and
    sampling the whole file badly skews the colour distribution."""
    dur = _probe_duration(video)
    lo, hi = 0.0, dur if dur else None
    if dur:
        margin = 0.2 if dur > 1.0 else 0.0   # stay clear of the very end (no frame there)
        if start is not None:
            lo = max(0.0, min(float(start), max(0.0, dur - margin)))
        if end is not None:
            hi = max(lo + 1e-3, min(float(end), dur))
    if n <= 1:
        mid = ((lo + hi) / 2.0) if hi is not None else None
        return extract_frame(video, mid, max_side=max_side)
    if not dur:
        # duration unknown -> can't spread samples; replicate the one frame so the
        # contract "n>1 returns n stacked frames" holds for every caller that
        # slices the stack back apart (e.g. the preview reuses stack[:H/n]).
        f0 = extract_frame(video, max_side=max_side)
        return np.concatenate([f0] * n, axis=0)
    # sample at interior points of the range, avoiding the very first/last frame
    span = hi - lo
    # In robust mode, sample EXTRA candidates and keep the n that share the
    # dominant look (drops cuts to other scenes / flashes / leaders). Off in
    # corresponded mode, where both clips must keep identical frame indices.
    m = n + 4 if (robust and n >= 5) else n
    times = [lo + span * (i + 1) / (m + 1) for i in range(m)]
    # each frame is an independent ffmpeg seek+decode: run a few at once (the
    # two clips are already decoded in parallel one level up)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(3, len(times))) as ex:
        frames = list(ex.map(lambda tt: _strip_black_bars(extract_frame(video, tt, max_side=max_side)),
                             times))
    if m > n:
        frames = _keep_dominant(frames, n)
    h = min(f.shape[0] for f in frames)
    w = min(f.shape[1] for f in frames)
    frames = [f[:h, :w] for f in frames]
    return np.concatenate(frames, axis=0)


def load_any(path: str | Path, t: float | None = None, frames: int = 1,
             start: float | None = None, end: float | None = None,
             robust: bool = True, max_side: int | None = MAX_SIDE) -> np.ndarray:
    """Load an image, or extract frame(s) from a video, into encoded float RGB.

    Routed by CAPABILITY, not by extension allowlist: only known still-image
    extensions go to the image reader; everything else is treated as video first
    (ffmpeg decodes far more formats than any list we could maintain — .webm,
    .mpg, .m2ts, .3gp, ProRes in odd containers...). If ffmpeg can't open it,
    fall back to the image reader before giving up, so a mislabelled still
    (say a .heic) still has a chance.
    """
    if Path(path).suffix.lower() in IMAGE_EXTS:
        return _cap_size(load_image(path), max_side)
    try:
        if t is not None:
            return extract_frame(path, t, max_side=max_side)
        return extract_frames(path, frames, start=start, end=end, robust=robust,
                              max_side=max_side)
    except Exception:
        return _cap_size(load_image(path), max_side)
