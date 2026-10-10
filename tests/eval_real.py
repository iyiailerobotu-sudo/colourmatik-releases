"""Real-footage evaluation: SEE what the engine ships, don't guess.

Synthetic tests prove the maths; the "sometimes the colours are wrecked" class
of bug only shows on real clips (an orange lamp turned green by a reference
full of green screens, a flat sky stretched into a banded blob, green blotches
on a sweater from a green-screen prop). This renders, for every (source,
reference) pair, one sheet: REFERENCE | SOURCE | SHIPPED (the engine's actual
choice) | DRAFT (quick mode) | every candidate, each with the judge's numbers
(distribution distance, steepness, clipping, hue twist and tone change against
the look transfer, neutral spread, local
contrast ratio) so a bad decision can be spotted by eye and by number.

usage:
  python -m tests.eval_real clips.txt pairs.txt out_dir
    clips.txt : one clip path per line (index = line number, from 0)
    pairs.txt : "src_idx ref_idx [src_t|-] [ref_t|-] [same|different]" per line
"""
from __future__ import annotations
import sys
import time
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw

from colourmatik import io as cmio, colorspace as cs, transforms as tf_mod
from colourmatik.match import match, _naturalness_probe, _naturalness
from colourmatik.lut import build_lut, apply_lut, resample_lut, gamut_guard, steep_guard, lut_steepness
from colourmatik.metrics import sliced_wasserstein

TF = "sRGB"


def _small(a, w=480):
    im = Image.fromarray((np.clip(a, 0, 1) * 255 + 0.5).astype("uint8"))
    return im.resize((w, int(im.height * w / im.width)), Image.BILINEAR)


def _sw(out, ref, seed=0):
    r = np.random.default_rng(seed)
    o = out.reshape(-1, 3); t = ref.reshape(-1, 3)
    oi = r.choice(o.shape[0], min(60000, o.shape[0]), replace=False)
    ti = r.choice(t.shape[0], min(60000, t.shape[0]), replace=False)
    return sliced_wasserstein(cs.encoded_to_oklab(o[oi], TF) * 100,
                              cs.encoded_to_oklab(t[ti], TF) * 100, seed=seed)


def _candidates(src, ref, seed=0, sample=300_000):
    rng = np.random.default_rng(seed)
    S_enc = src.reshape(-1, 3); T_enc = ref.reshape(-1, 3)
    S_lin = cs.decode(S_enc, TF); T_lin = cs.decode(T_enc, TF)
    idx = rng.choice(S_enc.shape[0], sample, replace=False) if S_enc.shape[0] > sample else np.arange(S_enc.shape[0])
    tidx = rng.choice(T_enc.shape[0], sample, replace=False) if T_enc.shape[0] > sample else np.arange(T_enc.shape[0])
    Sf_lin, Sf_enc, Tf_lin = S_lin[idx], S_enc[idx], T_lin[tidx]
    luts = {"mkl": build_lut(tf_mod.fit_mkl(Sf_lin, Tf_lin), size=65, tf=TF),
            "sep": build_lut(tf_mod.fit_sep(Sf_lin, Tf_lin), size=65, tf=TF),
            "grade": build_lut(tf_mod.fit_look(Sf_lin, Tf_lin), size=65, tf=TF)}
    cap = 150_000
    sel = rng.choice(Sf_lin.shape[0], cap, replace=False) if Sf_lin.shape[0] > cap else np.arange(Sf_lin.shape[0])
    tsel = rng.choice(Tf_lin.shape[0], cap, replace=False) if Tf_lin.shape[0] > cap else np.arange(Tf_lin.shape[0])
    tr = np.clip(tf_mod.fit_idt(Sf_lin[sel], Tf_lin[tsel], seed=seed), 0, None)
    luts["idt"] = resample_lut(tf_mod.fit_lut_lattice(Sf_enc[sel], cs.encode(tr, TF), L=25), 65)
    safe = luts["grade"]
    return {n: (steep_guard(gamut_guard(l, Sf_enc, safe), safe) if n != "grade" else l)
            for n, l in luts.items()}


def _panel(img, title, lines, w=480):
    im = _small(img, w)
    canvas = Image.new("RGB", (w, im.height + 14 + 12 * len(lines)), (18, 18, 18))
    canvas.paste(im, (0, 14))
    d = ImageDraw.Draw(canvas)
    d.text((4, 1), title, fill=(255, 230, 0))
    for k, l in enumerate(lines):
        d.text((4, im.height + 15 + 12 * k), l, fill=(200, 200, 200))
    return canvas


def run_pair(clips, si, ri, st, rt, mode, out_dir):
    src = cmio.load_any(clips[si], t=st, frames=1)
    ref = cmio.load_any(clips[ri], t=rt, frames=1)
    name = f"{si:02d}_to_{ri:02d}_{mode}"
    print(f"\n=== {name}: {Path(clips[si]).name[:40]} -> {Path(clips[ri]).name[:40]}")
    t0 = time.time(); res = match(src, ref, corresponded=(mode == "same"), tf=TF); t_full = time.time() - t0
    t0 = time.time(); resq = match(src, ref, corresponded=(mode == "same"), tf=TF, quick=True); t_quick = time.time() - t0
    probe = _naturalness_probe(src, TF, res.alts.get("grade"))

    def describe(lut, extra):
        out = apply_lut(src, lut)
        m = _naturalness(probe, lut, TF)
        return out, [f"sw {_sw(out, ref):.2f} steep {lut_steepness(lut):.1f} clip+{m['clip_inc']*100:.1f}% twist {m['twist']:.0f} tone {m.get('tone', 0.0):.1f} nspread {m['nspread']:.1f}",
                     f"detail {m['detail']:.2f} smooth {m['smooth']:.2f} {extra}"]
    panels = [_panel(ref, "REFERENCE", [""]), _panel(src, "SOURCE", [f"src->ref sw {_sw(src, ref):.2f}"])]
    rows = [("SHIPPED", res.lut, f"{res.method} {t_full:.1f}s"), ("DRAFT (quick)", resq.lut, f"{resq.method} {t_quick:.1f}s")]
    if mode != "same":
        for n, l in _candidates(src, ref).items():
            rows.append((n, l, f"score {res.scores.get(n, float('nan')):.2f}"))
    for title, lut, extra in rows:
        out, lines = describe(lut, extra)
        panels.append(_panel(out, title, lines))
        print(f"  {title:14s} " + " | ".join(lines))
    for n in res.notes:
        print("  note:", n)
    cols, W = 4, 480
    H = max(p.height for p in panels)
    sheet = Image.new("RGB", (cols * (W + 4), ((len(panels) + cols - 1) // cols) * (H + 4)), (0, 0, 0))
    for k, p in enumerate(panels):
        sheet.paste(p, ((k % cols) * (W + 4), (k // cols) * (H + 4)))
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet.save(out_dir / f"{name}.jpg", quality=86)


if __name__ == "__main__":
    clips_file, pairs_file, out_dir = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    clips = [l.strip() for l in open(clips_file) if l.strip() and not l.startswith("#")]
    for line in open(pairs_file):
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            continue
        si, ri = int(parts[0]), int(parts[1])
        st = float(parts[2]) if len(parts) > 2 and parts[2] != "-" else None
        rt = float(parts[3]) if len(parts) > 3 and parts[3] != "-" else None
        mode = parts[4] if len(parts) > 4 else "different"
        try:
            run_pair(clips, si, ri, st, rt, mode, out_dir)
        except Exception:
            import traceback; traceback.print_exc()
