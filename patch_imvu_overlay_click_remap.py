import argparse
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile


DEFAULT_JAR = os.path.join(os.environ.get("APPDATA", ""), "IMVUClient", "ui", "chrome", "imvuContent.jar")
PATCH_MARKER = "codex-px13-overlay-click-remap"

TARGETS = {
    "avatar_menu/AvatarMenu.js": r"function\s+AvatarMenu\s*\(\s*args\s*\)\s*\{",
    "room_widget/RoomWidget.js": r"RoomWidget\s*=\s*function\s*\(\s*args\s*\)\s*\{",
}


JS_PATCH = r"""
    // codex-px13-overlay-click-remap
    (function() {
        if (window.__px13OverlayClickRemapInstalled) {
            return;
        }
        window.__px13OverlayClickRemapInstalled = true;
        var scale = 2.5;

        function classNameOf(el) {
            return (el && el.className) ? String(el.className) : "";
        }

        function isUsefulClickTarget(el) {
            while (el && el !== document && el !== document.body && el !== document.documentElement) {
                var cls = classNameOf(el);
                if (/\b(ui-event|context-button|tab|tabImage|closeButton|colorSwatch|imvu-button)\b/.test(cls)) {
                    return el;
                }
                if (el.id && /^(dismiss|nameHolder|toggleHolder|addFavorite|info|explore|move|rotate|scale|clone|straighten|lock|delete|undo|redo)$/.test(el.id)) {
                    return el;
                }
                if (el.getAttribute && (el.getAttribute("actionid") || el.getAttribute("href"))) {
                    return el;
                }
                try {
                    if (window.getComputedStyle && window.getComputedStyle(el, null).cursor === "pointer") {
                        return el;
                    }
                } catch (e) {}
                el = el.parentNode;
            }
            return null;
        }

        function dispatchRemappedClick(originalEvent, target, x, y) {
            var ev = document.createEvent("MouseEvents");
            ev.initMouseEvent(
                "click",
                true,
                true,
                window,
                1,
                x,
                y,
                x,
                y,
                originalEvent.ctrlKey,
                originalEvent.altKey,
                originalEvent.shiftKey,
                originalEvent.metaKey,
                originalEvent.button || 0,
                null
            );
            ev.__px13Remapped = true;
            target.dispatchEvent(ev);
        }

        document.addEventListener("click", function(evt) {
            if (evt.__px13Remapped || evt.button) {
                return;
            }

            var x = Math.round(evt.clientX * scale);
            var y = Math.round(evt.clientY * scale);
            var scaledTarget = document.elementFromPoint(x, y);
            var useful = isUsefulClickTarget(scaledTarget);
            if (!useful || useful === evt.target) {
                return;
            }

            evt.preventDefault();
            evt.stopPropagation();
            if (evt.stopImmediatePropagation) {
                evt.stopImmediatePropagation();
            }
            dispatchRemappedClick(evt, useful, x, y);
        }, true);
    })();
"""


def parse_args():
    parser = argparse.ArgumentParser(description="Patch or restore IMVU overlay click remapping.")
    parser.add_argument("--jar", default=DEFAULT_JAR)
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def imvu_is_running(jar):
    imvu_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(jar)))).lower()
    try:
        output = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process IMVUClient -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Path",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        return False
    for line in output.splitlines():
        path = line.strip()
        if path and os.path.dirname(path).lower() == imvu_dir:
            return True
    return False


def newest_backup(path):
    dirname = os.path.dirname(os.path.abspath(path))
    basename = os.path.basename(path)
    matches = []
    for name in os.listdir(dirname):
        if name.startswith(basename + ".bak-clickremap-"):
            matches.append(os.path.join(dirname, name))
    return sorted(matches)[-1] if matches else None


def patch_js(text, entry_pattern):
    if PATCH_MARKER in text:
        old = """            var direct = isUsefulClickTarget(evt.target);
            if (direct && direct === evt.target) {
                return;
            }

            var x = Math.round(evt.clientX * scale);
            var y = Math.round(evt.clientY * scale);
            var scaledTarget = document.elementFromPoint(x, y);
            var useful = isUsefulClickTarget(scaledTarget);
            if (!useful || useful === direct || useful === evt.target) {
                return;
            }
"""
        new = """            var x = Math.round(evt.clientX * scale);
            var y = Math.round(evt.clientY * scale);
            var scaledTarget = document.elementFromPoint(x, y);
            var useful = isUsefulClickTarget(scaledTarget);
            if (!useful || useful === evt.target) {
                return;
            }
"""
        if old in text:
            return text.replace(old, new, 1)
        return text
    matches = list(re.finditer(entry_pattern, text))
    count = len(matches)
    if count != 1:
        raise RuntimeError("Expected one constructor pattern %r, found %s" % (entry_pattern, count))
    match = matches[0]
    return text[:match.end()] + JS_PATCH + "\n" + text[match.end():]


def rewrite_jar(path, replacements):
    dirname = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(prefix="imvuContent.clickremap.", suffix=".jar", dir=dirname)
    os.close(fd)
    try:
        with zipfile.ZipFile(path, "r") as zin:
            with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                for info in zin.infolist():
                    data = replacements.get(info.filename)
                    if data is None:
                        data = zin.read(info.filename)
                    zout.writestr(info, data)
        os.chmod(path, 0o666)
        try:
            os.replace(temp_path, path)
        except OSError:
            os.remove(path)
            os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def main():
    args = parse_args()
    jar = os.path.abspath(args.jar)
    if imvu_is_running(jar) and not args.force:
        print("IMVUClient is running. Close IMVU completely, then rerun this script.")
        return 2

    if args.restore:
        backup = newest_backup(jar)
        if not backup:
            raise SystemExit("No clickremap backup found.")
        shutil.copy2(backup, jar)
        print("Restored %s from %s" % (jar, backup))
        return 0

    replacements = {}
    with zipfile.ZipFile(jar, "r") as zf:
        for filename, marker in TARGETS.items():
            text = zf.read(filename).decode("utf-8")
            replacements[filename] = patch_js(text, marker).encode("utf-8")

    backup = "%s.bak-clickremap-%s" % (jar, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(jar, backup)
    rewrite_jar(jar, replacements)
    print("Patched overlay click remapping in %s" % jar)
    print("Backup: %s" % backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
