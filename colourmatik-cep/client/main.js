/* colourMatik — After Effects CEP panel.
 * Identical UI to the Premiere UXP panel (same index.html/CSS). Talks to the local
 * engine at 127.0.0.1:8765 via Node http (bypasses CORS), and drives the native
 * "colourMatik" effect through ExtendScript (jsx/host.jsx) via CSInterface.
 * Engine: ./colourmatik-app  ·  by Sevki Bugra Ozbek · catheadai.com
 */
"use strict";
var cs = new CSInterface();
// Load (or re-load) the ExtendScript bridge on every panel open, so host.jsx
// updates apply on a simple panel close/reopen — no AE restart needed.
try {
  var _jsxPath = cs.getSystemPath(SystemPath.EXTENSION).replace(/\\/g, "/") + "/jsx/host.jsx";
  cs.evalScript('$.evalFile("' + _jsxPath + '")');
} catch (e) {}
var SERVER_HOST = "127.0.0.1", SERVER_PORT = 8765;
var LOCAL_VERSION = "1.8.2";
// Update checks go through the local engine (GET /update_check), which asks
// GitHub at most once a day for the whole machine - the panel never contacts
// GitHub itself.
var SITE_URL = "https://catheadai.com";
var DEFAULT_INTENSITY = 100;

var _req = (typeof require !== "undefined") ? require : (typeof cep_node !== "undefined" ? cep_node.require : null);
var _Buffer = (typeof Buffer !== "undefined") ? Buffer : (typeof cep_node !== "undefined" ? cep_node.Buffer : null);
var _http = _req ? _req("http") : null;

var $ = function (id) { return document.getElementById(id); };
var state = { refPath: null, srcPath: null, srcLayerIndex: null, srcCompId: null, srcLayerName: null, rid: null, slot: null };
/* single-quote a value for embedding inside an evalScript() source string */
function jsArg(s) {
  return "'" + String(s == null ? "" : s)
    .replace(/\\/g, "\\\\")
    .replace(/'/g, "\\'")
    .replace(/[\u0000-\u001F\u2028\u2029]/g, function (ch) {
      return "\\u" + ("000" + ch.charCodeAt(0).toString(16)).slice(-4);
    }) + "'";
}
var bakeTimer = null;

/* ---- transport: Node http (primary) or fetch (fallback) ------------------- */
function httpJSON(method, path, payload, timeoutMs) {
  return new Promise(function (resolve, reject) {
    var body = payload != null ? JSON.stringify(payload) : null;
    if (_http) {
      var headers = {};
      if (body != null) { headers["Content-Type"] = "application/json"; headers["Content-Length"] = _Buffer.byteLength(body); }
      var r = _http.request({ host: SERVER_HOST, port: SERVER_PORT, path: path, method: method, headers: headers }, function (res) {
        var buf = ""; res.setEncoding("utf8");
        res.on("data", function (d) { buf += d; });
        res.on("end", function () {
          var j = null; try { j = JSON.parse(buf); } catch (e) {}
          if (res.statusCode >= 200 && res.statusCode < 300) resolve(j || {});
          else reject(new Error((j && j.error) || ("HTTP " + res.statusCode)));
        });
      });
      r.on("error", function () { reject(new Error("Can't reach the engine at http://" + SERVER_HOST + ":" + SERVER_PORT + " — start it with ./colourmatik-app")); });
      r.setTimeout(timeoutMs || 300000, function () { r.destroy(new Error("timeout")); });
      if (body != null) r.write(body);
      r.end();
    } else {
      var ctrl = new AbortController();
      var t = setTimeout(function () { ctrl.abort(); }, timeoutMs || 300000);
      var opt = { method: method, signal: ctrl.signal };
      if (body != null) { opt.headers = { "Content-Type": "application/json" }; opt.body = body; }
      fetch("http://" + SERVER_HOST + ":" + SERVER_PORT + path, opt)
        .then(function (res) { return res.json(); })
        .then(function (j) { resolve(j || {}); })
        .catch(function () { reject(new Error("Can't reach the engine at http://" + SERVER_HOST + ":" + SERVER_PORT)); })
        .then(function () { clearTimeout(t); }, function () { clearTimeout(t); });
    }
  });
}
function postJSON(path, payload, timeoutMs) { return httpJSON("POST", path, payload, timeoutMs); }
function getJSON(path, timeoutMs) { return httpJSON("GET", path, null, timeoutMs); }

/* ---- ExtendScript bridge -------------------------------------------------- */
function evalHost(call) {
  return new Promise(function (resolve) {
    cs.evalScript(call, function (raw) {
      if (raw === "EvalScript error." || raw == null) { resolve({ ok: false, message: "After Effects script error." }); return; }
      var j = null; try { j = JSON.parse(raw); } catch (e) {}
      resolve(j || { ok: false, message: "Bad response from After Effects." });
    });
  });
}

/* ---- UI helpers ----------------------------------------------------------- */
function setStatus(stateLabel, msg, kind) {
  $("status-state").textContent = stateLabel;
  $("status-msg").textContent = msg;
  $("status").className = kind || "idle";
}
function currentMode() { return $("mode-same").classList.contains("selected") ? "same" : "different"; }
function currentLook() { return "exact"; }   // Cinematic AI removed - accurate matching only
function refreshRun() { $("run").disabled = !(state.refPath && state.srcPath); }
function baseName(p) { return p ? p.split(/[\\\/]/).pop() : ""; }

/* ---- Match & Apply loading bar (fed by GET /progress) --------------------- */
var _progPoll = null, _progTick = null, _progReset = null, _chamTick = null, _dispPct = 0, _srvPct = 0, _srvMsg = "";
/* 18 frames of the real walk-cycle artwork stacked in one sprite image. */
var CHAM_FRAMES = 18, CHAM_W = 54, CHAM_H = 33, _chamFrame = 0;
/* The bar is a PICTURE the engine draws — see the Premiere panel. */
var CHAM_FRAMES = 18, _chamFrame = 0, _barPct = 0, _barBusy = false;
function _chamShow(on) {
  var i = $("prog-img");
  if (i) i.style.display = on ? "block" : "none";
}
function _chamStep() {
  var i = $("prog-img");
  if (!i || _barBusy) return;
  _barBusy = true;
  _chamFrame = (_chamFrame + 1) % CHAM_FRAMES;
  getJSON("/bardata?pct=" + _barPct.toFixed(2) + "&f=" + _chamFrame, 2500).then(function (j) {
    if (j && j.img) i.src = j.img;
  }).then(function () { _barBusy = false; }, function () { _barBusy = false; });
}
function _paintFill(pct) { _barPct = Math.max(0, Math.min(1, pct)); }
function _chamPlace(pct) { _paintFill(pct); }
function _paintProg() {
  _paintFill(_dispPct);
  _chamPlace(_dispPct);
  $("run-label").textContent = Math.round(_dispPct * 100) + "%" + (_srvMsg ? "  ·  " + _srvMsg : "");
}
function startProgress(jobId) {
  if (_progPoll) clearInterval(_progPoll);
  if (_progTick) clearInterval(_progTick);
  if (_chamTick) clearInterval(_chamTick);
  if (_progReset) { clearTimeout(_progReset); _progReset = null; }
  _dispPct = 0; _srvPct = 0; _srvMsg = "Starting";
  $("run").classList.add("loading");
  var polling = false;
  _progPoll = setInterval(function () {
    if (polling) return; polling = true;
    getJSON("/progress/" + jobId, 4000).then(function (j) {
      if (j && typeof j.pct === "number") { _srvPct = j.pct; if (j.msg) _srvMsg = j.msg; }
    }).then(function () { polling = false; }, function () { polling = false; });
  }, 500);
  _chamShow(true);
  _chamTick = setInterval(_chamStep, 140);          // the walk itself
  _progTick = setInterval(function () {
    var soft = Math.min(0.97, _srvPct + 0.10);
    var target = Math.max(_srvPct, Math.min(soft, _dispPct + 0.006));
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
  _progReset = setTimeout(function () {
    _progReset = null;
    $("run").classList.remove("loading");
    _paintFill(0);
    _chamShow(false);
    $("run-label").textContent = "MATCH & APPLY";
  }, done ? 450 : 0);
}
function newJobId() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }

/* ---- capture reference / target (the selected AE layer) ------------------- */
async function captureRef() {
  if (_running) return setStatus("MATCHING", "Wait for the current match to finish.", "busy");
  try {
    var s = await evalHost("cm_getSelectedSourcePath()");
    if (!s.ok) return setStatus("SELECT", s.message || "Select the reference layer, then click again.", "error");
    state.refPath = s.path;
    state.refAt = (typeof s.curT === "number" && s.curT >= 0) ? s.curT : null;
    $("refName").textContent = baseName(s.path);
    $("refName").className = "slot-name set";
    refreshRun();
    setStatus("READY", "Reference set. Now pick the target layer.", "idle");
  } catch (e) { setStatus("ERROR", String(e.message || e), "error"); }
}
async function captureSrc() {
  if (_running) return setStatus("MATCHING", "Wait for the current match to finish.", "busy");
  try {
    var s = await evalHost("cm_getSelectedSourcePath()");
    if (!s.ok) return setStatus("SELECT", s.message || "Select the target layer, then click again.", "error");
    state.srcPath = s.path;
    state.srcAt = (typeof s.curT === "number" && s.curT >= 0) ? s.curT : null;
    state.srcLayerIndex = s.layerIndex;
    state.srcCompId = s.compId || null;
    state.srcLayerName = s.layerName || null;
    state.slot = null;
    $("intensity-section").className = "section hidden";
    $("srcName").textContent = baseName(s.path);
    $("srcName").className = "slot-name set";
    refreshRun();
    setStatus("READY", "Target set. Match & Apply when ready.", "idle");
  } catch (e) { setStatus("ERROR", String(e.message || e), "error"); }
}

/* ---- Match & Apply -------------------------------------------------------- */
var _runGen = 0;
var _running = false;
async function run() {
  if (_updating) return setStatus("UPDATE", "Updating colourMatik — matching is paused until it finishes.", "busy");
  var gen = ++_runGen;
  _running = true;
  // Snapshot the target NOW: the engine takes seconds to minutes, and the user can
  // re-capture meanwhile — this match must land on the layer it was started for.
  var tgt = {
    srcPath: state.srcPath, refPath: state.refPath,
    srcAt: (typeof state.srcAt === "number") ? state.srcAt : null,
    refAt: (typeof state.refAt === "number") ? state.refAt : null,
    idx: state.srcLayerIndex, compId: state.srcCompId, name: state.srcLayerName
  };
  $("run").disabled = true;
  $("preview").className = "hidden";
  $("alts").className = "hidden"; $("alts").innerHTML = "";
  $("wipe").className = "hidden";
  resetAxes();
  state.slot = null;
  $("intensity-section").className = "section hidden";
  setStatus("MATCHING", "Working — this takes a few seconds…", "busy");
  var jobId = newJobId();
  startProgress(jobId);
  var ok = false;
  // INSTANT DRAFT: cheap 2-5s match applied immediately; the full pass below
  // refines and hot-swaps. Failures here are silently ignored.
  try {
    var dj = await postJSON("/match_paths", {
      source_path: tgt.srcPath, reference_path: tgt.refPath,
      source_at: tgt.srcAt, reference_at: tgt.refAt,
      mode: currentMode(), tf: ($("tf") && $("tf").value) || "sRGB", frames: 3, look: currentLook(), fast: true
    }, 45000);
    if (dj && dj.ok && gen === _runGen) {
      var de = await postJSON("/effect_lut", { rid: dj.rid }, 15000);
      if (de && de.ok && gen === _runGen) {
        state.slot = de.slot;
        var apd = await evalHost("cm_apply(" + parseInt(de.slot, 10) + ", " + DEFAULT_INTENSITY + ", " +
          (tgt.idx || 0) + ", " + (tgt.compId || 0) + ", " + jsArg(tgt.name) + ")");
        if (apd && apd.ok) setStatus("DRAFT", "Draft look applied - refining in the background...", "busy");
      }
    }
  } catch (e) {}
  if (gen !== _runGen) return;
  try {
    var j;
    try {
      j = await postJSON("/match_paths", {
        source_path: tgt.srcPath, reference_path: tgt.refPath,
        source_at: tgt.srcAt, reference_at: tgt.refAt,
        mode: currentMode(), tf: ($("tf") && $("tf").value) || "sRGB", frames: 7, look: currentLook(), job_id: jobId
      }, 300000);
    } catch (netErr) { throw new Error(String(netErr.message || netErr)); }
    if (!j || !j.ok) throw new Error((j && j.error) || "match failed");
    if (gen !== _runGen) return;
    ok = true;
    stopProgress(true);
    refreshRun();

    state.rid = j.rid;
    if (j.preview) { $("preview-img").src = j.preview; $("preview").className = ""; }

    var ej = { ok: false };
    try { ej = await postJSON("/effect_lut", { rid: j.rid }, 30000); } catch (e) {}
    if (gen !== _runGen) return;
    state.slot = (ej && ej.ok) ? ej.slot : null;

    if (state.slot != null) {
      $("intensity-section").className = "section";
      $("intensity").value = DEFAULT_INTENSITY;
      $("intensity-val").textContent = DEFAULT_INTENSITY + "%";
      state.lastTgt = tgt;              // alt-look clicks re-apply to THIS layer
      loadAlts(j.rid, tgt, gen);        // clickable alternative looks
      loadWipe(j.rid, gen);             // A/B wipe of the applied look
    }

    var label = j.method_label || j.method || "";
    var mTxt = j.ai_used ? (label + " 🧠") : label;
    var deTxt = (j.de_after != null) ? ("  ΔE00 " + Number(j.de_after).toFixed(2)) : "";
    if (state.slot == null) {
      setStatus("ERROR", "Match ok but the LUT slot couldn't be written — is the engine up to date?", "error");
    } else {
      var ap = await evalHost("cm_apply(" + parseInt(state.slot, 10) + ", " + DEFAULT_INTENSITY + ", " +
        (tgt.idx || 0) + ", " + (tgt.compId || 0) + ", " + jsArg(tgt.name) + ")");
      if (gen !== _runGen) return;
      if (ap.ok) setStatus("DONE", "Matched — " + mTxt + deTxt + ". colourMatik applied — drag Intensity to adjust.", "done");
      else setStatus("ERROR", ap.message || "Apply failed.", "error");
    }
  } catch (e) {
    if (gen === _runGen) setStatus("ERROR", String(e.message || e), "error");
  } finally {
    _running = false;
    if (gen === _runGen) { if (!ok) stopProgress(false); refreshRun(); }
  }
}

/* ---- Reference gallery + A/B wipe ----------------------------------------- */
async function loadLibrary() {
  try {
    var j = await getJSON("/library_list", 8000);
    var box = $("library");
    if (!j || !j.ok || !j.items || !j.items.length) { box.className = "hidden"; box.innerHTML = ""; return; }
    box.innerHTML = "";
    j.items.forEach(function (it) {
      var d = document.createElement("div");
      d.className = "lib-item"; d.title = it.name;
      d.innerHTML = '<img src="' + it.thumb + '"><span class="lib-x">&times;</span>';
      d.addEventListener("click", function () {
        state.refPath = it.path;
        $("refName").textContent = it.name; $("refName").className = "slot-name set";
        refreshRun();
        setStatus("READY", "Reference set from the gallery.", "idle");
      });
      d.querySelector(".lib-x").addEventListener("click", async function (ev) {
        ev.stopPropagation();
        try { await postJSON("/library_del", { id: it.id }, 8000); } catch (e) {}
        loadLibrary();
      });
      box.appendChild(d);
    });
    box.className = "";
  } catch (e) {}
}
async function saveRefToLibrary() {
  if (!state.refPath) return setStatus("SELECT", "Pick a reference first, then save it.", "error");
  try {
    var j = await postJSON("/library_add", { path: state.refPath }, 20000);
    if (!j || !j.ok) throw new Error((j && j.error) || "save failed");
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
    var j = await getJSON("/wipe/" + rid, 20000);
    if (!j || !j.ok || gen !== _runGen) return;
    $("wipe-before").src = j.before;
    var wa = $("wipe-after");
    wa.src = j.after;
    var wrap = $("wipe-wrap");
    var size = function () { wa.style.width = wrap.clientWidth + "px"; };
    wa.onload = size; size();
    $("wipe").className = "";
    _wipeSet(50);
    if (!wrap._wired) {
      wrap._wired = true;
      var move = function (ev) {
        var b = wrap.getBoundingClientRect();
        _wipeSet(((ev.clientX - b.left) / Math.max(1, b.width)) * 100);
      };
      wrap.addEventListener("pointerdown", function (ev) { move(ev); wrap.setPointerCapture(ev.pointerId); wrap._drag = true; });
      wrap.addEventListener("pointermove", function (ev) { if (wrap._drag) move(ev); });
      wrap.addEventListener("pointerup", function () { wrap._drag = false; });
    }
  } catch (e) {}
}

/* ---- Alternative looks: the contest's top candidates, clickable ----------- */
async function loadAlts(rid, tgt, gen) {
  try {
    var j = await getJSON("/alts/" + rid, 20000);
    if (!j || !j.ok || !j.alts || j.alts.length < 2) return;
    if (gen !== _runGen) return;
    var box = $("alts");
    box.innerHTML = "";
    j.alts.forEach(function (a) {
      var d = document.createElement("div");
      d.className = "alt" + (a.chosen ? " selected" : "");
      d.innerHTML = '<img src="' + a.preview + '"><div class="alt-name">' + a.label + "</div>";
      d.addEventListener("click", async function () {
        if (gen !== _runGen) return;
        try {
          var ej = await postJSON("/effect_lut", { rid: rid, variant: a.key }, 30000);
          if (!ej || !ej.ok) throw new Error((ej && ej.error) || "variant failed");
          if (gen !== _runGen) return;          // a newer run owns the UI now
          state.slot = ej.slot;
          var pct = parseInt($("intensity").value, 10) || DEFAULT_INTENSITY;
          var ap = await evalHost("cm_apply(" + parseInt(ej.slot, 10) + ", " + pct + ", " +
            (tgt.idx || 0) + ", " + (tgt.compId || 0) + ", " + jsArg(tgt.name) + ")");
          if (!ap.ok) throw new Error(ap.message || "apply failed");
          for (var i = 0; i < box.children.length; i++) box.children[i].className = "alt";
          d.className = "alt selected";
          resetAxes();
          loadWipe(rid, gen);
          setStatus("DONE", "Look switched to " + a.label + ".", "done");
        } catch (e) {
          setStatus("ERROR", "Couldn't switch look: " + (e.message || e), "error");
        }
      });
      box.appendChild(d);
    });
    box.className = "";
  } catch (e) {}
}

/* ---- WB / Tone / Colour strength axes ------------------------------------- */
var axesTimer = null;
function resetAxes() {
  ["wb", "tone", "color"].forEach(function (k) {
    var el = $("ax-" + k); if (!el) return;
    el.value = 100; $("ax-" + k + "-val").textContent = "100";
  });
}
function onAxis() {
  ["wb", "tone", "color"].forEach(function (k) {
    $("ax-" + k + "-val").textContent = String(Math.round(parseFloat($("ax-" + k).value) || 100));
  });
  if (axesTimer) clearTimeout(axesTimer);
  axesTimer = setTimeout(applyAxes, 300);
}
async function applyAxes() {
  if (!state.rid || state.slot == null || !state.lastTgt) return;
  var gen = _runGen, rid = state.rid;        // stale completions must not re-arm
  try {
    var body = { rid: rid,
      wb: Math.round(parseFloat($("ax-wb").value) || 100) / 100,
      tone: Math.round(parseFloat($("ax-tone").value) || 100) / 100,
      color: Math.round(parseFloat($("ax-color").value) || 100) / 100 };
    var j = await postJSON("/effect_lut", body, 30000);
    if (!j || !j.ok) throw new Error((j && j.error) || "strength failed");
    if (gen !== _runGen || rid !== state.rid) return;   // a new match owns the layer now
    state.slot = j.slot;
    var pct = parseInt($("intensity").value, 10) || DEFAULT_INTENSITY;
    var tgt = state.lastTgt;
    var ap = await evalHost("cm_apply(" + parseInt(j.slot, 10) + ", " + pct + ", " +
      (tgt.idx || 0) + ", " + (tgt.compId || 0) + ", " + jsArg(tgt.name) + ")");
    if (!ap.ok) throw new Error(ap.message || "apply failed");
    setStatus("TUNE", "WB " + $("ax-wb").value + "% · Tone " + $("ax-tone").value + "% · Color " + $("ax-color").value + "% — applied.", "busy");
  } catch (e) { setStatus("ERROR", "Strength error: " + (e.message || e), "error"); }
}

/* ---- Intensity: live re-set of the effect param --------------------------- */
function onIntensity() {
  var v = parseInt($("intensity").value, 10);
  $("intensity-val").textContent = v + "%";
  if (bakeTimer) clearTimeout(bakeTimer);
  bakeTimer = setTimeout(function () { applyIntensity(v); }, 110);
}
async function applyIntensity(v) {
  if (state.slot == null) return;
  try {
    var r = await evalHost("cm_setIntensity(" + v + ", " + (state.srcLayerIndex || 0) + ", " +
      (state.srcCompId || 0) + ", " + jsArg(state.srcLayerName) + ")");
    if (r.ok) setStatus("INTENSITY", v + "% — applied live on the layer.", "busy");
    else setStatus("INTENSITY", r.message || "intensity error", "error");
  } catch (e) { setStatus("INTENSITY", "intensity error: " + (e.message || e), "error"); }
}

/* ---- footer: updates + site ---------------------------------------------- */
function semverGt(a, b) {
  var pa = String(a).split("."), pb = String(b).split(".");
  for (var i = 0; i < 3; i++) { var x = +pa[i] || 0, y = +pb[i] || 0; if (x > y) return true; if (x < y) return false; }
  return false;
}
var updateReady = false;   // first click checks; once found, the next click INSTALLS
var _updating = false;
function _sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
/* Self-contained in-panel update: silent updater + progress file through the
 * engine; the big button's bar is the UI. No windows, no browser, no GitHub. */
async function runSelfUpdate(fromVersion) {
  if (_updating) return;
  if (_running) {                              // never restart the engine mid-match
    setTimeout(function () { autoUpdateCheck(); }, 60000);
    return;
  }
  _updating = true;
  var el = $("update-link");
  $("run").disabled = true;
  $("run").classList.add("loading");
  _chamShow(true);
  var _ub = setInterval(_chamStep, 140);   // the chameleon walks while it updates
  try {
    var j = await postJSON("/update_now", {}, 8000);
    if (!j || !j.ok) throw new Error((j && j.error) || "the engine couldn't start the update");
    setStatus("UPDATE", "Updating colourMatik — the bar below tracks it. On Windows, approve the admin prompt.", "busy");
    var t0 = Date.now();
    var pct = 0.02, msg = "Starting";
    while (Date.now() - t0 < 15 * 60 * 1000) {
      await _sleep(900);
      var failed = null;
      try {
        var pj = await getJSON("/update_progress", 2500);
        if (pj && pj.failed) failed = pj.msg || "the updater reported a failure";
        else if (pj && typeof pj.pct === "number") { pct = Math.max(pct, pj.pct); if (pj.msg) msg = pj.msg; }
      } catch (e) {}                     // engine restarting - keep the bar alive
      // Thrown inside the try above, a FAIL line was swallowed by that catch
      // (it is only meant for engine restarts): the bar ran on to its timeout.
      if (failed) { var fe = new Error(failed); fe.fromUpdater = true; throw fe; }
      _paintFill(pct);
      _chamPlace(pct);
      $("run-label").textContent = "UPDATING " + Math.round(pct * 100) + "%  ·  " + msg;
      el.textContent = "Updating " + Math.round(pct * 100) + "%";
      try {
        var vj = await getJSON("/version", 2500);
        if (vj && vj.version && vj.version !== fromVersion) {
          _paintFill(1);
          _chamPlace(1);
          $("run-label").textContent = "UPDATED";
          el.textContent = "Updated to v" + vj.version;
          if (!_reloadIntoNewPanel(vj.version))
            setStatus("UPDATED", "colourMatik is now v" + vj.version + " — the new engine is already running. Restart After Effects to load the new panel.", "done");
          return;
        }
      } catch (e) {}
    }
    throw new Error("timed out");
  } catch (e) {
    setStatus("ERROR", "Update couldn't finish: " + (e.message || e) +
      (e.fromUpdater ? "" : ". It may still be running — check again in a minute."), "error");
    el.textContent = "Update failed — retry";
    updateReady = true;
  } finally {
    _updating = false;
    clearInterval(_ub);
    setTimeout(function () {
      $("run").classList.remove("loading");
      _paintFill(0);
      _chamShow(false);
      $("run-label").textContent = "MATCH & APPLY";
      refreshRun();
    }, 1500);
  }
}
/* engine ahead of this panel == updated on disk, host still running the old
 * copy from memory. See the Premiere panel. */
function _restartPending() {
  return getJSON("/version", 3000).then(function (v) {
    var eng = (v && v.version) ? v.version : null;
    return (eng && semverGt(eng, LOCAL_VERSION)) ? eng : null;
  }).catch(function () { return null; });
}
function _installedVersion() {
  // older of engine vs panel — see the Premiere panel for why
  return getJSON("/version", 3000).then(function (v) {
    var eng = (v && v.version) ? v.version : null;
    if (!eng) return LOCAL_VERSION;
    return semverGt(eng, LOCAL_VERSION) ? LOCAL_VERSION : eng;
  }).catch(function () { return LOCAL_VERSION; });
}
/* newest release as the engine knows it - see the Premiere panel (force = the
 * user's click) */
function _latestRelease(force) {
  return getJSON("/update_check" + (force ? "?force=1" : ""), 20000).then(function (j) {
    if (!j || (!j.version && !j.ok)) throw new Error((j && j.error) || "no answer");
    return j;
  });
}
function checkForUpdates() {
  var el = $("update-link");
  if (_updating) return;
  if (updateReady) { _installedVersion().then(runSelfUpdate); return; }
  el.textContent = "Checking…";
  _restartPending().then(function (pend) {
    if (pend) {
      el.textContent = "Restart After Effects";
      setStatus("RESTART", "colourMatik " + pend + " is installed. Quit After Effects and open it again to load the new panel.", "done");
      return null;
    }
    return _installedVersion().then(function (local) {
      return _latestRelease(true).then(function (j) {
        if (j && j.version && semverGt(j.version, local)) {
          // One click: install right away (it used to wait for a second click).
          el.textContent = "Update v" + j.version + " found — installing";
          runSelfUpdate(local);
        }
        else el.textContent = "Up to date";
      });
    });
  }).catch(function () { el.textContent = "Check failed"; });
}
/* After an update the NEW panel files are already on disk (the updater copies
 * them before it restarts the engine) and this panel re-evaluates host.jsx on
 * every load - so a page reload IS the restart. Only reload once the on-disk
 * panel really carries the new version; otherwise ask for an AE restart. */
function _reloadIntoNewPanel(newVersion) {
  try {
    var fs = _req ? _req("fs") : null;
    var ext = cs.getSystemPath(SystemPath.EXTENSION).replace(/\\/g, "/");
    var js = fs ? fs.readFileSync(ext + "/client/main.js", "utf8") : "";
    if (js.indexOf('LOCAL_VERSION = "' + newVersion + '"') >= 0) {
      setStatus("UPDATED", "colourMatik is now v" + newVersion + " — reloading the panel…", "done");
      setTimeout(function () { window.location.reload(); }, 1500);
      return true;
    }
  } catch (e) {}
  return false;
}
/* ---- wire up -------------------------------------------------------------- */
$("refBtn").addEventListener("click", captureRef);
$("srcBtn").addEventListener("click", captureSrc);
$("mode-different").addEventListener("click", function () { $("mode-different").classList.add("selected"); $("mode-same").classList.remove("selected"); });
$("mode-same").addEventListener("click", function () { $("mode-same").classList.add("selected"); $("mode-different").classList.remove("selected"); });
$("run").addEventListener("click", run);
$("intensity").addEventListener("input", onIntensity);
["wb", "tone", "color"].forEach(function (k) { $("ax-" + k).addEventListener("input", onAxis); });
$("lib-save").addEventListener("click", saveRefToLibrary);
loadLibrary();
$("site-link").addEventListener("click", function () { cs.openURLInDefaultBrowser(SITE_URL); });
$("update-link").addEventListener("click", checkForUpdates);
$("version").textContent = "v" + LOCAL_VERSION;

/* the panel's REFERENCE/TARGET labels say "from the selected clip" — in AE that's
   the selected layer, so update the two slot hints to read "layer". */
try {
  var refLbl = document.querySelectorAll(".field-label")[0];
  var tgtLbl = document.querySelectorAll(".field-label")[1];
  if (refLbl) refLbl.textContent = "REFERENCE — THE LOOK TO COPY";
  if (tgtLbl) tgtLbl.textContent = "TARGET — APPLY THE LOOK TO THIS";
} catch (e) {}

/* ---- AUTOMATIC updates ----------------------------------------------------
 * Once per panel open, as soon as the engine answers: compare the newest
 * release (the engine's once-a-day check) against what is actually installed
 * (the engine's /version) and, if newer, START THE UPDATE without any clicks.
 * Runs at startup only, so it can never interrupt a match in progress. */
function autoUpdateCheck() {
  _restartPending().then(function (pend) {
    if (pend) {
      $("update-link").textContent = "Restart After Effects";
      setStatus("RESTART", "colourMatik " + pend + " is installed. Quit After Effects and open it again to load the new panel.", "done");
      return null;
    }
    return _installedVersion().then(function (local) {
      return _latestRelease(false).then(function (j) {
        if (!(j && j.version && semverGt(j.version, local))) return;
        runSelfUpdate(local);   // fully automatic, fully in-panel
      });
    });
  }).catch(function () {});
}

/* Engine takes ~30s to boot after login/install/update; do not open into a
 * scary error — poll and narrate, flip to READY when it answers, and only then
 * run the once-per-open update check (it needs the engine). */
(function engineWait(i) {
  getJSON("/version", 2500).then(function (j) {
    if (j && j.version) {
      if (i > 0) setStatus("READY", "Engine is up (v" + j.version + "). Pick a reference and a target.", "idle");
      autoUpdateCheck();
      return;
    }
    throw new Error("no");
  }).catch(function () {
    if (i === 0) setStatus("STARTING", "colourMatik engine is starting - this takes about half a minute after login or an update...", "busy");
    if (i < 40) setTimeout(function () { engineWait(i + 1); }, 3000);
    else setStatus("ERROR", "The engine did not start. Open the colourMatik installer once, or run ./colourmatik-app in the colourMatik folder.", "error");
  });
})(0);
