import argparse
import os
import re
import shutil
import tempfile
import time
import zipfile


DEFAULT_JAR = os.path.join(os.environ.get("APPDATA", ""), "IMVUClient", "ui", "chrome", "imvuContent.jar")
TAB_CSS = "tab_bar/TabBar.css"
THREED_HTML = "3d/index.html"
MARKER = "codex-px13-white-line-fix"


def parse_args():
    parser = argparse.ArgumentParser(description="Patch or restore IMVU white-line background fill.")
    parser.add_argument("--jar", default=DEFAULT_JAR)
    parser.add_argument("--restore", action="store_true")
    return parser.parse_args()


def newest_backup(path):
    dirname = os.path.dirname(os.path.abspath(path))
    basename = os.path.basename(path)
    matches = []
    for name in os.listdir(dirname):
        if name.startswith(basename + ".bak-whiteline-"):
            matches.append(os.path.join(dirname, name))
    return sorted(matches)[-1] if matches else None


def patch_tab_css(text):
    if MARKER in text:
        return text

    text, count = re.subn(
        r"(#tabBar\s*\{\s*)",
        r"\1\n    /* %s */\n    " % MARKER,
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError("Could not find #tabBar rule.")

    text, count = re.subn(
        r"height:\s*68px;",
        "height: 100%;\n    min-height: 68px;",
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError("Could not find tab height.")

    text, count = re.subn(
        r"body\s*\{\s*overflow\s*:\s*hidden\s*;\s*\}",
        "html, body {\n    overflow:hidden;\n    background-color:#131313;\n}",
        text,
        count=1,
    )
    if count != 1:
        text += "\nhtml, body {\n    overflow:hidden;\n    background-color:#131313;\n}\n"
    return text


def patch_3d_html(text):
    if MARKER in text:
        return text
    if "html, body { width: 100%; height: 100%; margin: 0; padding: 0; overflow: hidden;" in text:
        text = text.replace(
            "html, body { width: 100%; height: 100%; margin: 0; padding: 0; overflow: hidden;",
            "html, body { width: 100%; height: 100%; margin: 0; padding: 0; overflow: hidden; background: #000; /* %s */" % MARKER,
            1,
        )
    if "object, #SceneViewer { width: 100%; height: 100%;" in text and "object, #SceneViewer { width: 100%; height: 100%; background: #000;" not in text:
        text = text.replace(
            "object, #SceneViewer { width: 100%; height: 100%;",
            "object, #SceneViewer { width: 100%; height: 100%; background: #000;",
            1,
        )
    return text


def rewrite_jar(path, replacements):
    dirname = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(prefix="imvuContent.whiteline.", suffix=".jar", dir=dirname)
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
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def main():
    args = parse_args()
    jar = os.path.abspath(args.jar)
    if args.restore:
        backup = newest_backup(jar)
        if not backup:
            raise SystemExit("No whiteline backup found.")
        shutil.copy2(backup, jar)
        print("Restored %s from %s" % (jar, backup))
        return 0

    with zipfile.ZipFile(jar, "r") as zf:
        tab_css = zf.read(TAB_CSS).decode("utf-8")
        threed_html = zf.read(THREED_HTML).decode("utf-8")

    patched = {
        TAB_CSS: patch_tab_css(tab_css).encode("utf-8"),
        THREED_HTML: patch_3d_html(threed_html).encode("utf-8"),
    }
    backup = "%s.bak-whiteline-%s" % (jar, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(jar, backup)
    rewrite_jar(jar, patched)
    print("Patched white-line backgrounds in %s" % jar)
    print("Backup: %s" % backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
