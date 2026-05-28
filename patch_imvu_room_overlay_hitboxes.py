import argparse
import os
import shutil
import subprocess
import tempfile
import time
import zipfile


DEFAULT_LIBRARY = os.path.join(os.environ.get("APPDATA", ""), "IMVUClient", "library.zip")
GECKO_SOURCE = "imvu/gecko/geckocontext.py"
MARKER = "# IMVU PX13 avatar label hitbox patch"
TAB_SEAM_MARKER = "# IMVU PX13 tab seam fill patch"
TAB_SEAM_SNIPPET = """        # IMVU PX13 tab seam fill patch
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

"""
TAB_SEAM_INSERT_AFTER = """    def __px13CompensatedRect(self, x, y, w, h):
        scale = self.__px13DpiScale()
"""
HEIGHT_ONLY_SNIPPET = """        # IMVU PX13 avatar label hitbox patch
        # The name-label visual width is already about right in sharp mode, but
        # its Gecko input window is too short.  Scale only height so the "i"
        # button receives mouse events without making names huge.
        if self.__px13UriHasAny(('avatar_name_label/index.html',)):
            return (
                int(x),
                int(y),
                max(0, int(w)),
                max(0, int(round(h * scale))),
            )
"""
FULL_SIZE_SNIPPET = """        # IMVU PX13 avatar label hitbox patch
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
"""


OLD_BLOCK = """    def __px13CompensatedRect(self, x, y, w, h):
        scale = self.__px13DpiScale()
        if scale <= 1.0 or not self.__px13ShouldCompensateOverlay():
            return (x, y, w, h)
        return (
            int(x),
            int(y),
            max(0, int(round(w * scale))),
            max(0, int(round(h * scale))),
        )

"""


NEW_BLOCK = """    def __px13CompensatedRect(self, x, y, w, h):
        scale = self.__px13DpiScale()
        if scale <= 1.0:
            return (x, y, w, h)

        # IMVU PX13 avatar label hitbox patch
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

"""


def parse_args():
    parser = argparse.ArgumentParser(description="Patch or restore IMVU room overlay hitboxes.")
    parser.add_argument("--library", default=DEFAULT_LIBRARY)
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


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


def newest_backup(path):
    dirname = os.path.dirname(os.path.abspath(path))
    basename = os.path.basename(path)
    matches = []
    for name in os.listdir(dirname):
        if name.startswith(basename + ".bak-hitbox-"):
            matches.append(os.path.join(dirname, name))
    return sorted(matches)[-1] if matches else None


def patch_source(text):
    if "IMVU PX13 clean DPI overlay patch" not in text:
        raise RuntimeError("Expected the clean DPI overlay patch in geckocontext.py first.")

    if MARKER not in text:
        count = text.count(OLD_BLOCK)
        if count != 1:
            raise RuntimeError("Expected one clean compensated-rect block, found %s" % count)
        text = text.replace(OLD_BLOCK, NEW_BLOCK, 1)
    elif HEIGHT_ONLY_SNIPPET in text:
        text = text.replace(HEIGHT_ONLY_SNIPPET, FULL_SIZE_SNIPPET, 1)

    if TAB_SEAM_MARKER not in text:
        count = text.count(TAB_SEAM_INSERT_AFTER)
        if count != 1:
            raise RuntimeError("Expected one compensated-rect header, found %s" % count)
        text = text.replace(TAB_SEAM_INSERT_AFTER, TAB_SEAM_INSERT_AFTER + TAB_SEAM_SNIPPET, 1)

    compile(text, GECKO_SOURCE, "exec")
    return text


def rewrite_zip(path, patched_source):
    dirname = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(prefix="library.hitbox.", suffix=".zip", dir=dirname)
    os.close(fd)
    try:
        with zipfile.ZipFile(path, "r") as zin:
            with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                for info in zin.infolist():
                    data = patched_source.encode("utf-8") if info.filename == GECKO_SOURCE else zin.read(info.filename)
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
    library = os.path.abspath(args.library)
    if imvu_is_running(library) and not args.force:
        print("IMVUClient is running. Close IMVU completely, then rerun this script.")
        return 2

    if args.restore:
        backup = newest_backup(library)
        if not backup:
            raise SystemExit("No hitbox backup found.")
        shutil.copy2(backup, library)
        print("Restored %s from %s" % (library, backup))
        return 0

    with zipfile.ZipFile(library, "r") as zf:
        source = zf.read(GECKO_SOURCE).decode("utf-8")
    patched = patch_source(source)
    backup = "%s.bak-hitbox-%s" % (library, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(library, backup)
    rewrite_zip(library, patched)
    print("Patched avatar label hitboxes in %s" % library)
    print("Backup: %s" % backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
