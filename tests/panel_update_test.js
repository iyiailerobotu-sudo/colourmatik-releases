// Runs the REAL panel main.js files (Premiere UXP + After Effects CEP) in a sandbox
// with a fake engine + fake GitHub, clicks "Check for updates" ONCE, and checks that
// the update is installed by that single click (and that AE reloads itself into the
// new version only when the new panel files are really on disk).
//   node tests/panel_update_test.js      (exit code 1 on failure)
// clicks "Check for updates" once, and records what the panel does.
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
function engine(startVersion, newVersion) {
  const st = { version: startVersion, updateCalls: 0, polls: 0, progress: 0 };
  st.handle = (path, method) => {
    if (path === "/version") return { name: "colourMatik", version: st.version };
    if (path === "/update_now") { st.updateCalls++; st.progress = 2; return { ok: true, started: true }; }
    if (path === "/update_progress") {
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

async function testUXP() {
  const eng = engine("1.7.9", "9.9.9"); let gh = "1.7.9";
  const { els, document } = makeDom();
  const fetch = async (url, opts) => {
    const u = String(url);
    const body = u.includes("raw.githubusercontent.com") ? { version: gh } : eng.handle(u.replace("http://127.0.0.1:8765", "").split("?")[0], (opts || {}).method || "GET");
    return { ok: true, status: 200, json: async () => body };
  };
  const stub = new Proxy(function () {}, { get: (t, p) => p === "then" ? undefined : stub, apply: () => stub });
  const ctx = vm.createContext({ console, document, fetch, AbortController, ...fastTimers(), require: (m) => m === "uxp" ? { shell: { openExternal: async () => {} } } : stub,
                                 window: {}, Image: function () {}, navigator: {} });
  vm.runInContext(fs.readFileSync(ROOT + "/colourmatik-uxp/main.js", "utf8"), ctx, { filename: "uxp/main.js" });
  await wait(300);                                  // startup auto-check: nothing newer yet
  const autoCalls = eng.updateCalls;
  gh = "9.9.9";                                     // a new release appears while the panel is open
  ctx.checkForUpdates();                            // ONE click
  const done = await until(() => /Updated to v9\.9\.9/.test(document.getElementById("update-link").textContent));
  return { name: "Premiere (UXP)", autoCalls, updateCalls: eng.updateCalls, done, link: document.getElementById("update-link").textContent,
           status: document.getElementById("status-msg").textContent, bar: document.getElementById("run-label").textContent };
}

async function testCEP(onDiskVersion) {
  const eng = engine("1.7.9", "9.9.9"); let gh = "1.7.9";
  const { els, document } = makeDom();
  let reloaded = 0;
  const fakeFs = { readFileSync: () => 'var LOCAL_VERSION = "' + onDiskVersion + '";' };
  const ctx = vm.createContext({ console, document, AbortController, ...fastTimers(),
    CSInterface: function () { this.getSystemPath = () => "/fake/ext"; this.evalScript = (s, cb) => cb && cb("{}"); },
    SystemPath: { EXTENSION: "extension" }, window: { location: { reload: () => { reloaded++; } } },
    require: (m) => (m === "fs" ? fakeFs : { request() {}, get() {} }), Buffer,
    fetch: async (url) => ({ ok: true, json: async () => ({ version: gh }) }) });
  vm.runInContext(fs.readFileSync(ROOT + "/colourmatik-cep/client/main.js", "utf8"), ctx, { filename: "cep/main.js" });
  ctx.getJSON = async (p) => eng.handle(p.split("?")[0], "GET");     // the engine over Node http
  ctx.postJSON = async (p) => eng.handle(p, "POST");
  await wait(300);
  const autoCalls = eng.updateCalls;
  gh = "9.9.9";
  ctx.checkForUpdates();                            // ONE click
  const done = await until(() => /Updated to v9\.9\.9/.test(document.getElementById("update-link").textContent));
  await wait(100);
  return { name: "After Effects (CEP), panel on disk = " + onDiskVersion, autoCalls, updateCalls: eng.updateCalls, done, reloaded,
           link: document.getElementById("update-link").textContent, status: document.getElementById("status-msg").textContent };
}

(async () => {
  const u = await testUXP(), a = await testCEP("9.9.9"), b = await testCEP("1.7.9");
  for (const r of [u, a, b]) {
    console.log("\n" + r.name);
    for (const [k, v] of Object.entries(r)) if (k !== "name") console.log("   " + k.padEnd(12) + JSON.stringify(v));
  }
  const checks = [
    ["Premiere: one click installs", u.updateCalls === 1 && u.done],
    ["After Effects: one click installs and reloads into the new panel", a.updateCalls === 1 && a.done && a.reloaded === 1],
    ["After Effects: no reload when the new files are not on disk", b.updateCalls === 1 && b.done && b.reloaded === 0 && /Restart After Effects/.test(b.status)],
    ["no update starts on panel open when nothing is newer", u.autoCalls === 0 && a.autoCalls === 0],
  ];
  let bad = 0;
  console.log("");
  for (const [n, ok] of checks) { console.log((ok ? "  [PASS] " : "  [FAIL] ") + n); if (!ok) bad++; }
  process.exit(bad ? 1 : 0);
})().catch((e) => { console.error("HARNESS ERROR", e); process.exit(1); });
