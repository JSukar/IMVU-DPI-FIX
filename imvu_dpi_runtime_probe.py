import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import time


USER32 = ctypes.windll.user32
KERNEL32 = ctypes.windll.kernel32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MONITOR_DEFAULTTONEAREST = 2


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wt.LONG),
        ("top", wt.LONG),
        ("right", wt.LONG),
        ("bottom", wt.LONG),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wt.DWORD),
    ]


EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
EnumChildProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def rect_tuple(rect):
    return [rect.left, rect.top, rect.right, rect.bottom]


def rect_size(rect):
    return [rect.right - rect.left, rect.bottom - rect.top]


def get_text(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    USER32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def get_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    USER32.GetClassNameW(hwnd, buf, len(buf))
    return buf.value


def get_rect(hwnd, client=False):
    rect = RECT()
    if client:
        USER32.GetClientRect(hwnd, ctypes.byref(rect))
    else:
        USER32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect


def query_process_path(pid):
    handle = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wt.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        ok = KERNEL32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        if not ok:
            return ""
        return buf.value
    finally:
        KERNEL32.CloseHandle(handle)


def hwnd_pid(hwnd):
    pid = wt.DWORD()
    USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def get_dpi(hwnd):
    try:
        return int(USER32.GetDpiForWindow(hwnd))
    except Exception:
        try:
            return int(USER32.GetDpiForSystem())
        except Exception:
            return 96


def get_monitor(hwnd):
    monitor = USER32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if monitor and USER32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return {
            "monitor_rect": rect_tuple(info.rcMonitor),
            "monitor_size": rect_size(info.rcMonitor),
            "work_rect": rect_tuple(info.rcWork),
            "work_size": rect_size(info.rcWork),
        }
    return {}


def client_to_screen(hwnd, x, y):
    pt = POINT(int(x), int(y))
    USER32.ClientToScreen(hwnd, ctypes.byref(pt))
    return [pt.x, pt.y]


def screen_to_client(hwnd, x, y):
    pt = POINT(int(x), int(y))
    USER32.ScreenToClient(hwnd, ctypes.byref(pt))
    return [pt.x, pt.y]


def find_imvu_top_windows(process_name):
    windows = []

    def callback(hwnd, _):
        if not USER32.IsWindowVisible(hwnd):
            return True
        pid = hwnd_pid(hwnd)
        path = query_process_path(pid)
        if os.path.basename(path).lower() == process_name.lower():
            windows.append(hwnd)
        return True

    USER32.EnumWindows(EnumWindowsProc(callback), 0)
    return windows


def enum_children(hwnd):
    children = []

    def callback(child, _):
        children.append(child)
        return True

    USER32.EnumChildWindows(hwnd, EnumChildProc(callback), 0)
    return children


def describe_window(hwnd):
    wr = get_rect(hwnd, client=False)
    cr = get_rect(hwnd, client=True)
    center_client = [max(0, (cr.right - cr.left) // 2), max(0, (cr.bottom - cr.top) // 2)]
    center_screen = client_to_screen(hwnd, center_client[0], center_client[1])
    return {
        "hwnd": int(hwnd),
        "pid": hwnd_pid(hwnd),
        "class": get_class(hwnd),
        "title": get_text(hwnd),
        "dpi": get_dpi(hwnd),
        "window_rect": rect_tuple(wr),
        "window_size": rect_size(wr),
        "client_rect": rect_tuple(cr),
        "client_size": rect_size(cr),
        "client_center_to_screen": center_screen,
        "roundtrip_center": screen_to_client(hwnd, center_screen[0], center_screen[1]),
    }


def sample(process_name, include_children):
    tops = find_imvu_top_windows(process_name)
    result = []
    cursor = POINT()
    USER32.GetCursorPos(ctypes.byref(cursor))

    for hwnd in tops:
        item = describe_window(hwnd)
        item.update(get_monitor(hwnd))
        item["cursor_screen"] = [cursor.x, cursor.y]
        item["cursor_client"] = screen_to_client(hwnd, cursor.x, cursor.y)
        if include_children:
            children = []
            for child in enum_children(hwnd):
                child_info = describe_window(child)
                child_info["cursor_client"] = screen_to_client(child, cursor.x, cursor.y)
                children.append(child_info)
            item["children"] = children
        result.append(item)
    return result


def parse_args():
    parser = argparse.ArgumentParser(description="Probe live IMVU DPI/window coordinate state.")
    parser.add_argument("--process", default="IMVUClient.exe")
    parser.add_argument("--children", action="store_true", help="Include child windows.")
    parser.add_argument("--watch", action="store_true", help="Sample repeatedly.")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--out", default="imvu_dpi_probe.jsonl", help="JSONL output path.")
    return parser.parse_args()


def main():
    args = parse_args()
    with open(args.out, "a", encoding="utf-8") as f:
        while True:
            record = {"time": time.time(), "windows": sample(args.process, args.children)}
            line = json.dumps(record, sort_keys=True)
            print(line)
            f.write(line + "\n")
            f.flush()
            if not args.watch:
                break
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
