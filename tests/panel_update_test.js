// Runs the REAL panel main.js files (Premiere UXP + After Effects CEP) in a sandbox
// with a fake engine, clicks "Check for updates" ONCE, and checks that the update
// is installed by that single click (and that AE reloads itself into the new
// version only when the new panel files are really on disk). Also checks that the
// panels never contact GitHub themselves - the engine does that, once a day - and
// that a "FAIL|reason" from the updater stops the bar with that reason.
//   node tests/panel_update_test.js      (exit code 1 on failure)
const vm = require("vm"), fs = require("fs");
const ROOT = require("path").resolve(__dirname, "..");
function fastTimers() {
  const k = 1 / 200;   // 200x faster clock
  return { setTimeout: (f, ms, ...a) => setTimeout(f, Math.max(0, (ms || 0) * k), ...a), clearTimeout,
           setInterval: (f, ms, ...a) => setInterval(f, Math.max(1, (ms || 0) * k), ...a), clearInterval };
}
function makeDom() {
  const els = {};
  const el = (id) => els[id] || (els[id] = { id, textContent: "", className: "", disabled: false, value: "100", innerHTML: "", src: "", style: {},
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); }, toggle() {} },
    addEventListener() {}, appendChild() {}, setAttribute() {}, querySelector() { return el(id + "_q"); }, querySelectorAll() { return []; } });
  return { els, document: { getElementById: el, querySelectorAll: () => [], querySelector: (q) => el(q), createElement: (t) => el("new_" + t + Math.random()), body: el("body") } };
}
// The fake engine. latest() is the newest release its /update_check reports.
// failReason: the Windows updater reports "FAIL|<reason>" once it passes 60%
// (e.g. the admin prompt was declined) and restarts the engine right AFTER -
// exactly the order update-windows.cmd uses.
function engine(startVersion, newVersion, latest, failReason) {
  const st = { version: startVersion, updateCalls: 0, polls: 0, progress: 0, failPolls: 0, checks: [] };
  st.handle = (url, method) => {
    const [path, query] = url.split("?");
    if (path === "/version") return { name: "colourMatik", version: st.version };
    if (path === "/update_check") { st.checks.push(/(^|&)force=1(&|$)/.test(query || "") ? "force" : "auto"); return { ok: true, version: latest(), cached: false }; }
    if (path === "/update_now") { st.updateCalls++; st.progress = 2; return { ok: true, started: true }; }
    if (path === "/update_progress") {
      if (failReason && st.updateCalls && st.progress >= 60) {
        if (++st.failPolls >= 3) st.version = newVersion;        // the engine restarts after the FAIL line
        return { ok: true, pct: 0, msg: failReason, failed: true };
      }
      if (st.updateCalls) { st.polls++; st.progress = Math.min(100, st.progress + 17); if (st.progress >= 100) st.version = newVersion; }
      return { ok: true, pct: st.progress / 100, msg: st.progress >= 100 ? "Done" : "Refreshing the engine" };
    }
    if (path === "/library_list") return { ok: true, items: [] };
    if (path === "/diag") return { ok: true };
    return { ok: true };
  };
  return st;
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(cond, ms = 8000) { const t0 = Date.now(); while (!cond() && Date.now() - t0 < ms) await wait(20); return cond(); }
const settled = (document) => () => /Updated to v|Update failed/.test(document.getElementById("update-link").textContent);
const ENGINE = "http://127.0.0.1:8765";

async function testUXP(failReason) {
  let gh = "1.7.9";
  const eng = engine("1.7.9", "9.9.9", () => gh, failReason);
  const { document } = makeDom();
  const outside = [];                               // every request that is not the local engine
  const fetch = async (url, opts) => {
    const u = String(url);
    if (!u.startsWith(ENGINE + "/")) { outside.push(u); throw new Error("offline"); }
    const body = eng.handle(u.slice(ENGINE.length), (opts || {}).method || "GET");
    return { ok: true, status: 200, json: async () => body };
  };
  const stub = new Proxy(function () {}, { get: (t, p) => p === "then" ? undefined : stub, apply: () => stub });
  const ctx = vm.createContext({ console, document, fetch, AbortController, ...fastTimers(), require: (m) => m === "uxp" ? { shell: { openExternal: async () => {} } } : stub,
                                 window: {}, Image: function () {}, navigator: {} });
  vm.runInContext(fs.readFileSync(ROOT + "/colourmatik-uxp/main.js", "utf8"), ctx, { filename: "uxp/main.js" });
  await wait(300);                                  // startup auto-check: nothing newer yet
  const autoCalls = eng.updateCalls, openChecks = eng.checks.slice();
  gh = "9.9.9";                                     // a new release appears while the panel is open
  ctx.checkForUpdates();                            // ONE click
  await until(settled(document));
  const done = /Updated to v9\.9\.9/.test(document.getElementById("update-link").textContent);
  return { name: "Premiere (UXP)" + (failReason ? ", updater reports FAIL" : ""), autoCalls, openChecks, checks: eng.checks, outside,
           updateCalls: eng.updateCalls, done, link: document.getElementById("update-link").textContent,
           status: document.getElementById("status-msg").textContent, bar: document.getElementById("run-label").textContent };
}

async function testCEP(onDiskVersion, failReason) {
  let gh = "1.7.9";
  const eng = engine("1.7.9", "9.9.9", () => gh, failReason);
  const { document } = makeDom();
  let reloaded = 0;
  const outside = [];
  const fakeFs = { readFileSync: () => 'var LOCAL_VERSION = "' + onDiskVersion + '";' };
  const net = { request() {}, get(u) { outside.push(String(u)); throw new Error("offline"); } };
  const ctx = vm.createContext({ console, document, AbortController, ...fastTimers(),
    CSInterface: function () { this.getSystemPath = () => "/fake/ext"; this.evalScript = (s, cb) => cb && cb("{}"); },
    SystemPath: { EXTENSION: "extension" }, window: { location: { reload: () => { reloaded++; } } },
    require: (m) => (m === "fs" ? fakeFs : net), Buffer,
    fetch: async (url) => { outside.push(String(url)); throw new Error("offline"); } });
  vm.runInContext(fs.readFileSync(ROOT + "/colourmatik-cep/client/main.js", "utf8"), ctx, { filename: "cep/main.js" });
  ctx.getJSON = async (p) => eng.handle(p, "GET");  // the engine over Node http
  ctx.postJSON = async (p) => eng.handle(p, "POST");
  await wait(300);
  const autoCalls = eng.updateCalls, openChecks = eng.checks.slice();
  gh = "9.9.9";
  ctx.checkForUpdates();                            // ONE click
  await until(settled(document));
  const done = /Updated to v9\.9\.9/.test(document.getElementById("update-link").textContent);
  await wait(100);
  return { name: "After Effects (CEP), panel on disk = " + onDiskVersion + (failReason ? ", updater reports FAIL" : ""),
           autoCalls, openChecks, checks: eng.checks, outside, updateCalls: eng.updateCalls, done, reloaded,
           link: document.getElementById("update-link").textContent, status: document.getElementById("status-msg").textContent };
}

const DECLINED = "Windows admin approval was not given, so the effect was not updated.";
const failedWith = (r) => !r.done && r.reloaded !== 1 && /Update failed/.test(r.link) &&
  r.status.includes(DECLINED) && !/timed out|may still be running/.test(r.status);

(async () => {
  const u = await testUXP(), a = await testCEP("9.9.9"), b = await testCEP("1.7.9");
  const uf = await testUXP(DECLINED), af = await testCEP("9.9.9", DECLINED);
  const all = [u, a, b, uf, af];
  for (const r of all) {
    console.log("\n" + r.name);
    for (const [k, v] of Object.entries(r)) if (k !== "name") console.log("   " + k.padEnd(12) + JSON.stringify(v));
  }
  const checks = [
    ["Premiere: one click installs", u.updateCalls === 1 && u.done],
    ["After Effects: one click installs and reloads into the new panel", a.updateCalls === 1 && a.done && a.reloaded === 1],
    ["After Effects: no reload when the new files are not on disk", b.updateCalls === 1 && b.done && b.reloaded === 0 && /Restart After Effects/.test(b.status)],
    ["no update starts on panel open when nothing is newer", u.autoCalls === 0 && a.autoCalls === 0],
    // The engine answers from its once-a-day GitHub check unless the user
    // clicked: opening a panel must ask exactly once, without force.
    ["panel open: exactly one update check, not forced", all.every((r) => JSON.stringify(r.openChecks) === '["auto"]')],
    ["the click asks the engine to refresh (force)", all.every((r) => r.checks[1] === "force")],
    ["the panels never contact anything but the local engine", all.every((r) => r.outside.length === 0)],
    // "FAIL|reason" from the updater must end the wait with that reason - not be
    // swallowed until the restarted engine makes the panel call it "Updated".
    ["Premiere: a FAIL from the updater stops the bar and shows its reason", uf.updateCalls === 1 && failedWith(uf)],
    ["After Effects: a FAIL from the updater stops the bar and shows its reason (no reload)", af.updateCalls === 1 && failedWith(af)],
  ];
  let bad = 0;
  console.log("");
  for (const [n, ok] of checks) { console.log((ok ? "  [PASS] " : "  [FAIL] ") + n); if (!ok) bad++; }
  process.exit(bad ? 1 : 0);
})().catch((e) => { console.error("HARNESS ERROR", e); process.exit(1); });
