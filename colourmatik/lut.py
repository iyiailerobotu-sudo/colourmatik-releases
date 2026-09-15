"""3D LUT: bake a linear-space transform into a display-space .cube, apply it.

A Premiere/Resolve Input LUT maps display-encoded input -> display-encoded output.
So each grid node is decoded to linear, transformed, and re-encoded.
"""
from __future__ import annotations
import os
from pathlib import Path
import numpy as np

from .colorspace import decode, encode


def gamut_guard(lut: np.ndarray, samples_enc: np.ndarray,
                fallback: np.ndarray, sigma: float = 2.5,
                ref_pct: float = 30.0) -> np.ndarray:
    """Blend `lut` toward a safe `fallback` in cube regions the source never covered.

    Every candidate is FIT and SCORED only on pixels sampled from the source
    frames, but the baked LUT spans the whole RGB cube. Outside the sampled
    cloud nothing constrains the winner — a degree-3 polynomial happily explodes
    there, and a lattice's unsupported nodes drift toward black — yet later
    frames of the clip WILL contain colours the sampled frames didn't (a flash,
    a neon sign, deep shadow). Those pixels then read garbage: the occasional
    "insane colours" failure.

    Fix at one choke point: histogram the sampled source pixels into the LUT
    grid, smooth lightly, and per node blend `lut` toward `fallback` (the
    gain-capped global MKL — smooth and sane over the whole cube) by how much
    data support the node has. Well-sampled nodes keep the winner bit-for-bit,
    so measured accuracy is untouched; unsupported nodes get the safe global
    look instead of extrapolation noise.
    """
    from scipy.ndimage import gaussian_filter
    size = lut.shape[0]
    pts = np.clip(np.asarray(samples_enc, dtype=np.float64).reshape(-1, 3), 0.0, 1.0)
    bins = np.linspace(0.0, 1.0, size + 1)
    hist, _ = np.histogramdd(pts, bins=(bins, bins, bins))   # indexed [r,g,b]
    support = gaussian_filter(hist, sigma=sigma, mode="nearest")
    pos = support[support > 1e-12]
    if pos.size == 0:
        return fallback.copy()
    ref = np.percentile(pos, ref_pct)
    if ref <= 0:
        ref = float(pos.mean()) or 1.0
    w = np.clip(support / ref, 0.0, 1.0)[..., None]
    return w * lut + (1.0 - w) * fallback


# ---------------------------------------------------------------- strength axes
# The panel's WB / Tone / Colour sliders. The FINAL winning LUT (whatever
# candidate produced it) is factored as  winner ≈ Residual ∘ Tone ∘ WB :
#   WB    — per-channel linear gains matching the two clips' channel means,
#   Tone  — per-channel slope-capped quantile curves fitted after WB,
#   Residual — everything else (hue/saturation character), extracted from the
#              winner itself by pushing the cube through the INVERSE of Tone∘WB.
# Each stage can then be dialled toward identity independently and the three
# recomposed into one fresh 65^3 LUT — the native effect never changes.

def fit_wb_tone(src_lin: np.ndarray, tgt_lin: np.ndarray):
    """Fit the WB gains + tone curves stages from linear samples of both clips."""
    src_lin = np.asarray(src_lin, dtype=np.float64)
    tgt_lin = np.asarray(tgt_lin, dtype=np.float64)
    gains = np.array([
        float(np.clip((tgt_lin[:, c].mean() + 1e-6) / (src_lin[:, c].mean() + 1e-6),
                      0.25, 4.0))
        for c in range(3)
    ])
    wb_src = src_lin * gains
    qs = np.linspace(0.01, 0.99, 33)
    curves = []
    for c in range(3):
        xq = np.quantile(wb_src[:, c], qs)
        yq = np.quantile(tgt_lin[:, c], qs)
        xq = np.maximum.accumulate(xq) + np.arange(33) * 1e-9
        yq = np.maximum.accumulate(yq)
        sl = np.clip(np.diff(yq) / np.maximum(np.diff(xq), 1e-9), 0.2, 5.0)
        mid = 16                     # anchor at the median: midtones match exactly
        y2 = np.empty_like(yq)
        y2[mid] = yq[mid]
        for i in range(mid, 0, -1):
            y2[i - 1] = y2[i] - sl[i - 1] * (xq[i] - xq[i - 1])
        for i in range(mid, 32):
            y2[i + 1] = y2[i] + sl[i] * (xq[i + 1] - xq[i])
        curves.append((xq, y2))
    return gains, curves


def _curve_eval(x, xq, yq, s=1.0):
    v = np.interp(x, xq, yq)
    lo_m = np.clip((yq[1] - yq[0]) / max(xq[1] - xq[0], 1e-9), 0.0, 5.0)
    hi_m = np.clip((yq[-1] - yq[-2]) / max(xq[-1] - xq[-2], 1e-9), 0.0, 5.0)
    below = x < xq[0]
    above = x > xq[-1]
    v[below] = yq[0] + (x[below] - xq[0]) * lo_m
    v[above] = yq[-1] + (x[above] - xq[-1]) * hi_m
    return x + (v - x) * s


def apply_wb_tone(lin: np.ndarray, gains, curves, s_wb: float = 1.0,
                  s_tone: float = 1.0) -> np.ndarray:
    g = 1.0 + (np.asarray(gains) - 1.0) * float(s_wb)
    out = lin * g
    res = np.empty_like(out)
    for c in range(3):
        xq, yq = curves[c]
        res[:, c] = _curve_eval(out[:, c], xq, yq, s=float(s_tone))
    return res


def _invert_wb_tone(lin_y: np.ndarray, gains, curves) -> np.ndarray:
    """Full-strength inverse of Tone∘WB (both stages are per-channel monotone)."""
    out = np.empty_like(lin_y)
    for c in range(3):
        xq, yq = curves[c]
        yq2 = np.maximum.accumulate(yq) + np.arange(len(yq)) * 1e-9
        x = np.interp(lin_y[:, c], yq2, xq)
        lo_m = (yq2[1] - yq2[0]) / max(xq[1] - xq[0], 1e-9)
        hi_m = (yq2[-1] - yq2[-2]) / max(xq[-1] - xq[-2], 1e-9)
        below = lin_y[:, c] < yq2[0]
        above = lin_y[:, c] > yq2[-1]
        if lo_m > 1e-9:
            x[below] = xq[0] + (lin_y[below, c] - yq2[0]) / lo_m
        if hi_m > 1e-9:
            x[above] = xq[-1] + (lin_y[above, c] - yq2[-1]) / hi_m
        out[:, c] = x
    return out / np.maximum(np.asarray(gains), 1e-9)


def decompose_residual(winner_lut: np.ndarray, gains, curves,
                       tf: str = "sRGB") -> np.ndarray:
    """Extract R with winner ≈ R ∘ (Tone∘WB):  R(y) = winner((Tone∘WB)^-1(y))."""
    size = winner_lut.shape[0]
    ax = np.linspace(0.0, 1.0, size)
    Rg, Gg, Bg = np.meshgrid(ax, ax, ax, indexing="ij")
    grid = np.stack([Rg, Gg, Bg], -1).reshape(-1, 3)
    lin = decode(grid, tf)
    x_lin = _invert_wb_tone(lin, gains, curves)
    from .colorspace import is_display_tf
    if is_display_tf(tf):
        x_lin = np.clip(x_lin, 0.0, None)
    x_enc = np.clip(encode(x_lin, tf), 0.0, 1.0)
    vals = apply_lut_points(winner_lut, x_enc)
    return np.clip(vals, 0.0, 1.0).reshape(size, size, size, 3)


def recompose_lut(gains, curves, resid: np.ndarray, s_wb: float, s_tone: float,
                  s_color: float, size: int = 65, tf: str = "sRGB") -> np.ndarray:
    """Rebuild one LUT with each stage dialled to its own strength (1 = full)."""
    ax = np.linspace(0.0, 1.0, size)
    Rg, Gg, Bg = np.meshgrid(ax, ax, ax, indexing="ij")
    grid = np.stack([Rg, Gg, Bg], -1).reshape(-1, 3)
    lin = decode(grid, tf)
    lin2 = apply_wb_tone(lin, gains, curves, s_wb=s_wb, s_tone=s_tone)
    from .colorspace import is_display_tf
    if is_display_tf(tf):
        lin2 = soft_gamut(lin2)
    enc2 = np.clip(encode(lin2, tf), 0.0, 1.0)
    Rs = apply_intensity(np.asarray(resid, dtype=np.float64), float(s_color))
    out = apply_lut_points(Rs, enc2)
    return np.clip(out, 0.0, 1.0).reshape(size, size, size, 3)


def lut_steepness(lut: np.ndarray, pct: float = 99.5) -> float:
    """The LUT's near-worst LOCAL slope (unitless; identity = 1).

    Steepness is what turns a match into visible damage on real footage: any
    segment steeper than ~6x stretches 8-bit quantisation and H.264 macroblock
    steps into hard posterised patches. Natural grades sit under ~3x."""
    size = lut.shape[0]
    mx = []
    for ax in range(3):
        d = np.abs(np.diff(lut, axis=ax)) * (size - 1)
        mx.append(d.max(axis=-1).ravel())
    return float(np.percentile(np.concatenate(mx), pct))


def steep_guard(lut: np.ndarray, fallback: np.ndarray, cap: float = 6.0) -> np.ndarray:
    """Emergency brake: SMOOTH the LUT until its near-worst local slope is under
    `cap`. A no-op for every sane match; only an exact-distribution map between a
    narrow and a wide distribution (the posterisation failure) gets softened.

    Smoothing (not blending away) is the transport-map regularisation the colour
    OT literature prescribes: it keeps the grade's structure and only shaves the
    local spikes that turn 8-bit / macroblock steps into posterised patches. If
    even repeated smoothing can't tame it, fall back to blending toward the
    smooth capped-MKL as a last resort."""
    from scipy.ndimage import gaussian_filter
    out = lut
    for _ in range(4):
        if lut_steepness(out) <= cap:
            return out
        out = np.stack([gaussian_filter(out[..., c], sigma=1.0, mode="nearest")
                        for c in range(3)], axis=-1)
    sa = lut_steepness(out)
    if sa > cap * 1.5:
        sb = lut_steepness(fallback)
        t = float(np.clip((cap - sb) / max(sa - sb, 1e-9), 0.0, 1.0))
        out = t * out + (1.0 - t) * fallback
    return out


def soft_gamut(lin: np.ndarray) -> np.ndarray:
    """Hue-preserving gamut mapping for out-of-range LINEAR sRGB values.

    A hard per-channel clip at the cube edge visibly distorts hue (a too-bright
    warm highlight clips to yellow; deep saturated blues clip cyan-ward). Instead,
    out-of-gamut colours keep their Oklab hue and lightness while their CHROMA is
    bisected down to the gamut surface — Ottosson's constant-hue projection, the
    recipe CSS Color 4 standardised. In-gamut values pass through untouched."""
    from .colorspace import linear_to_oklab, oklab_to_linear
    lin = np.asarray(lin, dtype=np.float64)
    flat = lin.reshape(-1, 3)
    eps = 1e-6
    oog = np.any((flat < -eps) | (flat > 1.0 + eps), axis=-1)
    if not oog.any():
        return np.clip(lin, 0.0, 1.0)
    bad = flat[oog]
    lab = linear_to_oklab(np.clip(bad, 0.0, None))
    L = np.clip(lab[:, :1], 0.0, 1.0)
    ab = lab[:, 1:]
    lo = np.zeros((bad.shape[0], 1))
    hi = np.ones((bad.shape[0], 1))
    for _ in range(9):                    # bisect the largest in-gamut chroma
        mid = (lo + hi) / 2.0
        cand = oklab_to_linear(np.concatenate([L, ab * mid], axis=1))
        inside = np.all((cand > -1e-4) & (cand < 1.0 + 1e-4), axis=1, keepdims=True)
        lo = np.where(inside, mid, lo)
        hi = np.where(inside, hi, mid)
    mapped = oklab_to_linear(np.concatenate([L, ab * lo], axis=1))
    out = np.clip(flat, 0.0, 1.0)
    out[oog] = np.clip(mapped, 0.0, 1.0)
    return out.reshape(lin.shape)


def build_lut(transform_lin, size: int = 65, tf: str = "sRGB") -> np.ndarray:
    """Sample `transform_lin` on an encoded grid -> LUT array indexed [r, g, b, 3]."""
    axis = np.linspace(0.0, 1.0, size)
    R, G, B = np.meshgrid(axis, axis, axis, indexing="ij")  # [r,g,b]
    enc_in = np.stack([R, G, B], axis=-1).reshape(-1, 3)
    lin_in = decode(enc_in, tf)
    lin_out = np.nan_to_num(transform_lin(lin_in))
    from .colorspace import is_display_tf
    if is_display_tf(tf):
        lin_out = soft_gamut(lin_out)             # hue-preserving, not a hard clip
    # scene-referred log: linear values outside [0,1] (even slightly negative)
    # are real data the log ENCODING itself maps back into [0,1] — no clipping.
    enc_out = np.clip(encode(lin_out, tf), 0.0, 1.0)
    return enc_out.reshape(size, size, size, 3)


def write_cube(path: str | Path, lut: np.ndarray, title: str = "colourMatik") -> None:
    """Write an Adobe .cube 3D LUT. RED varies fastest (Adobe/Resolve spec).

    Vectorised: the old per-node Python loop (274k f-strings for a 65^3 cube)
    cost 0.29 s per file and every match wrote four of them - a full second of
    each match spent formatting text. Same bytes, ~20x faster."""
    size = lut.shape[0]
    out = [
        f'TITLE "{title}"',
        f"LUT_3D_SIZE {size}",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
        "",
    ]
    # red fastest, then green, then blue -> b outer, g middle, r inner
    flat = np.ascontiguousarray(np.asarray(lut, dtype=np.float64)
                                .transpose(2, 1, 0, 3).reshape(-1, 3))
    # one C-level format of the whole table (measured 2.3x faster than np.savetxt,
    # byte-identical output)
    body = ("%.6f %.6f %.6f\n" * flat.shape[0]) % tuple(flat.ravel().tolist())
    text = "\n".join(out) + "\n" + body
    # atomic write: temp file + replace, so a concurrent reader never sees a partial LUT
    path = Path(path)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def _trilinear(lut: np.ndarray, pts: np.ndarray, chunk: int = 400_000) -> np.ndarray:
    """Trilinear sample of an (S,S,S,3) LUT at (N,3) points in [0,1]. Hand-rolled
    gathers: 2.8x faster than RegularGridInterpolator (measured, identical to
    4e-16), and this runs on every preview, wipe, thumbnail and judge probe."""
    S = lut.shape[0]
    L = np.asarray(lut, dtype=np.float64).reshape(-1, 3)
    pts = np.asarray(pts, dtype=np.float64)
    out = np.empty(pts.shape, dtype=np.float64)
    for s0 in range(0, pts.shape[0], chunk):
        q = np.clip(pts[s0:s0 + chunk], 0.0, 1.0) * (S - 1)
        i0 = np.minimum(q.astype(np.int64), S - 2)
        f = q - i0
        fr, fg, fb = f[:, 0:1], f[:, 1:2], f[:, 2:3]
        base = (i0[:, 0] * S + i0[:, 1]) * S + i0[:, 2]      # index = r*S*S + g*S + b
        c000 = L[base]; c001 = L[base + 1]
        c010 = L[base + S]; c011 = L[base + S + 1]
        c100 = L[base + S * S]; c101 = L[base + S * S + 1]
        c110 = L[base + S * S + S]; c111 = L[base + S * S + S + 1]
        c00 = c000 + (c100 - c000) * fr
        c01 = c001 + (c101 - c001) * fr
        c10 = c010 + (c110 - c010) * fr
        c11 = c011 + (c111 - c011) * fr
        c0 = c00 + (c10 - c00) * fg
        c1 = c01 + (c11 - c01) * fg
        out[s0:s0 + chunk] = c0 + (c1 - c0) * fb
    return out


def apply_intensity(lut: np.ndarray, intensity: float) -> np.ndarray:
    """Blend a LUT toward identity: identity + t*(lut - identity).

    t=0 -> no change, t=1 -> full match, t>1 -> stronger (extrapolated). Lets the
    same match be dialed up or down without re-fitting."""
    t = float(intensity)
    if not np.isfinite(t):        # a NaN/Inf intensity must not poison the whole LUT
        t = 1.0
    if t == 1.0:
        return lut
    size = lut.shape[0]
    axis = np.linspace(0.0, 1.0, size)
    R, G, B = np.meshgrid(axis, axis, axis, indexing="ij")
    identity = np.stack([R, G, B], axis=-1)
    return np.clip(identity + t * (lut - identity), 0.0, 1.0)


def apply_lut(img_enc: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Apply a 3D LUT to a display-encoded image via trilinear interpolation."""
    shape = img_enc.shape
    return np.clip(_trilinear(lut, np.asarray(img_enc).reshape(-1, 3)).reshape(shape), 0.0, 1.0)


def apply_lut_points(lut: np.ndarray, pts_enc: np.ndarray) -> np.ndarray:
    """Apply a 3D LUT to an (N,3) array of display-encoded points."""
    return np.clip(_trilinear(lut, pts_enc), 0.0, 1.0)


def resample_lut(lut: np.ndarray, new_size: int) -> np.ndarray:
    """Resample a LUT lattice onto a finer/coarser cube via trilinear interpolation."""
    if lut.shape[0] == new_size:
        return lut
    axis = np.linspace(0.0, 1.0, new_size)
    R, G, B = np.meshgrid(axis, axis, axis, indexing="ij")
    pts = np.stack([R, G, B], axis=-1).reshape(-1, 3)
    out = _trilinear(lut, pts).reshape(new_size, new_size, new_size, 3)
    return np.clip(out, 0.0, 1.0)
