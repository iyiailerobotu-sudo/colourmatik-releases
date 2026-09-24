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


# Oklab L of a display black / white that still holds detail (sRGB 6/255, 251/255)
_L_BLACK, _L_WHITE = 0.12, 0.985


def _slope_capped_curve(xs: np.ndarray, ys: np.ndarray, lo: float, hi: float,
                        n: int = 41, protect: bool = True):
    """Monotone quantile-matching curve xs -> ys with every local slope clipped to
    [lo, hi], anchored at the median. Returns f(x) with linear extension.

    With `protect`, source values that carry detail (not already black/white)
    are never sent INTO black/white just because the reference has a lot of
    it: a reference full of dark uniforms otherwise crushes a third of a bright
    source to pure black (measured: 33% newly clipped). Matching a picture means
    its shadows go dark, not that they vanish."""
    qs = np.linspace(0.005, 0.995, n)
    xq = np.quantile(xs, qs)
    yq = np.quantile(ys, qs)
    xq = np.maximum.accumulate(xq) + np.arange(n) * 1e-9
    yq = np.maximum.accumulate(yq)
    if protect:
        yq = np.where(xq > _L_BLACK, np.maximum(yq, _L_BLACK), yq)
        yq = np.where(xq < _L_WHITE, np.minimum(yq, _L_WHITE), yq)
        yq = np.maximum.accumulate(yq)
    sl = np.clip(np.diff(yq) / np.maximum(np.diff(xq), 1e-9), lo, hi)
    mid = n // 2
    y2 = np.empty_like(yq)
    y2[mid] = yq[mid]
    for i in range(mid, 0, -1):
        y2[i - 1] = y2[i] - sl[i - 1] * (xq[i] - xq[i - 1])
    for i in range(mid, n - 1):
        y2[i + 1] = y2[i] + sl[i] * (xq[i + 1] - xq[i])
    # Beyond the observed range nothing is known: extend gently (the curve is
    # also the guard's fallback for every colour the clip never showed)
    lo_m = float(np.clip(sl[0], 0.5, 1.5))
    hi_m = float(np.clip(sl[-1], 0.5, 1.5))

    def _eval(x, knots):
        v = np.interp(x, xq, knots)
        below = x < xq[0]
        above = x > xq[-1]
        v[below] = knots[0] + (x[below] - xq[0]) * lo_m
        v[above] = knots[-1] + (x[above] - xq[-1]) * hi_m
        return v
    # Slope capping around the median leaves the MEAN off the reference (the
    # picture reads darker/brighter than it should): shift the whole curve so
    # the means agree, then re-apply the black/white protection, which must
    # hold on the curve that is actually used.
    shift = float(np.clip(np.mean(ys) - np.mean(_eval(np.asarray(xs, dtype=np.float64), y2)),
                          -0.15, 0.15))
    y2 = y2 + shift
    if protect:
        y2 = np.where(xq > _L_BLACK, np.maximum(y2, _L_BLACK), y2)
        y2 = np.where(xq < _L_WHITE, np.minimum(y2, _L_WHITE), y2)
        y2 = np.maximum.accumulate(y2)

    def f(x):
        return _eval(np.asarray(x, dtype=np.float64), y2)
    return f


def _chroma_shift_scale(ab_s: np.ndarray, ab_t: np.ndarray, ws=None, wt=None,
                        cap=(0.5, 2.0)):
    """Weighted mean shift + ISOTROPIC scale for Oklab chroma (a, b): the
    colorist's tint/white-balance move plus a saturation change. Isotropic on
    purpose: an anisotropic (covariance-matching) map rotates hues that are not
    aligned with its axes (measured 31 deg on real footage vs 6 deg here), and
    hue rotation is exactly what a match must never invent."""
    def wmean(x, w):
        return np.average(x, axis=0, weights=w) if w is not None else x.mean(0)

    def wvar(x, w, mu):
        d = ((x - mu) ** 2).sum(axis=1)
        return float(np.average(d, weights=w)) if w is not None else float(d.mean())
    mu_s, mu_t = wmean(ab_s, ws), wmean(ab_t, wt)
    vs, vt = wvar(ab_s, ws, mu_s), wvar(ab_t, wt, mu_t)
    s = float(np.clip(np.sqrt((vt + 1e-8) / (vs + 1e-8)), cap[0], cap[1]))
    return mu_s, mu_t, s


def fit_grade(src_lin: np.ndarray, tgt_lin: np.ndarray, slope=(0.33, 2.5),
              chroma_cap=(0.5, 2.0), bands: int = 3):
    """Hue-preserving 'colorist' transfer in Oklab (no correspondence needed).

    What a grader actually does between two shots: a tone curve, a white-balance /
    tint shift and a saturation change — never "turn this orange lamp green". The
    full-distribution methods (IDT) do exactly that on unrelated content, because
    exact mass matching must find SOME source pixels for every reference colour:
    measured on real footage, an orange engine light became green against a
    cockpit full of green screens, and a dark sweater grew green blotches from a
    reference's green-screen tablet. This transfer cannot: lightness goes through
    a monotone slope-capped curve (no contrast explosions, no banding), chroma
    through a symmetric per-lightness-band affine map (lift/gamma/gain colour
    wheels fitted from data, smooth in L so nothing can band). Hues stay hues.
    """
    from .colorspace import linear_to_oklab, oklab_to_linear
    S = linear_to_oklab(np.clip(src_lin, 0.0, None))
    T = linear_to_oklab(np.clip(tgt_lin, 0.0, None))
    Lc = _slope_capped_curve(S[:, 0], T[:, 0], slope[0], slope[1])
    g_mu_s, g_mu_t, g_s = _chroma_shift_scale(S[:, 1:], T[:, 1:], cap=chroma_cap)
    # the saturation gain is only known for chroma the clip actually showed;
    # beyond that it eases back to 1 so an unseen neon/flash can't be doubled
    c_src = np.hypot(S[:, 1] - g_mu_s[0], S[:, 2] - g_mu_s[1])
    c_seen = max(float(np.percentile(c_src, 98)), 0.02)
    centers = np.linspace(0.2, 0.8, bands) if bands > 1 else np.array([0.5])
    width = (centers[1] - centers[0]) if bands > 1 else 1.0

    def wfun(L):
        W = np.exp(-0.5 * ((L[:, None] - centers[None, :]) / (0.75 * width)) ** 2)
        return W / np.maximum(W.sum(1, keepdims=True), 1e-9)
    params = []
    if bands > 1:
        Ws, Wt = wfun(S[:, 0]), wfun(T[:, 0])
        for b in range(bands):
            mu_s, mu_t, sb = _chroma_shift_scale(S[:, 1:], T[:, 1:], Ws[:, b], Wt[:, b],
                                                 cap=chroma_cap)
            # a band with little data on either side is shrunk toward the global
            # map, so an empty highlight band can't invent a cast that exists nowhere
            n_eff = min(float(Ws[:, b].sum()), float(Wt[:, b].sum()))
            t = n_eff / (n_eff + 4000.0)
            params.append((mu_s * t + g_mu_s * (1 - t), mu_t * t + g_mu_t * (1 - t),
                           sb * t + g_s * (1 - t)))
    else:
        params.append((g_mu_s, g_mu_t, g_s))

    def f(x: np.ndarray) -> np.ndarray:
        ok = linear_to_oklab(np.clip(np.atleast_2d(x), 0.0, None))
        L2 = Lc(ok[:, 0])
        ab = ok[:, 1:]
        c = np.hypot(ab[:, 0] - g_mu_s[0], ab[:, 1] - g_mu_s[1])
        ease = np.clip((c - c_seen) / c_seen, 0.0, 1.0)[:, None]   # 0 inside seen chroma -> 1 at 2x
        if bands > 1:
            W = wfun(ok[:, 0])
            ab2 = np.zeros_like(ab)
            for b, (mu_s, mu_t, sb) in enumerate(params):
                gain = sb * (1.0 - ease) + 1.0 * ease
                ab2 += W[:, b:b + 1] * ((ab - mu_s) * gain + mu_t)
        else:
            mu_s, mu_t, sb = params[0]
            gain = sb * (1.0 - ease) + 1.0 * ease
            ab2 = (ab - mu_s) * gain + mu_t
        return oklab_to_linear(np.column_stack([np.clip(L2, 0.0, 1.0), ab2]))

    f.kind = "grade"  # type: ignore[attr-defined]
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
