# IMVU Classic Sharp-DPI Fix Toolkit (Windows)

Technical tooling for diagnosing and patching IMVU Classic DPI-scaling regressions on high-DPI displays (for example, 240 DPI / 250% scale).

This repository focuses on **mechanical, reversible binary/source patching** of IMVU runtime assets (`library.zip` and `imvuContent.jar`) and **window-level instrumentation** to validate behavior before/after each patch.

---

## 1) Problem Model

When IMVU Classic runs in sharp/high-DPI mode, multiple rendering/input layers can disagree on coordinate systems:

- Native Win32 window layout may be scaled by monitor DPI.
- Embedded Gecko HTML surfaces may be scaled independently.
- Hit-testing can occur in a differently scaled surface than what is rendered.

Observed symptoms include:

- Overlay click targets offset from visual elements.
- In-room overlay hitboxes smaller/larger than drawn content.
- Tab/3D seam artifacts (white line/background bleed).
- Dialog/card content clipping due to double-scaling.

The scripts here treat these as **separate failure classes** and patch each class with isolated changes and explicit backup/restore behavior.

---

## 2) Repository Layout

- `imvu_dpi_runtime_probe.py`  
  Runtime window/DPI probe (JSONL).
- `compare_imvu_probes.py`  
  Focused diff for key room windows (`compatible` vs `sharp`).
- `audit_imvu_dpi_probe.py`  
  Broad audit across visible IMVU child windows; emits Markdown report.
- `patch_imvu_clean_dpi_layout.py`  
  Core layout + Gecko compensation patch for sharp mode.
- `patch_imvu_room_overlay_hitboxes.py`  
  Overlay hitbox tuning (depends on clean-layout Gecko patch).
- `patch_imvu_overlay_click_remap.py`  
  Overlay click remap in packaged UI JS (`imvuContent.jar`).
- `patch_imvu_dialog_scaling.py`  
  HTML dialog/card scaling fix (preserve native sizing, remove harmful CSS transform scale effect).
- `patch_imvu_white_line.py`  
  Tab/3D background seam fix.

---

## 3) Environment and Constraints

### Supported runtime assumptions

- OS: Windows 10/11.
- Python: Python 3.x available in PATH.
- Target app: IMVU Classic installed under `%APPDATA%\IMVUClient` (default paths assumed by scripts).
- Permissions: write access to IMVU install/runtime asset directory.

### Files modified by scripts

- `%APPDATA%\IMVUClient\library.zip`
- `%APPDATA%\IMVUClient\ui\chrome\imvuContent.jar`
- Registry keys (optional, for sharp-mode config):
  - `HKCU\Software\IMVU\dpiScaling`
  - `HKCU\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers`

### Safety model

- Every mutating patch script writes a timestamped backup before replacing targets.
- Most scripts block mutation while `IMVUClient.exe` is running (unless `--force` is provided where supported).
- Restore mode is script-specific and restores from latest matching backup suffix.

---

## 4) Instrumentation Pipeline

Use instrumentation before and after patching to detect regressions.

### 4.1 Single/multi-sample runtime capture

```powershell
python .\imvu_dpi_runtime_probe.py --children --out .\compatible_probe.jsonl
python .\imvu_dpi_runtime_probe.py --children --watch --interval 1.0 --out .\sharp_probe.jsonl
```

What is captured per window:

- Process + HWND identity
- Class/title
- Window/client rectangles
- Per-window DPI (`GetDpiForWindow` fallback chain)
- Cursor coordinate conversions (`ScreenToClient`, roundtrip sanity)
- Monitor geometry (`MonitorFromWindow` + `GetMonitorInfoW`)

### 4.2 Focused compare on key room surfaces

```powershell
python .\compare_imvu_probes.py .\compatible_probe.jsonl .\sharp_probe.jsonl
```

Outputs per known key URI:

- Compatible client size
- Sharp client size
- Ratio sharp/compatible for width and height

### 4.3 Broad audit report (Markdown)

```powershell
python .\audit_imvu_dpi_probe.py --baseline .\compatible_probe.jsonl --capture --samples 3 --interval 1.0 --out .\output.md
```

Classification buckets include:

- `ok`
- `under-scaled`
- `over-scaled`
- `small`
- `large`
- `drift`
- `new`
- `missing`

This script groups windows by `(class, normalized-title/URI)` and compares dominant instances by area.

---

## 5) Patch Scripts (Deep Technical)

## 5.1 `patch_imvu_clean_dpi_layout.py` (core patch)

Primary responsibilities:

1. Rebuild `library.zip` from a chosen baseline backup.
2. Patch selected marshalled integer constants in `.pyo` payloads:
   - `toplevelwindow.pyo`: `71 -> 178`
   - `toolstrip.pyo`: `26 -> 65`
   - `HtmlTool.pyo`: `220 -> 550`
3. Replace packaged Gecko context source with a synthesized/patched `imvu/gecko/geckocontext.py`.

Gecko patch behavior:

- Adds DPI scale helper (`dpi / 96.0`).
- Compensates specific overlay URIs by scaling Gecko surface size.
- Includes tab-bar seam overdraw (`+3` height) to hide native/Gecko rounding gap.
- Keeps compensation narrow to selected overlays; avoids global Gecko inflation.

Notable options:

- `--baseline <path>` explicit baseline backup.
- `--restore` restore latest `bak-cleanlayout-*`.
- `--configure-sharp` write registry values for sharp mode.
- `--launch` configure sharp mode and launch IMVU.

Example:

```powershell
python .\patch_imvu_clean_dpi_layout.py --configure-sharp
```

---

## 5.2 `patch_imvu_room_overlay_hitboxes.py`

Purpose:

- Refines `geckocontext.py` compensation for in-room overlays and avatar labels.

Key mechanics:

- Requires clean-layout patch marker to already exist.
- Rewrites `__px13CompensatedRect` block.
- Ensures tab seam patch snippet exists.
- Promotes avatar label compensation to full-size scaling where needed.

Restore source:

- Latest `library.zip.bak-hitbox-*`.

Example:

```powershell
python .\patch_imvu_room_overlay_hitboxes.py
```

---

## 5.3 `patch_imvu_overlay_click_remap.py`

Target: `imvuContent.jar` JavaScript overlays:

- `avatar_menu/AvatarMenu.js`
- `room_widget/RoomWidget.js`

Injected logic:

- Global click listener in capture phase.
- Computes scaled point (`clientX/clientY * 2.5`).
- Uses `document.elementFromPoint` at scaled coords.
- Filters for actionable targets (class/id/cursor heuristics).
- Prevents original event and dispatches synthetic click to remapped target.

Restore source:

- Latest `imvuContent.jar.bak-clickremap-*`.

Example:

```powershell
python .\patch_imvu_overlay_click_remap.py
```

---

## 5.4 `patch_imvu_dialog_scaling.py`

Goal:

- Preserve native dialog sizing while disabling effective Gecko-side over-scaling path for HTML dialogs/cards.

Patch details:

- Rebuilds `imvu/gecko/HTMLDialog/windows.py` from decompiled structured source.
- Fixes known decompiler artifact in `__dialogSpec`.
- Replaces `dpiScaleFactor(...)` usage with `scale = 1.0` in rescale path.
- Drops stale bytecode entry and writes source entry only.

Restore source:

- Latest `library.zip.bak-dialogdpi-*`.

Example:

```powershell
python .\patch_imvu_dialog_scaling.py
```

---

## 5.5 `patch_imvu_white_line.py`

Goal:

- Remove visible seam/background mismatch between tab bar and 3D content areas.

Patch details:

- `tab_bar/TabBar.css`
  - adjusts tab bar height logic (`height: 100%`, `min-height: 68px`)
  - enforces dark background on `html, body`
- `3d/index.html`
  - enforces black background on root/body and scene viewer container

Restore source:

- Latest `imvuContent.jar.bak-whiteline-*`.

Example:

```powershell
python .\patch_imvu_white_line.py
```

---

## 6) Recommended End-to-End Runbook

1. Close IMVU completely.
2. Capture baseline probe in compatible mode.
3. Apply core patch:
   - `patch_imvu_clean_dpi_layout.py --configure-sharp`
4. Optionally apply targeted patches:
   - `patch_imvu_room_overlay_hitboxes.py`
   - `patch_imvu_overlay_click_remap.py`
   - `patch_imvu_dialog_scaling.py`
   - `patch_imvu_white_line.py`
5. Relaunch IMVU in sharp mode.
6. Capture sharp probe.
7. Run compare + audit and inspect ratio/classification changes.

If behavior regresses, restore the specific patch family first rather than rolling back everything at once.

---

## 7) Troubleshooting Matrix

- `IMVUClient is running...`  
  Close all IMVU processes; rerun without `--force` when possible.

- `Expected one ... found N` patch errors  
  Your IMVU build likely differs from the expected byte/source signature.  
  Action: collect your current `library.zip`/`imvuContent.jar` signatures and update patch patterns safely.

- `No ... backup found` on restore  
  Script-specific backup suffix not present in target directory.  
  Action: verify path overrides (`--library` / `--jar`) and backup naming family.

- Probe output appears empty  
  IMVU process name/path mismatch.  
  Action: pass explicit `--process` to `imvu_dpi_runtime_probe.py`.

---

## 8) Development Notes

- Changes are intentionally small, deterministic, and reversible.
- Patch scripts avoid broad binary rewrite logic and instead validate expected signatures before mutation.
- Runtime probes are non-intrusive: no injection, no process memory patching.

---

## 9) Contributing

High-value contributions:

- Additional patch signatures for newer IMVU builds.
- Better URI-key normalization in audit tooling.
- Automated regression harness over saved probe datasets.
- Documentation of behavior across monitor topologies and mixed-DPI setups.

When contributing patches, include:

- IMVU version/build metadata
- Before/after probe JSONL samples
- Expected size-ratio deltas for key windows
- Clear rollback path

---

## 10) Disclaimer

This project modifies local IMVU client assets for compatibility/debugging on high-DPI systems. Use at your own risk.  
Back up your installation and verify changes in a non-critical environment before daily use.

This project is not affiliated with, endorsed by, or sponsored by IMVU, Inc.  
IMVU is a trademark of its respective owner.

---

## 11) License

Licensed under the MIT License. See `LICENSE` for the full text.
