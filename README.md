# colourMatik 🎨

**Match the colours of one clip to another — accurately, and fully on your own machine.**

colourMatik recolours a **target** clip so its look matches a **reference** clip, and applies the
result as a real, named **`colourMatik`** effect inside Premiere Pro / After Effects — with a
built-in **Intensity** slider. Nothing is uploaded; every frame stays on your computer.

It does the job of aescripts' *AI Color Match*, with two things it doesn't have: **measurable
colour accuracy** (ΔE00) and a choice between a precise **classical** match and a learned
**cinematic AI** grade — the engine measures both and keeps whichever is best.

> by **Sevki Bugra Ozbek** · [catheadai.com](https://catheadai.com)

---

## Two match types

| Mode | What it does | Best for |
|------|--------------|----------|
| **Accurate** | Classical colour science (Monge–Kantorovich / IDT / smoothed 3D-LUT) plus a **SegFormer** scene model that matches region-to-region (sky↔sky, skin↔skin). Auto-selected by measured ΔE00. | Matching shots from the same shoot — highest measurable accuracy. |
| **Cinematic AI** 🧠 | **CanonCGT** (CVPR 2026), a learned reference-grading model, running on your GPU (Apple Silicon / MPS). | A tasteful, photorealistic *look* transfer across very different scenes. |

Both bake into a single flicker-free 3D LUT that the native effect applies, and both are driven by
the same live Intensity slider (0–200 %).

## How accurate?

Measured by applying a known colour distortion and recovering it (ΔE00 = perceived colour
difference; **< 1 = the eye can't tell**). Verified against an independent `.cube` reader.

| Scenario | Before ΔE00 | After ΔE00 |
|----------|-------------|------------|
| Linear difference (white balance + primaries) | 4.73 | **0.03** |
| Non-linear difference (tone curve + saturation) | 9.45 | **0.06** |
| Different scene (distribution match) | 9.45 | **0.70** |
| Real H.264 video (end-to-end) | 9.50 | **0.32** |

---

## Install (macOS, Apple Silicon)

**Easiest — one file, double-click (notarized, no warnings):**
1. Download **[colourMatik-mac.zip](https://github.com/iyiailerobotu-sudo/colourmatik-releases/releases/download/darwin-latest/colourMatik-mac.zip)** from the latest release.
2. It unzips itself on download — double-click **colourMatik Installer**.
3. Enter your Mac password once. It then installs everything in the background (about 10–20 minutes)
   and shows a notification when it's ready. Then **restart Premiere Pro**.

It's signed and **notarized by Apple**, so it opens with no "unidentified developer" warning. It sets up the
engine + AI, the panel, and the effect, and keeps the engine running automatically.

**Manual (from source):**

```bash
git clone https://github.com/iyiailerobotu-sudo/colourmatik-releases.git colourMatik
cd colourMatik
./setup.sh            # venv + deps + local-AI model  (or: ./setup.sh --no-ai)
./install-panel.sh    # installs the Premiere UXP panel
./install-effect.sh   # installs the native colourMatik effect
```

Then **restart Premiere Pro**. The engine runs automatically after install; start it manually any time
with `./colourmatik-app`.

**Updating:** the panel updates itself in one click (it checks at most once a day). By hand: double-click
**`update.command`** (in the installed `~/colourMatik` folder) — it downloads the newest installer (one file,
through the GitHub API), takes the program out of it and reinstalls. **Removing:** double-click **`uninstall.command`**.

## Install (Windows 10/11, x64) — beta

> The Windows port ships the same engine, panel and effect. It has not yet been
> verified on a Windows machine — please report anything odd.

**Easiest — one file, double-click (like the Mac installer):**
1. Download **[colourMatik-windows-setup.exe](https://github.com/iyiailerobotu-sudo/colourmatik-releases/releases/download/windows-latest/colourMatik-windows-setup.exe)** from the latest release.
2. Double-click it. (If SmartScreen warns: *More info ▸ Run anyway* — the effect itself is
   CI-built from this repo.) The whole program is inside the Setup — nothing is downloaded from
   GitHub. It installs Python 3.11 / ffmpeg (winget) if missing, the engine and its Python packages,
   the Premiere and After Effects panels, the native effect, and engine autostart. Approve the one
   admin prompt (Premiere's shared plug-ins folder).
3. **Restart Premiere Pro** → *Window ▸ UXP Plugins ▸ colourMatik*.

*(Manual alternative: Code ▸ Download ZIP → run `windows\install-windows.cmd`.)*

**Updating:** the panel updates itself in one click (it checks at most once a day); by hand:
`windows\update-windows.cmd` — it downloads the newest Setup (one file, through the GitHub API) and
installs the program inside it; every step logs to `%APPDATA%\colourMatik\update.log`. · **Removing:** `windows\uninstall-windows.cmd` ·
**Engine console (debug):** `windows\colourmatik-app.cmd`

*Building the installers:* Windows — `windows\setup\build-setup.ps1` (NSIS 3; packs the committed tree
into `colourMatik-windows-setup.exe`). macOS — `./mac/app/build-app.sh sign` on the Mac (packs it into
the notarized installer app as `payload.zip`). A release is one file per fixed tag (`windows-latest`,
`darwin-latest`) plus its versioned copy (`win-vX.Y.Z` / `mac-vX.Y.Z`, published right after).

*Building the Windows effect yourself:* the `.aex` is compiled by the
[`windows-effect`](.github/workflows/windows-effect.yml) GitHub Action — run it manually and
paste a download URL for Adobe's **Windows** After Effects SDK zip (Adobe's license doesn't
allow us to bundle the SDK). The action reuses the SDK's own sample project, so Adobe's
official PiPL build steps apply unmodified.

## Use it (2 clicks)

1. Open **Window ▸ UXP Plugins ▸ colourMatik**.
2. Select the **reference** clip → *Use selected clip*; select the **target** clip → *Use selected clip*.
3. Pick **Accurate** or **Cinematic AI**, then **Match & Apply**. The `colourMatik` effect is added
   automatically. Drag **Intensity** to taste (live).

There's also a headless CLI: `./colourmatik-cli target.mp4 reference.mp4 -o match.cube`.

---

## How it works

- **Engine** (`colourmatik/`) — a local FastAPI server that does the colour maths + AI and bakes a
  33³/65³ `.cube` LUT. Runs on `http://127.0.0.1:8765`; the `.cube` also works in DaVinci Resolve / FCP.
- **Panel** (`colourmatik-uxp/`) — the Premiere UXP panel; reads the selected clips, calls the
  engine, and adds/configures the native effect.
- **Native effect** (`colourmatik-fx/`) — a real After Effects–SDK effect that applies the LUT with a
  built-in Intensity slider (C++ source + a pre-built Apple-Silicon build).

Run the tests with `PYTHONPATH=. ./.venv/bin/python tests/run_tests.py`.

## Credits & third-party

- **CanonCGT** — *Reference-Based Color Grading via Canonical Pivot Representation*, CVPR 2026
  ([repo](https://github.com/Jinwon-Ko/CanonCGT), Apache-2.0) — fetched by `setup.sh`.
- **SegFormer** (NVIDIA, ADE20K) via 🤗 Transformers — scene segmentation.
- Classical transport after Reinhard (2001) and Pitié & Kokaram (Monge–Kantorovich / IDT).

Personal tool by **Sevki Bugra Ozbek** — [catheadai.com](https://catheadai.com).
