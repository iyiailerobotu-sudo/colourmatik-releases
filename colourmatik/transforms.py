"""Colour-matching transforms. Every transform is a function f: linear_rgb -> linear_rgb.

Two families:
  * Correspondence-based (fit_linear / fit_polynomial): most accurate when the two
    shots share content (same subject / aligned frames / colour chart). Solves the
    exact map from paired source->target pixels via least squares.
  * Distribution-based (fit_mkl): for different scenes with no correspondence.
    Monge-Kantorovich linear map — matches mean + covariance, smooth, artifact-free.
"""
from __future__ import annotations
from itertools import combinations_with_replacement
import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla


# ---------------------------------------------------------------- helpers
def _sqrtm_sym(M: np.ndarray, inverse: bool = False, eps: float = 1e-10) -> np.ndarray:
    """Symmetric (inverse) square root of a symmetric PSD matrix via eigendecomp.

    Eigenvalues are floored RELATIVE to the spectrum (not an absolute 1e-10), so a
    degenerate direction — a flat frame or a crushed/constant colour channel —
    yields a bounded transfer instead of exploding by ~1e5 along that eigenvector."""
    w, V = np.linalg.eigh(M)
    floor = max(eps, 1e-6 * float(w.max()) if w.size else eps)
    w = np.clip(w, floor, None)
    s = 1.0 / np.sqrt(w) if inverse else np.sqrt(w)
    return (V * s) @ V.T


def _poly_features(x: np.ndarray, degree: int) -> np.ndarray:
    """Monomial features up to `degree` for 3-channel input x (N,3), incl. bias."""
    n = x.shape[0]
    feats = [np.ones(n)]
    for d in range(1, degree + 1):
        for combo in combinations_with_replacement((0, 1, 2), d):
            term = np.ones(n)
            for c in combo:
                term = term * x[:, c]
            feats.append(term)
    return np.stack(feats, axis=1)


# ---------------------------------------------------------------- correspondence
def fit_linear(src_lin: np.ndarray, tgt_lin: np.ndarray, ridge: float = 1e-6):
    """Affine 3x3 + offset. Recovers any linear camera difference (gain/WB/primaries)."""
    return fit_polynomial(src_lin, tgt_lin, degree=1, ridge=ridge)


def fit_polynomial(src_lin: np.ndarray, tgt_lin: np.ndarray, degree: int = 3,
                   ridge: float = 1e-4, weights: np.ndarray | None = None):
    """(Weighted) ridge polynomial regression src->tgt (requires correspondence)."""
    Phi = _poly_features(src_lin, degree)
    if weights is not None:
        sw = np.sqrt(weights)[:, None]
        Phi = Phi * sw
        tgt_lin = tgt_lin * sw
    A = Phi.T @ Phi + ridge * np.eye(Phi.shape[1])
    B = Phi.T @ tgt_lin
    W = np.linalg.solve(A, B)  # (F, 3)

    def f(x: np.ndarray) -> np.ndarray:
        return _poly_features(np.atleast_2d(x), degree) @ W

    f.kind = f"poly{degree}"  # type: ignore[attr-defined]
    return f


# ---------------------------------------------------------------- distribution
def fit_mkl(src_lin: np.ndarray, tgt_lin: np.ndarray):
    """Monge-Kantorovich linear transfer (no correspondence needed).

    Maps the source Gaussian (mu_s, Sigma_s) onto the target (mu_t, Sigma_t):
        x -> T (x - mu_s) + mu_t,  with
        T = Ss^-1/2 ( Ss^1/2 St Ss^1/2 )^1/2 Ss^-1/2   (Pitie & Kokaram 2007)
    """
    mu_s = src_lin.mean(0)
    mu_t = tgt_lin.mean(0)
    Ss = np.cov(src_lin.T)
    St = np.cov(tgt_lin.T)
    # Shrink both covariances toward isotropic before inverting. On rank-deficient
    # input (a grey ramp, a flat wall — src variance lives on a line) Ss^-1/2 is
    # numerically unbounded along the missing directions and T comes out as junk
    # there; since MKL is also every guard's FALLBACK, the junk poisoned the whole
    # chain (measured: identity input mapped a cube corner 0.77 away). Shrinkage
    # makes the transfer act as ~identity along directions the data never showed,
    # which is exactly the conservative behaviour a fallback needs. At 1% of the
    # average variance the effect on well-conditioned real footage is negligible.
    lam_s = 0.01 * max(np.trace(Ss) / 3.0, 1e-12)
    lam_t = 0.01 * max(np.trace(St) / 3.0, 1e-12)
    Ss = Ss + lam_s * np.eye(3)
    St = St + lam_t * np.eye(3)
    Ss_half = _sqrtm_sym(Ss)
    Ss_ihalf = _sqrtm_sym(Ss, inverse=True)
    mid = _sqrtm_sym(Ss_half @ St @ Ss_half)
    T = Ss_ihalf @ mid @ Ss_ihalf
    # Bound the transfer's gain. When the source has almost no variance along some
    # axis (a green screen, a flat wall, a single-colour frame), Sigma_s^-1/2 blows
    # up along it and T reaches gains of 40x+ — every pixel is catapulted out of
    # gamut and the baked LUT clamps to the cube corners (the observed all-magenta
    # slot). Real grades measure around 1.2x per axis; 4x is already an extreme
    # look, so cap T's eigenvalues there. The mean shift (mu_s -> mu_t) is
    # untouched, so a degenerate source still gets a sane, bounded look.
    T = 0.5 * (T + T.T)
    w, V = np.linalg.eigh(T)
    T = (V * np.clip(w, 0.0, 4.0)) @ V.T

    def f(x: np.ndarray) -> np.ndarray:
        x = np.atleast_2d(x)
        return (x - mu_s) @ T.T + mu_t

    f.kind = "mkl"  # type: ignore[attr-defined]
    return f


def fit_sep(src_lin: np.ndarray, tgt_lin: np.ndarray):
    """Separated transfer: per-channel 1D quantile curves, then a capped-MKL 3D
    residual — composed into one transform.

    The 1D stage absorbs exposure, white balance, and contrast (each channel's
    marginal distribution is matched with a monotone curve); the 3D stage only
    has to fix what 1D cannot — cross-channel hue/saturation mixing — so it stays
    small and stable. SepLUT (ECCV 2022) measured this 1D-then-3D split beating a
    single coupled 3D map; it also mirrors how hardware ISPs are built.
    """
    qs = np.linspace(0.01, 0.99, 33)
    curves = []
    for c in range(3):
        xq = np.quantile(src_lin[:, c], qs)
        yq = np.quantile(tgt_lin[:, c], qs)
        xq = np.maximum.accumulate(xq) + np.arange(33) * 1e-9  # strictly increasing
        yq = np.maximum.accumulate(yq)
        # Cap the curve's LOCAL slope everywhere. Exact quantile matching between
        # a NARROW source and a WIDE target demands huge per-segment stretches
        # (measured 14-32x on a dark cold shot matched to a broad reference) —
        # each such segment amplifies 8-bit / macroblock steps into posterised
        # patches. Rebuild the curve from slopes clipped to [0.2, 5], anchored at
        # the median so midtones stay exactly matched.
        sl = np.clip(np.diff(yq) / np.maximum(np.diff(xq), 1e-9), 0.2, 5.0)
        mid = 16
        y2 = np.empty_like(yq)
        y2[mid] = yq[mid]
        for i in range(mid, 0, -1):
            y2[i - 1] = y2[i] - sl[i - 1] * (xq[i] - xq[i - 1])
        for i in range(mid, 32):
            y2[i + 1] = y2[i] + sl[i] * (xq[i + 1] - xq[i])
        yq = y2
        # linear extension beyond the observed range, same sane slope bound
        lo_m = np.clip((yq[1] - yq[0]) / max(xq[1] - xq[0], 1e-9), 0.0, 5.0)
        hi_m = np.clip((yq[-1] - yq[-2]) / max(xq[-1] - xq[-2], 1e-9), 0.0, 5.0)
        curves.append((xq, yq, lo_m, hi_m))

    def apply_curves(x):
        out = np.empty_like(x)
        for c in range(3):
            xq, yq, lo_m, hi_m = curves[c]
            v = np.interp(x[:, c], xq, yq)
            below = x[:, c] < xq[0]
            above = x[:, c] > xq[-1]
            v[below] = yq[0] + (x[below, c] - xq[0]) * lo_m
            v[above] = yq[-1] + (x[above, c] - xq[-1]) * hi_m
            out[:, c] = v
        return out

    resid = fit_mkl(apply_curves(src_lin), tgt_lin)   # capped, see fit_mkl

    def f(x: np.ndarray) -> np.ndarray:
        return resid(apply_curves(np.atleast_2d(x)))

    f.kind = "sep"  # type: ignore[attr-defined]
    return f


# ---------------------------------------------------------------- look transfer
# What a colourist matches between two UNRELATED shots is the LOOK - black and
# white point, the tint of the neutrals in the shadows / mids / highlights, the
# overall saturation - not the content. Matching colour STATISTICS confuses the
# two: the 1.7.7 "grade" took the mean chroma of the whole picture for its colour
# cast and the full histogram for its exposure, so a field of grass read as
# "shifted to green" (matched to a night street its sky went purple), and a
# reference full of dark uniforms crushed a bright shot. Seen on real footage;
# measured on 31 real pairs. This transfer only uses what IS comparable.
_LOOK_BANDS = np.array([0.15, 0.35, 0.55, 0.75, 0.92])     # Oklab lightness bands
_LOOK_SIGMA = 0.15


def _band_weights(L: np.ndarray) -> np.ndarray:
    """Soft membership of each lightness in the tonal bands (rows sum to 1)."""
    Lc = np.clip(np.asarray(L, dtype=np.float64), 0.0, 1.0)
    W = np.exp(-0.5 * ((Lc[:, None] - _LOOK_BANDS[None, :]) / _LOOK_SIGMA) ** 2)
    return W / np.maximum(W.sum(1, keepdims=True), 1e-12)


def _neutral_cast(ok: np.ndarray, W: np.ndarray, sigma: float = 0.04,
                  prior: float = 0.05, iters: int = 2, cmax: float = 0.045) -> np.ndarray:
    """The tint of the NEAR-NEUTRAL population of each tonal band, (bands, 2) in
    Oklab a/b - the way white balance is judged: from greys, not from the average
    of everything. A short mean-shift walk from neutral finds the grey cluster even
    under a cast; the neutral prior (5% of the band) makes a band with no greys
    claim almost no cast instead of calling its content one; the result is capped
    at the strength of a strong grade."""
    ab = ok[:, 1:]
    tot = W.sum(0)
    casts = np.zeros((len(_LOOK_BANDS), 2))
    for b in range(len(_LOOK_BANDS)):
        if tot[b] < 50:
            continue
        c = np.zeros(2)
        for _ in range(iters):
            w = W[:, b] * np.exp(-((ab - c) ** 2).sum(1) / (2.0 * sigma * sigma))
            c = (w[:, None] * ab).sum(0) / (w.sum() + prior * tot[b])
        m = float(np.hypot(c[0], c[1]))
        casts[b] = c * (cmax / m) if m > cmax else c
    good = tot > 0.01 * len(ok)
    if good.any() and not good.all():       # a tonal range the picture barely has
        casts[~good] = (casts[good] * tot[good, None]).sum(0) / tot[good].sum()
    return casts


def _levels_curve(Ls: np.ndarray, Lt: np.ndarray, mid_trust: float = 0.5,
                  slope=(0.4, 2.2), end_cap=(0.15, 0.25), mid_cap: float = 0.12):
    """Tone as a look: black point, white point and - half as much - the
    midtones follow the reference, each by no more than a grade would move it.
    Monotone, slopes capped.

    How much of a frame is dark is CONTENT (dark clothes are not dark exposure),
    so the histogram's shape is not copied. The same goes for the ends when the
    difference is large: a grade lifts or crushes black by a little (0.15 in
    Oklab lightness) and moves white by a stop or so (0.25); past that the two
    frames simply hold different things - a high-key face holds nothing dark, a
    night street nothing white - and following the reference all the way dragged
    the darkest thing in a bright frame down to the black of somebody's coat.

    Above the source's own white point the curve is a shoulder that approaches
    white without reaching it: the brightest thing in the frame (a moon, a lamp)
    is brighter than 99% of the picture, and a straight continuation sent it
    past white - an orange moon came out as a white disc."""
    from scipy.interpolate import PchipInterpolator
    xs = np.percentile(Ls, [1.0, 50.0, 99.0])
    ys = np.percentile(Lt, [1.0, 50.0, 99.0])
    lo = xs[0] + float(np.clip(ys[0] - xs[0], -end_cap[0], end_cap[0]))
    top_t = xs[2] + float(np.clip(ys[2] - xs[2], -end_cap[1], end_cap[1]))
    if xs[1] <= 0.02:
        # More than half the frame is pure black: an element on black (smoke,
        # sparks), not a faded picture. Its black is a matte and stays black.
        lo = min(lo, xs[0])
    b = float(np.clip((top_t - lo) / max(xs[2] - xs[0], 1e-3), slope[0], slope[1]))
    m_lin = lo + b * (xs[1] - xs[0])                    # where plain levels put the median
    mid = m_lin + float(np.clip(mid_trust * (ys[1] - m_lin), -mid_cap, mid_cap))
    s1 = float(np.clip((mid - lo) / max(xs[1] - xs[0], 1e-3), slope[0], slope[1]))
    mid = lo + s1 * (xs[1] - xs[0])
    hi = lo + b * (xs[2] - xs[0])
    if xs[2] >= 0.985:
        # Blown-out whites hold no detail to reveal: pulled all the way down to
        # a dim reference white they become flat grey patches (an overexposed
        # cheek against a classroom). They go half of the way.
        hi = max(hi, 0.5 * (hi + 0.96))
    s2 = float(np.clip((hi - mid) / max(xs[2] - xs[1], 1e-3), slope[0], slope[1]))
    hi = mid + s2 * (xs[2] - xs[1])
    xk = np.array([xs[0], xs[1], xs[2]]) + np.arange(3) * 1e-6
    yk = np.array([lo, mid, hi]) + np.arange(3) * 1e-9
    pch = PchipInterpolator(xk, yk)
    dp = pch.derivative()
    lo_m = float(np.clip(dp(xk[0]), 0.5, 1.5))          # beyond what the clip showed: gentle
    hi_m = float(np.clip(dp(xk[2]), 0.5, 1.5))
    top = max(1.0, float(np.max(Lt)))                   # scene-referred whites may sit above 1
    room = max(top - yk[2], 1e-3)

    def f(L):
        L = np.asarray(L, dtype=np.float64)
        v = pch(np.clip(L, xk[0], xk[2]))
        v = np.where(L < xk[0], yk[0] + (L - xk[0]) * lo_m, v)
        over = np.maximum(L - xk[2], 0.0)
        v = np.where(L > xk[2], yk[2] + room * (1.0 - np.exp(-over * hi_m / room)), v)
        return v
    return f


def fit_look(src_lin: np.ndarray, tgt_lin: np.ndarray, skin_src: np.ndarray | None = None,
             mid_trust: float = 0.5, sat_trust: float = 0.6, sat_cap=(0.65, 1.4),
             cast_cap: float = 0.045, shift_cap: float = 0.05):
    """Look transfer in Oklab (no correspondence needed) - the engine's "grade".

      tone : `_levels_curve` (black / white point and half of the midtone
             difference, each by no more than a grade would move it)
      tint : per tonal band, the reference's neutral cast replaces the source's
             (`_neutral_cast`); content colours are carried along, not "corrected"
      sat  : chroma follows the lightness change the way an RGB tone curve does,
             then the overall chroma level moves 60% of the way to the reference's,
             within 0.65-1.4x (how colourful a frame is depends on what is in it)
      skin : `skin_src` (per-sample skin-tone probability) keeps skin-toned things
             on their hue - the tint is scaled back until they rotate < 15 degrees

    Hues are never rotated into other hues. The Intensity slider can push a
    deliberately conservative transfer further; a wrecked picture cannot be undone.
    """
    from .colorspace import linear_to_oklab, oklab_to_linear
    S = linear_to_oklab(np.clip(src_lin, 0.0, None))
    T = linear_to_oklab(np.clip(tgt_lin, 0.0, None))
    Lc = _levels_curve(S[:, 0], T[:, 0], mid_trust=mid_trust)
    Ws, Wt = _band_weights(S[:, 0]), _band_weights(T[:, 0])
    cast_s = _neutral_cast(S, Ws, cmax=cast_cap)
    cast_t = _neutral_cast(T, Wt, cmax=cast_cap)

    def lscale(L, L2):
        return np.clip(np.maximum(L2, 1e-3) / np.maximum(L, 1e-3), 0.45, 1.6) ** 0.8

    def shift(c_s, c_t, L2):
        d = c_t - c_s
        m = np.hypot(d[:, 0], d[:, 1])
        d = d * np.minimum(1.0, shift_cap / np.maximum(m, 1e-9))[:, None]
        return d * np.clip(L2 / 0.12, 0.0, 1.0)[:, None]      # blacks stay black

    L2s = np.maximum(Lc(S[:, 0]), 0.0)
    cs_s = Ws @ cast_s
    rel_s = (S[:, 1:] - cs_s) * lscale(S[:, 0], L2s)[:, None]
    rel_t = T[:, 1:] - Wt @ cast_t
    ratio = (float(np.hypot(rel_t[:, 0], rel_t[:, 1]).mean()) + 1e-4) / \
            (float(np.hypot(rel_s[:, 0], rel_s[:, 1]).mean()) + 1e-4)
    g = float(np.clip(ratio ** sat_trust, sat_cap[0], sat_cap[1]))
    c_seen = max(float(np.percentile(np.hypot(S[:, 1] - cs_s[:, 0], S[:, 2] - cs_s[:, 1]), 98)), 0.02)

    # Skin-toned things keep their hue: scale the tint back until they do.
    k_tint = 1.0
    if skin_src is not None:
        sk = np.asarray(skin_src).reshape(-1) > 0.5
        if sk.shape[0] == S.shape[0] and sk.sum() >= max(200, int(0.02 * len(S))):
            v_in = S[sk, 1:].mean(0)
            d_sk = shift(cs_s[sk], _band_weights(L2s[sk]) @ cast_t, L2s[sk])
            base = (S[sk, 1:] - cs_s[sk]) * (g * lscale(S[sk, 0], L2s[sk]))[:, None] + cs_s[sk]
            n_in = float(np.hypot(v_in[0], v_in[1]))
            for k_tint in (1.0, 0.8, 0.6, 0.4, 0.2, 0.0):
                v_out = (base + k_tint * d_sk).mean(0)
                n_out = float(np.hypot(v_out[0], v_out[1]))
                cosang = float(v_in @ v_out) / max(n_in * n_out, 1e-12)
                if n_in < 0.01 or (np.degrees(np.arccos(np.clip(cosang, -1.0, 1.0))) <= 15.0
                                   and n_out >= 0.5 * n_in * min(1.0, g)):
                    break

    def f(x: np.ndarray) -> np.ndarray:
        ok = linear_to_oklab(np.clip(np.atleast_2d(x), 0.0, None))
        L = ok[:, 0]
        L2 = np.maximum(Lc(L), 0.0)
        c_s = _band_weights(L) @ cast_s
        d = shift(c_s, _band_weights(L2) @ cast_t, L2) * k_tint
        rel = ok[:, 1:] - c_s
        C = np.hypot(rel[:, 0], rel[:, 1])
        ease = np.clip((C - c_seen) / c_seen, 0.0, 1.0)   # unseen neon: gain eases to 1
        gain = (g * (1.0 - ease) + ease) * lscale(L, L2)
        return oklab_to_linear(np.column_stack([L2, rel * gain[:, None] + c_s + d]))

    f.kind = "grade"  # type: ignore[attr-defined]
    f.info = {"sat_gain": g, "tint_kept": k_tint}  # type: ignore[attr-defined]
    return f


def fit_uot(src_pts: np.ndarray, tgt_pts: np.ndarray, blur: float = 0.05,
            reach: float = 0.4) -> np.ndarray:
    """Unbalanced Sinkhorn transport of `src_pts` toward `tgt_pts` (linear RGB).

    Pitié's IDT matches distributions EXACTLY, mass for mass — so when the
    reference has, say, a huge blue sky and the target has little of it, exact
    matching forcibly paints something else sky-blue. Relaxing mass conservation
    (`reach`) lets modes shift without being conserved, and the entropic `blur`
    keeps the map smooth (the OT literature's fix for grain/quantisation
    artifacts of raw transport maps). One Sinkhorn gradient step is the
    documented GeomLoss colour-transfer recipe. Requires torch+geomloss (the AI
    extras); callers treat ImportError as 'candidate unavailable'.
    """
    import torch
    from geomloss import SamplesLoss
    x = torch.tensor(src_pts, dtype=torch.float32, requires_grad=True)
    y = torch.tensor(tgt_pts, dtype=torch.float32)
    # backend="tensorized": pure torch, runs everywhere. The faster "online"
    # backend needs pykeops, which compiles C++ on the user's machine — a
    # non-starter for a shipped tool. Tensorized is O(N*M) memory, so callers
    # keep N,M at a few thousand points (plenty for a 3D colour distribution).
    loss = SamplesLoss("sinkhorn", p=2, blur=blur, reach=reach, scaling=0.8,
                       backend="tensorized")
    L = loss(x, y)
    (g,) = torch.autograd.grad(L, [x])
    # uniform weights 1/N -> Brenier displacement is -N * grad
    out = (x - float(len(src_pts)) * g).detach().cpu().numpy()
    return np.clip(out.astype(np.float64), 0.0, None)


def _rand_rotation(d: int, rng) -> np.ndarray:
    Q, R = np.linalg.qr(rng.normal(size=(d, d)))
    return Q * np.sign(np.diag(R))


def _match_marginal(s: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Map values `s` so their 1D distribution matches `t` (CDF / quantile match)."""
    n = len(s)
    ranks = np.empty(n, dtype=np.float32)
    ranks[np.argsort(s)] = np.arange(n, dtype=np.float32)
    q = ranks / max(n - 1, 1)
    ts = np.sort(t)
    return np.interp(q, np.linspace(0.0, 1.0, len(ts), dtype=np.float32), ts).astype(s.dtype, copy=False)


def fit_idt(src: np.ndarray, tgt: np.ndarray, n_iter: int = 16, seed: int = 0) -> np.ndarray:
    """Iterative Distribution Transfer (Pitié): transport `src` samples so their FULL
    3D colour distribution matches `tgt` — no correspondence needed. Returns the
    transported source samples (nonlinear; captures tone + saturation, not just MKL's
    mean/covariance). Feed the (src -> transported) pairs to fit_lut_lattice to bake.

    Runs in float32: the work is 3 sorts per iteration on every point, and colour
    values need nothing like double precision. 16 iterations on 100k points
    measured within sampling noise of 24 on 150k on real footage (distribution
    distance 0.16 vs 0.17, 0.146 vs 0.141, 0.442 vs 0.443) at 2.4x the speed."""
    rng = np.random.default_rng(seed)
    x = np.ascontiguousarray(src, dtype=np.float32)
    t = np.ascontiguousarray(tgt, dtype=np.float32)
    for _ in range(n_iter):
        Rm = _rand_rotation(src.shape[1], rng).astype(np.float32)
        xp = x @ Rm
        tp = t @ Rm
        for k in range(src.shape[1]):
            xp[:, k] = _match_marginal(xp[:, k], tp[:, k])
        x = xp @ Rm.T
    return x.astype(np.float64)


# ---------------------------------------------------------------- lattice LUT fit
def _trilinear_matrix(pts: np.ndarray, L: int) -> sp.csr_matrix:
    """Sparse (N, L^3) trilinear interpolation weights for points in [0,1]^3.
    Node index = r + g*L + b*L^2  (red fastest), matching .cube ordering."""
    c = np.clip(pts, 0.0, 1.0) * (L - 1)
    i0 = np.clip(np.floor(c).astype(np.int64), 0, L - 2)
    frac = c - i0
    n = pts.shape[0]
    rows, cols, data = [], [], []
    ar = np.arange(n)
    for corner in range(8):
        dr, dg, db = corner & 1, (corner >> 1) & 1, (corner >> 2) & 1
        wr = frac[:, 0] if dr else 1.0 - frac[:, 0]
        wg = frac[:, 1] if dg else 1.0 - frac[:, 1]
        wb = frac[:, 2] if db else 1.0 - frac[:, 2]
        node = (i0[:, 0] + dr) + (i0[:, 1] + dg) * L + (i0[:, 2] + db) * L * L
        rows.append(ar)
        cols.append(node)
        data.append(wr * wg * wb)
    return sp.coo_matrix(
        (np.concatenate(data), (np.concatenate(rows), np.concatenate(cols))),
        shape=(n, L ** 3),
    ).tocsr()


def _laplacian(L: int) -> sp.csr_matrix:
    """6-neighbour graph Laplacian over an LxLxL lattice (smoothness prior)."""
    ar = np.arange(L)
    R, G, B = np.meshgrid(ar, ar, ar, indexing="ij")
    nid = (R + G * L + B * L * L).astype(np.int64)  # indexed [r,g,b]
    I, J = [], []
    for a in range(3):
        s0 = [slice(None)] * 3
        s1 = [slice(None)] * 3
        s0[a] = slice(0, L - 1)
        s1[a] = slice(1, L)
        n1 = nid[tuple(s0)].ravel()
        n2 = nid[tuple(s1)].ravel()
        I += [n1, n2]
        J += [n2, n1]
    I = np.concatenate(I)
    J = np.concatenate(J)
    A = sp.coo_matrix((np.ones(len(I)), (I, J)), shape=(L ** 3, L ** 3)).tocsr()
    deg = np.asarray(A.sum(1)).ravel()
    return (sp.diags(deg) - A).tocsr()


def fit_lut_lattice(src_enc: np.ndarray, tgt_enc: np.ndarray, L: int = 25,
                    smooth: float = 0.10, ridge: float = 1e-4,
                    weights: np.ndarray | None = None) -> np.ndarray:
    """Fit a display-space LUT lattice directly from corresponded pixels.

    Minimises sum_i w_i || W_i v - t_i ||^2 + smooth * v^T Lap v + ridge ||v||^2,
    where W are trilinear weights. Returns an (L,L,L,3) LUT indexed [r,g,b]. Local
    support + smoothness keep highlights/saturated colours accurate (small dE tails)
    without the edge overshoot of a global polynomial. `weights` (e.g. skin) bias
    the fit toward those samples.
    """
    W = _trilinear_matrix(src_enc, L)
    if weights is not None:
        sw = np.sqrt(weights)
        W = W.multiply(sw[:, None]).tocsr()
        tgt_enc = tgt_enc * sw[:, None]
    Lap = _laplacian(L)
    A = (W.T @ W + smooth * Lap + ridge * sp.identity(L ** 3)).tocsr()
    B = np.asarray(W.T @ tgt_enc)  # (L^3, 3) dense
    # The system is SPD and well conditioned by the smoothness prior: Jacobi-
    # preconditioned conjugate gradients solve it in ~40 ms where the sparse LU
    # took 0.6-1.5 s (measured; results agree to 2e-5). LU stays as the fallback.
    V = np.empty((L ** 3, 3))
    ok = True
    try:
        diag = A.diagonal()
        M = spla.LinearOperator(A.shape, matvec=lambda x: x / diag, dtype=np.float64)
        for c in range(3):
            try:
                x, info = spla.cg(A, B[:, c], rtol=1e-6, maxiter=500, M=M)
            except TypeError:           # SciPy < 1.12 calls the tolerance `tol`
                x, info = spla.cg(A, B[:, c], tol=1e-6, maxiter=500, M=M)
            if info != 0:
                ok = False
                break
            V[:, c] = x
    except Exception:
        ok = False
    if not ok:
        V = spla.splu(A.tocsc()).solve(B)
    lattice = np.empty((L, L, L, 3))
    for ch in range(3):
        lattice[..., ch] = V[:, ch].reshape((L, L, L), order="F")
    return np.clip(lattice, 0.0, 1.0)
