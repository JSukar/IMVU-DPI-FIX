import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile


DEFAULT_IMVU_DIR = os.path.join(os.environ.get("APPDATA", ""), "IMVUClient")
SOURCE = os.path.join(
    "library_decompiled_structured", "imvu", "gecko", "HTMLDialog", "windows.py"
)
ZIP_SOURCE = "imvu/gecko/HTMLDialog/windows.py"
ZIP_BYTECODE = "imvu/gecko/HTMLDialog/windows.pyo"
PATCH_MARKER = "# IMVU PX13 dialog scaling patch"


REPLACEMENTS = [
    (
        "        scale = dpi.dpiScaleFactor(self.__serviceProvider)\n",
        "        scale = 1.0\n",
    ),
]


def parse_args():
    parser = argparse.ArgumentParser(description="Patch or restore HTMLDialog card scaling.")
    parser.add_argument("--imvu-dir", default=DEFAULT_IMVU_DIR)
    parser.add_argument("--library", help="Path to library.zip. Overrides --imvu-dir.")
    parser.add_argument("--restore", action="store_true")
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


def newest_backup(library):
    matches = sorted(glob.glob(library + ".bak-dialogdpi-*"))
    return matches[-1] if matches else None


def restore(library):
    backup = newest_backup(library)
    if not backup:
        raise RuntimeError("No dialogdpi backup found for %s" % library)
    shutil.copy2(backup, library)
    return backup


def build_source():
    with open(SOURCE, "r", encoding="utf-8") as f:
        text = f.read()

    if PATCH_MARKER in text:
        raise RuntimeError("Source already contains patch marker.")

    dialog_spec_bug = (
        "        self.__dialogSpec = [\n"
        "         [\n"
        "          6, rect, style, exstyle, 0, 0, 0]]\n"
    )
    dialog_spec_fix = (
        "        self.__dialogSpec = [\n"
        "         [\n"
        "          'loading...', rect, style, exstyle, None, None, None]]\n"
    )
    count = text.count(dialog_spec_bug)
    if count != 1:
        raise RuntimeError("Expected one HTMLDialog dialog template artifact, found %s" % count)
    text = text.replace(dialog_spec_bug, dialog_spec_fix)

    for old, new in REPLACEMENTS:
        count = text.count(old)
        if count != 1:
            raise RuntimeError("Expected one occurrence of %r, found %s" % (old, count))
        text = text.replace(old, new)

    text = text.replace(
        "    def rescale(self):\n        scale = 1.0\n",
        "    def rescale(self):\n        # IMVU PX13 dialog scaling patch\n        scale = 1.0\n",
    )

    stripped = text.rstrip()
    if stripped.endswith("\nreturn"):
        text = stripped[:-len("\nreturn")] + "\n"

    compile(text, ZIP_SOURCE, "exec")
    return text.encode("utf-8")


def rewrite(library, patched_source):
    backup = "%s.bak-dialogdpi-%s" % (library, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(library, backup)

    fd, temp_path = tempfile.mkstemp(prefix="library.dialogdpi.", suffix=".zip", dir=os.path.dirname(os.path.abspath(library)))
    os.close(fd)

    try:
        with zipfile.ZipFile(library, "r") as zin:
            payloads = []
            for info in zin.infolist():
                if info.filename in (ZIP_BYTECODE, ZIP_SOURCE):
                    continue
                payloads.append((info, zin.read(info.filename)))

        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for info, data in payloads:
                zout.writestr(info, data)
            info = zipfile.ZipInfo(ZIP_SOURCE, time.localtime(time.time())[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zout.writestr(info, patched_source)

        os.chmod(library, 0o666)
        os.replace(temp_path, library)
        return backup
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


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
        backup = restore(library)
        print("Restored %s from %s" % (library, backup))
        return 0

    patched_source = build_source()
    backup = rewrite(library, patched_source)
    print("Patched HTMLDialog scaling in %s" % library)
    print("Backup: %s" % backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
