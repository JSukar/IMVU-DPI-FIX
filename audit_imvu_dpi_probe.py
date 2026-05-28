import argparse
import json
import math
import os
import subprocess
import sys
import time


CURRENT_PROBE = "audit_current_probe.jsonl"


def load_first_record(path):
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                return json.loads(line)
    raise RuntimeError("No JSON records found in %s" % path)


def flatten(record):
    rows = []
    for top_index, window in enumerate(record.get("windows", [])):
        item = dict(window)
        item["_top_index"] = top_index
        item["_depth"] = 0
        rows.append(item)
        for child in window.get("children", []):
            child_item = dict(child)
            child_item["_top_index"] = top_index
            child_item["_depth"] = 1
            rows.append(child_item)
    return rows


def uri_key(title):
    if title.startswith("HTML Overlay '") and title.endswith("'"):
        title = title[len("HTML Overlay '"):-1]
    if "chrome://imvu/content/" in title:
        return title.split("chrome://imvu/content/", 1)[1].split("?", 1)[0]
    if title.startswith("http://") or title.startswith("https://"):
        return title.split("?", 1)[0]
    return title


def row_key(row):
    title = row.get("title") or ""
    cls = row.get("class") or ""
    key = uri_key(title)
    if key:
        return "%s|%s" % (cls, key)
    return "%s|<blank>" % cls


def area(row):
    w, h = row.get("client_size", [0, 0])
    return max(0, w) * max(0, h)


def useful_rows(record):
    rows = []
    for row in flatten(record):
        w, h = row.get("client_size", [0, 0])
        title = row.get("title") or ""
        cls = row.get("class") or ""
        if w == 0 and h == 0 and not title:
            continue
        if not cls:
            continue
        rows.append(row)
    return rows


def group_rows(record):
    groups = {}
    for row in useful_rows(record):
        key = row_key(row)
        groups.setdefault(key, []).append(row)
    for key in groups:
        groups[key].sort(key=area, reverse=True)
    return groups


def ratio(value, baseline):
    if not baseline:
        return None
    return float(value) / float(baseline)


def fmt_ratio(value):
    if value is None:
        return "n/a"
    return "%.3f" % value


def classify_ratio(rx, ry, dpi_scale):
    values = [v for v in (rx, ry) if v is not None]
    if not values:
        return "no-baseline"
    if all(0.9 <= v <= 1.1 for v in values):
        return "ok"
    inverse = 1.0 / dpi_scale if dpi_scale else 0.4
    if any(abs(v - inverse) <= 0.08 for v in values):
        return "under-scaled"
    if any(abs(v - dpi_scale) <= 0.25 for v in values):
        return "over-scaled"
    if any(v < 0.75 for v in values):
        return "small"
    if any(v > 1.25 for v in values):
        return "large"
    return "drift"


def capture_current(out_path, samples, interval):
    if os.path.exists(out_path):
        os.remove(out_path)
    cmd = [
        sys.executable,
        "imvu_dpi_runtime_probe.py",
        "--children",
        "--out",
        out_path,
    ]
    if samples > 1:
        cmd.extend(["--watch", "--interval", str(interval)])
        proc = subprocess.Popen(cmd)
        try:
            time.sleep(max(0.2, samples * interval + 0.5))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    else:
        subprocess.check_call(cmd)


def pick_latest_record(path):
    last = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                last = json.loads(line)
    if last is None:
        raise RuntimeError("No JSON records found in %s" % path)
    return last


def report(baseline, current, tolerance, dpi_scale):
    base_groups = group_rows(baseline)
    cur_groups = group_rows(current)
    all_keys = sorted(set(base_groups) | set(cur_groups))

    lines = []
    lines.append("# IMVU DPI Audit")
    lines.append("")
    lines.append("baseline windows: %s" % len(useful_rows(baseline)))
    lines.append("current windows: %s" % len(useful_rows(current)))
    lines.append("dpi scale expectation: %.2fx" % dpi_scale)
    lines.append("")
    lines.append("| status | key | baseline | current | ratio | dpi |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: |")

    issues = 0
    for key in all_keys:
        base = base_groups.get(key, [])
        cur = cur_groups.get(key, [])
        if not base:
            row = cur[0]
            cw, ch = row.get("client_size", [0, 0])
            lines.append("| new | `%s` | - | %sx%s | - | %s |" % (key, cw, ch, row.get("dpi", "")))
            continue
        if not cur:
            row = base[0]
            bw, bh = row.get("client_size", [0, 0])
            lines.append("| missing | `%s` | %sx%s | - | - | %s |" % (key, bw, bh, row.get("dpi", "")))
            issues += 1
            continue

        b = base[0]
        c = cur[0]
        bw, bh = b.get("client_size", [0, 0])
        cw, ch = c.get("client_size", [0, 0])
        rx = ratio(cw, bw)
        ry = ratio(ch, bh)
        status = classify_ratio(rx, ry, dpi_scale)
        if status == "ok":
            if rx is not None and abs(rx - 1.0) <= tolerance and ry is not None and abs(ry - 1.0) <= tolerance:
                continue
        else:
            issues += 1
        lines.append(
            "| %s | `%s` | %sx%s | %sx%s | %s x %s | %s |"
            % (status, key, bw, bh, cw, ch, fmt_ratio(rx), fmt_ratio(ry), c.get("dpi", ""))
        )

    lines.append("")
    lines.append("issue count: %s" % issues)
    return "\n".join(lines) + "\n"


def parse_args():
    parser = argparse.ArgumentParser(description="Capture and audit all IMVU DPI windows.")
    parser.add_argument("--baseline", default="compatible_probe.jsonl")
    parser.add_argument("--current", default=CURRENT_PROBE)
    parser.add_argument("--capture", action="store_true", help="Capture current IMVU state before auditing.")
    parser.add_argument("--samples", type=int, default=1, help="Samples to capture when --capture is used.")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--dpi-scale", type=float, default=2.5)
    parser.add_argument("--tolerance", type=float, default=0.12)
    parser.add_argument("--out", default="output.md")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.capture:
        capture_current(args.current, args.samples, args.interval)

    baseline = load_first_record(args.baseline)
    current = pick_latest_record(args.current)
    text = report(baseline, current, args.tolerance, args.dpi_scale)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
