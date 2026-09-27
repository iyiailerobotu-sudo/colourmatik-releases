"""Ground-truth accuracy + LUT-correctness tests for colourMatik.

Run:  ./.venv/bin/python -m tests.run_tests
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import colour

from colourmatik.colorspace import decode, encode
from colourmatik.match import match, format_report
from colourmatik.lut import build_lut, write_cube, apply_lut
from colourmatik import transforms as tf_mod
from tests import synth

OUT = Path(__file__).parent / "_out"
OUT.mkdir(exist_ok=True)
FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}   {detail}")
    if not cond:
        FAILS.append(name)


def save(name, enc):
    from colourmatik.io import save_image
    save_image(OUT / name, enc)


def test_accuracy():
    print("\n=== 1. Ground-truth colour-match accuracy ===")
    ref = synth.build_reference()
    save("reference.png", ref)

    cases = {
        "linear": (synth.distort_linear, 1.0),      # affine -> expect near-perfect
        "nonlinear": (synth.distort_nonlinear, 2.0),  # tone+sat -> expect excellent
    }
    for cname, (distort, thresh) in cases.items():
        src = distort(ref)                 # simulated "video 2"
        save(f"{cname}_distorted.png", src)
        res = match(src, ref, corresponded=True, size=65)
        print(f"\n-- distortion: {cname} --")
        print(format_report(res))
        matched = apply_lut(src, res.lut)
        save(f"{cname}_matched.png", matched)

        b, a = res.de_before["mean"], res.de_after["mean"]
        check(f"{cname}: LUT accuracy mean dE00 < {thresh}", a < thresh, f"(got {a:.3f})")
        check(f"{cname}: improved >=5x", b / max(a, 1e-9) >= 5.0,
              f"(before {b:.2f} -> after {a:.3f}, {b/max(a,1e-9):.1f}x)")


def test_distribution():
    """Distribution mode: match WITHOUT using pixel correspondence.
    We still secretly have ground truth (same scene, known grade) so we can
    measure real dE00 even though the matcher only saw the two distributions."""
    print("\n=== 4. Distribution mode (no correspondence) ===")
    ref = synth.build_reference()
    src = synth.distort_nonlinear(ref)  # aligned GT exists, but matcher won't use it
    res = match(src, ref, corresponded=False, size=65)
    print(format_report(res))
    matched = apply_lut(src, res.lut)
    from colourmatik.metrics import image_delta_e00, summarize
    b = summarize(image_delta_e00(src, ref))["mean"]
    a = summarize(image_delta_e00(matched, ref))["mean"]
    print(f"  (ground-truth check) per-pixel dE00: before {b:.3f} -> after {a:.3f}")
    # Same scene, known grade: the judge must keep the accurate candidate here
    # (the naturalness penalties exist for UNRELATED content, not for this).
    check("distribution: real dE00 after < 1.5", a < 1.5,
          f"(after {a:.3f}, chose {res.method}; scores "
          + ", ".join(f"{k} {v:.2f}" for k, v in sorted(res.scores.items(), key=lambda kv: kv[1])) + ")")
    check("distribution: recolour improves real dE00 >=3x", b / max(a, 1e-9) >= 3.0,
          f"(before {b:.2f} -> after {a:.3f}, {b/max(a,1e-9):.1f}x)")


def test_skin():
    """Skin-tone protection: detection specificity + weighted fit lowers skin dE00."""
    print("\n=== 5. Skin-tone protection ===")
    from colourmatik.skin import skin_probability
    from colourmatik import transforms as tfm
    from colourmatik.colorspace import decode, encoded_to_lab
    from colourmatik.metrics import delta_e00

    ref, mask = synth.build_portrait()
    save("portrait_reference.png", ref)
    p = skin_probability(ref)
    face_p, bg_p = float(p[mask].mean()), float(p[~mask].mean())
    check("skin detected on face", face_p > 0.5, f"(face prob {face_p:.2f})")
    check("skin NOT on background", bg_p < 0.15, f"(bg prob {bg_p:.2f})")

    # Constrained model (poly1) so protection must trade off -> effect is visible.
    src = synth.distort_nonlinear(ref)
    S_lin, T_lin = decode(src).reshape(-1, 3), decode(ref).reshape(-1, 3)
    skin_p = skin_probability(src).reshape(-1)
    ref_lab = encoded_to_lab(ref).reshape(-1, 3)
    m = mask.reshape(-1)

    def skin_de(weights):
        f = tfm.fit_polynomial(S_lin, T_lin, degree=1, weights=weights)
        lut = build_lut(f, size=33)
        out_lab = encoded_to_lab(apply_lut(src, lut)).reshape(-1, 3)
        return float(delta_e00(out_lab[m], ref_lab[m]).mean())

    de_plain = skin_de(None)
    de_prot = skin_de(1.0 + 8.0 * skin_p)
    print(f"  skin dE00 (poly1):  unprotected {de_plain:.3f} -> protected {de_prot:.3f}")
    check("skin protection lowers skin dE00", de_prot < de_plain - 1e-3,
          f"({de_plain:.3f} -> {de_prot:.3f})")

    res = match(src, ref, corresponded=True, size=65, skin_protect=True)
    check("match reports skin accuracy", res.de_skin_after is not None,
          f"(skin dE00 {res.de_skin_after})")


def test_lut_correctness():
    print("\n=== 2. .cube correctness vs independent reader (colour-science) ===")
    ref = synth.build_reference()
    src = synth.distort_nonlinear(ref)

    # fit a real transform, bake to LUT, write .cube
    S = decode(src).reshape(-1, 3)
    T = decode(ref).reshape(-1, 3)
    f = tf_mod.fit_polynomial(S, T, degree=3)
    lut = build_lut(f, size=33)
    cube = OUT / "roundtrip.cube"
    write_cube(cube, lut, title="roundtrip")

    # intended transform, evaluated directly (ground truth for the LUT)
    out_direct = np.clip(encode(np.clip(f(decode(src.reshape(-1, 3))), 0, None)), 0, 1)

    # our own LUT application
    out_mine = apply_lut(src, lut).reshape(-1, 3)

    # independent, spec-correct reader applies the WRITTEN FILE
    lut3d = colour.io.read_LUT(str(cube))
    out_colour = np.clip(np.asarray(lut3d.apply(src)).reshape(-1, 3), 0, 1)

    d_mine = float(np.mean(np.abs(out_mine - out_direct)))
    d_colour = float(np.mean(np.abs(out_colour - out_direct)))
    d_cross = float(np.max(np.abs(out_mine - out_colour)))

    check("our apply matches intended transform", d_mine < 5e-3, f"(mean|d|={d_mine:.5f})")
    check("WRITTEN .cube (read by colour-science) matches intended transform",
          d_colour < 5e-3, f"(mean|d|={d_colour:.5f})  <- ordering/format proof")
    check("our apply == colour-science apply", d_cross < 5e-3, f"(max|d|={d_cross:.5f})")


def test_identity():
    print("\n=== 3. Identity LUT sanity ===")
    ref = synth.build_reference()
    ident = build_lut(lambda x: x, size=33)
    out = apply_lut(ref, ident)
    d = float(np.max(np.abs(out - ref)))
    check("identity transform LUT ~ passthrough", d < 5e-3, f"(max|d|={d:.5f})")


def test_decode_plan():
    """Frames must be decoded the way Premiere shows them: matrix by tag (else
    Rec.709 for HD / 601 for SD), range by tag, HDR tone-mapped to SDR."""
    print("\n=== 4. Frame decoding: colour matrix / range / HDR plan ===")
    from colourmatik import io as cmio
    cases = {
        "untagged HD -> bt709": (
            "Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 1920x1080, 25921 kb/s, 24 fps",
            "in_color_matrix=bt709:in_range=tv", None),
        "untagged SD -> bt601": (
            "Stream #0:0: Video: mpeg4, yuv420p, 720x576, 25 fps",
            "in_color_matrix=bt601:in_range=tv", None),
        "tagged 709 full-range": (
            "Stream #0:1[0x2](und): Video: h264 (Constrained Baseline) (avc1 / 0x31637661), yuv420p(pc, bt709/bt709/iec61966-2-1, progressive), 1280x720, 14219 kb/s",
            "in_color_matrix=bt709:in_range=pc", None),
        "tagged 709 prores 4444 12-bit": (
            "Stream #0:0[0x1]: Video: prores (4444) (ap4h / 0x68347061), yuv444p12le(tv, bt709, progressive), 4608x3164, 1976385 kb/s",
            "in_color_matrix=bt709:in_range=tv", None),
        "iPhone HLG -> bt2020 + tone-map": (
            "Stream #0:0[0x1](und): Video: hevc (Main 10) (hvc1 / 0x31637668), yuv420p10le(tv, bt2020nc/bt2020/arib-std-b67), 1920x1080, 12635 kb/s, 25 fps",
            "in_color_matrix=bt2020:in_range=tv", "arib-std-b67"),
    }
    for name, (banner, want, want_hdr) in cases.items():
        args, hdr = cmio._decode_plan(cmio._parse_ffmpeg_banner(banner))
        vf = args[args.index("-vf") + 1] if "-vf" in args else ""
        check(f"decode plan: {name}", want in vf and hdr == want_hdr, f"(got {vf!r}, hdr={hdr})")
    args, hdr = cmio._decode_plan(cmio._parse_ffmpeg_banner(
        "Stream #0:0: Video: png, rgb24(pc), 1920x1080, 25 fps"))
    check("decode plan: RGB source gets no matrix filter", args == [] and hdr is None, f"(got {args})")
    d = cmio._parse_ffmpeg_banner("Duration: 00:01:02.50, start: 0.000000, bitrate: 1 kb/s")
    check("banner duration parsed", abs(d.get("duration", 0) - 62.5) < 1e-6, f"(got {d})")

    # HLG reference white (signal 0.75 = 203 nits, BT.2408) must land on SDR white,
    # HLG black on black, and a pure BT.2020 red must stay red after the
    # primaries conversion (no channel swap, no hue flip).
    white = cmio._hdr_to_sdr(np.full((1, 1, 3), 0.75), "arib-std-b67")[0, 0]
    black = cmio._hdr_to_sdr(np.zeros((1, 1, 3)), "arib-std-b67")[0, 0]
    red = cmio._hdr_to_sdr(np.array([[[0.6, 0.0, 0.0]]]), "arib-std-b67")[0, 0]
    check("HLG 0.75 -> SDR white", float(white.min()) > 0.93 and float(white.max()) <= 1.0, f"(got {white.round(3)})")
    check("HLG 0 -> black", float(black.max()) < 1e-3, f"(got {black.round(4)})")
    check("HLG red stays red", red[0] > 0.5 and red[1] < 0.2 and red[2] < 0.2, f"(got {red.round(3)})")
    pq_white = cmio._hdr_to_sdr(np.full((1, 1, 3), 0.58), "smpte2084")[0, 0]
    check("PQ 0.58 (~203 nits) -> SDR white", float(pq_white.min()) > 0.93, f"(got {pq_white.round(3)})")

    # frames stream from ffmpeg as PPM: 8-bit, 16-bit (HDR) and a truncated stream
    rgb8 = (np.arange(2 * 3 * 3) % 256).astype(np.uint8).reshape(3, 2, 3)
    ppm8 = b"P6\n2 3\n255\n" + rgb8.tobytes()
    a = cmio._parse_ppm(ppm8)
    check("PPM 8-bit parsed", a is not None and a.shape == (3, 2, 3) and abs(a[2, 1, 2] * 255 - rgb8[2, 1, 2]) < 1e-9)
    rgb16 = (np.arange(2 * 3 * 3) * 3000 % 65536).astype(">u2").reshape(3, 2, 3)
    ppm16 = b"P6\n# comment-free\n2 3\n65535\n" + rgb16.tobytes()
    b = cmio._parse_ppm(ppm16)
    check("PPM 16-bit parsed", b is not None and b.shape == (3, 2, 3) and abs(b[1, 0, 1] * 65535 - rgb16[1, 0, 1]) < 1e-6,
          f"(got {None if b is None else b.shape})")
    check("PPM truncated -> None (falls back to PNG)", cmio._parse_ppm(ppm16[:-5]) is None)


def _scene(rng, h=240, w=360):
    """A soft-lit synthetic 'scene': gradient + shapes, moderate chroma."""
    y, x = np.mgrid[0:h, 0:w].astype(np.float64)
    base = 0.25 + 0.5 * (x / w) * (0.6 + 0.4 * (y / h))
    img = np.stack([base * 0.95, base * 0.9, base * 1.0], -1)
    for _ in range(12):
        cy, cx, r = rng.uniform(0, h), rng.uniform(0, w), rng.uniform(15, 60)
        m = ((y - cy) ** 2 + (x - cx) ** 2) < r * r
        img[m] = np.clip(img[m] * rng.uniform(0.5, 1.5, 3) + rng.uniform(-0.15, 0.15, 3), 0, 1)
    img += rng.normal(0, 0.01, img.shape)
    return np.clip(img, 0.0, 1.0)


def test_robustness():
    """The field failures of 1.7.x, each reduced to a synthetic case that must
    stay fixed: contrast explosion on a flat source, hue twisting of a colour
    the reference lacks, green blotches from a small saturated reference prop,
    a degenerate single-colour source, log-to-log identity, and 'Same scene'
    clicked for two different shots."""
    print("\n=== 6. Robustness on the field failure cases ===")
    from colourmatik.match import _naturalness_probe, _naturalness
    from colourmatik.colorspace import encoded_to_oklab
    rng = np.random.default_rng(7)
    wide = _scene(rng)                                   # a normal, wide-range reference

    # 1) flat sky -> wide reference: no contrast explosion, no banding slopes
    y, x = np.mgrid[0:240, 0:360].astype(np.float64)
    sky = np.stack([0.55 + 0.04 * y / 240, 0.62 + 0.04 * y / 240, 0.72 + 0.03 * y / 240], -1)
    sky = np.clip(sky + rng.normal(0, 0.004, sky.shape), 0, 1)
    res = match(sky, wide, corresponded=False, size=33)
    m = _naturalness(_naturalness_probe(sky, "sRGB"), res.lut, "sRGB")
    check("flat sky: local contrast amplified < 2.5x", m["detail"] < 2.5,
          f"(x{m['detail']:.2f}, chose {res.method})")
    from colourmatik.lut import lut_steepness
    check("flat sky: shipped LUT slope <= 6", lut_steepness(res.lut) <= 6.0 + 1e-6,
          f"({lut_steepness(res.lut):.1f})")

    # 2) orange lamp vs a reference full of green screens: the lamp stays orange
    src = _scene(rng)
    lamp = ((y - 120) ** 2 + (x - 180) ** 2) < 40 ** 2
    src[lamp] = [0.95, 0.55, 0.15]
    ref = _scene(rng)
    scr = ((y - 100) ** 2 / 2 + (x - 200) ** 2) < 60 ** 2
    ref[scr] = [0.15, 0.85, 0.35]
    res = match(src, ref, corresponded=False, size=33)
    out = apply_lut(src, res.lut)
    ho = np.degrees(np.arctan2(*encoded_to_oklab(out[lamp].mean(0))[[2, 1]]))
    hs = np.degrees(np.arctan2(*encoded_to_oklab(src[lamp].mean(0))[[2, 1]]))
    dh = abs((ho - hs + 180) % 360 - 180)
    check("orange lamp does not turn green", dh < 40, f"(hue moved {dh:.0f} deg, chose {res.method})")

    # 3) a small saturated prop in the reference must not blotch the neutral source
    neutral = np.repeat(np.linspace(0.15, 0.75, 360)[None, :, None], 240, axis=0)
    neutral = np.clip(np.repeat(neutral, 3, axis=2) + rng.normal(0, 0.01, (240, 360, 3)), 0, 1)
    res = match(neutral, ref, corresponded=False, size=33)
    m = _naturalness(_naturalness_probe(neutral, "sRGB"), res.lut, "sRGB")
    check("neutral source stays blotch-free (neutral spread < 2.5)", m["nspread"] < 2.5,
          f"({m['nspread']:.2f}, chose {res.method})")

    # 4) single-colour (green screen) source: finite, bounded, no explosion
    flat = np.tile(np.array([0.1, 0.8, 0.2]), (240, 360, 1)) + rng.normal(0, 0.003, (240, 360, 3))
    res = match(np.clip(flat, 0, 1), wide, corresponded=False, size=33)
    check("green-screen source: LUT finite and in range",
          np.isfinite(res.lut).all() and res.lut.min() >= 0 and res.lut.max() <= 1)
    check("green-screen source: LUT slope bounded", lut_steepness(res.lut) <= 6.0 + 1e-6,
          f"({lut_steepness(res.lut):.1f})")

    # 5) log-to-log identity: matching a clip to itself in S-Log3 is a no-op
    logc = np.clip(wide * 0.6 + 0.1, 0, 1)
    res = match(logc, logc, corresponded=True, tf="SLog3", size=33)
    d = float(np.abs(apply_lut(logc, res.lut) - logc).max())
    check("S-Log3 self-match is identity", d < 0.02, f"(max|d|={d:.4f}, chose {res.method})")

    # 6) 'Same scene' clicked for two different shots -> distribution fallback
    res = match(_scene(rng), _scene(np.random.default_rng(99)), corresponded=True, size=33)
    check("mis-clicked 'Same scene' falls back to distribution matching",
          res.corresponded is False, f"(chose {res.method})")




def test_slot_retention():
    """A saved project points at slot numbers and the effect renders identity when
    the file is gone - so garbage collection may only remove what no project can
    still reference."""
    print("\n=== 7. Effect slot retention ===")
    import json, os, tempfile, time
    from colourmatik import webapp as W
    d = Path(tempfile.mkdtemp())
    old_dir, old_idx, old_budget = W._SLOT_DIR, W._SLOT_INDEX, W._SLOT_BUDGET
    W._SLOT_DIR, W._SLOT_INDEX = d, d / "slots.jsonl"
    try:
        now = time.time()
        day = 86400.0
        def mk(n, age_days, rid=None, draft=False, src="", t_off=0.0, size=1000):
            f = d / f"slot_{n}.cube"
            f.write_bytes(b"x" * size)
            t = now - age_days * day + t_off
            os.utime(f, (t, t))
            if rid is not None:
                with open(W._SLOT_INDEX, "a") as fh:
                    fh.write(json.dumps({"slot": n, "rid": rid, "draft": draft, "src": src, "t": t}) + "\n")
        # match A on clip X: result 1, slider steps 2..5 (all 30 days old)
        for i, n in enumerate([1, 2, 3, 4, 5]):
            mk(n, 30, rid="A", src="/clip/X.mp4", t_off=i)
        mk(6, 30, rid="DX", draft=True, src="/clip/X.mp4", t_off=-10)   # draft replaced by A
        mk(7, 30, rid="DY", draft=True, src="/clip/Y.mp4")              # draft, refine crashed
        mk(8, 30)                                                       # pre-index legacy slot
        mk(9, 2, rid="B", src="/clip/Z.mp4")                            # recent match B...
        mk(10, 2, rid="B", src="/clip/Z.mp4", t_off=1)
        mk(11, 2, rid="B", src="/clip/Z.mp4", t_off=2)
        mk(12, 2, rid="B", src="/clip/Z.mp4", t_off=3)                  # ...10 is superseded but young
        st = W._gc_slots(now=now)
        left = sorted(int(p.stem.split("_")[1]) for p in d.glob("slot_*.cube"))
        check("keeps each match's result and its last two states", {1, 4, 5} <= set(left), f"(left {left})")
        check("drops old intermediate slider steps", 2 not in left and 3 not in left, f"(left {left})")
        check("drops a draft once its final exists", 6 not in left, f"(left {left})")
        check("keeps a draft whose refine never finished", 7 in left, f"(left {left})")
        check("keeps pre-index (legacy) slots", 8 in left, f"(left {left})")
        check("keeps young superseded slots (undo window)", {9, 10, 11, 12} <= set(left), f"(left {left})")
        # disk budget: only slots older than the keep window may go
        W._SLOT_BUDGET = 2500
        mk(20, 400, size=3000)
        W._gc_slots(now=now)
        left = sorted(int(p.stem.split("_")[1]) for p in d.glob("slot_*.cube"))
        check("over budget: a >180-day-old slot is removed", 20 not in left, f"(left {left})")
        check("over budget: younger live slots are never removed", {1, 4, 5, 7, 8} <= set(left), f"(left {left})")
        empty = Path(tempfile.mkdtemp())
        W._SLOT_DIR, W._SLOT_INDEX = empty, empty / "slots.jsonl"
        check("empty slot folder is fine", W._gc_slots(now=now)["files"] == 0)
    finally:
        W._SLOT_DIR, W._SLOT_INDEX, W._SLOT_BUDGET = old_dir, old_idx, old_budget


def test_frame_cap_and_solver():
    """4K/6K frames are area-downscaled to 1920 px for matching (memory), HD is
    untouched bit for bit; the lattice solver works on any SciPy version."""
    print("\n=== 8. Frame size cap + solver fallback ===")
    import subprocess, tempfile
    from colourmatik import io as cmio
    d = Path(tempfile.mkdtemp())
    ff = cmio._ffmpeg_exe()
    for name, size in (("uhd.mp4", "3840x2160"), ("hd.mp4", "1920x1080"), ("vert.mp4", "2160x3840")):
        subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"testsrc2=size={size}:rate=25:duration=1", "-pix_fmt", "yuv420p",
                        str(d / name)], check=True)
    a = cmio.extract_frame(d / "uhd.mp4", 0.5)
    check("UHD frame capped to 1920x1080", a.shape == (1080, 1920, 3), f"(got {a.shape})")
    v = cmio.extract_frame(d / "vert.mp4", 0.5)
    check("vertical UHD capped to 1080x1920", v.shape == (1920, 1080, 3), f"(got {v.shape})")
    h1 = cmio.extract_frame(d / "hd.mp4", 0.5)
    h2 = cmio.extract_frame(d / "hd.mp4", 0.5, max_side=None)
    check("HD frame untouched (bit-identical)", h1.shape == (1080, 1920, 3) and float(np.abs(h1 - h2).max()) == 0.0)
    full = cmio.extract_frame(d / "uhd.mp4", 0.5, max_side=None)
    diff = abs(float(full.mean()) - float(a.mean()))
    check("area downscale keeps the picture's average", diff < 2e-3, f"(|mean diff| {diff:.5f})")
    pooled = cmio.load_any(d / "uhd.mp4", frames=7)
    check("pooled UHD stack is 7 capped frames", pooled.shape[1] == 1920 and pooled.shape[0] % 1080 == 0,
          f"(got {pooled.shape})")
    # SciPy < 1.12 has no cg(rtol=...): the lattice fit must still work
    import scipy.sparse.linalg as spla
    real_cg = spla.cg
    def old_cg(A, b, x0=None, *, tol=1e-5, maxiter=None, M=None, **kw):
        if "rtol" in kw:
            raise TypeError("cg() got an unexpected keyword argument 'rtol'")
        return real_cg(A, b, x0=x0, rtol=tol, maxiter=maxiter, M=M)
    rng = np.random.default_rng(1)
    src = rng.random((20000, 3)); tgt = np.clip(src * 0.9 + 0.05, 0, 1)
    want = tf_mod.fit_lut_lattice(src, tgt, L=17)
    spla.cg = old_cg
    try:
        got = tf_mod.fit_lut_lattice(src, tgt, L=17)
    finally:
        spla.cg = real_cg
    check("lattice fit works with the pre-1.12 SciPy API", float(np.abs(got - want).max()) < 1e-6)


def test_update_check():
    """The engine reads releases.catheadai.com/colourmatik/latest.json (our R2
    server, not GitHub) at most once a day (a click: at most every 10 minutes),
    failures included; it takes this platform's entry - only a file on that
    server - and names itself honestly in the User-Agent."""
    print("\n=== 9. Update check: R2 latest.json, once a day, honest client ===")
    import json, tempfile, time, urllib.request, urllib.error
    from colourmatik import webapp as W
    d = Path(tempfile.mkdtemp())
    saved = (W._UPDATE_CACHE, W._fetch_latest, W._platform_key, W._SLOT_DIR)
    R2 = "https://releases.catheadai.com/colourmatik/"
    doc = {"version": "1.9.0",
           "mac": {"url": R2 + "colourMatik-mac.zip", "size": 5, "sha256": "AB", "version": "1.8.9"},
           "windows": {"url": R2 + "colourMatik-windows-setup.exe", "size": 7, "sha256": "CD", "version": "1.9.0"},
           "effect_windows": {"url": R2 + "colourMatik-effect-windows.zip", "size": 1, "sha256": "EF", "version": "1.8.1"}}
    calls = []
    mode = {"reply": "ok"}

    def fake_fetch(etag=""):
        calls.append(etag)
        if mode["reply"] == "fail":
            raise OSError("offline")
        if mode["reply"] == "304":
            return 304, None, etag
        return 200, doc, '"etag-1"'
    W._UPDATE_CACHE, W._fetch_latest, W._SLOT_DIR = d / "update_check.json", fake_fetch, d
    W._platform_key = lambda: "windows"
    try:
        def age(seconds):                   # pretend the last check was this long ago
            c = json.loads(W._UPDATE_CACHE.read_text())
            c["checked_at"] = time.time() - seconds
            W._UPDATE_CACHE.write_text(json.dumps(c))
        # what a 1.8.2 engine left behind (GitHub releases): never reused
        W._UPDATE_CACHE.write_text(json.dumps({"prefix": "win-v", "checked_at": time.time(),
                                               "latest": {"version": "1.8.1"}}))
        r = W.update_check()
        check("first check reads latest.json once (a 1.8.2 GitHub cache is not reused)",
              len(calls) == 1 and r["cached"] is False)
        check("takes this platform's entry: version, URL, size, SHA-256 (Windows 1.9.0, not the Mac's 1.8.9)",
              (r["version"], r["url"], r["size"], r["sha256"]) == ("1.9.0", R2 + "colourMatik-windows-setup.exe", 7, "cd"),
              f"(got {r})")
        W.update_check(); W.update_check(); W.update_check(force=1)
        check("panel opens and a click within 10 minutes use the cache", len(calls) == 1)
        age(11 * 60)
        W.update_check()
        check("an automatic check still waits for the day", len(calls) == 1)
        W.update_check(force=1)
        check("a click after 10 minutes refreshes, conditionally (If-None-Match)",
              len(calls) == 2 and calls[-1] == '"etag-1"')
        age(25 * 3600); mode["reply"] = "304"
        r = W.update_check()
        check("after a day the automatic check refreshes; 304 keeps the known release",
              len(calls) == 3 and r["version"] == "1.9.0" and r["cached"] is False)
        age(25 * 3600); mode["reply"] = "fail"
        r = W.update_check(); W.update_check(); W.update_check()
        check("a failed check keeps the last known release", r["version"] == "1.9.0" and r["error"])
        check("a failed check is not retried on every panel open", len(calls) == 4)
        W._UPDATE_CACHE.unlink()
        r = W.update_check()
        check("offline with nothing known -> ok:false (panel says 'Check failed')",
              r["ok"] is False and r["version"] is None and len(calls) == 5)
        W._platform_key = lambda: "mac"; mode["reply"] = "ok"
        r = W.update_check()
        check("a Mac engine reads the mac entry (a Windows cache is not reused)",
              r["version"] == "1.8.9" and len(calls) == 6)
        # Only a file on our own server counts, and a broken entry is no update.
        bad = [{"windows": {**doc["windows"], "url": "https://github.com/x/colourMatik-windows-setup.exe"}},
               {"windows": {**doc["windows"], "version": "1.9"}}, {"windows": "1.9.0"}, [], None]
        got = [W._release_for(b, "windows") for b in bad]
        check("an entry off releases.catheadai.com, a bad version or a malformed file is ignored",
              got == [None] * len(bad), f"({got})")
        top = W._release_for({"version": "2.0.1", "windows": {"url": R2 + "a.exe"}}, "windows")
        check("an entry without its own version takes the top-level one", top and top["version"] == "2.0.1")
    finally:
        W._UPDATE_CACHE, W._fetch_latest, W._platform_key, W._SLOT_DIR = saved

    # The real request: our server, honest User-Agent, conditional.
    seen = {}

    class Resp:
        status = 200
        headers = {"ETag": '"e2"'}
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None, context=None):
        seen["url"], seen["headers"] = req.full_url, {k.lower(): v for k, v in req.header_items()}
        if seen["headers"].get("if-none-match"):
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)
        return Resp()
    real = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        st, body, etag = W._fetch_latest()
        ua = seen["headers"].get("user-agent", "")
        check("reads releases.catheadai.com/colourmatik/latest.json (not GitHub)", seen["url"] == R2 + "latest.json")
        check("User-Agent names colourMatik, not a browser",
              ua.startswith("colourMatik-engine/") and "mozilla" not in ua.lower(), f"(UA {ua!r})")
        st2, body2, etag2 = W._fetch_latest('"e2"')
        check("304 Not Modified is an answer, not an error", (st, etag, st2, body2, etag2) == (200, '"e2"', 304, None, '"e2"'))
    finally:
        urllib.request.urlopen = real


def test_github_policy():
    """Download rules, enforced on the shipped files: installing never
    downloads from GitHub (no source archives, no raw files, no git); updates
    come from our own server (releases.catheadai.com, R2) - latest.json, then
    the one installer it names, checked against its size and SHA-256 - with an
    honest User-Agent and never touch GitHub; the panels leave all of it to the
    engine; and CI has no scheduled or push-triggered builds."""
    print("\n=== 10. Download policy: R2, never GitHub at run time ===")
    import re
    root = Path(__file__).resolve().parents[1]
    shipped = ["windows/install-windows.ps1", "windows/install-windows.cmd", "windows/fetch-latest.ps1",
               "windows/update-windows.cmd", "windows/install-effect.ps1", "windows/install-panel.ps1",
               "windows/setup.ps1", "windows/diag.ps1", "windows/setup/colourMatik.nsi",
               "windows/setup/build-setup.ps1", "mac/app/install-mac.sh", "mac/app/build-app.sh",
               "update.command", "install-panel.sh", "install-effect.sh",
               "colourmatik-uxp/main.js", "colourmatik-cep/client/main.js", "colourmatik/webapp.py"]
    forbidden = {"a source archive": r"archive/refs/|codeload\.github\.com|/zipball/|/tarball/",
                 "a raw file": r"raw\.githubusercontent\.com",
                 "a browser download link": r"releases/download/|releases/latest/download|browser_download_url"}
    for rel_path in shipped:
        text = (root / rel_path).read_text(encoding="utf-8", errors="replace")
        for what, pat in forbidden.items():
            hits = [ln.strip() for ln in text.splitlines() if re.search(pat, ln)]
            check(f"{rel_path}: never downloads {what}", not hits, f"({hits[:2]})")
    for rel_path in ["windows/install-windows.ps1", "windows/fetch-latest.ps1", "windows/update-windows.cmd",
                     "windows/setup/colourMatik.nsi", "mac/app/install-mac.sh", "update.command"]:
        text = (root / rel_path).read_text(encoding="utf-8", errors="replace")
        hits = [ln.strip() for ln in text.splitlines()
                if re.search(r"\bgit\s+(-C\s+\S+\s+)?(pull|clone|fetch)\b", ln) and not ln.lstrip().startswith(("#", "rem ", ";"))]
        check(f"{rel_path}: no git pull/clone/fetch", not hits, f"({hits[:2]})")
    for rel_path, request, own_ua, sha in [
            ("windows/fetch-latest.ps1", r"Invoke-(WebRequest|RestMethod)\b", r"-UserAgent \$ua\b", "Get-FileHash $SetupExe -Algorithm SHA256"),
            ("update.command", r"\bcurl\b", r'-A "\$UA"', "shasum -a 256")]:
        text = (root / rel_path).read_text(encoding="utf-8", errors="replace")
        code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        requests = [ln.strip() for ln in code if re.search(request, ln)]
        check(f"{rel_path}: updates from releases.catheadai.com/colourmatik/latest.json, checking size and SHA-256",
              "https://releases.catheadai.com/colourmatik/latest.json" in text and sha in text)
        check(f"{rel_path}: never contacts GitHub", not [ln for ln in code if "github" in ln.lower()])
        check(f"{rel_path}: every request sends its own User-Agent, never a browser's",
              requests and all(re.search(own_ua, ln) for ln in requests) and "colourMatik-updater/" in text
              and not any("mozilla" in ln.lower() for ln in code), f"({len(requests)} requests)")
    engine = [ln for ln in (root / "colourmatik" / "webapp.py").read_text(encoding="utf-8").splitlines()
              if not ln.lstrip().startswith("#")]
    check("engine: its update check reads releases.catheadai.com, never GitHub",
          any("https://releases.catheadai.com/colourmatik/latest.json" in ln for ln in engine)
          and not [ln for ln in engine if "github.com" in ln])
    for js in ["colourmatik-uxp/main.js", "colourmatik-cep/client/main.js"]:
        text = (root / js).read_text(encoding="utf-8")
        urls = set(re.findall(r"https?://[A-Za-z0-9.\-]+", text)) - {"http://127.0.0.1", "https://catheadai.com"}
        check(f"{js}: talks only to the local engine (and opens the site)", not urls, f"({sorted(urls)})")
    wf_dir = root / ".github" / "workflows"
    for wf in sorted(wf_dir.glob("*.y*ml")):
        text = wf.read_text(encoding="utf-8")
        check(f"{wf.name}: no scheduled (cron) runs", not re.search(r"^\s*(schedule|- cron)\s*:", text, re.M))
        on = re.search(r"^on:\s*\n((?:[ \t]+.*\n|\s*\n)*)", text, re.M)
        triggers = re.findall(r"^  (\w[\w-]*):", on.group(1), re.M) if on else []
        push = re.search(r"^  push:\s*\n((?:    .*\n)*)", on.group(1), re.M) if on else None
        push_ok = push is None or set(re.findall(r"^    (\w[\w-]*):", push.group(1), re.M)) <= {"tags"}
        check(f"{wf.name}: builds only by hand or on a tag",
              on is not None and set(triggers) <= {"workflow_dispatch", "push"} and push_ok, f"(triggers {triggers})")


def test_windows_update_log():
    """The Windows updater logs every step - the PowerShell ones too - to
    update.log with no window, in the engine-started and the self-elevated
    run; the engine it restarts inherits none of its variables; a declined
    admin prompt reaches the panel at once as FAIL|reason and "retry" starts a
    new update; and an older updater still running update-windows.cmd IN PLACE
    (its engine had inherited CMK_RELAUNCHED=1) resumes inside the landing
    strip, not mid-line."""
    print("\n=== 11. Windows updater: every step logs; a declined prompt fails fast; old updaters land safely ===")
    import os, re, shutil, subprocess, tempfile, time, uuid
    root = Path(__file__).resolve().parents[1]
    text = (root / "windows" / "update-windows.cmd").read_bytes().replace(b"\r\n", b"\n")
    # Where an in-place 1.8.1 / 1.8.2 updater resumes reading once its download
    # has replaced the file. 1.8.2 installs (CRLF from the Setup, LF from the
    # 1.8.1 main.zip migration) get the new file from the Setup: CRLF. 1.8.1
    # installs (LF or CRLF; zip or git path) get it from main.zip: LF.
    resume = {b"\r\n": [1919, 1957, 1958], b"\n": [1916, 2033, 2076]}
    fetch = b'powershell -NoProfile -ExecutionPolicy Bypass -File "%CMK_HOME%fetch-latest.ps1" -Dest "%CD%"'
    form = {b"\r\n": "CRLF", b"\n": "LF"}
    for eol, offsets in resume.items():
        data = text.replace(b"\n", eol)
        start = data.index(fetch) + len(fetch) + len(eol)
        end = start
        while re.match(rb":+" + re.escape(eol), data[end:]):
            end = data.index(eol, end) + len(eol)
        check(f"update-windows.cmd ({form[eol]}): every old in-place resume point is in the landing strip",
              all(start <= o < end for o in offsets), f"(strip [{start}, {end}), resume points {offsets})")
    check("the elevated copy is told /elevated and never asks for elevation again",
          b"'/silent','/elevated' -Verb RunAs" in text and b'if errorlevel 1 if not "%~2"=="/elevated" (' in text)
    check("a failed elevation request (a declined admin prompt) goes to :no_admin, never on to the update",
          b"  if errorlevel 1 goto :no_admin\n  exit /b\n)\n" in text and b"\n:no_admin\n" in text)
    if os.name != "nt":
        print("  (the rest runs the real update-windows.cmd: Windows only)")
        return

    # Resuming at each of those points runs straight into the download check,
    # the download's errorlevel intact.
    tmp = Path(tempfile.mkdtemp(prefix="cmk-upd-test-"))
    try:
        (tmp / "wrap.cmd").write_bytes(b'@echo off\r\ncmd /c exit 7\r\ncall "%~dp0rest.cmd"\r\n')
        for eol, offsets in resume.items():
            data = text.replace(b"\n", eol)
            stop = data.index(b"if errorlevel 1 (" + eol, data.index(fetch))
            outs = set()
            for o in offsets:
                (tmp / "rest.cmd").write_bytes(data[o:stop] + b"echo LANDED %errorlevel%" + eol)
                outs.add(subprocess.run(["cmd", "/c", str(tmp / "wrap.cmd")], capture_output=True,
                                        text=True).stdout.strip())
            check(f"{form[eol]}: an old updater resuming there reaches the download check, errorlevel intact",
                  outs == {"LANDED 7"}, f"({sorted(outs)})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # The real script in a sandbox install: stub steps that print to stdout and
    # stderr and fail, a stub engine restart that records the CMK* variables it
    # inherited. Never stops a real engine, never asks for admin (no UAC prompt
    # in a test run), never writes the real %APPDATA%.
    from colourmatik import webapp as W
    sb = Path(tempfile.mkdtemp(prefix="cmk-upd-test-"))
    win, data_dir = sb / "app" / "windows", sb / "appdata" / "colourMatik"
    win.mkdir(parents=True); data_dir.mkdir(parents=True); (sb / "temp").mkdir()
    script, kill, admin = text.replace(b"\n", b"\r\n"), b"'colourmatik.webapp'", b"net session >nul 2>&1"
    sandboxable = script.count(kill) == 1 and script.count(admin) == 1
    check("the test can sandbox update-windows.cmd (its engine kill and admin check are where it expects)", sandboxable)
    if not sandboxable:             # never run it against the real engine, never prompt for admin
        shutil.rmtree(sb, ignore_errors=True)
        return
    script = script.replace(kill, b"'" + uuid.uuid4().hex[:18].encode() + b"'")
    (win / "update-windows.cmd").write_bytes(script.replace(admin, b"ver >nul"))
    (win / "fetch-latest.ps1").write_text(
        'param([string]$Dest)\nAdd-Type -Namespace W -Name K -MemberDefinition \'[DllImport("kernel32.dll")] public static extern '
        'System.IntPtr GetConsoleWindow(); [DllImport("user32.dll")] public static extern bool IsWindowVisible(System.IntPtr h);\'\n'
        '$w = [W.K]::GetConsoleWindow()\n"visible=" + ($w -ne [IntPtr]::Zero -and [W.K]::IsWindowVisible($w)) | '
        'Out-File -Append -Encoding ascii (Join-Path $PSScriptRoot "..\\..\\window.txt")\n'
        'Write-Host "==> Looking up the newest colourMatik release..."\n'
        '[Console]::Error.WriteLine("stub fetch: stderr line")\n', encoding="utf-8")
    (win / "setup.ps1").write_text('cmd /c echo stub setup: native child output\nWrite-Error "stub setup: an error"\n', encoding="utf-8")
    (win / "install-panel.ps1").write_text('Write-Output "stub panel: installed"\n', encoding="utf-8")
    (win / "install-effect.ps1").write_text('$ErrorActionPreference = "Stop"\nthrow "stub effect: access denied"\n', encoding="utf-8")
    (win / "engine-hidden.vbs").write_text(
        'Set sh = CreateObject("WScript.Shell")\nSet fso = CreateObject("Scripting.FileSystemObject")\ns = ""\n'
        'For Each e In sh.Environment("Process")\n  If UCase(Left(e, 3)) = "CMK" Then s = s & e & ";"\nNext\n'
        'Set f = fso.OpenTextFile(fso.GetParentFolderName(WScript.ScriptFullName) & "\\..\\..\\restart.txt", 8, True)\n'
        'f.WriteLine "[" & s & "]"\nf.Close\n', encoding="utf-8")
    prog, log = data_dir / "update_progress", data_dir / "update.log"

    def finished(timeout=120, ends=("100|", "FAIL|")):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if prog.read_text().startswith(ends):
                    time.sleep(1.0)
                    return True
            except OSError:
                pass
            time.sleep(0.25)
        return False

    saved_env, saved_popen, saved_dir = dict(os.environ), subprocess.Popen, W._SLOT_DIR
    launched, starts = {}, []

    def popen(args, **kw):          # the engine's own launch, pointed at the sandbox
        launched.update(kw)
        starts.append(args)
        return saved_popen([args[0], args[1], str(win / "update-windows.cmd"), *args[3:]], **{**kw, "cwd": str(sb / "app")})
    try:
        for k in [k for k in os.environ if k.upper().startswith("CMK")]:
            del os.environ[k]
        os.environ.update({"APPDATA": str(sb / "appdata"), "TEMP": str(sb / "temp"), "TMP": str(sb / "temp")})
        subprocess.Popen, W._SLOT_DIR = popen, data_dir
        W.__dict__.pop("_UPDATE_STARTED_AT", None)
        r = W.update_now()
        subprocess.Popen = saved_popen
        flags = launched.get("creationflags", 0)
        check("engine: the updater gets a console with no window, not DETACHED_PROCESS",
              r.get("started") and flags & 0x08000000 and not flags & 0x00000008, f"(flags {flags:#x})")
        done = finished()
        out = log.read_text(errors="replace") if log.exists() else ""
        check("engine-started update: PowerShell steps' output and errors reach update.log",
              done and all(s in out for s in ["==> Updating colourMatik...", "==> Looking up the newest colourMatik release...",
                                              "stub fetch: stderr line", "stub setup: native child output", "stub setup: an error",
                                              "stub panel: installed", "stub effect: access denied", "==> Updated."]),
              f"(log: {out[-300:]!r})")
        restarts = (sb / "restart.txt").read_text().split() if (sb / "restart.txt").exists() else []
        windows = (sb / "window.txt").read_text().split() if (sb / "window.txt").exists() else []
        check("engine-started update: no visible window; the restarted engine inherits no CMK* variable",
              windows == ["visible=False"] and restarts == ["[]"], f"({windows}, {restarts})")

        # The self-elevated copy - started the way the elevation request starts
        # it, minus the prompt - has to write update.log itself, and waits while
        # another process (the window that asked for admin) still holds it.
        prog.write_text("2|Starting the update")
        hold = open(log, "ab")
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Start-Process -FilePath '%s' -ArgumentList '/silent','/elevated' -WindowStyle Hidden"
                        % (win / "update-windows.cmd")], check=True)
        time.sleep(2.0)
        held = log.read_text(errors="replace")
        hold.close()
        done = finished()
        out = log.read_text(errors="replace")[len(held):]
        check("self-elevated update: waits for update.log, then logs every step there",
              done and "==> Looking up the newest colourMatik release..." in out and "stub effect: access denied" in out
              and "==> Updated." in out, f"(appended: {out[-300:]!r})")
        restarts = (sb / "restart.txt").read_text().split() if (sb / "restart.txt").exists() else []
        windows = (sb / "window.txt").read_text().split() if (sb / "window.txt").exists() else []
        check("self-elevated update: hidden window; the restarted engine inherits no CMK* variable",
              windows == ["visible=False"] * 2 and restarts == ["[]"] * 2, f"({windows}, {restarts})")

        # The admin prompt, from the engine's launch on. The admin check fails
        # and the real Start-Process gets another verb instead of RunAs, so a
        # test run never shows a prompt: "open" succeeds like an approved
        # prompt (its copy is not elevated; the stub steps don't mind), and an
        # unknown verb fails just as a declined prompt does (same error, exit
        # code 1). Declined, nothing may run and the panel must hear it at once
        # (FAIL|reason) - its bar used to wait 15 minutes, then say "timed
        # out". "retry" must then start a new update rather than get the old
        # FAIL back, while a second panel still joins an update that is running.
        runas = b"-Verb RunAs -WindowStyle Hidden"
        check("the test can stand in for the admin prompt (its RunAs is where it expects)", script.count(runas) == 1)
        if script.count(runas) == 1:
            def click_update():             # the panel's Update button, launched into the sandbox
                subprocess.Popen = popen
                try:
                    r = W.update_now()
                finally:
                    subprocess.Popen = saved_popen
                return r if isinstance(r, dict) else {"error": bytes(r.body).decode(errors="replace")}

            def through_prompt(verb, ends):
                (win / "update-windows.cmd").write_bytes(
                    script.replace(admin, b"cmd /c exit 1").replace(runas, b"-Verb " + verb + b" -WindowStyle Hidden"))
                held = log.read_text(errors="replace")
                W.__dict__.pop("_UPDATE_STARTED_AT", None)      # a fresh engine, as after an update
                r, t0 = click_update(), time.time()
                done = finished(ends=ends)
                return r, done, time.time() - t0, log.read_text(errors="replace")[len(held):]

            r, done, took, out = through_prompt(b"open", ("100|",))
            check("approved admin prompt: no FAIL; the copy it starts runs the update to the end",
                  r.get("started") and done and "admin approval was not given" not in out and "==> Updated." in out,
                  f"({r}, log: {out[-200:]!r})")
            steps = lambda: [(sb / f).read_bytes() if (sb / f).exists() else b"" for f in ("window.txt", "restart.txt")]
            ran = steps()
            r, done, took, out = through_prompt(b"cmkDeclined", ("FAIL|",))
            p = W.update_progress()
            check("declined admin prompt: the panel gets FAIL with the reason at once; no step ran",
                  r.get("started") and done and p.get("failed") and "admin approval was not given" in p.get("msg", "")
                  and steps() == ran, f"({p}, {took:.1f} s)")
            check("declined admin prompt: update.log has PowerShell's error and the reason",
                  "Start-Process" in out and "==> Windows admin approval was not given" in out, f"(log: {out[-300:]!r})")
            n = len(starts)
            retry = click_update()
            again = finished(ends=("FAIL|",))
            prog.write_text("25|Refreshing the engine")          # as if that new update were running now
            joined = click_update()
            check("after a FAIL, retry starts a new update; while one runs, a second panel joins it",
                  retry.get("started") and not retry.get("already") and again and joined.get("already")
                  and len(starts) == n + 1, f"({len(starts) - n} new updater(s); retry {retry}, then {joined})")
            if len(starts) > n + 1:
                finished()                                        # one started after all: let it end first
    finally:
        subprocess.Popen, W._SLOT_DIR = saved_popen, saved_dir
        W.__dict__.pop("_UPDATE_STARTED_AT", None)
        os.environ.clear(); os.environ.update(saved_env)
        shutil.rmtree(sb, ignore_errors=True)


if __name__ == "__main__":
    test_accuracy()
    test_distribution()
    test_skin()
    test_lut_correctness()
    test_identity()
    test_decode_plan()
    test_robustness()
    test_slot_retention()
    test_frame_cap_and_solver()
    test_update_check()
    test_github_policy()
    test_windows_update_log()
    print("\n" + ("=" * 48))
    if FAILS:
        print(f"FAILED: {len(FAILS)} -> {FAILS}")
        sys.exit(1)
    print("ALL TESTS PASSED")
