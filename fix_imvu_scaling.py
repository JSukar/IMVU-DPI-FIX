#!/usr/bin/env python3
"""Resize IMVU Classic and optionally set Windows DPI compatibility.

This helper does not patch IMVU files.  It only:
  * finds visible IMVUClient.exe top-level windows,
  * optionally sets the Windows compatibility DPI override for IMVUClient.exe,
  * restores/maximizes/resizes the window.

Close and reopen IMVU after changing --compat-dpi.
"""

import argparse
import ctypes
import os
import subprocess
import sys
import time
import winreg
from ctypes import wintypes


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE = 9
SW_MAXIMIZE = 3
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
MONITOR_DEFAULTTONEAREST = 2

DEFAULT_PROCESS_NAME = "imvuclient.exe"
DEFAULT_EXE = os.path.join(os.environ.get("APPDATA", ""), "IMVUClient", "IMVUClient.exe")
LAYERS_KEY = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"


USER32 = ctypes.WinDLL("user32", use_last_error=True)
KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
SHCORE = ctypes.WinDLL("shcore", use_last_error=True)


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

USER32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
USER32.EnumWindows.restype = wintypes.BOOL
USER32.IsWindowVisible.argtypes = [wintypes.HWND]
USER32.IsWindowVisible.restype = wintypes.BOOL
USER32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
USER32.GetWindowTextLengthW.restype = ctypes.c_int
USER32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
USER32.GetWindowTextW.restype = ctypes.c_int
USER32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
USER32.GetWindowThreadProcessId.restype = wintypes.DWORD
USER32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
USER32.ShowWindow.restype = wintypes.BOOL
USER32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint,
]
USER32.SetWindowPos.restype = wintypes.BOOL
USER32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
USER32.MonitorFromWindow.restype = wintypes.HMONITOR
USER32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
USER32.GetMonitorInfoW.restype = wintypes.BOOL

KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
KERNEL32.OpenProcess.restype = wintypes.HANDLE
KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
KERNEL32.CloseHandle.restype = wintypes.BOOL
KERNEL32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
KERNEL32.QueryFullProcessImageNameW.restype = wintypes.BOOL


def win_error(prefix):
    return ctypes.WinError(ctypes.get_last_error(), prefix)


def query_process_path(pid):
    handle = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not KERNEL32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        KERNEL32.CloseHandle(handle)


def query_process_name(pid):
    return os.path.basename(query_process_path(pid)).lower()


def get_window_title(hwnd):
    length = USER32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    USER32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def find_imvu_windows(process_name):
    process_name = process_name.lower()
    windows = []

    def callback(hwnd, _lparam):
        if not USER32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return True
        exe_path = query_process_path(pid.value)
        if os.path.basename(exe_path).lower() != process_name:
            return True
        title = get_window_title(hwnd)
        if not title and not exe_path:
            return True
        windows.append((hwnd, pid.value, title, exe_path))
        return True

    if not USER32.EnumWindows(EnumWindowsProc(callback), 0):
        raise win_error("EnumWindows failed")
    return windows


def get_dpi_for_window(hwnd):
    try:
        get_dpi = USER32.GetDpiForWindow
    except AttributeError:
        get_dpi = None
    if get_dpi:
        get_dpi.argtypes = [wintypes.HWND]
        get_dpi.restype = ctypes.c_uint
        dpi = int(get_dpi(hwnd))
        if dpi:
            return dpi

    monitor = USER32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    dpi_x = ctypes.c_uint(96)
    dpi_y = ctypes.c_uint(96)
    try:
        if SHCORE.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
            return int(dpi_x.value or 96)
    except Exception:
        pass
    return 96


def get_work_area(hwnd):
    monitor = USER32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not USER32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        raise win_error("GetMonitorInfoW failed")
    return info.rcWork


def resize_window(hwnd, mode, base_width, base_height, margin):
    dpi = get_dpi_for_window(hwnd)
    scale = float(dpi) / 96.0 if dpi else 1.0

    work = get_work_area(hwnd)
    work_width = int(work.right - work.left)
    work_height = int(work.bottom - work.top)

    USER32.ShowWindow(hwnd, SW_RESTORE)
    if mode == "maximize":
        USER32.ShowWindow(hwnd, SW_MAXIMIZE)
        return dpi, scale, work_width, work_height

    if mode == "workarea":
        target_width = work_width
        target_height = work_height
    else:
        target_width = min(int(round(base_width * scale)), max(640, work_width - margin * 2))
        target_height = min(int(round(base_height * scale)), max(480, work_height - margin * 2))

    x = int(work.left + max(0, (work_width - target_width) // 2))
    y = int(work.top + max(0, (work_height - target_height) // 2))

    ok = USER32.SetWindowPos(
        hwnd,
        None,
        x,
        y,
        int(target_width),
        int(target_height),
        SWP_NOZORDER | SWP_NOACTIVATE,
    )
    if not ok:
        raise win_error("SetWindowPos failed")
    return dpi, scale, target_width, target_height


def set_compat_dpi_override(exe_path, override):
    if not exe_path or override == "none":
        return

    values = {
        "application": "~ HIGHDPIAWARE",
        "system": "~ DPIUNAWARE",
        "system-enhanced": "~ GDIDPISCALING DPIUNAWARE",
    }
    value = values[override]
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, LAYERS_KEY)
    try:
        winreg.SetValueEx(key, exe_path, 0, winreg.REG_SZ, value)
    finally:
        winreg.CloseKey(key)
    print("Set compatibility DPI override for %r to %r" % (exe_path, value))


def clear_compat_dpi_override(exe_path):
    if not exe_path:
        return
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, LAYERS_KEY, 0, winreg.KEY_SET_VALUE)
    except OSError:
        return
    try:
        try:
            winreg.DeleteValue(key, exe_path)
            print("Cleared compatibility DPI override for %r" % exe_path)
        except FileNotFoundError:
            pass
    finally:
        winreg.CloseKey(key)


def set_imvu_dpi_pref(value):
    if value in ("skip", None):
        return
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\IMVU\dpiScaling")
    try:
        winreg.SetValue(key, "", winreg.REG_SZ, value)
    finally:
        winreg.CloseKey(key)
    print(r"Set HKCU\Software\IMVU\dpiScaling to %r" % value)


def apply_preset_defaults(args):
    """Make the public presets behave like the original helper script."""
    if args.preset == "sharp":
        if args.compat_dpi == "none" and not args.clear_compat_dpi:
            args.compat_dpi = "application"
        if args.imvu_dpi_pref == "skip":
            args.imvu_dpi_pref = "Recommended"
        return

    if args.preset == "lowres":
        if args.compat_dpi == "none" and not args.clear_compat_dpi:
            args.compat_dpi = "system"
        if args.imvu_dpi_pref == "skip":
            args.imvu_dpi_pref = "Recommended"
        return

    if args.preset == "compatible":
        if args.compat_dpi == "none" and not args.clear_compat_dpi:
            args.compat_dpi = "system-enhanced"
        if args.imvu_dpi_pref == "skip":
            args.imvu_dpi_pref = "Recommended"
        return

    if args.preset == "system":
        if args.compat_dpi == "none" and not args.clear_compat_dpi:
            args.compat_dpi = "system"
        if args.imvu_dpi_pref == "skip":
            args.imvu_dpi_pref = "Recommended"
        return


def maybe_start_imvu(path):
    if not path:
        path = DEFAULT_EXE
    if not os.path.exists(path):
        raise RuntimeError("IMVUClient.exe not found: %s" % path)
    subprocess.Popen([path], cwd=os.path.dirname(path), close_fds=True)
    print("Started %s" % path)


def parse_args():
    parser = argparse.ArgumentParser(description="Resize IMVU Classic windows and manage Windows DPI compatibility.")
    parser.add_argument("--process-name", default=DEFAULT_PROCESS_NAME)
    parser.add_argument(
        "--preset",
        default="lowres",
        choices=["lowres", "sharp", "compatible", "system", "custom"],
        help="'lowres' makes IMVU see the Windows scaled desktop instead of the physical panel.",
    )
    parser.add_argument("--start", action="store_true", help="Start IMVU before searching for windows.")
    parser.add_argument("--exe", default=DEFAULT_EXE, help="Path used with --start.")
    parser.add_argument("--watch", action="store_true", help="Keep applying the selected mode.")
    parser.add_argument("--interval", type=float, default=2.0, help="Watch polling interval in seconds.")
    parser.add_argument("--mode", default="scaled", choices=["maximize", "workarea", "scaled"])
    parser.add_argument(
        "--compat-dpi",
        default="none",
        choices=["none", "application", "system", "system-enhanced"],
        help="Set Windows DPI compatibility override. Requires IMVU restart.",
    )
    parser.add_argument("--clear-compat-dpi", action="store_true", help="Remove Windows DPI compatibility override.")
    parser.add_argument(
        "--imvu-dpi-pref",
        default="skip",
        choices=["skip", "Recommended", "System", "Off"],
        help="Set HKCU\\Software\\IMVU\\dpiScaling.",
    )
    parser.add_argument("--base-width", type=int, default=1024)
    parser.add_argument("--base-height", type=int, default=768)
    parser.add_argument("--margin", type=int, default=40)
    return parser.parse_args()


def apply_once(args):
    windows = find_imvu_windows(args.process_name)
    if not windows:
        print("No visible %s windows found." % args.process_name)
        return 1

    for hwnd, pid, title, exe_path in windows:
        if args.clear_compat_dpi:
            clear_compat_dpi_override(exe_path)
        else:
            set_compat_dpi_override(exe_path, args.compat_dpi)

        dpi, scale, width, height = resize_window(
            hwnd,
            args.mode,
            args.base_width,
            args.base_height,
            args.margin,
        )
        label = title or "<untitled>"
        print(
            "Fixed hwnd=%s pid=%s title=%r dpi=%s scale=%.2f mode=%s size=%sx%s"
            % (hwnd, pid, label, dpi, scale, args.mode, width, height)
        )
    return 0


def main():
    args = parse_args()
    apply_preset_defaults(args)
    set_imvu_dpi_pref(args.imvu_dpi_pref)

    if args.start:
        maybe_start_imvu(args.exe)
        time.sleep(3.0)

    if not args.watch:
        return apply_once(args)

    while True:
        try:
            apply_once(args)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print("Error: %s" % exc, file=sys.stderr)
        time.sleep(max(0.25, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
