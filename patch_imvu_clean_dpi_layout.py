import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
import zipfile


DEFAULT_IMVU_DIR = os.path.join(os.environ.get("APPDATA", ""), "IMVUClient")
LAYERS_KEY = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"

GECKO_SOURCE_PATH = os.path.join(
    "library_decompiled_structured", "imvu", "gecko", "geckocontext.py"
)
ZIP_GECKO_SOURCE = "imvu/gecko/geckocontext.py"
ZIP_GECKO_BYTECODE = "imvu/gecko/geckocontext.pyo"

LAYOUT_PATCHES = {
    "imvu/client/toplevelwindow.pyo": [(71, 178)],
    "imvu/client/toolstrip.pyo": [(26, 65)],
    "imvu/tool/HtmlTool.pyo": [(220, 550)],
}

PATCH_MARKER = "# IMVU PX13 clean DPI overlay patch"

INSERT_BEFORE = """    def __getattr__(self, name):
        return getattr(self.__window, name)
"""

OVERLAY_PATCH_BLOCK = """    # IMVU PX13 clean DPI overlay patch
    def __px13UriHasAny(self, fragments):
        uri = self.__uri or ''
        for fragment in fragments:
            if fragment in uri:
                return True
        return False

    def __px13DpiScale(self):
        try:
            user32 = ctypes.windll.user32
            hwnd = getattr(self.__window, 'hwnd', 0)
            try:
                dpi = user32.GetDpiForWindow(hwnd)
            except Exception:
                try:
                    dpi = user32.GetDpiForSystem()
                except Exception:
                    dpi = 96
            dpi = int(dpi or 96)
        except Exception:
            dpi = 96
        if dpi <= 96:
            return 1.0
        return float(dpi) / 96.0

    def __px13ShouldCompensateOverlay(self):
        return self.__px13UriHasAny((
            'avatar_menu/index.html',
            'room_widget/index.html',
            'widgets/camera/index.html',
        ))

    def __px13CompensatedRect(self, x, y, w, h):
        scale = self.__px13DpiScale()

        # The native layout and Gecko child height round differently at 240 DPI,
        # leaving a one-pixel gap below the tabs.  Overdraw the tab Gecko child
        # slightly without changing the layout position of the room content.
        if self.__px13UriHasAny(('tab_bar/index.html',)):
            return (
                int(x),
                int(y),
                max(0, int(w)),
                max(0, int(h) + 3),
            )

        if scale <= 1.0:
            return (x, y, w, h)

        # The name-label HTML is still drawn at its normal font size.  What is
        # wrong in sharp mode is the off-screen Gecko surface used for hit
        # testing, so enlarge that surface without changing the CSS.
        if self.__px13UriHasAny(('avatar_name_label/index.html',)):
            return (
                int(x),
                int(y),
                max(0, int(round(w * scale))),
                max(0, int(round(h * scale))),
            )

        if not self.__px13ShouldCompensateOverlay():
            return (x, y, w, h)
        return (
            int(x),
            int(y),
            max(0, int(round(w * scale))),
            max(0, int(round(h * scale))),
        )

    def setPositionAndSize(self, x, y, w, h):
        x, y, w, h = self.__px13CompensatedRect(x, y, w, h)
        self.__window.setPositionAndSize(x, y, w, h)
        return

"""


def parse_args():
    parser = argparse.ArgumentParser(description="Apply or restore the clean PX13 DPI layout patch.")
    parser.add_argument("--imvu-dir", default=DEFAULT_IMVU_DIR)
    parser.add_argument("--library", help="Path to library.zip. Overrides --imvu-dir.")
    parser.add_argument("--baseline", help="Original baseline library.zip backup to start from.")
    parser.add_argument("--scale", type=float, default=2.5)
    parser.add_argument("--restore", action="store_true", help="Restore newest clean-layout backup.")
    parser.add_argument("--configure-sharp", action="store_true")
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def library_path(args):
    if args.library:
        return os.path.abspath(args.library)
    return os.path.join(args.imvu_dir, "library.zip")


def imvu_is_running(library):
    imvu_dir = os.path.dirname(os.path.abspath(library)).lower()
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


def find_baseline(library, explicit):
    if explicit:
        if not os.path.exists(explicit):
            raise RuntimeError("Baseline does not exist: %s" % explicit)
        return explicit
    preferred = library + ".bak-20260527-112715"
    if os.path.exists(preferred):
        return preferred
    candidates = sorted(glob.glob(library + ".bak-*"))
    if not candidates:
        raise RuntimeError("No library.zip baseline backups found.")
    return candidates[0]


def newest_clean_backup(library):
    matches = sorted(glob.glob(library + ".bak-cleanlayout-*"))
    return matches[-1] if matches else None


def marshalled_int(value):
    return b"i" + int(value).to_bytes(4, "little", signed=True)


def patch_layout_bytes(entry, data):
    for old, new in LAYOUT_PATCHES.get(entry, []):
        old_token = marshalled_int(old)
        new_token = marshalled_int(new)
        count = data.count(old_token)
        if count < 1:
            new_count = data.count(new_token)
            if new_count >= 1:
                continue
            raise RuntimeError("%s: expected %s at least once, found 0" % (entry, old))
        data = data.replace(old_token, new_token)
    return data


def build_gecko_source():
    with open(GECKO_SOURCE_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    text = text.replace(
        "import inspect, logging, os, urllib, urlparse, imvu.program",
        "import ctypes, inspect, logging, os, urllib, urlparse, imvu.program",
    )

    decompiler_bug = """    except AttributeError:
        for methodName, method in inspect.getmembers(ch, inspect.ismethod):
            if methodName.startswith('handle_'):
                yield (
                 methodName[7:], method)

    for methodName, method in imvuCallHandlers.items():
"""
    decompiler_fix = """    except AttributeError:
        for methodName, method in inspect.getmembers(ch, inspect.ismethod):
            if methodName.startswith('handle_'):
                yield (
                 methodName[7:], method)
        return

    for methodName, method in imvuCallHandlers.items():
"""
    if text.count(decompiler_bug) != 1:
        raise RuntimeError("Could not repair geckocontext getCallHandlers artifact.")
    text = text.replace(decompiler_bug, decompiler_fix)

    stripped = text.rstrip()
    if stripped.endswith("\nreturn"):
        text = stripped[:-len("\nreturn")] + "\n"

    if text.count(INSERT_BEFORE) != 1:
        raise RuntimeError("Could not find GeckoContext __getattr__ insertion point.")
    text = text.replace(INSERT_BEFORE, OVERLAY_PATCH_BLOCK + INSERT_BEFORE)
    compile(text, ZIP_GECKO_SOURCE, "exec")
    return text.encode("utf-8")


def rewrite_from_baseline(library, baseline):
    backup = "%s.bak-cleanlayout-%s" % (library, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(library, backup)

    fd, temp_path = tempfile.mkstemp(prefix="library.clean.", suffix=".zip", dir=os.path.dirname(library))
    os.close(fd)
    gecko_source = build_gecko_source()

    try:
        with zipfile.ZipFile(baseline, "r") as zin:
            with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                for info in zin.infolist():
                    if info.filename in (ZIP_GECKO_BYTECODE, ZIP_GECKO_SOURCE):
                        continue
                    data = zin.read(info.filename)
                    data = patch_layout_bytes(info.filename, data)
                    zout.writestr(info, data)

                info = zipfile.ZipInfo(ZIP_GECKO_SOURCE, time.localtime(time.time())[:6])
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zout.writestr(info, gecko_source)
        os.chmod(library, 0o666)
        os.replace(temp_path, library)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise
    return backup


def set_imvu_dpi_pref(value):
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\IMVU\dpiScaling")
    try:
        winreg.SetValue(key, "", winreg.REG_SZ, value)
    finally:
        winreg.CloseKey(key)


def set_application_dpi_override(exe_path):
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, LAYERS_KEY)
    try:
        winreg.SetValueEx(key, exe_path, 0, winreg.REG_SZ, "~ HIGHDPIAWARE")
    finally:
        winreg.CloseKey(key)


def configure_sharp(imvu_dir):
    exe = os.path.join(imvu_dir, "IMVUClient.exe")
    if not os.path.exists(exe):
        raise RuntimeError("Missing IMVUClient.exe: %s" % exe)
    set_imvu_dpi_pref("Recommended")
    set_application_dpi_override(exe)
    return exe


def main():
    args = parse_args()
    library = library_path(args)
    if not os.path.exists(library):
        print("Missing library.zip: %s" % library, file=sys.stderr)
        return 1
    if imvu_is_running(library) and not args.force:
        print("IMVUClient is running. Close IMVU completely, then rerun this script.", file=sys.stderr)
        return 2

    if args.restore:
        backup = newest_clean_backup(library)
        if not backup:
            print("No clean-layout backup found.", file=sys.stderr)
            return 1
        shutil.copy2(backup, library)
        print("Restored %s from %s" % (library, backup))
        return 0

    baseline = find_baseline(library, args.baseline)
    backup = rewrite_from_baseline(library, baseline)
    print("Rebuilt %s from baseline %s" % (library, baseline))
    print("Backup of previous state: %s" % backup)
    print("Layout constants: tab 178, toolstrip 65, chat 550; overlay Gecko correction only.")

    exe = None
    if args.configure_sharp or args.launch:
        exe = configure_sharp(os.path.dirname(os.path.abspath(library)))
        print("Configured sharp mode: dpiScaling='Recommended', AppCompat='~ HIGHDPIAWARE'.")
    if args.launch:
        subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True)
        print("Launched IMVU.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
