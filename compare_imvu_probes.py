import json
import sys


KEYS = [
    "tab_bar/index.html",
    "toolstrip/index.html",
    "3d/index.html",
    "avatar_name_label/index.html",
    "avatar_menu/index.html",
    "room_widget/index.html",
    "tool/chat/index.html",
]


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.loads(f.readline())


def flatten(record):
    rows = []
    for window in record["windows"]:
        rows.append(window)
        rows.extend(window.get("children", []))
    return rows


def key_for(row):
    title = row.get("title", "")
    for key in KEYS:
        if key in title:
            return key
    if row.get("title") == "3D Scene":
        return "native 3D Scene"
    return None


def collect(record):
    result = {}
    for row in flatten(record):
        key = key_for(row)
        if key and (key not in result or row.get("class") == "MozillaUIWindowClass"):
            result[key] = row
    return result


def ratio(a, b):
    if not b:
        return ""
    return "%.3f" % (float(a) / float(b))


def main():
    compatible_path = sys.argv[1] if len(sys.argv) > 1 else "compatible_probe.jsonl"
    sharp_path = sys.argv[2] if len(sys.argv) > 2 else "sharp_probe.jsonl"
    compatible = collect(load(compatible_path))
    sharp = collect(load(sharp_path))

    print("key, compatible_size, sharp_size, sharp/compatible")
    for key in KEYS + ["native 3D Scene"]:
        c = compatible.get(key)
        s = sharp.get(key)
        if not c or not s:
            continue
        cw, ch = c["client_size"]
        sw, sh = s["client_size"]
        print("%s, %sx%s, %sx%s, %s x %s" % (key, cw, ch, sw, sh, ratio(sw, cw), ratio(sh, ch)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
