/* colourMatik — Premiere Pro UXP panel.
 * Match the SOURCE clip's colours to a REFERENCE, apply Lumetri to the source,
 * and dial the strength with a live intensity slider.
 * Engine: local FastAPI at http://127.0.0.1:8765 (run ./colourmatik-app).
 */
const ppro = require("premierepro");
const uxp = require("uxp");

const SERVER = "http://127.0.0.1:8765";
const DEFAULT_INTENSITY = 100;   // 100 = the exact computed match; slider dials 0–200 live
const LOCAL_VERSION = "1.7.8";

/* fetch with a hard timeout — a wedged engine must never freeze the panel */
async function fetchT(url, opts, ms) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), ms);
  try { return await fetch(url, { ...(opts || {}), signal: ctrl.signal }); }
  finally { clearTimeout(t); }
}
// Update checks read version.json straight from the GitHub repo (always hosted,
// CORS-friendly). Bump version.json + this constant together on each release.
const UPDATE_URL = "https://raw.githubusercontent.com/burskozbekov/colourMatik/main/version.json";
const SITE_URL = "https://catheadai.com";

const $ = (id) => document.getElementById(id);
const state = { refPath: null, srcPath: null, srcTrackItem: null, rid: null, slot: null,
                refIn: null, refOut: null, srcIn: null, srcOut: null };
let bakeTimer = null;

function setStatus(stateLabel, msg, kind) {
  $("status-state").textContent = stateLabel;
  $("status-msg").textContent = msg;
  $("status").className = kind || "idle";
}

function currentMode() {
  return $("mode-same").classList.contains("selected") ? "same" : "different";
}

function currentLook() { return "exact"; }   // Cinematic AI removed - accurate matching only

function refreshRun() {
  $("run").disabled = !(state.refPath && state.srcPath) || _updating || _matchingAll;
  try { refreshRunAll(); } catch (e) {}
}

/* ---- Match & Apply loading bar (fed by the engine's /progress) ------------- */
let _progPoll = null, _progTick = null, _progReset = null, _chamTick = null, _dispPct = 0, _srvPct = 0, _srvMsg = "";
/* The bar is a PICTURE the engine draws (green fill + walking chameleon frame,
 * composed with PIL at /bardata). Every layout technique we handed this host —
 * stylesheet rules, inline pixel geometry, flex rows — was mis-rendered, but a
 * data-URL image at natural size always drew correctly. So the panel owns ONE
 * <img> and asks the engine for the next frame; nothing is laid out here. */
const CHAM_FRAMES = 18;
let _chamFrame = 0, _barPct = 0, _barBusy = false;
function _chamShow(on) {
  const i = $("prog-img");
  if (i) i.style.display = on ? "block" : "none";
}
async function _chamStep() {
  const i = $("prog-img");
  if (!i || _barBusy) return;
  _barBusy = true;
  _chamFrame = (_chamFrame + 1) % CHAM_FRAMES;
  try {
    const r = await fetchT(SERVER + "/bardata?pct=" + _barPct.toFixed(2) + "&f=" + _chamFrame, { cache: "no-cache" }, 2500);
    const j = await r.json();
    if (j && j.img) i.src = j.img;
  } catch (e) {} finally { _barBusy = false; }
}
function _paintFill(pct) { _barPct = Math.max(0, Math.min(1, pct)); }
function _chamPlace(pct) { _paintFill(pct); }
/* One-shot report of what the host ACTUALLY laid out, so a failure is diagnosed
 * from measurements instead of guesses. */
let _diagSent = false;
function _sendDiag() {
  if (_diagSent) return;
  _diagSent = true;
  try {
    const g = (id) => { const e = $(id); return e ? { w: e.offsetWidth, h: e.offsetHeight } : null; };
    fetchT(SERVER + "/diag", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host: "premiere", version: LOCAL_VERSION,
        bar: g("prog-img"),
        btn: g("run") }) }, 4000).catch(() => {});
  } catch (e) {}
}
function _paintProg() {
  _paintFill(_dispPct);
  _chamPlace(_dispPct);
  $("run-label").textContent = Math.round(_dispPct * 100) + "%" + (_srvMsg ? "  ·  " + _srvMsg : "");
}
function startProgress(jobId) {
  // defensively clear anything a previous run left behind (intervals must never orphan)
  if (_progPoll) clearInterval(_progPoll);
  if (_progTick) clearInterval(_progTick);
  if (_chamTick) clearInterval(_chamTick);
  if (_progReset) { clearTimeout(_progReset); _progReset = null; }
  _dispPct = 0; _srvPct = 0; _srvMsg = "Starting";
  $("run").classList.add("loading");
  let polling = false;                       // don't stack requests on a slow engine
  _progPoll = setInterval(async () => {
    if (polling) return;
    polling = true;
    try {
      const r = await fetchT(SERVER + "/progress/" + jobId, { cache: "no-cache" }, 4000);
      const j = await r.json();
      if (typeof j.pct === "number") { _srvPct = j.pct; if (j.msg) _srvMsg = j.msg; }
    } catch (e) {} finally { polling = false; }
  }, 500);
  // Smoothly ease toward the server value, and gently creep forward within a
  // stage so the bar never looks frozen during the long AI steps.
  _chamShow(true);
  _chamTick = setInterval(_chamStep, 140);          // the walk itself
  setTimeout(_sendDiag, 900);                      // report the real layout once
  _progTick = setInterval(() => {
    const soft = Math.min(0.97, _srvPct + 0.10);
    const target = Math.max(_srvPct, Math.min(soft, _dispPct + 0.006));
    _dispPct += (target - _dispPct) * 0.25;
    _paintProg();
  }, 120);
  _paintProg();
}
function stopProgress(done) {
  if (_progPoll) clearInterval(_progPoll);
  if (_progTick) clearInterval(_progTick);
  if (_chamTick) clearInterval(_chamTick);
  _progPoll = _progTick = _chamTick = null;
  if (!done) _chamShow(false);
  if (done) { _dispPct = 1; _srvMsg = "Done"; _paintProg(); }
  if (_progReset) clearTimeout(_progReset);
  _progReset = setTimeout(() => {
    _progReset = null;
    $("run").classList.remove("loading");
    _paintFill(0);
    _chamShow(false);
    $("run-label").textContent = "MATCH & APPLY";
  }, done ? 450 : 0);
}
function newJobId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

/* ---- Read the selected clip: timeline first (so we can grab the track item
 * for applying an effect AND its used in/out segment), then the bin (path only). */
async function getSelected() {
  const project = await ppro.Project.getActiveProject();
  if (!project) throw new Error("No project is open.");

  const seq = await project.getActiveSequence();
  if (seq) {
    const tsel = await seq.getSelection();
    const clips = tsel ? await tsel.getTrackItems() : [];
    for (const c of clips) {
      // Skip AUDIO track items of linked clips when the API can tell us — applying
      // a video effect to an audio component chain fails cryptically. When the
      // media-type API is unavailable, fall back to the old duck-typing unchanged.
      try {
        if (typeof c.getMediaType === "function" && ppro.Constants && ppro.Constants.MediaType
            && ppro.Constants.MediaType.AUDIO !== undefined) {
          const mt = await c.getMediaType();
          if (String(mt) === String(ppro.Constants.MediaType.AUDIO)) continue;
        }
      } catch (e) {}
      if (typeof c.getComponentChain === "function") {      // video clip on the timeline
        const pi = await c.getProjectItem();
        const clip = ppro.ClipProjectItem.cast(pi);
        const p = clip ? await clip.getMediaFilePath() : null;
        if (p) {
          // The segment actually used in the edit (source-media seconds). Long
          // source files hold many scenes — sampling only this range is what
          // makes the match reflect the shot you're grading.
          let inS = null, outS = null, atS = null;
          try {
            const ti = await c.getInPoint();
            const to = await c.getOutPoint();
            if (ti && typeof ti.seconds === "number" && isFinite(ti.seconds)) inS = ti.seconds;
            if (to && typeof to.seconds === "number" && isFinite(to.seconds)) outS = to.seconds;
            if (inS != null && outS != null && outS - inS < 0.04) { inS = null; outS = null; }
          } catch (e) {}
          // "Take the colour from the frame I am showing": if the playhead sits
          // on this clip, capture the exact SOURCE time under it.
          try {
            const pos = await seq.getPlayerPosition();
            const posS = pos && typeof pos.seconds === "number" ? pos.seconds
                        : (typeof pos === "number" ? pos / 254016000000 : null);
            const ts = await c.getStartTime();
            const te = await c.getEndTime();
            const startS = ts && typeof ts.seconds === "number" ? ts.seconds : null;
            const endS = te && typeof te.seconds === "number" ? te.seconds : null;
            if (posS != null && startS != null && endS != null &&
                posS >= startS && posS < endS && inS != null) {
              // SEQUENCE seconds x clip speed = SOURCE seconds. A 119.54% clip
              // sampled without this reads a frame ~20% earlier than the one on
              // screen (field case) - the whole match then describes the wrong
              // moment. Speed unavailable or remapped/reversed -> drop the
              // playhead capture entirely and let the in/out range sampling
              // (which is speed-agnostic) take over.
              let speed = 1.0;
              try {
                if (typeof c.getSpeed === "function") {
                  const sp = await c.getSpeed();
                  const v = (sp && typeof sp.value === "number") ? sp.value
                          : (typeof sp === "number" ? sp : null);
                  if (v != null && isFinite(v)) speed = v;
                }
              } catch (e) {}
              if (speed > 0) {
                atS = inS + (posS - startS) * speed;
                // Clamp inside the used segment: a slowed clip's sequence
                // offset can otherwise run past the out-point into leader /
                // the next scene of the source file.
                if (outS != null && atS > outS - 0.02) atS = Math.max(inS, outS - 0.02);
              }
              // speed <= 0 (reversed) -> atS stays null: range sampling instead.
            }
          } catch (e) {}
          return { path: p, trackItem: c, inS, outS, atS };
        }
      }
    }
  }
  const sel = await ppro.ProjectUtils.getSelection(project);
  const items = sel ? await sel.getItems() : [];
  for (const it of items) {
    const clip = ppro.ClipProjectItem.cast(it);
    if (clip) { const p = await clip.getMediaFilePath(); if (p) return { path: p, trackItem: null, inS: null, outS: null }; }
  }
  return { path: null, trackItem: null, inS: null, outS: null };
}

function baseName(p) { return p ? p.split(/[\\/]/).pop() : ""; }   // mac + windows paths

async function captureRef() {
  if (_matchingAll) return setStatus("MATCH ALL", "Batch matching is running — wait for it to finish.", "busy");
  try {
    const s = await getSelected();
    if (!s.path) return setStatus("SELECT", "Select the reference clip (bin or timeline), then click again.", "error");
    state.refPath = s.path;
    state.refIn = s.inS; state.refOut = s.outS;
    state.refAt = (typeof s.atS === "number") ? s.atS : null;
    $("refName").textContent = baseName(s.path);
    $("refName").className = "slot-name set";
    refreshRun();
    setStatus("READY", "Reference set. Now pick the target clip.", "idle");
  } catch (e) { setStatus("ERROR", String(e.message || e), "error"); }
}

async function captureSrc() {
  if (_matchingAll) return setStatus("MATCH ALL", "Batch matching is running — wait for it to finish.", "busy");
  try {
    const s = await getSelected();
    if (!s.path) return setStatus("SELECT", "Select the target clip on the timeline, then click again.", "error");
    state.srcPath = s.path;
    state.srcTrackItem = s.trackItem;   // needed to apply the effect
    state.srcIn = s.inS; state.srcOut = s.outS;
    state.srcAt = (typeof s.atS === "number") ? s.atS : null;
    state.slot = null;                  // a new target invalidates the prior match's slot
    $("intensity-section").className = "section hidden";   // intensity inert until a fresh match
    $("srcName").textContent = baseName(s.path) + (s.trackItem ? "" : "  (not on timeline)");
    $("srcName").className = "slot-name set";
    refreshRun();
    setStatus("READY", "Target set. Match & Apply when ready.", "idle");
  } catch (e) { setStatus("ERROR", String(e.message || e), "error"); }
}

/* ---- Auto-apply the match as our native "colourMatik" effect --------------
 * The engine bakes the accurate 33^3 LUT into a slot file (/effect_lut); we add
 * the colourMatik effect to the target clip and point its "Match Slot" param at
 * that slot. The effect's own Intensity param dials strength live (0..200%), so
 * intensity changes never touch the engine — instant, and inside the effect. -
 * API notes: getComponentCount/getComponentAtIndex/getParamCount/getParam/
 * displayName are SYNC (inside lockedAccess); getMatchName is ASYNC (await
 * OUTSIDE the lock); writes go createKeyframe(raw)->createSetValueAction(kf,true). */
const CM_RE = /colourMatik/i;   // Premiere exposes it as "AE.catheadai colourMatik"

async function findEffect(project, chain) {
  const comps = [];
  project.lockedAccess(() => { const n = chain.getComponentCount(); for (let i = 0; i < n; i++) comps.push(chain.getComponentAtIndex(i)); });
  for (const c of comps) { let mn = ""; try { mn = await c.getMatchName(); } catch (e) {} if (CM_RE.test(mn)) return c; }
  return null;
}

async function ensureEffect(project, trackItem) {
  const chain = await trackItem.getComponentChain();
  let fx = await findEffect(project, chain);
  if (fx) return fx;
  const names = await ppro.VideoFilterFactory.getMatchNames();
  const mn = names.find((n) => CM_RE.test(n));
  if (!mn) throw new Error("colourMatik effect not installed — restart Premiere once after installing it.");
  const comp = await ppro.VideoFilterFactory.createComponent(mn);
  project.lockedAccess(() => { project.executeTransaction((ca) => ca.addAction(chain.createAppendComponentAction(comp)), "colourMatik: add effect"); });
  return await findEffect(project, chain);
}

function setEffectParams(project, fx, values) {
  let ok = false;
  project.lockedAccess(() => {
    const pmap = {};
    const pc = fx.getParamCount();
    for (let i = 0; i < pc; i++) { const p = fx.getParam(i); const dn = (p && p.displayName) || ""; if (dn && !(dn in pmap)) pmap[dn] = p; }
    ok = project.executeTransaction((ca) => {
      for (const [name, raw] of Object.entries(values)) { const p = pmap[name]; if (!p) continue; ca.addAction(p.createSetValueAction(p.createKeyframe(raw), true)); }
    }, "colourMatik: set params");
  });
  return ok;
}

async function applyEffect(trackItem, slot, intensityPct) {
  const project = await ppro.Project.getActiveProject();
  const fx = await ensureEffect(project, trackItem);
  if (!fx) throw new Error("couldn't add the colourMatik effect");
  const values = { "Match Slot": slot, "Intensity": intensityPct };
  if (!setEffectParams(project, fx, values)) throw new Error("couldn't set effect params");
  return true;
}

/* ---- Match & Apply -------------------------------------------------------- */
let _runGen = 0;   // generation token: a newer run() invalidates an older one's cleanup
let _matchBusy = false;   // a match is in flight (draft or full)
async function run() {
  if (_updating) return setStatus("UPDATE", "Updating colourMatik — matching is paused until it finishes.", "busy");
  if (_matchingAll) return setStatus("MATCH ALL", "Batch matching is running — wait for it to finish.", "busy");
  const gen = ++_runGen;
  _matchBusy = true;
  // Snapshot the target NOW. The match takes seconds to minutes and the user can
  // re-capture meanwhile (the eyedropper stays live) — this run must apply to the
  // clip it was started for, not whatever was captured last.
  const tgt = {
    srcPath: state.srcPath, refPath: state.refPath, trackItem: state.srcTrackItem,
    srcIn: state.srcIn, srcOut: state.srcOut, refIn: state.refIn, refOut: state.refOut,
    srcAt: (typeof state.srcAt === "number") ? state.srcAt : null,
    refAt: (typeof state.refAt === "number") ? state.refAt : null,
    // Freeze mode/tf too: the draft phase runs for seconds, and flipping the
    // SCENE toggle or INPUT FORMAT mid-run used to make the hot-swapped full
    // result compute under DIFFERENT settings than the draft on the clip.
    mode: currentMode(), tf: ($("tf") && $("tf").value) || "sRGB", look: currentLook(),
  };
  $("run").disabled = true;
  $("preview").className = "hidden";
  $("alts").className = "hidden"; $("alts").innerHTML = "";
  $("wipe").className = "hidden";
  resetAxes();
  state.slot = null;                 // mid-match intensity drags must no-op, not apply the old LUT
  $("intensity-section").className = "section hidden";
  setStatus("MATCHING", "Working — this takes a few seconds…", "busy");
  const jobId = newJobId();
  startProgress(jobId);
  let ok = false;
  let draftApplied = false;   // a draft LUT is live on the clip and must never outlive an error silently
  // INSTANT DRAFT: a 2-5s cheap match applied immediately, so the user sees the
  // look right away while the full contest (AI included) refines in the
  // background and hot-swaps the result. Any failure here is silently ignored -
  // the full pass below is the source of truth.
  try {
    const dres = await fetchT(SERVER + "/match_paths", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_path: tgt.srcPath, reference_path: tgt.refPath,
        mode: tgt.mode, tf: tgt.tf, frames: 3, look: tgt.look,
        source_in: tgt.srcIn ?? null, source_out: tgt.srcOut ?? null,
        reference_in: tgt.refIn ?? null, reference_out: tgt.refOut ?? null,
        source_at: tgt.srcAt, reference_at: tgt.refAt,
        fast: true,
      }),
    }, 45000);
    const dj = await dres.json();
    if (dj && dj.ok && gen === _runGen && tgt.trackItem) {
      const der = await fetchT(SERVER + "/effect_lut", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rid: dj.rid }) }, 15000);
      const de = await der.json();
      if (de && de.ok && gen === _runGen) {
        state.slot = de.slot;
        try {
          await applyEffect(tgt.trackItem, de.slot, DEFAULT_INTENSITY);
          draftApplied = true;
          setStatus("DRAFT", "Draft look applied - refining in the background...", "busy");
        } catch (e) {}
      }
    }
  } catch (e) {}
  if (gen !== _runGen) return;
  try {
    let res;
    try {
      // generous timeout: AI matches can take a while, but a wedged engine must
      // never leave the button dead forever
      res = await fetchT(SERVER + "/match_paths", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_path: tgt.srcPath, reference_path: tgt.refPath,
          mode: tgt.mode, tf: tgt.tf, frames: 7, look: tgt.look,
          source_in: tgt.srcIn ?? null, source_out: tgt.srcOut ?? null,
          reference_in: tgt.refIn ?? null, reference_out: tgt.refOut ?? null,
          source_at: tgt.srcAt, reference_at: tgt.refAt,
          job_id: jobId,
        }),
      }, 300000);
    } catch (netErr) {
      // Distinguish "we gave up waiting" from "nothing is listening" — the match may
      // well still be running in the engine after our 5-minute client timeout.
      if (netErr && netErr.name === "AbortError")
        throw new Error("The match is taking longer than 5 minutes — the engine may still be working. Try again with shorter clips, or wait and re-run.");
      throw new Error("Can't reach the engine at " + SERVER + " — start it with ./colourmatik-app");
    }
    const j = await res.json().catch(() => ({ ok: false, error: "HTTP " + res.status }));
    if (!j.ok) throw new Error(j.error || ("HTTP " + res.status));
    if (gen !== _runGen) return;     // a newer run took over — leave its UI alone
    ok = true;
    // The match (the slow part the bar tracks) is done — finish the bar NOW and
    // stop its polling timers before the quick apply steps, so nothing keeps
    // spinning while we add the effect. Re-enable the button immediately too, so
    // even if the UXP apply call resolves late the panel is never wedged.
    stopProgress(true);
    refreshRun();

    state.rid = j.rid;
    $("preview-img").src = j.preview;
    $("preview").className = "";

    // bake the accurate LUT into a fresh slot for the native colourMatik effect
    let ej = { ok: false };
    try {
      const eres = await fetchT(SERVER + "/effect_lut", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rid: j.rid }) }, 30000);
      ej = await eres.json().catch(() => ({ ok: false }));
    } catch (e) {}
    if (gen !== _runGen) return;
    state.slot = ej.ok ? ej.slot : null;
    const slot = state.slot;
    // reveal Intensity only now that the slot is known, so the slider is never
    // live-but-dead — and only if the user hasn't re-targeted meanwhile (the
    // slider drives state.srcTrackItem, which may no longer be this run's clip)
    if (slot != null && tgt.trackItem && state.srcTrackItem === tgt.trackItem) {
      $("intensity-section").className = "section";
      $("intensity").value = DEFAULT_INTENSITY;
      $("intensity-val").textContent = DEFAULT_INTENSITY + "%";
    }
    if (slot != null) { loadAlts(j.rid, tgt, gen); loadWipe(j.rid, gen); }   // looks + A/B wipe

    const label = j.method_label || j.method;
    const mTxt = j.ai_used ? `${label} 🧠` : label;   // brain = local AI chose the match
    const deTxt = (j.de_after != null) ? `  ΔE00 ${Number(j.de_after).toFixed(2)}` : "";
    if (!tgt.trackItem) {
      setStatus("DONE", `Matched — ${mTxt}${deTxt}. Select the TARGET clip on the timeline and Match & Apply again to auto-apply.`, "done");
    } else if (slot == null) {
      // The full winner could not be baked: never leave the un-previewed DRAFT
      // grade silently on the clip (the preview above shows the FULL winner -
      // the timeline would render a different LUT than the panel displays).
      if (draftApplied) { try { await applyEffect(tgt.trackItem, 0, DEFAULT_INTENSITY); } catch (e) {} }
      setStatus("ERROR", "Match ok but the LUT slot couldn't be written — the draft grade was removed; re-run Match & Apply.", "error");
    } else {
      try {
        // The UXP apply call can occasionally resolve late; never let it wedge the
        // panel — if it doesn't return in a few seconds, move on (the effect is
        // already added by then) so the button re-enables for the next clip.
        // The .catch() keeps a post-timeout rejection from surfacing as unhandled.
        const applying = applyEffect(tgt.trackItem, slot, DEFAULT_INTENSITY);
        // If this call settles LATE (after the 6s race): on success, re-assert
        // the user's CURRENT choice so the stale apply can't silently revert
        // it; on FAILURE, downgrade the optimistic DONE we printed at timeout —
        // otherwise the status says "applied" while the clip has the draft (or
        // nothing).
        applying.then(() => {
          if (state.slot != null && state.slot !== slot && tgt.trackItem) {
            const pct = parseInt($("intensity").value, 10) || DEFAULT_INTENSITY;
            applyEffect(tgt.trackItem, state.slot, pct).catch(() => {});
          }
        }, () => {
          if (gen === _runGen)
            setStatus("ERROR", "The effect could not be applied to the clip — re-run Match & Apply.", "error");
        }).catch(() => {});
        await Promise.race([
          applying,
          new Promise((_, rej) => setTimeout(() => rej(new Error("apply-timeout")), 6000)),
        ]);
        if (gen !== _runGen) return;   // a newer run owns the status line now
        setStatus("DONE", `Matched — ${mTxt}${deTxt}. colourMatik effect applied — drag Intensity to adjust (live).`, "done");
      } catch (e) {
        if (gen !== _runGen) return;
        if (String(e.message) === "apply-timeout")
          setStatus("DONE", `Matched — ${mTxt}${deTxt}. colourMatik effect applied. Drag Intensity to adjust (live).`, "done");
        else
          setStatus("ERROR", "Apply failed: " + (e.message || e), "error");
      }
    }
  } catch (e) {
    // Full pass failed after a draft was applied: roll the effect back to slot 0
    // (identity) so the clip is never silently left graded by the cheap 3-frame
    // draft nobody previewed or approved.
    if (draftApplied && tgt.trackItem) {
      try { applyEffect(tgt.trackItem, 0, DEFAULT_INTENSITY).catch(() => {}); } catch (e2) {}
      state.slot = null;
    }
    if (gen === _runGen) setStatus("ERROR", String(e.message || e) + (draftApplied ? " (draft grade removed)" : ""), "error");
  } finally {
    // only the newest run may touch the shared bar/button state
    if (gen === _runGen) {
      _matchBusy = false;
      if (!ok) stopProgress(false);   // error before the match resolved -> clear the bar
      refreshRun();
    }
  }
}

/* ---- Reference gallery + A/B wipe ----------------------------------------- */
async function loadLibrary() {
  try {
    const r = await fetchT(SERVER + "/library_list", { cache: "no-cache" }, 8000);
    const j = await r.json();
    const box = $("library");
    if (!j.ok || !j.items || !j.items.length) { box.className = "hidden"; box.innerHTML = ""; return; }
    box.innerHTML = "";
    for (const it of j.items) {
      const d = document.createElement("div");
      d.className = "lib-item"; d.title = it.name;
      d.innerHTML = '<img src="' + it.thumb + '"><span class="lib-x">&times;</span>';
      d.addEventListener("click", () => {
        if (_matchingAll) return;               // batch params are frozen
        // refAt must reset too: a stale playhead time from a previously
        // eyedropped TIMELINE reference would seek that second in the gallery
        // asset and match one arbitrary frame nobody chose.
        state.refPath = it.path; state.refIn = null; state.refOut = null; state.refAt = null;
        $("refName").textContent = it.name; $("refName").className = "slot-name set";
        refreshRun();
        setStatus("READY", "Reference set from the gallery.", "idle");
      });
      d.querySelector(".lib-x").addEventListener("click", async (ev) => {
        ev.stopPropagation();
        try { await fetchT(SERVER + "/library_del", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: it.id }) }, 8000); } catch (e) {}
        loadLibrary();
      });
      box.appendChild(d);
    }
    box.className = "";
  } catch (e) {}
}
async function saveRefToLibrary() {
  if (!state.refPath) return setStatus("SELECT", "Pick a reference first, then save it.", "error");
  try {
    const r = await fetchT(SERVER + "/library_add", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.refPath }) }, 20000);
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "save failed");
    setStatus("SAVED", "Reference saved to the gallery.", "done");
    loadLibrary();
  } catch (e) { setStatus("ERROR", "Couldn't save: " + (e.message || e), "error"); }
}
function _wipeSet(pct) {
  pct = Math.max(2, Math.min(98, pct));
  $("wipe-top").style.width = pct + "%";
  $("wipe-bar").style.left = pct + "%";
}
async function loadWipe(rid, gen) {
  try {
    const r = await fetchT(SERVER + "/wipe/" + rid, { cache: "no-cache" }, 20000);
    const j = await r.json();
    if (!j.ok || gen !== _runGen) return;
    $("wipe-before").src = j.before;
    const wa = $("wipe-after");
    wa.src = j.after;
    const wrap = $("wipe-wrap");
    const size = () => { wa.style.width = wrap.clientWidth + "px"; };
    wa.onload = size; size();
    $("wipe").className = "";
    _wipeSet(50);
    if (!wrap._wired) {
      wrap._wired = true;
      const move = (ev) => {
        const b = wrap.getBoundingClientRect();
        _wipeSet(((ev.clientX - b.left) / Math.max(1, b.width)) * 100);
      };
      wrap.addEventListener("pointerdown", (ev) => { move(ev); wrap.setPointerCapture(ev.pointerId); wrap._drag = true; });
      wrap.addEventListener("pointermove", (ev) => { if (wrap._drag) move(ev); });
      wrap.addEventListener("pointerup", () => { wrap._drag = false; });
    }
  } catch (e) {}
}

/* ---- MATCH ALL: every clip on the timeline, grouped, one LUT per group ---- */
let _matchingAll = false;
function refreshRunAll() { $("run-all").disabled = !(state.refPath) || _matchingAll || _updating; }
async function getAllVideoClips() {
  const project = await ppro.Project.getActiveProject();
  if (!project) throw new Error("No project is open.");
  const seq = await project.getActiveSequence();
  if (!seq) throw new Error("Open a sequence first.");
  const clips = [];
  let trackCount = 0;
  try { trackCount = await seq.getVideoTrackCount(); } catch (e) { trackCount = 0; }
  for (let ti = 0; ti < trackCount; ti++) {
    let track = null;
    try { track = await seq.getVideoTrack(ti); } catch (e) { continue; }
    if (!track) continue;
    let items = [];
    try {
      if (typeof track.getTrackItems === "function") {
        // 1 = CLIP track items; second arg: include empty
        items = await track.getTrackItems(1, false);
      }
    } catch (e) { items = []; }
    for (const c of items || []) {
      try {
        if (typeof c.getComponentChain !== "function") continue;   // not a video clip
        const pi = await c.getProjectItem();
        const clip = ppro.ClipProjectItem.cast(pi);
        const p = clip ? await clip.getMediaFilePath() : null;
        if (!p) continue;
        let inS = null, outS = null;
        try {
          const a = await c.getInPoint(); const b = await c.getOutPoint();
          if (a && isFinite(a.seconds)) inS = a.seconds;
          if (b && isFinite(b.seconds)) outS = b.seconds;
        } catch (e) {}
        clips.push({ id: clips.length, path: p, inS, outS, trackItem: c });
      } catch (e) {}
    }
  }
  return clips;
}
/* MATCH ALL button: same colour-fill + walking chameleon as the main button */
function _allBar(pct, text) {
  const b = $("run-all"), f = $("run-all-fill"), l = $("run-all-label");
  if (!f || !l) { if (b) b.textContent = text; return; }
  if (pct == null) { b.classList.remove("loading"); f.style.width = "0%"; l.textContent = text; return; }
  b.classList.add("loading");
  f.style.width = (Math.max(0, Math.min(1, pct)) * 100).toFixed(1) + "%";
  l.textContent = text;
}
async function matchAll() {
  if (_matchingAll) return;
  if (!state.refPath) return setStatus("SELECT", "Pick a REFERENCE first.", "error");
  if (_updating) return setStatus("UPDATE", "Updating — try MATCH ALL when it finishes.", "busy");
  if (_matchBusy) return setStatus("MATCHING", "A match is running — wait for it first.", "busy");
  _matchingAll = true; refreshRunAll(); refreshRun();
  // Freeze every parameter NOW: gallery clicks / toggles / tf changes during the
  // minutes-long batch must not re-aim the remaining groups.
  const B = { refPath: state.refPath, refIn: state.refIn, refOut: state.refOut,
              look: currentLook(), tf: ($("tf") && $("tf").value) || "sRGB" };
  // stale single-match controls would operate on an evicted rid — clear them
  $("alts").className = "hidden"; $("alts").innerHTML = "";
  $("wipe").className = "hidden";
  $("intensity-section").className = "section hidden";
  state.slot = null; state.rid = null;
  const btn = $("run-all");
  try {
    setStatus("SCAN", "Reading the timeline…", "busy");
    _allBar(0.03, "READING TIMELINE…");
    const clips = await getAllVideoClips();
    if (!clips.length) throw new Error("No video clips found on the active sequence (this needs Premiere's 2026 UXP timeline API).");
    _allBar(0.08, "GROUPING " + clips.length + " CLIPS…");
    const gr = await fetchT(SERVER + "/group_shots", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: clips.map(c => ({ id: c.id, path: c.path, "in": c.inS, "out": c.outS })) }) }, 120000);
    const gj = await gr.json();
    if (!gj.ok) throw new Error(gj.error || "grouping failed");
    const groups = gj.groups;
    const skipped = (gj.failed ? gj.failed.length : 0) + (gj.truncated || 0);
    let done = 0, applied = 0;
    for (const g of groups) {
      done++;
      _allBar(0.10 + 0.88 * (done - 1) / groups.length, "MATCHING GROUP " + done + "/" + groups.length + "…");
      setStatus("MATCH ALL", "Group " + done + "/" + groups.length + " (" + g.length + " clip" + (g.length > 1 ? "s" : "") + ")…", "busy");
      // longest member represents the group
      const members = g.map(id => clips[id]).filter(Boolean);
      if (!members.length) continue;
      let rep = members[0];
      for (const m of members)
        if ((m.outS || 0) - (m.inS || 0) > (rep.outS || 0) - (rep.inS || 0)) rep = m;
      try {
        const mr = await fetchT(SERVER + "/match_paths", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            source_path: rep.path, reference_path: B.refPath,
            mode: "different", tf: B.tf,
            frames: 7, look: B.look,
            source_in: rep.inS, source_out: rep.outS,
            reference_in: B.refIn, reference_out: B.refOut,
          }) }, 300000);
        const mj = await mr.json();
        if (!mj.ok) continue;
        const er = await fetchT(SERVER + "/effect_lut", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rid: mj.rid }) }, 30000);
        const ej = await er.json();
        if (!ej.ok) continue;
        for (const m of members) {
          try { await applyEffect(m.trackItem, ej.slot, DEFAULT_INTENSITY); applied++; } catch (e) {}
        }
      } catch (e) {}
    }
    _allBar(1, "DONE");
    setStatus("DONE", "Matched " + applied + "/" + clips.length + " clips in " + groups.length + " groups" +
      (skipped ? " (" + skipped + " clip" + (skipped > 1 ? "s" : "") + " skipped — offline media or over the 60-clip limit)" : "") +
      " — same-look shots share one LUT (no pops at cuts).", "done");
  } catch (e) {
    setStatus("ERROR", String(e.message || e), "error");
  } finally {
    _matchingAll = false;
    setTimeout(() => { _allBar(null, "MATCH ALL CLIPS"); refreshRunAll(); }, 1400);
    refreshRunAll();
  }
}

/* ---- Alternative looks: the contest's top candidates, clickable ----------- */
async function loadAlts(rid, tgt, gen) {
  try {
    const r = await fetchT(SERVER + "/alts/" + rid, { cache: "no-cache" }, 20000);
    const j = await r.json();
    if (!j.ok || !j.alts || j.alts.length < 2) return;   // nothing worth choosing
    if (gen !== _runGen) return;
    const box = $("alts");
    box.innerHTML = "";
    for (const a of j.alts) {
      const d = document.createElement("div");
      d.className = "alt" + (a.chosen ? " selected" : "");
      d.innerHTML = '<img src="' + a.preview + '"><div class="alt-name">' + a.label + "</div>";
      d.addEventListener("click", async () => {
        if (gen !== _runGen) return;
        try {
          const res = await fetchT(SERVER + "/effect_lut", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ rid: rid, variant: a.key }),
          }, 30000);
          const ej = await res.json();
          if (!ej.ok) throw new Error(ej.error || "variant failed");
          if (gen !== _runGen) return;          // a newer run owns the UI now
          state.slot = ej.slot;
          const pct = parseInt($("intensity").value, 10) || DEFAULT_INTENSITY;
          if (tgt.trackItem) { try { await applyEffect(tgt.trackItem, ej.slot, pct); } catch (e) {} }
          for (const el of box.children) el.className = "alt";
          d.className = "alt selected";
          resetAxes();
          loadWipe(rid, gen);
          setStatus("DONE", "Look switched to " + a.label + ".", "done");
        } catch (e) {
          setStatus("ERROR", "Couldn't switch look: " + (e.message || e), "error");
        }
      });
      box.appendChild(d);
    }
    box.className = "";
  } catch (e) {}
}

/* ---- WB / Tone / Colour strength axes ------------------------------------- */
let axesTimer = null;
function resetAxes() {
  for (const k of ["wb", "tone", "color"]) {
    const el = $("ax-" + k); if (!el) continue;
    el.value = 100; $("ax-" + k + "-val").textContent = "100";
  }
}
function onAxis() {
  for (const k of ["wb", "tone", "color"])
    $("ax-" + k + "-val").textContent = String(Math.round(parseFloat($("ax-" + k).value) || 100));
  if (axesTimer) clearTimeout(axesTimer);
  axesTimer = setTimeout(applyAxes, 300);
}
async function applyAxes() {
  if (!state.rid || state.slot == null) return;
  const gen = _runGen, rid = state.rid;      // stale completions must not re-arm
  try {
    const body = { rid: rid,
      wb: Math.round(parseFloat($("ax-wb").value) || 100) / 100,
      tone: Math.round(parseFloat($("ax-tone").value) || 100) / 100,
      color: Math.round(parseFloat($("ax-color").value) || 100) / 100 };
    const r = await fetchT(SERVER + "/effect_lut", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }, 30000);
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "strength failed");
    if (gen !== _runGen || rid !== state.rid) return;   // a new match owns the clip now
    state.slot = j.slot;
    const pct = parseInt($("intensity").value, 10) || DEFAULT_INTENSITY;
    if (state.srcTrackItem) { try { await applyEffect(state.srcTrackItem, j.slot, pct); } catch (e) {} }
    setStatus("TUNE", "WB " + $("ax-wb").value + "% · Tone " + $("ax-tone").value + "% · Color " + $("ax-color").value + "% — applied.", "busy");
  } catch (e) { setStatus("ERROR", "Strength error: " + (e.message || e), "error"); }
}

/* ---- Intensity: live re-scale of the applied Lumetri sliders --------------- */
function onIntensity() {
  const v = parseInt($("intensity").value, 10);
  $("intensity-val").textContent = v + "%";
  if (bakeTimer) clearTimeout(bakeTimer);
  bakeTimer = setTimeout(() => applyIntensity(v), 110);
}

async function applyIntensity(v) {
  if (!state.srcTrackItem || state.slot == null) return;
  try {
    await applyEffect(state.srcTrackItem, state.slot, v);   // just re-set the effect's Intensity param — no engine round-trip
    setStatus("INTENSITY", v + "% — applied live on the clip.", "busy");
  } catch (e) { setStatus("INTENSITY", "intensity error: " + (e.message || e), "error"); }
}

/* ---- Footer: updates + site ---------------------------------------------- */
function semverGt(a, b) {
  const pa = String(a).split("."), pb = String(b).split(".");
  for (let i = 0; i < 3; i++) {
    const x = +pa[i] || 0, y = +pb[i] || 0;
    if (x > y) return true; if (x < y) return false;
  }
  return false;
}
async function openUrl(u) { try { await uxp.shell.openExternal(u); } catch (e) {} }
let updateReady = false;   // first click checks; once found, the next click INSTALLS
let _updating = false;
const _sleep = (ms) => new Promise((res) => setTimeout(res, ms));
/* Self-contained in-panel update: the engine runs the updater silently (no
 * Terminal, no browser, no GitHub — ever) and writes progress to a file; we
 * drive the big button's bar from it, then detect the restarted engine's new
 * version and call it done. */
async function runSelfUpdate(fromVersion) {
  if (_updating) return;
  if (_matchBusy || _matchingAll) {             // never restart the engine mid-match
    setTimeout(() => autoUpdateCheck(), 60000);
    return;
  }
  _updating = true;
  const el = $("update-link");
  $("run").disabled = true;
  $("run").classList.add("loading");
  _chamShow(true);
  const _ub = setInterval(_chamStep, 140);   // the chameleon walks while it updates
  try {
    const r = await fetchT(SERVER + "/update_now", { method: "POST" }, 8000);
    const j = await r.json().catch(() => ({ ok: false }));
    if (!j.ok) throw new Error(j.error || "the engine couldn't start the update");
    setStatus("UPDATE", "Updating colourMatik — the bar below tracks it. On Windows, approve the admin prompt.", "busy");
    const t0 = Date.now();
    let pct = 0.02, msg = "Starting";
    while (Date.now() - t0 < 15 * 60 * 1000) {
      await _sleep(900);
      try {
        const pr = await fetchT(SERVER + "/update_progress", { cache: "no-cache" }, 2500);
        const pj = await pr.json();
        if (pj && pj.failed) throw new Error(pj.msg || "the updater reported a failure");
        if (pj && typeof pj.pct === "number") { pct = Math.max(pct, pj.pct); if (pj.msg) msg = pj.msg; }
      } catch (e) {}                     // engine restarting — keep the bar alive
      _paintFill(pct);
      _chamPlace(pct);
      $("run-label").textContent = "UPDATING " + Math.round(pct * 100) + "%  ·  " + msg;
      el.textContent = "Updating " + Math.round(pct * 100) + "%";
      try {
        const vr = await fetchT(SERVER + "/version", { cache: "no-cache" }, 2500);
        const vj = await vr.json();
        if (vj && vj.version && vj.version !== fromVersion) {
          _paintFill(1);
          _chamPlace(1);
          $("run-label").textContent = "UPDATED";
          el.textContent = "Updated to v" + vj.version;
          setStatus("UPDATED", "colourMatik is now v" + vj.version + ". Restart Premiere Pro to load the new panel.", "done");
          return;
        }
      } catch (e) {}
    }
    throw new Error("timed out");
  } catch (e) {
    setStatus("ERROR", "Update couldn't finish: " + (e.message || e) + ". It may still be running — check again in a minute.", "error");
    el.textContent = "Update failed — retry";
    updateReady = true;
  } finally {
    _updating = false;
    clearInterval(_ub);
    setTimeout(() => {
      $("run").classList.remove("loading");
      _paintFill(0);
      _chamShow(false);
      $("run-label").textContent = "MATCH & APPLY";
      refreshRun();
    }, 1500);
  }
}
/* The engine reporting a HIGHER version than this panel means the update
 * already landed on disk and only the host is still running the old panel from
 * memory — Adobe loads panels at app start. That state used to render as
 * "Up to date", which reads as "the update did nothing". Name it instead. */
async function _engineVersion() {
  try {
    const vr = await fetchT(SERVER + "/version", { cache: "no-cache" }, 3000);
    const vj = await vr.json(); if (vj && vj.version) return vj.version;
  } catch (e) {}
  return null;
}
async function _restartPending() {
  const eng = await _engineVersion();
  return (eng && semverGt(eng, LOCAL_VERSION)) ? eng : null;
}
async function _installedVersion() {
  // The OLDER of engine and panel: comparing only the engine hid a panel that
  // failed to reinstall — it stayed stale forever because the engine already
  // reported the new version and no update was ever offered.
  let eng = null;
  try {
    const vr = await fetchT(SERVER + "/version", { cache: "no-cache" }, 3000);
    const vj = await vr.json(); if (vj && vj.version) eng = vj.version;
  } catch (e) {}
  if (!eng) return LOCAL_VERSION;
  return semverGt(eng, LOCAL_VERSION) ? LOCAL_VERSION : eng;
}
async function checkForUpdates() {
  const el = $("update-link");
  if (_updating) return;
  if (updateReady) { runSelfUpdate(await _installedVersion()); return; }
  el.textContent = "Checking…";
  try {
    const pend = await _restartPending();
    if (pend) {
      el.textContent = "Restart Premiere Pro";
      setStatus("RESTART", "colourMatik " + pend + " is installed. Quit Premiere Pro and open it again to load the new panel.", "done");
      return;
    }
    const local = await _installedVersion();
    const r = await fetchT(UPDATE_URL, { cache: "no-cache" }, 10000);
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    if (j.version && semverGt(j.version, local)) {
      el.textContent = "Update v" + j.version + " — install";
      updateReady = true;
    } else {
      el.textContent = "Up to date";
    }
  } catch (e) {
    el.textContent = "Check failed";
  }
}

/* ---- Wire up -------------------------------------------------------------- */
$("refBtn").addEventListener("click", captureRef);
$("srcBtn").addEventListener("click", captureSrc);
$("mode-different").addEventListener("click", () => {
  $("mode-different").classList.add("selected"); $("mode-same").classList.remove("selected");
});
$("mode-same").addEventListener("click", () => {
  $("mode-same").classList.add("selected"); $("mode-different").classList.remove("selected");
});
$("run").addEventListener("click", run);
$("intensity").addEventListener("input", onIntensity);
for (const k of ["wb", "tone", "color"]) $("ax-" + k).addEventListener("input", onAxis);
$("lib-save").addEventListener("click", saveRefToLibrary);
$("run-all").addEventListener("click", matchAll);
loadLibrary();
$("site-link").addEventListener("click", () => openUrl(SITE_URL));
$("update-link").addEventListener("click", checkForUpdates);
$("version").textContent = "v" + LOCAL_VERSION;

/* ---- AUTOMATIC updates ----------------------------------------------------
 * On every panel open: compare the shipped version.json against what is
 * actually installed (the engine's /version) and, if newer, START THE UPDATE
 * without any clicks. Windows shows its one unavoidable admin prompt; after
 * that the updater pulls, reinstalls panel+effect and restarts the engine —
 * the user only restarts Premiere when it finishes. Runs once per panel open,
 * at startup only, so it can never interrupt a match in progress. */
async function autoUpdateCheck() {
  try {
    const pend = await _restartPending();
    if (pend) {                       // already updated; just needs the restart
      $("update-link").textContent = "Restart Premiere Pro";
      setStatus("RESTART", "colourMatik " + pend + " is installed. Quit Premiere Pro and open it again to load the new panel.", "done");
      return;
    }
    const local = await _installedVersion();
    const r = await fetchT(UPDATE_URL, { cache: "no-cache" }, 10000);
    if (!r.ok) return;
    const j = await r.json();
    if (!(j && j.version && semverGt(j.version, local))) return;
    // Fully automatic, fully in-panel: bar below, no windows, no browser.
    runSelfUpdate(local);
  } catch (e) {}
}
setTimeout(autoUpdateCheck, 1500);

/* The engine imports heavy AI libraries and takes ~30s to answer after login,
 * install or an update — during which every call fails and the panel used to
 * open straight into a scary red ERROR. Poll it on open and say what is
 * actually happening; flip to READY the moment it answers. */
(async function engineWait() {
  for (let i = 0; i < 40; i++) {
    try {
      const r = await fetchT(SERVER + "/version", { cache: "no-cache" }, 2500);
      const j = await r.json();
      if (j && j.version) {
        if (i > 0) setStatus("READY", "Engine is up (v" + j.version + "). Pick a reference and a target.", "idle");
        return;
      }
    } catch (e) {}
    if (i === 0) setStatus("STARTING", "colourMatik engine is starting - this takes about half a minute after login or an update...", "busy");
    await _sleep(3000);
  }
  setStatus("ERROR", "The engine did not start. Open the colourMatik installer once, or run ./colourmatik-app in the colourMatik folder.", "error");
})();
