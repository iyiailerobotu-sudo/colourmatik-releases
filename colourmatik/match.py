"""Orchestrator: try candidate transforms, measure dE00 on the actual output,
keep the most accurate, bake it to a LUT, and report before/after accuracy.

Every candidate is scored the same way — by the dE00 of its *display-encoded
output* (i.e. exactly what the .cube will do) versus the reference — so the winner
is chosen on true deliverable accuracy, not on fitting-space proxies.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np

from . import colorspace as cs
from . import transforms as tf_mod
from .metrics import (delta_e00, image_delta_e00, sliced_wasserstein,
                      summarize, verdict)
from .lut import (build_lut, apply_lut, apply_lut_points, resample_lut,
                  gamut_guard, lut_steepness, steep_guard)
from .skin import skin_probability, skin_mask


@dataclass
class MatchResult:
    method: str
    scores: dict
    lut: np.ndarray
    tf: str
    corresponded: bool
    score_metric: str = "dE00"
    de_before: dict | None = None
    de_after: dict | None = None
    de_skin_before: float | None = None
    de_skin_after: float | None = None
    notes: list = field(default_factory=list)
    # Top alternative candidate LUTs (name -> guarded float32 LUT), winner first.
    # The panel shows these as clickable look previews instead of silently
    # discarding every runner-up.
    alts: dict = field(default_factory=dict)
    # Small linear-RGB sample of each side (float32), kept so the strength
    # decomposition (WB / tone / colour sliders) can be fitted on demand.
    sample_src_lin: np.ndarray | None = None
    sample_tgt_lin: np.ndarray | None = None


def match(src_enc: np.ndarray, tgt_enc: np.ndarray, *, corresponded: bool = True,
          tf: str = "sRGB", size: int = 65, degrees=(1, 2, 3),
          lattice_L: int = 25, sample: int = 300_000, seed: int = 0,
          skin_protect: bool = True, skin_weight: float = 8.0,
          neural: bool = False, look: str = "exact",
          refine: bool = False, quick: bool = False, progress=None) -> MatchResult:
    """Match `src_enc` (video 2) to `tgt_enc` (video 1). Returns winning LUT + report.

    Every candidate is turned into its actual `size^3` LUT, that LUT is applied to
    the source, and the result's mean dE00 vs the reference is the score — so the
    winner is chosen on exactly what ships, not on a fitting-space proxy.

    skin_protect weights skin pixels up during fitting (eyes are most critical of
    skin), so the match protects skin tones even at a small cost elsewhere.
    """
    # Optional progress reporter: progress(fraction 0..1, human message). No-op if None.
    def _p(frac, msg):
        if progress:
            try: progress(float(frac), msg)
            except Exception: pass

    # Sanitize inputs: display-referred [0,1], no NaN/Inf (e.g. a 32-bit float TIFF
    # can carry NaN). Keeps every downstream path — classical, SegFormer, CanonCGT — safe.
    # (one working copy per side: clip copies, nan_to_num then works in place -
    # a 7-frame pooled stack is 350 MB per copy)
    src_enc = np.nan_to_num(np.clip(np.asarray(src_enc, dtype=np.float64), 0.0, 1.0), copy=False)
    tgt_enc = np.nan_to_num(np.clip(np.asarray(tgt_enc, dtype=np.float64), 0.0, 1.0), copy=False)

    # "AI cinematic grade" mode: use the CanonCGT learned reference grade directly,
    # instead of the accuracy contest. This is a photorealistic *look* transfer, not a
    # literal distribution match, so it is NOT scored against the classical candidates.
    if look == "ai_grade":
        try:
            from . import canoncgt as cg_mod
            _p(0.35, "AI cinematic grade")
            lut = cg_mod.canon_lut(src_enc, tgt_enc, tf, size=size,
                                   lattice_L=lattice_L, seed=seed)
        except Exception:
            lut = None
        if lut is not None:
            # Same gamut guard as the classical path: outside the colours the
            # source actually contains, fall back to the capped global MKL so the
            # learned grade can't hand out-of-sample pixels something insane.
            try:
                _rngg = np.random.default_rng(seed)
                _Se = src_enc.reshape(-1, 3)
                _Te = tgt_enc.reshape(-1, 3)
                _si = (_rngg.choice(_Se.shape[0], 200_000, replace=False)
                       if _Se.shape[0] > 200_000 else np.arange(_Se.shape[0]))
                _ti = (_rngg.choice(_Te.shape[0], 200_000, replace=False)
                       if _Te.shape[0] > 200_000 else np.arange(_Te.shape[0]))
                _fb = build_lut(tf_mod.fit_mkl(cs.decode(_Se[_si], tf),
                                               cs.decode(_Te[_ti], tf)),
                                size=size, tf=tf)
                lut = gamut_guard(lut, _Se[_si], _fb)
                lut = steep_guard(lut, _fb)
            except Exception:
                pass
            res = MatchResult(method="canon", scores={"canon": 0.0}, lut=lut, tf=tf,
                              corresponded=corresponded,
                              score_metric="AI cinematic grade (CanonCGT)")
            res.notes.append("AI cinematic grade (CanonCGT): learned photorealistic "
                             "reference grade (a look transfer, not a literal match).")
            if corresponded and src_enc.shape == tgt_enc.shape:
                res.de_before = summarize(image_delta_e00(src_enc, tgt_enc, tf))
                res.de_after = summarize(image_delta_e00(apply_lut(src_enc, lut), tgt_enc, tf))
            return res
        # CanonCGT unavailable -> fall through to the classical accuracy contest.

    # Corresponded mode needs pixel-aligned frames; if the two clips differ in
    # resolution, resize the reference onto the source grid so correspondence (and
    # the same-index sampling below) is well-defined instead of crashing.
    if corresponded and src_enc.shape[:2] != tgt_enc.shape[:2]:
        from PIL import Image
        h, w = src_enc.shape[:2]
        _rt = Image.fromarray((np.clip(tgt_enc, 0, 1) * 255 + 0.5).astype("uint8"))
        tgt_enc = np.asarray(_rt.resize((w, h))).astype(np.float64) / 255.0

    # "Same scene" clicked for two DIFFERENT shots is the single most damaging
    # misuse: corresponded matching pairs pixels BY POSITION, so unaligned frames
    # produce a garbage mapping that still "wins" its own contest (seen in the
    # field: MKL crowned at dE00 26). Detect it — structural correlation of the
    # two frames at 64px — and quietly fall back to distribution matching.
    if corresponded:
        from PIL import Image as _Im
        def _tiny_gray(a):
            g = _Im.fromarray((np.clip(a, 0, 1) * 255 + 0.5).astype("uint8")).convert("L")
            return np.asarray(g.resize((64, 64)), dtype=np.float64).ravel()
        ga, gb = _tiny_gray(src_enc), _tiny_gray(tgt_enc)
        ga -= ga.mean(); gb -= gb.mean()
        denom = float(np.sqrt((ga * ga).sum() * (gb * gb).sum()))
        corr = float((ga * gb).sum() / denom) if denom > 1e-9 else 0.0
        if corr < 0.45:
            corresponded = False
        _CORR_PRECHECK = corr

    # Decode to linear only what is SAMPLED: the full frame stack (14.5M pixels
    # for 7 pooled 1080p frames) was being decoded and then 300k of it used -
    # 1.1 s and 350 MB per match for nothing.
    S_enc = src_enc.reshape(-1, 3)
    T_enc = tgt_enc.reshape(-1, 3)

    # Quick mode: the 2-5s draft the panel applies immediately while the full
    # contest keeps running in the background. Fewer samples, cheap candidates
    # only, no AI, no refine - the full pass hot-swaps the result moments later.
    if quick:
        sample = min(sample, 60_000)
        degrees = (1, 2)
        neural = False
        refine = False

    rng = np.random.default_rng(seed)

    def _pick(n_total):
        return (rng.choice(n_total, sample, replace=False)
                if n_total > sample else np.arange(n_total))

    idx = _pick(S_enc.shape[0])          # source sample indices (used downstream)
    Sf_enc = S_enc[idx]
    Sf_lin = cs.decode(Sf_enc, tf)
    if corresponded:
        # aligned pixels: the reference must be sampled at the SAME indices
        Tf_enc = T_enc[idx]
    else:
        # independent distributions: sample the reference on its own (clips may
        # differ in resolution / frame count, so indices need not match)
        tidx = _pick(T_enc.shape[0])
        Tf_enc = T_enc[tidx]
    Tf_lin = cs.decode(Tf_enc, tf)

    weights = None
    if skin_protect:
        skin_p = skin_probability(Sf_enc)
        if skin_p.max() > 0.2:  # only when some skin is actually present
            weights = 1.0 + skin_weight * skin_p

    luts: dict = {}
    nctx = None  # local-AI (segmentation) context, set in distribution mode if available
    _gate_note = None
    if corresponded:
        # Same content, pixel-aligned: learn the exact map from source->target pairs.
        for d in degrees:
            f = tf_mod.fit_polynomial(Sf_lin, Tf_lin, degree=d, weights=weights)
            luts[f"poly{d}"] = build_lut(f, size=size, tf=tf)
        luts["lattice"] = resample_lut(
            tf_mod.fit_lut_lattice(Sf_enc, Tf_enc, L=lattice_L, weights=weights), size)
        luts["mkl"] = build_lut(tf_mod.fit_mkl(Sf_lin, Tf_lin), size=size, tf=tf)
        luts["sep"] = build_lut(tf_mod.fit_sep(Sf_lin, Tf_lin), size=size, tf=tf)
        luts["grade"] = build_lut(tf_mod.fit_grade(Sf_lin, Tf_lin), size=size, tf=tf)
    else:
        # Different scenes / not aligned: match the colour DISTRIBUTIONS.
        _p(0.10, "Matching colour distributions")
        # ModFlows runs on the GPU while the CPU fits the classical candidates —
        # the two workloads barely contend, so this is nearly free wall-clock.
        _flow_box = {}
        _flow_th = None
        if neural:
            def _flow_job():
                try:
                    from . import modflows as mf_mod
                    if mf_mod.loaded_nowait():
                        _flow_box["lut"] = mf_mod.modflows_lut(src_enc, tgt_enc, size=size)
                except Exception:
                    pass
            import threading as _thr
            _flow_th = _thr.Thread(target=_flow_job, daemon=True)
            _flow_th.start()
        luts["mkl"] = build_lut(tf_mod.fit_mkl(Sf_lin, Tf_lin), size=size, tf=tf)  # linear
        luts["sep"] = build_lut(tf_mod.fit_sep(Sf_lin, Tf_lin), size=size, tf=tf)  # 1D curves + 3D residual
        # Hue-preserving Oklab "colorist" transfer: tone curve + balance + saturation.
        # Cheap (0.2s), so the draft has it too — the draft and the final then
        # agree whenever the full contest also lands on it.
        luts["grade"] = build_lut(tf_mod.fit_grade(Sf_lin, Tf_lin), size=size, tf=tf)
        # How alike are the two pictures' colour worlds? Exact-distribution
        # transport (IDT) is a superb match between shots of the same kind of
        # content and a wrecking ball between unrelated ones: on real footage
        # every pairing beyond ~8 Oklab units (a selfie vs a classroom, a hand
        # vs a night sky) came back blotched or posterised, while the colorist
        # grade stayed clean. Skip it there — it would only cost 3-9 s to lose.
        _gate_n = 20_000
        _gs = rng.choice(Sf_enc.shape[0], min(_gate_n, Sf_enc.shape[0]), replace=False)
        _gt = rng.choice(Tf_enc.shape[0], min(_gate_n, Tf_enc.shape[0]), replace=False)
        scene_gap = sliced_wasserstein(cs.encoded_to_oklab(Sf_enc[_gs], tf) * 100.0,
                                       cs.encoded_to_oklab(Tf_enc[_gt], tf) * 100.0,
                                       n_proj=32, seed=seed)
        if quick:
            transported = None
        elif scene_gap > 8.0:
            transported = None
            _gate_note = (f"the two clips show very different colour worlds (distance "
                          f"{scene_gap:.1f}); exact distribution transport skipped, "
                          "matched tone, balance and saturation instead")
        else:
            # IDT + the lattice both scale with point count, and together they
            # were the single biggest cost of a match (profiled 9.7s of ~20s on
            # a warm engine at 300k points). A 25^3 lattice has 15,625 nodes —
            # 150k points saturate it; accuracy is checked by the regression
            # battery, wall-clock is roughly halved.
            _cap = 100_000
            if Sf_lin.shape[0] > _cap:
                _sel = rng.choice(Sf_lin.shape[0], _cap, replace=False)
                _idt_src_lin, _idt_src_enc = Sf_lin[_sel], Sf_enc[_sel]
                _idt_w = weights[_sel] if weights is not None else None
                _idt_tgt = Tf_lin[rng.choice(Tf_lin.shape[0], _cap, replace=False)]
            else:
                _idt_src_lin, _idt_src_enc, _idt_w, _idt_tgt = Sf_lin, Sf_enc, weights, Tf_lin
            transported = np.clip(tf_mod.fit_idt(_idt_src_lin, _idt_tgt, seed=seed), 0.0, None)
        if transported is not None:
            lat = tf_mod.fit_lut_lattice(_idt_src_enc, cs.encode(transported, tf),
                                         L=lattice_L, weights=_idt_w)
            luts["idt"] = resample_lut(lat, size)
        # ModFlows (AAAI 2025, MIT weights) — collect the parallel result.
        if _flow_th is not None:
            _flow_th.join(timeout=60)
            if _flow_box.get("lut") is not None:
                luts["flow"] = _flow_box["lut"]
        # Unbalanced Sinkhorn transport (optional — needs torch+geomloss from the
        # AI extras). Fixes IDT's exact-mass failure: a reference dominated by one
        # colour (a huge sky) no longer forces that colour onto unrelated content.
        try:
            if quick or not neural:
                raise ImportError("uot belongs to the optional AI stack")
            _u = np.random.default_rng(seed + 1)
            cap = 6_000   # tensorized Sinkhorn is O(N*M); 6k points describe a 3D colour cloud fine
            ui = (_u.choice(Sf_lin.shape[0], cap, replace=False)
                  if Sf_lin.shape[0] > cap else np.arange(Sf_lin.shape[0]))
            uj = (_u.choice(Tf_lin.shape[0], cap, replace=False)
                  if Tf_lin.shape[0] > cap else np.arange(Tf_lin.shape[0]))
            tx = tf_mod.fit_uot(Sf_lin[ui], Tf_lin[uj])
            uw = weights[ui] if weights is not None else None
            luts["uot"] = resample_lut(
                tf_mod.fit_lut_lattice(Sf_enc[ui], cs.encode(tx, tf),
                                       L=lattice_L, weights=uw), size)
        except ImportError:
            pass          # geomloss not installed -> candidate simply absent
        except Exception:
            pass          # never let an optional candidate sink the match
        # Local-AI candidate #1: scene segmentation -> region-to-region transport.
        if neural:
            try:
                from . import neural as nn_mod
                if not nn_mod.loaded_nowait():
                    raise ImportError("segmentation still warming up — skip this round")
                _p(0.35, "AI scene analysis")
                nctx = nn_mod.prepare(src_enc, tgt_enc, tf, size=size,
                                      lattice_L=lattice_L, seed=seed)
                if nctx is not None:
                    luts["neural"] = nctx.lut
            except Exception:
                nctx = None

    # Score every candidate LUT on exactly what it will output. Judged in OKLAB
    # (x100 to keep dE-like magnitudes): near-CAM16 perceptual uniformity without
    # CIELAB's blue-drifts-purple defect, so the judge can no longer be gamed by
    # hue errors CIELAB under-counts. Reported accuracy (de_after) stays dE00 —
    # the industry-familiar number.
    # Score what SHIPS. Every candidate first goes through the same gamut and
    # steepness guards it would get as the winner. Scoring the raw maps let an
    # exact transport promise a distribution distance of 0.18 while its guarded,
    # smoothed self — the LUT actually delivered — measured 3.25 on the picture:
    # the judge was crowning a candidate that never existed.
    safe = luts.get("grade", luts.get("mkl"))
    if safe is not None:
        for name in list(luts):
            if name == "grade":
                luts[name] = steep_guard(luts[name], luts[name])
            else:
                luts[name] = steep_guard(gamut_guard(luts[name], Sf_enc, safe), safe)

    _p(0.62, "Scoring candidates")
    scores = {}
    # Score on a bounded subsample: with 6 candidates the full pixel set costs
    # seconds for a MEAN that 120k pixels already pin to 3 decimals.
    _SCORE_N = 60_000
    if corresponded:
        metric = "Oklab dE (x100)"
        sidx = (rng.choice(S_enc.shape[0], _SCORE_N, replace=False)
                if S_enc.shape[0] > _SCORE_N else np.arange(S_enc.shape[0]))
        S_sc, T_sc = S_enc[sidx], T_enc[sidx]
        tgt_ok = cs.encoded_to_oklab(T_sc, tf) * 100.0
        for name, lut in luts.items():
            out_ok = cs.encoded_to_oklab(apply_lut_points(lut, S_sc), tf) * 100.0
            scores[name] = float(np.mean(np.linalg.norm(out_ok - tgt_ok, axis=-1)))
    elif nctx is not None:
        # AI available: judge REGION BY REGION (sky<->sky, skin<->skin) — the
        # cross-scene accuracy a single global distance misses — AND by the
        # neutral global distribution distance.
        #
        # Both are needed because the semantic metric is derived from the very
        # segmentation the "neural" candidate is fitted on: it grades its own
        # homework. Measured on a two-region test scene, the semantic judge
        # crowned "neural" (0.133) while a neutral Oklab sliced-Wasserstein put
        # it LAST (0.209 vs idt's 0.076) — i.e. the engine was shipping the
        # objectively weaker match. Combining as a geometric mean of each
        # metric RELATIVE TO ITS OWN BEST keeps the region insight but denies
        # either judge the power to carry a candidate alone.
        metric = "region + distribution (combined)"
        sem, neu = {}, {}
        # bounded scoring sample (12 metric passes across 6 candidates otherwise
        # dominates the match); idx must be sliced identically so the semantic
        # judge still looks up the right per-pixel class labels
        ssel = (rng.choice(Sf_enc.shape[0], _SCORE_N, replace=False)
                if Sf_enc.shape[0] > _SCORE_N else np.arange(Sf_enc.shape[0]))
        Sf_sc, idx_sc = Sf_enc[ssel], idx[ssel]
        tsel = (rng.choice(Tf_enc.shape[0], _SCORE_N, replace=False)
                if Tf_enc.shape[0] > _SCORE_N else np.arange(Tf_enc.shape[0]))
        tgt_ok_s = cs.encoded_to_oklab(Tf_enc[tsel], tf) * 100.0
        for name, lut in luts.items():
            out_enc = apply_lut_points(lut, Sf_sc)
            sem[name] = float(nctx.semantic_distance(out_enc, idx_sc))
            neu[name] = float(sliced_wasserstein(
                cs.encoded_to_oklab(out_enc, tf) * 100.0, tgt_ok_s, seed=seed))
        smin = max(min(sem.values()), 1e-9)
        nmin = max(min(neu.values()), 1e-9)
        for name in luts:
            scores[name] = float(np.sqrt((sem[name] / smin) * (neu[name] / nmin)))

        def _combined(out_enc):          # refine must be judged the SAME way
            s = float(nctx.semantic_distance(out_enc, idx_sc)) / smin
            n = float(sliced_wasserstein(
                cs.encoded_to_oklab(out_enc, tf) * 100.0, tgt_ok_s, seed=seed)) / nmin
            return float(np.sqrt(s * n))
    else:
        metric = "sliced-Wasserstein (Oklab)"
        ssel = (rng.choice(Sf_enc.shape[0], _SCORE_N, replace=False)
                if Sf_enc.shape[0] > _SCORE_N else np.arange(Sf_enc.shape[0]))
        tsel = (rng.choice(Tf_enc.shape[0], _SCORE_N, replace=False)
                if Tf_enc.shape[0] > _SCORE_N else np.arange(Tf_enc.shape[0]))
        tgt_ok = cs.encoded_to_oklab(Tf_enc[tsel], tf) * 100.0
        for name, lut in luts.items():
            out_ok = cs.encoded_to_oklab(apply_lut_points(lut, Sf_enc[ssel]), tf) * 100.0
            scores[name] = sliced_wasserstein(out_ok, tgt_ok, seed=seed)

    # Naturalness penalties, measured on what each candidate does to the actual
    # picture. A distribution distance alone is gamed by exact-transport maps:
    # on real footage IDT "won" while turning an orange engine lamp green (hue
    # twist 125 deg), growing green blotches on a dark sweater from a reference's
    # green-screen prop, and stretching a flat sky into a banded 5x-contrast
    # blob. Each of those is a measurable property of the output, so each is a
    # multiplicative penalty (scale-free across the metrics): steepness, local
    # contrast amplification, new clipping, hue twist of chromatic pixels and
    # colour spread among pixels that were neutral. A wilder map now only wins
    # if it is MUCH closer to the reference than a natural-looking one.
    nat = _naturalness_probe(src_enc, tf, luts.get("grade"))
    penalties = {}
    for name in scores:
        pen = 1.0 + 0.15 * max(0.0, lut_steepness(luts[name]) - 4.0)
        m = _naturalness(nat, luts[name], tf)
        pen *= 1.0 + 0.8 * max(0.0, m["detail"] - 1.5)
        pen *= 1.0 + 4.0 * max(0.0, m["clip_inc"] - 0.01)
        pen *= 1.0 + 0.05 * max(0.0, m["twist"] - 20.0)
        pen *= 1.0 + 0.5 * max(0.0, m["nspread"] - 2.0)
        pen *= 1.0 + 0.3 * max(0.0, m["uneven"] - 3.5)
        pen *= 1.0 + 4.0 * max(0.0, m["smooth"] - 1.8)
        penalties[name] = (pen, m)
        scores[name] = float(scores[name]) * pen
    # Hard limits. An exact-transport map is BUILT to minimise the distribution
    # distance, so on unrelated content it can be 4-5x "closer" while visibly
    # wrecking the picture — no proportional penalty is safe against that. Past
    # these limits the damage is unmistakable to any viewer, so such a candidate
    # is out of the running whenever a clean one exists.
    clean = [n for n in scores
             if penalties[n][1]["twist"] <= 40.0 and penalties[n][1]["detail"] <= 3.0
             and penalties[n][1]["clip_inc"] <= 0.15 and penalties[n][1]["nspread"] <= 4.0
             and penalties[n][1]["uneven"] <= 8.0]
    if clean:
        ranked = clean
        best = min(ranked, key=scores.get)
    else:
        # Nothing is clean (a reference that is 96% black, a source that is one
        # flat colour): every candidate wrecks the picture, so "closest to the
        # reference" is the wrong tie-break — it rewards the biggest wreck (a
        # day exterior crushed 40% to black to honour a black leader). Ship the
        # LEAST damaging candidate instead.
        ranked = sorted(scores, key=lambda n: penalties[n][0])
        best = ranked[0]
    # Incumbent rule: the hue-preserving grade is the one candidate that cannot
    # scramble a picture, so an exact-transport map (idt / flow / uot) has to
    # BEAT it clearly — 25% lower score, penalties included — to ship. Where
    # IDT genuinely belongs (two shots of one set) it scores 2-5x lower than
    # the grade, not 1.3x; the near-ties are exactly the pairs where its extra
    # "accuracy" is the reference's content forced onto the wrong objects
    # (the live engine crowned it at 0.48 vs 0.67 on the lamp pair, hue twist
    # 48 deg — the lamp went white-green; the limits were set from that).
    if ("grade" in ranked and best in ("idt", "flow", "uot")
            and scores[best] > 0.75 * scores["grade"]):
        best = "grade"
    res = MatchResult(method=best, scores=scores, lut=luts[best], tf=tf,
                      corresponded=corresponded, score_metric=metric)
    if _gate_note:
        res.notes.append(_gate_note)
    _pm = penalties[best][1]
    res.notes.append(f"naturalness of the chosen look: contrast x{_pm['detail']:.2f}, "
                     f"hue twist {_pm['twist']:.0f} deg, new clipping "
                     f"{100 * max(0.0, _pm['clip_inc']):.1f}%")

    # Residual second pass (distribution mode, AI regions available): apply the
    # winner, derive a region-to-region correction on (applied, reference), and keep
    # the composed LUT only if it MEASURABLY improves the region match (+5.5% in
    # testing). The original nctx stays the judge, so this can never make it worse.
    if not corresponded and refine and nctx is not None:
        try:
            from . import neural as nn_mod
            _p(0.66, "Fine-tuning the match")
            applied_enc = apply_lut(src_enc, res.lut)
            # Reuse both segmentations: the reference is the SAME image, and a LUT
            # recolours the source without moving object boundaries. This was the
            # match's single most expensive step (measured 30.8s of a 60s match,
            # the "stuck at 67%" stall) and it was recomputing known answers.
            ctx2 = nn_mod.prepare(applied_enc, tgt_enc, tf, size=size,
                                  lattice_L=lattice_L, seed=seed,
                                  src_labels=getattr(nctx, "src_map", None),
                                  ref_labels=getattr(nctx, "ref_map", None))
            _p(0.76, "Fine-tuning the match")
            if ctx2 is not None:
                ax = np.linspace(0.0, 1.0, size)
                Rg, Gg, Bg = np.meshgrid(ax, ax, ax, indexing="ij")
                grid = np.stack([Rg, Gg, Bg], -1).reshape(-1, 3)
                comp = apply_lut_points(ctx2.lut, apply_lut_points(res.lut, grid))
                comp = comp.reshape(size, size, size, 3)
                s_base = scores[best]
                s_comp = _combined(apply_lut_points(comp, Sf_sc))
                _p(0.86, "Fine-tuning the match")
                if s_comp < s_base:
                    res.lut = comp
                    res.method = best + "+refine"
                    res.scores[res.method] = float(s_comp)
                    res.notes.append(f"residual refine: {s_base:.3f} -> {s_comp:.3f}")
        except Exception:
            pass

    # The gamut guard (the "sometimes the colours go insane" fix) was applied to
    # every candidate before scoring: the winner was fit on sampled pixels only,
    # and in cube regions those samples never touched it is blended toward the
    # safe global look — the hue-preserving grade — so a later frame's
    # out-of-sample colour (flash, neon, deep shadow) can never read
    # extrapolation garbage. (The linear MKL that used to fill this role clipped
    # 8-43% of the picture on real footage when the reference was much darker or
    # brighter.) A +refine composition is the one thing built after scoring.
    if safe is not None:
        if res.method.endswith("+refine"):
            res.lut = steep_guard(gamut_guard(res.lut, Sf_enc, safe), safe)
        # Keep the top runner-up candidates (same guards) so the panel can offer
        # them as alternative looks. Winner goes first under its own name.
        res.alts[res.method] = res.lut.astype(np.float32)
        # small linear samples for the WB/Tone/Colour strength decomposition
        res.sample_src_lin = Sf_lin[:30000].astype(np.float32)
        res.sample_tgt_lin = Tf_lin[:30000].astype(np.float32)
        for name in sorted(ranked, key=scores.get):
            if len(res.alts) >= 3:
                break
            if name in res.alts or name == best:
                continue
            res.alts[name] = luts[name].astype(np.float32)

    if corresponded and src_enc.shape == tgt_enc.shape:
        # The before/after accuracy report on an evenly strided 1M-pixel subset:
        # CIEDE2000 over a whole 7-frame stack (14.5M pixels) took ~15 s and
        # 7 GB of RAM for four summary numbers that 1M pixels pin to 0.01.
        k = max(1, int(np.ceil(S_enc.shape[0] / 1_000_000)))
        S_x, T_x = S_enc[::k], T_enc[::k]
        de_b = delta_e00(cs.encoded_to_lab(S_x, tf), cs.encoded_to_lab(T_x, tf))
        de_a = delta_e00(cs.encoded_to_lab(apply_lut_points(res.lut, S_x), tf),
                         cs.encoded_to_lab(T_x, tf))
        res.de_before = summarize(de_b)
        res.de_after = summarize(de_a)
        sm = skin_mask(S_x)
        if sm.sum() > 50:
            res.de_skin_before = float(de_b[sm].mean())
            res.de_skin_after = float(de_a[sm].mean())
    else:
        res.notes.append("Distribution mode: matched colour distributions "
                         "(no per-pixel ground truth).")
    _p(1.0, "Match complete")
    # Outcome-based safety net for a mis-clicked "Same scene": if the winning
    # corresponded mapping is still catastrophically wrong (dE00 > 8 means the
    # frames were never really aligned — the field report showed MKL "winning"
    # at 26), redo the whole match as distribution matching. Costs a second pass
    # only in the broken case; an honest match on aligned frames scores ~0-3.
    if (corresponded and res.de_after is not None
            and res.de_after.get("mean", 0.0) > 8.0):
        de_bad = float(res.de_after["mean"])
        res = match(src_enc, tgt_enc, corresponded=False, tf=tf, size=size,
                    degrees=degrees, lattice_L=lattice_L, sample=sample,
                    seed=seed, skin_protect=skin_protect, skin_weight=skin_weight,
                    neural=neural, look=look, refine=refine, quick=quick,
                    progress=progress)
        res.notes.append("Same-scene mode produced a poor aligned match "
                         f"(dE00 {de_bad:.1f} before fallback); "
                         "re-matched as different scenes.")
        return res

    return res


def _naturalness_probe(src_enc: np.ndarray, tf: str,
                       hue_ref_lut: np.ndarray | None = None) -> dict:
    """A small copy of the source picture (<= 360 px wide, spatial structure kept)
    plus its own local-contrast energy, clipping and Oklab chroma/hue, so each
    candidate's effect on the PICTURE can be measured, not just on a pixel bag.
    `hue_ref_lut` (the hue-preserving grade) defines where each colour SHOULD
    land hue-wise; candidates are judged for twisting against it."""
    from scipy.ndimage import laplace
    a = np.asarray(src_enc, dtype=np.float64)
    if a.ndim != 3:
        a = a.reshape(-1, 1, 3)
    # <= 360 px wide AND <= 250k pixels: a 7-frame pooled stack is 7x taller,
    # and every candidate is applied to this picture
    step = max(1, int(np.ceil(a.shape[1] / 360.0)),
               int(np.ceil(np.sqrt(a.shape[0] * a.shape[1] / 250_000.0))))
    img = np.ascontiguousarray(a[::step, ::step])
    luma = 0.2126 * img[..., 0] + 0.7152 * img[..., 1] + 0.0722 * img[..., 2]
    flat = img.reshape(-1, 3)
    ok = cs.encoded_to_oklab(flat, tf)
    C = np.hypot(ok[:, 1], ok[:, 2])
    ref_ab = None
    if hue_ref_lut is not None:
        ref_ab = cs.encoded_to_oklab(apply_lut(img, hue_ref_lut).reshape(-1, 3), tf)[:, 1:]
    # the smoothest 30% of the picture in Oklab (skies, walls, skin): where
    # banding and hue arcs show first
    g_in = _oklab_gradient(ok.reshape(img.shape[0], img.shape[1], 3) * 100.0)
    smooth_mask = g_in <= max(1.0, float(np.percentile(g_in, 30)))
    return {
        "img": img,
        "lap": max(float(np.std(laplace(luma))), 1e-6),
        "blocks": _block_energy(laplace(luma)),
        "clip": float(np.mean((luma <= 2 / 255) | (luma >= 253 / 255))),
        "ab": ok[:, 1:],
        "ref_ab": ref_ab,
        "chroma": C,
        "chromatic": C > 0.05,
        "neutral": C < 0.02,
        "smooth_mask": smooth_mask if smooth_mask.sum() >= 400 else None,
        "smooth_g": float(g_in[smooth_mask].mean()) + 1e-9 if smooth_mask.sum() >= 400 else 1.0,
    }


def _oklab_gradient(ok_img: np.ndarray) -> np.ndarray:
    gx = np.diff(ok_img, axis=1)[:-1]
    gy = np.diff(ok_img, axis=0)[:, :-1]
    return np.sqrt((gx ** 2).sum(-1) + (gy ** 2).sum(-1))


def _block_energy(lap: np.ndarray, b: int = 8) -> np.ndarray:
    """Local-contrast energy per bxb block of a Laplacian image."""
    h, w = (lap.shape[0] // b) * b, (lap.shape[1] // b) * b
    if h == 0 or w == 0:
        return np.array([float(np.std(lap))])
    blk = lap[:h, :w].reshape(h // b, b, w // b, b)
    return blk.std(axis=(1, 3)).ravel()


def _naturalness(probe: dict, lut: np.ndarray, tf: str) -> dict:
    """detail: local-contrast energy after/before (5x = a flat sky stretched into
    banding); clip_inc: fraction of the picture newly crushed to black/white;
    twist: chroma-weighted 90th-percentile hue rotation (deg) of clearly-
    chromatic pixels AFTER removing the best global affine chroma move — a
    white-balance shift or a saturation change is free (that is a grade), only
    pixels sent somewhere else than the rest count (orange->green scores ~120);
    nspread: std of output chroma (x100) over pixels that were neutral (a cast
    is uniform; blotches are not)."""
    from scipy.ndimage import laplace
    out = apply_lut(probe["img"], lut)
    luma = 0.2126 * out[..., 0] + 0.7152 * out[..., 1] + 0.0722 * out[..., 2]
    flat = out.reshape(-1, 3)
    ok = cs.encoded_to_oklab(flat, tf)
    C = np.hypot(ok[:, 1], ok[:, 2])
    lap_out = laplace(luma)
    m = {"detail": float(np.std(lap_out)) / probe["lap"],
         "clip_inc": float(np.mean((luma <= 2 / 255) | (luma >= 253 / 255))) - probe["clip"],
         "twist": 0.0, "nspread": 0.0, "uneven": 1.0, "smooth": 1.0}
    # Smooth-region damage: how much rougher (in Oklab, so banding and hue
    # arcs both count) the candidate renders the regions the source keeps
    # smooth. Field pair (a no-sky wheat close-up as reference for a wide shot
    # with a blue sky): idt 3.14, sep 1.17, mkl 0.89; a well-matched pair sits
    # near 1.5 for every candidate.
    if probe.get("smooth_mask") is not None:
        g_out = _oklab_gradient(ok.reshape(out.shape[0], out.shape[1], 3) * 100.0)
        m["smooth"] = float(g_out[probe["smooth_mask"]].mean()) / probe["smooth_g"]
    # Unevenness: local contrast change per block, 95th percentile over the
    # median. A tone curve changes contrast smoothly with tone (ratio ~1-2.5);
    # posterised blotches — smooth skin turned into patches — show as a few
    # blocks with 5-10x the change of the rest.
    e_in = probe["blocks"]
    e_out = _block_energy(lap_out)
    textured = e_in >= max(float(np.percentile(e_in, 20)), 0.003)   # blocks with real texture only
    if textured.sum() >= 30:
        ratio = e_out[textured] / e_in[textured]
        m["uneven"] = float(np.percentile(ratio, 95) / max(float(np.median(ratio)), 1e-6))
    # chroma-collapse counts as twist too: the output-chroma condition that used
    # to be here hid an orange lamp turned white (its output chroma fell under
    # the threshold, so the pixels that mattered most were skipped)
    sel = probe["chromatic"]
    if sel.sum() > 50:
        a_out = ok[sel, 1:]
        w = probe["chroma"][sel]
        # Where SHOULD each colour land? Under the hue-preserving grade when it
        # exists (else: where it already is). That prediction already contains
        # the legitimate balance shift and saturation change, so NO further
        # freedom is granted — a fit that allowed a global shift or rotation on
        # top absorbed a whole family of oranges turning cyan as "global"
        # (measured). Deviation is an angle over the predicted chroma, so a
        # collapsed (whitened) lamp counts as much as a rotated one.
        base = probe["ref_ab"][sel] if probe.get("ref_ab") is not None else probe["ab"][sel]
        zb = base[:, 0] + 1j * base[:, 1]
        zo = a_out[:, 0] + 1j * a_out[:, 1]
        dev = np.abs(zo - zb)
        dh = np.degrees(np.arctan2(dev, np.maximum(np.abs(zb), 0.02)))
        # per HUE FAMILY (30-degree bins of the source hue): a small orange lamp
        # is 0.4% of the frame, so a picture-wide percentile never reaches it,
        # yet "the orange things went white/green" is exactly what a viewer
        # sees. Score each family that carries at least 2% of the chroma by its
        # weighted median deviation; the worst family is the twist.
        hue_bin = ((np.degrees(np.arctan2(probe["ab"][sel][:, 1], probe["ab"][sel][:, 0]))
                    + 360.0) % 360.0 / 30.0).astype(int)
        worst = 0.0
        for b in range(12):
            inb = hue_bin == b
            wb = w[inb]
            if wb.sum() < 0.02 * w.sum() or inb.sum() < 30:
                continue
            order = np.argsort(dh[inb])
            cw = np.cumsum(wb[order]) / wb.sum()
            worst = max(worst, float(dh[inb][order][min(int(np.searchsorted(cw, 0.5)), len(wb) - 1)]))
        m["twist"] = worst
    neut = probe["neutral"]
    if neut.sum() > 50:
        m["nspread"] = float(np.sqrt(ok[neut, 1:].var(axis=0).sum())) * 100.0
    return m


def format_report(res: MatchResult) -> str:
    lines = ["colourMatik — match report",
             f"  working space : {res.tf}",
             f"  mode          : {'corresponded' if res.corresponded else 'distribution'}",
             f"  candidates (lower is better, {res.score_metric}):"]
    for name, s in sorted(res.scores.items(), key=lambda kv: kv[1]):
        star = "  <- chosen" if name == res.method else ""
        lines.append(f"      {name:<8}: {s:6.3f}{star}")
    if res.de_before and res.de_after:
        b, a = res.de_before, res.de_after
        lines += ["  applied-LUT accuracy (per-pixel dE00):",
                  f"      before : mean {b['mean']:.3f}  p95 {b['p95']:.3f}  max {b['max']:.3f}",
                  f"      after  : mean {a['mean']:.3f}  p95 {a['p95']:.3f}  max {a['max']:.3f}   [{verdict(a['mean'])}]",
                  f"      improvement: {b['mean'] / max(a['mean'], 1e-9):.1f}x lower dE00"]
    if res.de_skin_after is not None:
        lines.append(f"  skin-tone accuracy: dE00 {res.de_skin_before:.3f} -> "
                     f"{res.de_skin_after:.3f}   [{verdict(res.de_skin_after)}]")
    for n in res.notes:
        lines.append(f"  note: {n}")
    return "\n".join(lines)
