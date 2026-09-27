"""colourMatik local web app — drag two clips, match colours, download the .cube.

Run:  ./.venv/bin/python -m colourmatik.webapp      (then open http://localhost:8765)
Everything stays on your machine; nothing is uploaded anywhere.
"""
from __future__ import annotations
import base64
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import base64 as _b64
import threading
import traceback
from . import __version__
import numpy as np
from . import io as cmio
from . import colorspace as cs
from .match import match, format_report
from .lut import (write_cube, apply_lut, apply_lut_points, apply_intensity, resample_lut,
                  fit_wb_tone, decompose_residual, recompose_lut)
from .viz import make_comparison
from .metrics import image_delta_e00, summarize

app = FastAPI(title="colourMatik")


def _warm_ai_models_disabled():
    """Fetch/load the AI models in the BACKGROUND right after the engine starts.
    EoMT (~1.2GB) and ModFlows (~170MB) download once per machine; without this
    the user's FIRST match silently pays that download and the progress bar
    looks frozen for minutes ("stuck at 67%")."""
    def _warm():
        try:
            from . import neural as _nn
            _nn.available()
        except Exception:
            pass
        try:
            from . import modflows as _mf
            _mf.available()
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True).start()
# UXP panels (and any local caller) fetch this server cross-origin.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


@app.middleware("http")
async def _block_foreign_websites(request, call_next):
    """The engine reads local files by path, so a malicious WEB PAGE must not be
    able to drive it (a browser tab can reach 127.0.0.1). Browsers always send an
    Origin header on cross-origin requests; the UXP panel and local tools send
    none (or a non-http scheme). Block only http(s) origins that aren't local."""
    origin = request.headers.get("origin", "")
    # A LOCAL html file opened in a browser sends Origin: null — that is still a
    # web page driving the engine (arbitrary path reads, library deletion,
    # updater launches). The panels never send "null": UXP fetch sends no
    # Origin, and the CEP panel talks over Node http.
    if origin == "null":
        return JSONResponse({"ok": False, "error": "forbidden origin"}, status_code=403)
    if origin.startswith(("http://", "https://")):
        host = origin.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            return JSONResponse({"ok": False, "error": "forbidden origin"}, status_code=403)
    return await call_next(request)
WORK = Path(tempfile.gettempdir()) / "colourmatik_web"
WORK.mkdir(exist_ok=True)

# Every match is appended here as one JSON line (paths, mode, tf, frame times,
# winner, scores). When a user reports "the colours came out wrong yesterday",
# this file answers WHICH clips and WHAT the engine decided - the uvicorn access
# log alone made that question unanswerable in the field.
_MATCH_LOG = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "colourMatik" / "matches.log"


def _log_match(entry: dict) -> None:
    try:
        import datetime as _dt
        entry = {"time": _dt.datetime.now().isoformat(timespec="seconds"), **entry}
        _MATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_MATCH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
_RESULTS: dict[str, Path] = {}
_JOBS: dict[str, dict] = {}  # rid -> {lut, tf, src1, ref1, corresponded} for live re-baking

# Live progress for the panel's loading bar. Keyed by a client-supplied job_id;
# the panel polls GET /progress/{job_id} while /match_paths is still running.
_PROGRESS: dict[str, dict] = {}


def _set_progress(job_id, pct, msg):
    if job_id:
        _PROGRESS[job_id] = {"pct": max(0.0, min(1.0, float(pct))), "msg": msg}
        while len(_PROGRESS) > 64:                 # bounded; old jobs fall off
            _PROGRESS.pop(next(iter(_PROGRESS)), None)
_LOCK = threading.Lock()     # serialize LUT-folder writes (avoid concurrent-write races)
_MAX_CACHE = 12              # cap the in-memory caches — each job holds a LUT + frames + alt LUTs (~10MB)


def _touch_job(rid: str):
    """Move a just-used job to the end of the eviction order (LRU behaviour) —
    a MATCH ALL burst must not evict the hero match whose sliders/alts the user
    still has on screen."""
    with _LOCK:
        j = _JOBS.pop(rid, None)
        if j is not None:
            _JOBS[rid] = j
        c = _RESULTS.pop(rid, None)
        if c is not None:
            _RESULTS[rid] = c


def _remember(rid: str, cube: Path, job: dict | None = None) -> None:
    """Store a match's result + evict the oldest so memory AND disk can't grow
    unbounded (each match holds a LUT + frames in RAM and a WORK/<uuid> dir on disk)."""
    with _LOCK:
        _RESULTS[rid] = cube
        if job is not None:
            _JOBS[rid] = job
        while len(_RESULTS) > _MAX_CACHE:
            old_rid, old_cube = next(iter(_RESULTS.items()))
            _RESULTS.pop(old_rid, None)
            try:                                    # delete the evicted match's WORK dir
                d = Path(old_cube).parent
                if d.parent == WORK and d.exists():
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
        while len(_JOBS) > _MAX_CACHE:
            _JOBS.pop(next(iter(_JOBS)), None)

# Per-user application-support root: macOS ~/Library/Application Support,
# Windows %APPDATA% (Roaming). Everything the engine writes lives under these.
if sys.platform == "win32":
    _APP_SUPPORT = Path(os.environ.get("APPDATA",
                                       str(Path.home() / "AppData" / "Roaming")))
else:
    _APP_SUPPORT = Path.home() / "Library/Application Support"

# Premiere scans these at launch; we drop the LUT here so it shows in the
# Lumetri Input-LUT / Creative-Look dropdowns (folder names drift across installs).
_LUT_DIRS = [
    _APP_SUPPORT / "Adobe/Common/LUTs/Creative",
    _APP_SUPPORT / "Adobe/Common/LUTs/Technical",
    _APP_SUPPORT / "Adobe/Common/LUTs/Input",
]


def _install_lut(lut, tf: str, name: str = "colourMatik") -> None:
    """Drop the winning LUT into Premiere's LUT folders (a convenience for the
    Lumetri dropdowns). Written once and copied, and called from a background
    thread by the match: nothing the panel waits for depends on it."""
    with _LOCK:
        first = None
        for d in _LUT_DIRS:
            try:
                d.mkdir(parents=True, exist_ok=True)
                dst = d / f"{name}.cube"
                if first is None:
                    write_cube(dst, lut, title=name)
                    first = dst
                else:
                    shutil.copyfile(first, dst)
            except Exception:
                pass


# The native "colourMatik" effect reads its 33^3 LUT from a per-match "slot" file
# here. A NEW slot number each call is what makes the effect reload (it caches by
# slot), so the panel just points the effect's Slot param at the returned number.
_SLOT_DIR = _APP_SUPPORT / "colourMatik"
_SLOT_COUNTER = _SLOT_DIR / ".next_slot"
_EFFECT_LUT_SIZE = 65  # must be <= CM_LUT_MAX in the native effect (65 since v1.1)


def _next_slot() -> int:
    with _LOCK:
        _SLOT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            n = int(_SLOT_COUNTER.read_text().strip())
        except Exception:
            n = 0
        # Never fall behind the slot files on disk: if the counter file was lost
        # (partial uninstall, restored backup) but slot_*.cube files survive, a
        # counter restart would REUSE slot numbers that open projects still point
        # at — the native effect caches a slot's LUT for the whole app session, so
        # a reused number silently serves the older look. Deriving from the files
        # keeps every number fresh.
        try:
            n = max(n, max((int(p.stem.split("_")[1]) for p in _SLOT_DIR.glob("slot_*.cube")),
                           default=0))
        except Exception:
            pass
        # Never 0 (0 = the effect's "no LUT" default). NOT a modulo: once a
        # slot_99999.cube existed on disk, `n % 99999 + 1` pinned EVERY later
        # match to slot 1 forever (max(files) kept returning 99999) - and the
        # native effect caches by slot number per session, so new matches
        # silently rendered the first slot-1 look. Wrap past the cap, then skip
        # over any number whose cube still exists so a live slot is never reused.
        n = 1 if n >= 99999 else n + 1
        while (_SLOT_DIR / f"slot_{n}.cube").exists():
            n = 1 if n >= 99999 else n + 1
        try:
            _SLOT_COUNTER.write_text(str(n))
        except Exception:
            pass
        global _SLOT_GC_LAST
        if time.time() - _SLOT_GC_LAST > 600:          # at most every 10 minutes
            _SLOT_GC_LAST = time.time()
            try:
                _gc_slots()
            except Exception:
                pass
        return n


# ---- slot retention -----------------------------------------------------------
# A saved Premiere / After Effects project points at slot NUMBERS, and the native
# effect renders identity when slot_<n>.cube is gone - silently, with no error.
# The old rule "keep the newest 200 files" therefore wiped the grades of every
# older project after ~200 applies (a busy day: each Match writes a draft and a
# final, every slider pause and alternative-look click one more). Retention now
# follows what can still be referenced:
#   * every match (rid) keeps its first slot (the result itself) and its last
#     two (the final state, and the one before it for a Premiere undo);
#   * a draft is dropped once a later final was written for the same clip (the
#     panel always replaces it; a crashed refine keeps it);
#   * other intermediate states (earlier slider / alternative steps) are dropped
#     after 14 days, long after any undo history is gone;
#   * possibly-live slots, and slots written before this index existed, are never
#     touched for 180 days; after that only if the folder exceeds 8 GB, oldest first.
# The effect LUT stays 65^3: measured on real footage, 33^3 / 49^3 slots moved
# some pictures by dE00 4-8 at the 99.9th percentile.
_SLOT_INDEX = _SLOT_DIR / "slots.jsonl"
_SLOT_GRACE_S = 14 * 86400
_SLOT_KEEP_S = 180 * 86400
_SLOT_BUDGET = 8 * 1024 ** 3
_SLOT_GC_LAST = 0.0


def _read_slot_index() -> list:
    try:
        lines = _SLOT_INDEX.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out = []
    for ln in lines:
        try:
            e = json.loads(ln)
            e["slot"] = int(e["slot"])
            out.append(e)
        except Exception:
            continue
    return out


def _record_slot(slot: int, rid: str, job: dict) -> None:
    """Append one line per written slot: which match it belongs to, whether it is
    a draft, and which source clip it grades."""
    entry = {"slot": int(slot), "rid": rid, "draft": bool(job.get("draft")),
             "src": str(job.get("src") or ""), "t": time.time()}
    with _LOCK:
        try:
            _SLOT_DIR.mkdir(parents=True, exist_ok=True)
            with open(_SLOT_INDEX, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass


def _gc_slots(now: float | None = None) -> dict:
    """Delete only slot files no saved project can still be using (see above).
    Caller holds _LOCK. Returns counts for tests / diagnostics."""
    now = time.time() if now is None else now
    files = {}
    for p in _SLOT_DIR.glob("slot_*.cube"):
        try:
            n = int(p.stem.split("_", 1)[1])
            st = p.stat()
        except Exception:
            continue
        files[n] = (p, st.st_mtime, st.st_size)
    stats = {"files": len(files), "superseded": 0, "deleted": 0, "budget_deleted": 0}
    if not files:
        return stats
    raw = _read_slot_index()
    entries = {}
    for e in raw:
        if e["slot"] in files:
            entries[e["slot"]] = e                     # the latest record for a number wins
    by_rid: dict = {}
    newest_final_for_src: dict = {}
    for n, e in entries.items():
        t = float(e.get("t", files[n][1]))
        by_rid.setdefault(e.get("rid"), []).append((t, n))
        if not e.get("draft") and e.get("src"):
            newest_final_for_src[e["src"]] = max(newest_final_for_src.get(e["src"], 0.0), t)
    superseded = set()
    for rid, lst in by_rid.items():
        lst.sort()
        keep = {lst[0][1]} | {n for _, n in lst[-2:]}
        for t, n in lst:
            e = entries[n]
            if e.get("draft"):
                if newest_final_for_src.get(e.get("src"), 0.0) > t:
                    superseded.add(n)
            elif n not in keep:
                superseded.add(n)
    stats["superseded"] = len(superseded)
    for n in superseded:
        p, mtime, size = files[n]
        if now - mtime > _SLOT_GRACE_S:
            try:
                p.unlink()
                files.pop(n, None)
                stats["deleted"] += 1
            except Exception:
                pass
    total = sum(v[2] for v in files.values())
    if total > _SLOT_BUDGET:
        for n in sorted(files, key=lambda k: files[k][1]):
            if total <= _SLOT_BUDGET:
                break
            p, mtime, size = files[n]
            if now - mtime <= _SLOT_KEEP_S:
                break                                   # everything after this is younger
            try:
                p.unlink()
                total -= size
                files.pop(n, None)
                stats["budget_deleted"] += 1
            except Exception:
                pass
    # keep the index proportional to what still exists
    if len(raw) > 2 * len(files) + 500:
        try:
            keep_lines = [json.dumps(e, ensure_ascii=False) for e in raw if e["slot"] in files]
            tmp = _SLOT_INDEX.with_name(_SLOT_INDEX.name + ".tmp")
            tmp.write_text("\n".join(keep_lines) + ("\n" if keep_lines else ""), encoding="utf-8")
            os.replace(tmp, _SLOT_INDEX)
        except Exception:
            pass
    return stats


_METHOD_LABELS = {
    "canon": "AI grade (CanonCGT)",
    "neural": "AI scene-match",
    "idt": "distribution (IDT)",
    "grade": "colorist grade (tone + balance)",
    "mkl": "linear (MKL)",
    "lattice": "3D lattice",
    "poly1": "linear fit", "poly2": "polynomial", "poly3": "polynomial",
    "sep": "filmic (curves+3D)",
    "flow": "neural flow",
    "uot": "balanced transport",
}


def _method_label(m: str) -> str:
    base, _, suffix = m.partition("+")
    label = _METHOD_LABELS.get(base, base)
    return label + (" + fine-tune" if suffix == "refine" else "")


def _preview_dataurl(ref1, src1, matched1, tf, corresponded, job: Path) -> tuple[str, dict | None, dict | None]:
    db = da = None
    if corresponded and src1.shape == ref1.shape:
        db = summarize(image_delta_e00(src1, ref1, tf))["mean"]
        da = summarize(image_delta_e00(matched1, ref1, tf))["mean"]
    # The panel shows this at a few hundred pixels: full-size PNG panels made a
    # 4.4 MB data URL per match (slow to ship, slow to paint). 960 px JPEG panels
    # look identical there at ~150 KB.
    prev = job / "preview.jpg"
    make_comparison(ref1, src1, matched1, prev, db, da, show_error=corresponded, tf=tf, max_w=960)
    return "data:image/jpeg;base64," + _b64.b64encode(prev.read_bytes()).decode(), db, da


def _save_upload(up: UploadFile, dst_dir: Path) -> Path:
    suffix = Path(up.filename or "clip").suffix or ".mp4"
    dst = dst_dir / f"in_{uuid.uuid4().hex}{suffix}"
    with dst.open("wb") as f:
        shutil.copyfileobj(up.file, f)
    return dst


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


def _frame_is_degenerate(img: np.ndarray) -> bool:
    """True for a frame with (almost) no colour information to match: near-black
    or near-white over most of the picture, or essentially flat."""
    a = np.asarray(img)
    if a.ndim != 3 or a.shape[0] * a.shape[1] < 64:
        return False
    small = a[::max(1, a.shape[0] // 180), ::max(1, a.shape[1] // 320)].astype(np.float32)
    luma = 0.2126 * small[..., 0] + 0.7152 * small[..., 1] + 0.0722 * small[..., 2]
    if float(np.mean(luma < 0.04)) > 0.85 or float(np.mean(luma > 0.96)) > 0.85:
        return True
    return float(luma.std()) < 0.015 and float(small.reshape(-1, 3).std(axis=0).max()) < 0.02


def _process(src_path: Path, ref_path: Path, mode: str, tf: str, frames: int,
             job: Path, title: str, look: str = "exact",
             src_range: tuple | None = None, ref_range: tuple | None = None,
             src_at: float | None = None, ref_at: float | None = None,
             job_id: str | None = None, fast: bool = False) -> dict:
    corresponded = (mode == "same")
    # A pre-1.7 panel with "Cinematic AI" still selected sends look="ai_grade".
    # CanonCGT is opt-in now; on machines that still HAVE the old AI stack the
    # request would otherwise run it on a 7-frame stacked mosaic (the single-
    # coherent-image special case is gone). Coerce at the boundary: legacy
    # requests get the same accurate contest as everyone else.
    if look == "ai_grade":
        look = "exact"
    # Frame pooling (stacked frames) helps the classical distribution methods, but the
    # learned look-transfer (CanonCGT) analyses ONE coherent image — give it a single frame.
    # "ai_grade" from a pre-1.7 panel: CanonCGT is opt-in now, so the request
    # degrades to the classical contest — which WANTS pooled frames. The old
    # single-frame special case would have silently degraded those users'
    # matches instead.
    f = frames
    if fast:
        f = min(f, 3)            # the draft reads 3 frames; the full pass re-reads properly
    si, so = src_range or (None, None)
    ri, ro = ref_range or (None, None)

    _set_progress(job_id, 0.04, "Reading the clips")
    # Load both clips concurrently — each is an independent ffmpeg decode, so
    # running them in parallel roughly halves the frame-extraction wait.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as ex:
        # robust sampling (extra candidates + dominant-look selection) only in
        # distribution mode — corresponded mode needs identical frame indices on
        # both clips, and dropping different outliers per clip would break pairs.
        # An exact frame time wins over everything: one frame, at that instant.
        if src_at is not None:
            fs = ex.submit(cmio.load_any, src_path, t=float(src_at), frames=1)
        else:
            fs = ex.submit(cmio.load_any, src_path, frames=f, start=si, end=so,
                           robust=not corresponded)
        if ref_at is not None:
            fr = ex.submit(cmio.load_any, ref_path, t=float(ref_at), frames=1)
        else:
            fr = ex.submit(cmio.load_any, ref_path, frames=f, start=ri, end=ro,
                           robust=not corresponded)
        src, ref = fs.result(), fr.result()
    src_n = 1 if src_at is not None else f      # frames stacked per side (preview slicing)
    ref_n = 1 if ref_at is not None else f

    # "Take the colour from the frame I am showing you" is only meaningful when
    # that frame HAS colour. A playhead parked on a fade, a black leader, a white
    # flash or a flat wall hands the match a degenerate distribution, and every
    # method then drives the whole clip to that nothing (seen: a day exterior
    # crushed to 70% pure black by an explosion clip's black lead-in frame). Fall
    # back to sampling across the clip in that case and say so in the report.
    _frame_notes = []
    if src_at is not None and _frame_is_degenerate(src):
        src = cmio.load_any(src_path, frames=f, start=si, end=so, robust=not corresponded)
        src_n = f
        _frame_notes.append(f"the source frame at {float(src_at):.2f}s is nearly "
                            "black/white/flat - sampled across the clip instead")
    if ref_at is not None and _frame_is_degenerate(ref):
        ref = cmio.load_any(ref_path, frames=f, start=ri, end=ro, robust=not corresponded)
        ref_n = f
        _frame_notes.append(f"the reference frame at {float(ref_at):.2f}s is nearly "
                            "black/white/flat - sampled across the clip instead")

    _set_progress(job_id, 0.16, "Analysing colour")
    # match() spans 16%..82% of the bar; forward its internal milestones.
    # Back to the fast, proven core: mkl + sep + idt with the gamut/steepness
    # guards, judged in Oklab. The heavy AI candidates (EoMT, ModFlows, UOT,
    # refine) cost 1.4GB of downloads and 15-25s per match for gains the field
    # never felt — the product this replaces was "fast and right", so that is
    # the default again. The code stays importable for the future.
    res = match(src, ref, corresponded=corresponded, tf=tf, look=look, quick=fast,
                neural=False, refine=False,
                progress=lambda p, m: _set_progress(job_id, 0.16 + p * 0.66, m))

    res.notes.extend(_frame_notes)

    _set_progress(job_id, 0.84, "Baking the LUT")
    cube = job / "colourMatik.cube"
    write_cube(cube, res.lut, title=title)
    if not fast:
        # expose in Premiere LUT dropdowns (visible after next launch) - best effort,
        # off the response path
        threading.Thread(target=_install_lut, args=(res.lut.copy(), tf), daemon=True).start()

    # Preview uses ONE frame — reuse the first frame already decoded above (each
    # pooled VIDEO clip is n frames stacked vertically) instead of decoding again.
    # Images are never stacked, so they must not be sliced (a PNG target would
    # otherwise preview as its top 1/f strip). NOTE the per-side frame count: a
    # playhead time (src_at/ref_at) decodes ONE frame regardless of `f` — slicing
    # that by f showed only the top 1/f STRIP of the frame in every preview,
    # wipe, alt thumbnail and dE number of every playhead match (the panel's
    # default flow). The LUT itself was always fine; the previews lied.
    def _first_frame(arr, path, n_frames):
        # Mirror load_any's routing: anything that is not a known STILL extension
        # was decoded as video and arrives as n_frames frames stacked vertically.
        if n_frames > 1 and Path(path).suffix.lower() not in cmio.IMAGE_EXTS:
            h = arr.shape[0] // n_frames
            if h > 0:
                return arr[:h]
        return arr
    src1 = _first_frame(src, src_path, src_n)
    ref1 = _first_frame(ref, ref_path, ref_n)
    if fast:
        preview, db, da = "", None, None       # the full pass renders the real preview
    else:
        _set_progress(job_id, 0.92, "Rendering the preview")
        matched1 = apply_lut(src1, res.lut)
        preview, db, da = _preview_dataurl(ref1, src1, matched1, tf, corresponded, job)

    rid = uuid.uuid4().hex
    # Store SMALL COPIES of the frames. src1/ref1 are numpy VIEWS into the full
    # multi-frame decode stack — keeping them pinned the whole 7-frame float64
    # stack (~350MB per 1080p side, GBs at 4K) alive per cached job. The wipe
    # and alt thumbnails never need more than ~720px.
    def _small(img):
        from PIL import Image as _Im
        h, w = img.shape[:2]
        tw = min(720, w); th = max(1, int(h * tw / max(w, 1)))
        arr = _Im.fromarray((np.clip(img, 0, 1) * 255 + 0.5).astype("uint8")).resize((tw, th))
        return (np.asarray(arr, dtype=np.float32) / 255.0)
    _remember(rid, cube, {"lut": res.lut, "tf": tf,
                          "src1": _small(src1), "ref1": _small(ref1),
                          "corresponded": corresponded, "alts": res.alts,
                          "scores": res.scores, "method": res.method,
                          "s_lin": res.sample_src_lin, "t_lin": res.sample_tgt_lin,
                          "draft": bool(fast), "src": str(src_path)})
    _log_match({"rid": rid, "target": str(src_path), "reference": str(ref_path),
                "mode": mode, "tf": tf, "frames": f, "fast": bool(fast),
                "src_at": src_at, "ref_at": ref_at,
                "src_range": src_range, "ref_range": ref_range,
                "winner": res.method, "corresponded": res.corresponded,
                "scores": {k: round(float(v), 4) for k, v in res.scores.items()},
                "notes": list(res.notes)})
    _set_progress(job_id, 1.0, "Done")
    return {
        "ok": True,
        "rid": rid,
        "report": format_report(res),
        "method": res.method,
        "method_label": _method_label(res.method),
        "ai_used": res.method.startswith(("neural", "canon")),
        "metric": res.score_metric,
        "scores": res.scores,
        "de_before": db,
        "de_after": da,
        "de_skin_after": res.de_skin_after,
        "corresponded": corresponded,
        "cube_path": str(cube),
        "preview": preview,
        "download": f"/download/{rid}",
    }


@app.post("/match")
def do_match(source: UploadFile = File(...), reference: UploadFile = File(...),
             mode: str = Form("different"), tf: str = Form("sRGB"),
             frames: int = Form(3)):
    job = WORK / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=True)
    try:
        src_path = _save_upload(source, job)
        ref_path = _save_upload(reference, job)
        return JSONResponse(_process(src_path, ref_path, mode, tf, frames, job,
                                     f"colourMatik {Path(source.filename or 'clip').stem}"))
    except Exception as e:
        traceback.print_exc()
        shutil.rmtree(job, ignore_errors=True)   # don't leak the work dir on failure
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=400)


class PathReq(BaseModel):
    source_path: str
    reference_path: str
    mode: str = "different"
    tf: str = "sRGB"
    frames: int = 7            # frames pooled per clip (sampling variance dominates accuracy)
    fast: bool = False         # 2-5s draft: fewer samples/candidates; panel refines after
    look: str = "exact"        # "exact" = accuracy contest; "ai_grade" = CanonCGT look
    # Timeline segment (seconds, source-media-relative). When the panel sends the
    # track item's in/out, sampling stays inside the part actually used in the edit.
    source_in: float | None = None
    source_out: float | None = None
    reference_in: float | None = None
    reference_out: float | None = None
    # EXACT frame times (seconds, source-media-relative). When the panel sends
    # these, matching reads ONE frame at that instant instead of pooling across
    # the clip — "take the colour from the frame I am showing you". Trumps the
    # in/out range.
    source_at: float | None = None
    reference_at: float | None = None
    job_id: str | None = None   # client tag so the panel can poll GET /progress/{job_id}


@app.post("/match_paths")
def match_paths(req: PathReq):
    """Match by on-disk file paths — used by the Premiere UXP panel (which sends the
    selected clips' media paths). Reads files directly; nothing is uploaded."""
    job = WORK / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=True)
    try:
        src = Path(req.source_path)
        ref = Path(req.reference_path)
        if not src.exists() or not ref.exists():
            return JSONResponse({"ok": False, "error": "file not found"}, status_code=400)
        return JSONResponse(_process(src, ref, req.mode, req.tf, req.frames, job,
                                     f"colourMatik {src.stem}", look=req.look,
                                     src_range=(req.source_in, req.source_out),
                                     ref_range=(req.reference_in, req.reference_out),
                                     src_at=req.source_at, ref_at=req.reference_at,
                                     job_id=req.job_id, fast=req.fast))
    except Exception as e:
        traceback.print_exc()
        if req.job_id:
            _set_progress(req.job_id, 1.0, "error")
        shutil.rmtree(job, ignore_errors=True)   # don't leak the work dir on failure
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=400)


@app.get("/progress/{job_id}")
def get_progress(job_id: str):
    """Live progress for the panel's loading bar (0..1 + a short stage message)."""
    return JSONResponse(_PROGRESS.get(job_id, {"pct": 0.0, "msg": "Starting"}))


class BakeReq(BaseModel):
    rid: str
    intensity: float = 1.0  # 1.0 = full match; panel maps 0..150% -> 0..1.5


@app.post("/bake")
def bake(req: BakeReq):
    """Re-bake a prior match at a new intensity: updates the preview live and
    rewrites the .cube (+ LUT dropdowns). intensity<1 weaker, >1 stronger."""
    j = _JOBS.get(req.rid)
    if j is None:
        return JSONResponse({"ok": False, "error": "unknown rid"}, status_code=404)
    job = None
    try:
        baked = apply_intensity(j["lut"], float(req.intensity))
        job = WORK / uuid.uuid4().hex
        job.mkdir(parents=True, exist_ok=True)
        cube = job / "colourMatik.cube"
        write_cube(cube, baked, title=f"colourMatik {int(round(req.intensity * 100))}%")
        _install_lut(baked, j["tf"])
        matched = apply_lut(j["src1"], baked)
        preview, _db, da = _preview_dataurl(j["ref1"], j["src1"], matched, j["tf"],
                                            j["corresponded"], job)
        rid2 = uuid.uuid4().hex
        _remember(rid2, cube)
        return JSONResponse({"ok": True, "download": f"/download/{rid2}",
                             "cube_path": str(cube), "preview": preview, "de_after": da})
    except Exception as e:
        traceback.print_exc()
        if job is not None:
            shutil.rmtree(job, ignore_errors=True)   # don't leak the work dir on failure
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=400)


def _decompose_lut(lut, tf: str) -> dict:
    """Project the match LUT onto settable Lumetri sliders (WB + exposure + contrast
    + saturation), computed in linear light by probing the LUT."""
    eps = 1e-4
    # neutral grey ramp -> per-channel gains
    greys = np.linspace(0.10, 0.90, 9)[:, None] * np.ones((1, 3))
    lin_in = cs.decode(greys, tf)
    lin_out = cs.decode(apply_lut_points(lut, greys), tf)
    k = np.array([np.median(lin_out[:, c] / np.maximum(lin_in[:, c], eps)) for c in range(3)])
    k = np.maximum(k, eps)
    g = float(np.exp(np.mean(np.log(k))))            # overall gain (geometric mean)
    r = k / g                                        # per-channel white-balance ratio (∏r=1)

    exposure = float(np.clip(np.log2(max(g, eps)), -4, 4))
    FW = 130.0                                       # WB scale -> Lumetri -100..100 (calibrated)
    temperature = float(np.clip(FW * (np.log(r[0]) - np.log(r[2])) / 2.0, -100, 100))
    tint = float(np.clip(FW * (np.log(r[1]) - (np.log(r[0]) + np.log(r[2])) / 2.0), -100, 100))

    # contrast from the neutral tone slope (log-log), 0 for a pure gain
    lg_in = np.log(np.maximum(lin_in.mean(1), eps))
    lg_out = np.log(np.maximum(lin_out.mean(1) / g, eps))
    slope = float(np.polyfit(lg_in, lg_out, 1)[0])
    contrast = float(np.clip(60.0 * (slope - 1.0), -100, 100))

    # saturation from probing saturated primaries/secondaries
    prim = np.array([[.75, .15, .15], [.15, .75, .15], [.15, .15, .75],
                     [.75, .75, .15], [.15, .75, .75], [.75, .15, .75]])
    pin = cs.decode(prim, tf)
    pout = cs.decode(apply_lut_points(lut, prim), tf)
    chroma = lambda x: float(np.mean(np.linalg.norm(x - x.mean(1, keepdims=True), axis=1)))
    saturation = float(np.clip(100.0 * chroma(pout) / max(chroma(pin), eps), 0, 200))

    return {"Exposure": round(exposure, 3), "Temperature": round(temperature, 1),
            "Tint": round(tint, 1), "Contrast": round(contrast, 1),
            "Saturation": round(saturation, 1)}


@app.post("/decompose")
def decompose(req: BakeReq):
    """Return the match as settable Lumetri slider values (auto-applied by the panel,
    no dropdown). intensity scales the deltas from neutral."""
    j = _JOBS.get(req.rid)
    if j is None:
        return JSONResponse({"ok": False, "error": "unknown rid"}, status_code=404)
    try:
        base = _decompose_lut(j["lut"], j["tf"])
        return {"ok": True, "params": base}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=400)


class EffectLutReq(BaseModel):
    rid: str
    intensity: float = 1.0  # baked at full strength by default; the effect's own
    #                         Intensity slider does the live dialing, so leave at 1.0
    variant: str | None = None   # pick an alternative candidate look by name
    # Per-axis strengths (1.0 = the full match). The winning LUT is factored into
    # WB / Tone / Colour stages on first use and recomposed at these strengths.
    wb: float = 1.0
    tone: float = 1.0
    color: float = 1.0


@app.get("/alts/{rid}")
def alts(rid: str):
    """The top candidate looks for a finished match, as small before-preview
    thumbnails. The panel renders them as a clickable row; /effect_lut with
    variant=<key> then bakes the chosen one."""
    _touch_job(rid)
    j = _JOBS.get(rid)
    if j is None:
        return JSONResponse({"ok": False, "error": "unknown rid"}, status_code=404)
    try:
        from PIL import Image
        src1 = j["src1"]
        # thumbnail base: ~204px wide, keep aspect
        h, w = src1.shape[:2]
        tw = 204; th = max(1, int(h * tw / max(w, 1)))
        base = np.asarray(Image.fromarray(
            (np.clip(src1, 0, 1) * 255 + 0.5).astype("uint8")).resize((tw, th)),
            dtype=np.float64) / 255.0
        import io as _io
        out = []
        for name, alt in (j.get("alts") or {}).items():
            img = apply_lut(base, np.asarray(alt, dtype=np.float64))
            bio = _io.BytesIO()   # in-memory: mkstemp leaked one fd per thumbnail
            Image.fromarray((np.clip(img, 0, 1) * 255 + 0.5).astype("uint8")).save(bio, format="JPEG", quality=82)
            data = "data:image/jpeg;base64," + _b64.b64encode(bio.getvalue()).decode()
            out.append({"key": name, "label": _method_label(name),
                        "score": float((j.get("scores") or {}).get(name.partition("+")[0], 0.0)),
                        "preview": data,
                        "chosen": name == j.get("method")})
        return {"ok": True, "alts": out}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=400)


@app.post("/effect_lut")
def effect_lut(req: EffectLutReq):
    """Write the match as a 65^3 .cube into a fresh slot the native colourMatik
    effect reads (full engine precision). Returns the slot number for the panel."""
    _touch_job(req.rid)
    j = _JOBS.get(req.rid)
    if j is None:
        return JSONResponse({"ok": False, "error": "unknown rid"}, status_code=404)
    try:
        lut = j["lut"]
        if req.variant:
            alt = (j.get("alts") or {}).get(req.variant)
            if alt is None:
                return JSONResponse({"ok": False, "error": "unknown variant"}, status_code=404)
            lut = np.asarray(alt, dtype=np.float64)
            j["lut"] = lut          # strength/wipe operations follow the chosen look
            j.pop("_dec", None)     # decomposition belongs to the previous look
        # NaN-guard the axis strengths (pydantic accepts the string "NaN" as a
        # float; one NaN here would poison every node of the baked cube).
        _ax = [req.wb, req.tone, req.color]
        if not all(np.isfinite(v) for v in _ax):
            req.wb, req.tone, req.color = 1.0, 1.0, 1.0
        if (req.wb, req.tone, req.color) != (1.0, 1.0, 1.0) and j.get("s_lin") is None:
            # No linear samples (MKL candidate absent): the decomposition cannot
            # run. Say so instead of silently baking full strength while the
            # panel UI claims reduced WB/Tone/Colour.
            return JSONResponse({"ok": False, "error": "strength decomposition "
                                 "unavailable for this match - use Intensity"},
                                status_code=409)
        if (req.wb, req.tone, req.color) != (1.0, 1.0, 1.0) and j.get("s_lin") is not None:
            dec = j.get("_dec")
            if dec is None:
                gains, curves = fit_wb_tone(np.asarray(j["s_lin"], dtype=np.float64),
                                            np.asarray(j["t_lin"], dtype=np.float64))
                resid = decompose_residual(np.asarray(j["lut"], dtype=np.float64),
                                           gains, curves, j["tf"])
                dec = {"gains": gains, "curves": curves, "resid": resid}
                j["_dec"] = dec
            lut = recompose_lut(dec["gains"], dec["curves"], dec["resid"],
                                s_wb=float(req.wb), s_tone=float(req.tone),
                                s_color=float(req.color),
                                size=j["lut"].shape[0], tf=j["tf"])
        if req.intensity != 1.0:
            lut = apply_intensity(lut, float(req.intensity))
        lut_fx = resample_lut(lut, _EFFECT_LUT_SIZE)
        slot = _next_slot()
        path = _SLOT_DIR / f"slot_{slot}.cube"
        write_cube(path, lut_fx, title=f"colourMatik slot {slot}")
        _record_slot(slot, req.rid, j)
        return {"ok": True, "slot": slot, "path": str(path)}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=400)


@app.get("/wipe/{rid}")
def wipe(rid: str):
    """Before/after frames of the CURRENT look for the panel's A/B wipe."""
    _touch_job(rid)
    j = _JOBS.get(rid)
    if j is None:
        return JSONResponse({"ok": False, "error": "unknown rid"}, status_code=404)
    try:
        from PIL import Image
        src1 = j["src1"]
        h, w = src1.shape[:2]
        tw = 480; th = max(1, int(h * tw / max(w, 1)))
        base = np.asarray(Image.fromarray(
            (np.clip(src1, 0, 1) * 255 + 0.5).astype("uint8")).resize((tw, th)),
            dtype=np.float64) / 255.0
        after = apply_lut(base, np.asarray(j["lut"], dtype=np.float64))
        def _durl(img):
            import io as _io
            bio = _io.BytesIO()   # in-memory: mkstemp leaked one fd per frame
            Image.fromarray((np.clip(img, 0, 1) * 255 + 0.5).astype("uint8")).save(bio, format="JPEG", quality=85)
            return "data:image/jpeg;base64," + _b64.b64encode(bio.getvalue()).decode()
        return {"ok": True, "before": _durl(base), "after": _durl(after)}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=400)


# ---- Reference stills library (the colorist "gallery" idiom) ---------------
_LIB_DIR = _SLOT_DIR / "library"
_LIB_JSON = _LIB_DIR / "library.json"


def _lib_load() -> list:
    try:
        import json
        return json.loads(_LIB_JSON.read_text())
    except Exception:
        return []


def _lib_save(items: list) -> None:
    import json
    _LIB_DIR.mkdir(parents=True, exist_ok=True)
    _LIB_JSON.write_text(json.dumps(items, ensure_ascii=False))


class LibAddReq(BaseModel):
    path: str
    t: float | None = None      # optional source-relative second for the thumb


@app.post("/library_add")
def library_add(req: LibAddReq):
    """Save a reference (video or still) into the local gallery with a thumb."""
    try:
        from PIL import Image
        src = Path(req.path)
        if not src.exists():
            return JSONResponse({"ok": False, "error": "file not found"}, status_code=400)
        frame = cmio.load_any(src, t=req.t, frames=1)
        h, w = frame.shape[:2]
        tw = 160; th = max(1, int(h * tw / max(w, 1)))
        thumb = Image.fromarray((np.clip(frame, 0, 1) * 255 + 0.5).astype("uint8")).resize((tw, th))
        _LIB_DIR.mkdir(parents=True, exist_ok=True)
        lid = uuid.uuid4().hex[:12]
        thumb.save(_LIB_DIR / f"{lid}.jpg", quality=85)
        items = _lib_load()
        items.insert(0, {"id": lid, "path": str(src), "name": src.name})
        _lib_save(items[:60])                      # bounded gallery
        return {"ok": True, "id": lid}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=400)


@app.get("/library_list")
def library_list():
    out = []
    for it in _lib_load():
        tp = _LIB_DIR / f"{it['id']}.jpg"
        if not tp.exists():
            continue
        out.append({**it, "thumb": "data:image/jpeg;base64," +
                    _b64.b64encode(tp.read_bytes()).decode()})
    return {"ok": True, "items": out}


class LibDelReq(BaseModel):
    id: str


@app.post("/library_del")
def library_del(req: LibDelReq):
    # ids are hex we minted in library_add — anything else (e.g. "../…") is an
    # attempted path traversal into unlink() and gets rejected outright.
    import re
    if not re.fullmatch(r"[0-9a-f]{12}", req.id or ""):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    items = [it for it in _lib_load() if it.get("id") != req.id]
    _lib_save(items)
    try:
        (_LIB_DIR / f"{req.id}.jpg").unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True}


class GroupShotsReq(BaseModel):
    items: list          # [{id, path, in?, out?}] — one timeline clip each


@app.post("/group_shots")
def group_shots(req: GroupShotsReq):
    """Cluster timeline clips by LOOK so MATCH ALL can fit ONE correction per
    group — same-setup shots share a LUT (no grade 'pops' at A/B/A cuts), and a
    60-clip timeline needs a handful of matches, not sixty."""
    try:
        from concurrent.futures import ThreadPoolExecutor
        items = list(req.items)[:60]

        def sig(it):
            try:
                lo = it.get("in"); hi = it.get("out")
                mid = None
                if lo is not None and hi is not None and hi > lo:
                    mid = lo + (hi - lo) / 2.0
                frame = cmio.load_any(it["path"], t=mid, frames=1)
                small = frame[::max(1, frame.shape[0] // 72),
                              ::max(1, frame.shape[1] // 128)]
                h, _ = np.histogramdd(small.reshape(-1, 3), bins=(8, 8, 8),
                                      range=((0, 1),) * 3)
                h = h.ravel(); h = h / (h.sum() or 1.0)
                return it["id"], h
            except Exception:
                return it["id"], None

        sigs = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(sig, it): it["id"] for it in items}
            for fu, iid in futs.items():
                try:
                    rid_, h = fu.result(timeout=30)   # offline/NAS media must not wedge the pool
                    sigs[rid_] = h
                except Exception:
                    sigs[iid] = None

        ids = [it["id"] for it in items if sigs.get(it["id"]) is not None]
        failed = [it["id"] for it in items if sigs.get(it["id"]) is None]
        groups: list[list] = []
        THRESH = 0.55            # L1 distance between 8^3 histograms (0..2)
        for cid in ids:
            placed = False
            for g in groups:
                d = float(np.abs(sigs[cid] - sigs[g[0]]).sum())
                if d < THRESH:
                    g.append(cid); placed = True; break
            if not placed:
                groups.append([cid])
        return {"ok": True, "groups": groups, "failed": failed,
                "truncated": max(0, len(req.items) - len(items))}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=400)


@app.get("/version")
def version():
    return {"name": "colourMatik", "version": __version__}


class DiagReq(BaseModel):
    host: str = ""
    version: str = ""
    row: dict | None = None
    fill: dict | None = None
    rest: dict | None = None
    cham: dict | None = None
    chamNatural: dict | None = None
    chamSrc: str | None = None
    btn: dict | None = None


@app.post("/diag")
def diag(req: DiagReq):
    """The panel reports what the HOST actually laid out (measured sizes), so a
    rendering problem inside Premiere/AE can be diagnosed from numbers instead
    of screenshots. Appended to App Support/colourMatik/panel-diag.log."""
    try:
        _SLOT_DIR.mkdir(parents=True, exist_ok=True)
        with open(_SLOT_DIR / "panel-diag.log", "a") as f:
            f.write(req.model_dump_json() + "\n")
    except Exception:
        pass
    return {"ok": True}


# ── The progress bar as a PICTURE ───────────────────────────────────────────
# The panel host proved unable to render ANY layout we tried (stylesheet rules
# ignored, inline geometry ignored, flex rows collapsed into overlap). Images at
# natural size are the one thing it draws faithfully — so the whole bar (track,
# green fill, walking chameleon frame) is composed HERE with PIL and handed to
# the panel as a ready data-URL. The panel owns one <img> and nothing else.
_BAR_W, _BAR_H = 300, 34
_CHAM_FRAMES: list = []
_BAR_CACHE: dict = {}


def _load_cham_frames():
    if _CHAM_FRAMES:
        return _CHAM_FRAMES
    from PIL import Image
    base = Path(__file__).resolve().parents[1] / "colourmatik-uxp"
    for i in range(18):
        f = base / f"cham{i:02d}.png"
        if f.exists():
            _CHAM_FRAMES.append(Image.open(f).convert("RGBA"))
    return _CHAM_FRAMES


@app.get("/bardata")
def bardata(pct: float = 0.0, f: int = 0):
    import io as _io
    from PIL import Image, ImageDraw
    p = max(0.0, min(1.0, pct))
    frames = _load_cham_frames()
    fi = (f % len(frames)) if frames else 0
    key = (round(p * 100), fi)
    hit = _BAR_CACHE.get(key)
    if hit is None:
        img = Image.new("RGBA", (_BAR_W, _BAR_H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 10, _BAR_W - 1, _BAR_H - 11], radius=6, fill=(58, 63, 71, 255))
        cham_w = frames[0].width if frames else 52
        track = _BAR_W - cham_w
        fill_w = int(track * p) + cham_w // 2
        if fill_w > 0:
            d.rounded_rectangle([0, 10, min(fill_w, _BAR_W - 1), _BAR_H - 11],
                                radius=6, fill=(47, 181, 107, 255))
        if frames:
            ch = frames[fi]
            x = int(track * p)
            y = (_BAR_H - ch.height) // 2
            img.paste(ch, (x, y), ch)
        bio = _io.BytesIO()
        img.save(bio, format="PNG", optimize=True)
        hit = "data:image/png;base64," + _b64.b64encode(bio.getvalue()).decode()
        _BAR_CACHE[key] = hit
        while len(_BAR_CACHE) > 2200:
            _BAR_CACHE.pop(next(iter(_BAR_CACHE)), None)
    return {"ok": True, "img": hit}


@app.get("/update_progress")
def update_progress():
    """The updater writes "pct|message" here; the panel's bar polls it."""
    try:
        # Tolerate an older on-disk updater that wrote the value with its quotes
        # still attached (cmd's `set /p=` does not strip them) — otherwise a new
        # engine's bar sits frozen while a perfectly good update runs.
        raw = (_SLOT_DIR / "update_progress").read_text().strip().strip('"')
        pct_s, _, msg = raw.partition("|")
        pct_s = pct_s.strip().strip('"')
        if pct_s.upper() == "FAIL":          # updater reported a real failure
            return {"ok": True, "pct": 0.0, "msg": msg or "update failed", "failed": True}
        return {"ok": True, "pct": max(0.0, min(1.0, float(pct_s) / 100.0)), "msg": msg}
    except Exception:
        return {"ok": True, "pct": 0.0, "msg": ""}


# ── Update check ─────────────────────────────────────────────────────────────
# The panels ask the ENGINE whether a newer colourMatik exists, and the engine
# asks our download server at most once a day for the whole machine - however
# many panels, hosts and restarts - while a click on "Check for updates" may
# refresh it at most every 10 minutes. A failed check counts too, so an offline
# machine does not retry on every panel open. It reads the latest.json the
# website reads, on releases.catheadai.com (Cloudflare R2 - not GitHub), names
# itself in the User-Agent (never a browser's) and asks conditionally
# (If-None-Match), so an unchanged file costs a bodiless 304.
_LATEST_JSON = "https://releases.catheadai.com/colourmatik/latest.json"
_DOWNLOADS = "https://releases.catheadai.com/colourmatik/"
_UPDATE_CACHE = _SLOT_DIR / "update_check.json"
_UPDATE_EVERY_AUTO = 24 * 3600
_UPDATE_EVERY_MANUAL = 10 * 60
_UPDATE_LOCK = threading.Lock()


def _platform_key() -> str:
    """This platform's entry in latest.json."""
    return "windows" if os.name == "nt" else "mac"


def _release_for(latest, key: str):
    """latest.json's entry for this platform as {version, url, size, sha256},
    or None. Only a file on our own download server counts."""
    if not isinstance(latest, dict) or not isinstance(latest.get(key), dict):
        return None
    e = latest[key]
    ver = str(e.get("version") or latest.get("version") or "")
    url = str(e.get("url") or "")
    parts = ver.split(".")
    if len(parts) != 3 or not all(p.isascii() and p.isdigit() for p in parts) or not url.startswith(_DOWNLOADS):
        return None
    try:
        size = int(e.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return {"version": ver, "url": url, "size": size, "sha256": str(e.get("sha256") or "").lower()}


def _fetch_latest(etag: str = ""):
    """GET latest.json: (status, parsed JSON or None, etag); 304 -> None."""
    import ssl
    import urllib.error
    import urllib.request
    ctx = None
    try:            # python.org's macOS Python ships without CA certificates
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    hdr = {"Accept": "application/json",
           "User-Agent": "colourMatik-engine/%s (%s)" % (
               __version__, "Windows" if os.name == "nt" else "macOS")}
    if etag:
        hdr["If-None-Match"] = etag
    try:
        with urllib.request.urlopen(urllib.request.Request(_LATEST_JSON, headers=hdr),
                                    timeout=10, context=ctx) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), resp.headers.get("ETag") or ""
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, None, etag
        raise


@app.get("/update_check")
def update_check(force: int = 0):
    """Newest released colourMatik for this platform ({version, url, size,
    sha256}), from the cache unless that is older than a day - or 10 minutes,
    when the user clicked "Check for updates" (force=1)."""
    source = "r2:" + _platform_key()
    now = time.time()
    with _UPDATE_LOCK:
        try:
            cache = json.loads(_UPDATE_CACHE.read_text(encoding="utf-8"))
            # (1.8.2 cached GitHub releases under "prefix": never reused)
            if not isinstance(cache, dict) or cache.get("source") != source:
                cache = {}
        except Exception:
            cache = {}
        age = now - float(cache.get("checked_at") or 0)
        fresh = 0 <= age < (_UPDATE_EVERY_MANUAL if force else _UPDATE_EVERY_AUTO)
        if not fresh:
            latest, etag, err = cache.get("latest"), cache.get("etag") or "", None
            try:
                status, doc, etag = _fetch_latest(etag if latest else "")
                if status != 304:
                    latest = _release_for(doc, _platform_key())
            except Exception as e:
                err = "%s: %s" % (type(e).__name__, e)
            cache = {"source": source, "checked_at": now, "latest": latest,
                     "etag": etag, "error": err}
            try:
                _SLOT_DIR.mkdir(parents=True, exist_ok=True)
                _UPDATE_CACHE.write_text(json.dumps(cache), encoding="utf-8")
            except Exception:
                pass
    latest = cache.get("latest") or {}
    return {"ok": bool(latest) or not cache.get("error"),
            "version": latest.get("version"), "url": latest.get("url", ""),
            "size": latest.get("size"), "sha256": latest.get("sha256", ""),
            "checked_at": cache.get("checked_at"), "cached": fresh, "error": cache.get("error")}


@app.post("/update_now")
def update_now():
    """Launch the platform updater, detached — the panel's Update button.

    The updater pulls the newest code, refreshes deps, reinstalls panel+effect
    and restarts this engine, so it must OUTLIVE this process: on Windows it
    runs in its own windowless console (update-windows.cmd self-elevates with
    its own UAC prompt); on macOS in its own session. Both log to update.log."""
    import subprocess, time
    root = Path(__file__).resolve().parents[1]
    # Single-flight: Premiere AND After Effects auto-update on open; two updaters
    # doing git pull + pip into the same venv concurrently corrupt each other.
    # The second caller just gets started:true and watches the same bar.
    global _UPDATE_STARTED_AT
    try:
        # Must outlive the update itself: a first run downloads multi-GB AI
        # wheels and routinely passes 20 minutes. A 10-minute window let a
        # second panel start a CONCURRENT pip into the same venv.
        # Unless the updater reported FAIL: it has stopped (a declined admin
        # prompt, a failed download), and "retry" used to get that same FAIL
        # back for the rest of the window instead of a new update.
        if time.time() - _UPDATE_STARTED_AT < 2400 and not update_progress().get("failed"):
            return {"ok": True, "started": True, "already": True, "from_version": __version__}
    except NameError:
        pass
    _UPDATE_STARTED_AT = time.time()
    try:
        # reset the progress file the updater will write ("pct|message")
        try:
            _SLOT_DIR.mkdir(parents=True, exist_ok=True)
            (_SLOT_DIR / "update_progress").write_text("2|Starting the update")
        except Exception:
            pass
        log = _SLOT_DIR / "update.log"
        if os.name == "nt":
            upd = root / "windows" / "update-windows.cmd"
            if not upd.exists():
                return JSONResponse({"ok": False, "error": "updater not found"}, status_code=404)
            # /silent: hidden elevated window, no pause — the PANEL is the UI.
            # Log on Windows too — every failure there used to vanish with the
            # hidden window, leaving nothing to diagnose.
            # Never DETACHED_PROCESS: cmd then has no console, so every console
            # program it starts (the PowerShell steps, pip) gets a new VISIBLE
            # console as its output instead of the log — update.log only ever
            # held cmd's own echo lines. CREATE_NO_WINDOW gives cmd a console
            # with no window; the steps share it and inherit the log. The
            # updater still outlives this engine: it is not tied to its parent's
            # life, and killing the engine leaves the updater's console alone.
            with open(log, "ab") as lf:
                subprocess.Popen(["cmd", "/c", str(upd), "/silent"], cwd=str(root),
                                 stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                                 creationflags=(0x08000000 | 0x00000200))
            #                CREATE_NO_WINDOW | NEW_PROCESS_GROUP
        else:
            upd = root / "update.command"
            if not upd.exists():
                return JSONResponse({"ok": False, "error": "updater not found"}, status_code=404)
            # No Terminal window: the panel's own bar is the UI. Output -> log.
            with open(log, "ab") as lf:
                subprocess.Popen(["/bin/bash", str(upd)], cwd=str(root),
                                 stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        return {"ok": True, "started": True, "from_version": __version__}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/download/{rid}")
def download(rid: str):
    path = _RESULTS.get(rid)
    if not path or not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, filename="colourMatik.cube", media_type="text/plain")


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>colourMatik</title>
<style>
:root{--bg:#0e0f13;--card:#171922;--line:#262a36;--fg:#e8eaf0;--mut:#9aa3b2;--acc:#5b8cff;--good:#39d98a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:26px;margin:0 0 2px;letter-spacing:.3px}
h1 b{color:var(--acc)}.sub{color:var(--mut);margin:0 0 26px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.drop{background:var(--card);border:1.5px dashed var(--line);border-radius:14px;padding:22px;
text-align:center;cursor:pointer;transition:.15s}
.drop:hover,.drop.hot{border-color:var(--acc);background:#1b1e2b}
.drop .t{font-weight:600}.drop .h{color:var(--mut);font-size:13px;margin-top:4px}
.drop .f{margin-top:10px;color:var(--good);font-size:13px;word-break:break-all;min-height:18px}
.tag{display:inline-block;font-size:12px;color:var(--mut);border:1px solid var(--line);
border-radius:999px;padding:2px 10px;margin-bottom:8px}
.opts{display:flex;gap:20px;flex-wrap:wrap;align-items:center;margin:22px 0}
.opts label{color:var(--mut);cursor:pointer}.opts input{accent-color:var(--acc);margin-right:6px}
button.go{background:var(--acc);color:#fff;border:0;border-radius:12px;padding:13px 26px;
font-size:16px;font-weight:600;cursor:pointer}button.go:disabled{opacity:.5;cursor:default}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin-top:22px}
.hidden{display:none}.row{display:flex;gap:26px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.stat .n{font-size:30px;font-weight:700}.stat.good .n{color:var(--good)}
.stat .l{color:var(--mut);font-size:12px}
img.prev{width:100%;border-radius:10px;border:1px solid var(--line)}
pre{background:#0b0c10;border:1px solid var(--line);border-radius:10px;padding:14px;overflow:auto;
color:var(--mut);font-size:12.5px}
a.dl{display:inline-block;background:var(--good);color:#062;border-radius:12px;padding:12px 22px;
font-weight:700;text-decoration:none;margin-top:6px}
.note{color:var(--mut);font-size:13px;margin-top:14px;line-height:1.7}
.spin{width:20px;height:20px;border:3px solid #fff5;border-top-color:#fff;border-radius:50%;
display:inline-block;vertical-align:-4px;margin-right:8px;animation:s .7s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
</style></head><body><div class="wrap">
<h1><b>colour</b>Matik</h1>
<p class="sub">Match your source clip's colours to a reference. Measured accuracy (ΔE00), fully on your machine.</p>
<div class="grid">
  <div><span class="tag">REFERENCE — THE LOOK TO COPY</span>
    <div class="drop" id="dropRef"><div class="t">Reference clip</div>
    <div class="h">The clip whose colours we sample</div><div class="f" id="fRef"></div>
    <input type="file" id="inRef" accept="video/*,image/*" class="hidden"></div></div>
  <div><span class="tag">SOURCE — RECOLOUR THIS</span>
    <div class="drop" id="dropSrc"><div class="t">Source clip</div>
    <div class="h">The clip we recolour</div><div class="f" id="fSrc"></div>
    <input type="file" id="inSrc" accept="video/*,image/*" class="hidden"></div></div>
</div>
<div class="opts">
  <span style="color:var(--mut)">These two clips are:</span>
  <label><input type="radio" name="mode" value="different" checked>Different scene</label>
  <label><input type="radio" name="mode" value="same">Same scene (aligned)</label>
</div>
<button class="go" id="go" disabled>Match Colours</button>
<div class="card hidden" id="result"></div>
<div style="margin-top:28px;text-align:center;color:var(--mut);font-size:12px;border-top:1px solid var(--line);padding-top:16px">
  by <b style="color:var(--fg)">Sevki Bugra Ozbek</b> · <a href="https://catheadai.com" style="color:var(--acc);text-decoration:none">catheadai.com</a>
</div>
<script>
const $=s=>document.querySelector(s);let fRef=null,fSrc=null;
function wire(drop,input,label,set){const d=$(drop),i=$(input);
 d.onclick=()=>i.click();
 d.ondragover=e=>{e.preventDefault();d.classList.add('hot')};
 d.ondragleave=()=>d.classList.remove('hot');
 d.ondrop=e=>{e.preventDefault();d.classList.remove('hot');if(e.dataTransfer.files[0]){i.files=e.dataTransfer.files;pick()}};
 i.onchange=pick;
 function pick(){const f=i.files[0];if(f){$(label).textContent='✓ '+f.name;set(f);check()}}}
wire('#dropRef','#inRef','#fRef',f=>fRef=f);
wire('#dropSrc','#inSrc','#fSrc',f=>fSrc=f);
function check(){$('#go').disabled=!(fRef&&fSrc)}
$('#go').onclick=async()=>{
 const btn=$('#go');btn.disabled=true;btn.innerHTML='<span class=spin></span>Matching…';
 const r=$('#result');r.classList.remove('hidden');r.innerHTML='<p class=note>Extracting frames, choosing the best method…</p>';
 const fd=new FormData();fd.append('reference',fRef);fd.append('source',fSrc);
 fd.append('mode',document.querySelector('input[name=mode]:checked').value);
 try{const res=await fetch('/match',{method:'POST',body:fd});const j=await res.json();
  if(!j.ok){r.innerHTML='<p class=note style="color:#ff6b6b">Error: '+j.error+'</p>';}
  else{render(j);}
 }catch(e){r.innerHTML='<p class=note style="color:#ff6b6b">Error: '+e+'</p>';}
 btn.disabled=false;btn.textContent='Match Colours';
};
function render(j){const v=x=>x==null?'—':x.toFixed(2);
 const acc=j.de_after!=null?(j.de_after<2?'good':''):'';
 let stats=j.de_after!=null?`
   <div class="stat"><div class="n">${v(j.de_before)}</div><div class="l">BEFORE ΔE00</div></div>
   <div class="stat ${acc}"><div class="n">${v(j.de_after)}</div><div class="l">AFTER ΔE00 ${j.de_after<2?'(imperceptible)':''}</div></div>`
   :`<div class="stat"><div class="l">Distribution matched (method: ${j.method}). Cross-scene pixel-ΔE isn't defined — judge by the preview.</div></div>`;
 $('#result').innerHTML=`
   <div class="row">${stats}<div class="stat"><div class="n">${j.method}</div><div class="l">METHOD</div></div></div>
   <img class="prev" src="${j.preview}">
   <p class="note"><b>Next steps:</b><br>1) Download the <b>.cube</b> below.<br>
   2) In Premiere select your source clip → <b>Lumetri Color ▸ Basic Correction ▸ Input LUT ▸ Browse…</b> → pick this .cube.<br>
   3) The colours snap to the reference. Fine-tune on top if you like.</p>
   <a class="dl" href="${j.download}" download>⬇︎ Download colourMatik.cube</a>
   <details style="margin-top:16px"><summary style="color:var(--mut);cursor:pointer">Technical report</summary><pre>${j.report}</pre></details>`;
}
</script>
</div></body></html>"""


def run(host: str = "127.0.0.1", port: int = 8765):
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()
