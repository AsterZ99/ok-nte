"""Phase 3 input capability probe for the cloud NTE window.

Sends test input to the cloud game window and records a capability matrix.
Read-only EXCEPT for the deliberately sent input; the user must be watching
the game. Safety rules:

- The target is resolved fail-closed: no unambiguous main window, no input.
- Every dispatch re-verifies the HWND identity before sending.
- Key down/up are always paired; held keys are released in a finally block.
- Background PostMessage never moves the user's cursor.

Frame diffs come from WGC captures taken through the cloud streaming latency
window, so a near-zero diff only means "no visible change yet"; the human
watching the game makes the final call.
"""

import argparse
import ctypes
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import win32con
import win32gui

from tools import cloud_nte_probe as probe
from tools.cloud_wgc_spike import _capture_frames_impl

MAPVK_VK_TO_VSC = 0
INPUT_WAIT_SECONDS = 0.8
DIFF_PIXEL_THRESHOLD = 8
RECEIVED_RATIO_HINT = 0.01

KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
FOREGROUND_TIMEOUT_SECONDS = 10.0


class _KeybdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeybdInput), ("padding", ctypes.c_ubyte * 32)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _InputUnion)]


def sendinput_key(scan, down):
    """Hardware-level key injection. Goes to whatever window has focus."""
    flags = KEYEVENTF_SCANCODE
    if not down:
        flags |= KEYEVENTF_KEYUP
    key = _KeybdInput(0, scan, flags, 0, None)
    item = _Input(INPUT_KEYBOARD, _InputUnion(ki=key))
    return user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(_Input)) == 1

KEY_TAPS = (
    ("E", 0x45),
    ("Esc", 0x1B),
    ("Q", 0x51),
    ("R", 0x52),
    ("F", 0x46),
    ("1", 0x31),
    ("2", 0x32),
    ("3", 0x33),
    ("Space", 0x20),
)

user32 = ctypes.windll.user32


def post_key(hwnd, vk, down):
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    lparam = 1 | (scan << 16)
    if not down:
        lparam |= (1 << 30) | (1 << 31)
    msg = win32con.WM_KEYDOWN if down else win32con.WM_KEYUP
    try:
        # pywin32 returns None on success and raises win32gui.error on failure.
        win32gui.PostMessage(hwnd, msg, vk, lparam)
        return True
    except win32gui.error:
        return False


def post_click(hwnd, x, y, down):
    lparam = (y << 16) | (x & 0xFFFF)
    try:
        if down:
            win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
        else:
            win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lparam)
        return True
    except win32gui.error:
        return False


def post_scroll(hwnd, x, y, delta):
    lparam = (y << 16) | (x & 0xFFFF)
    wparam = (win32con.WHEEL_DELTA * delta) << 16
    try:
        win32gui.PostMessage(hwnd, win32con.WM_MOUSEWHEEL, wparam, lparam)
        return True
    except win32gui.error:
        return False


def verify_target(hwnd):
    """Identity gate re-checked before every dispatch. Returns error or None."""
    if not win32gui.IsWindow(hwnd):
        return "hwnd is gone"
    class_name = win32gui.GetClassName(hwnd)
    if class_name != "Qt51517QWindowIcon":
        return f"class changed: {class_name!r}"
    title = win32gui.GetWindowText(hwnd)
    if title != "云·异环":
        return f"title changed: {title!r}"
    if win32gui.IsIconic(hwnd):
        return "window is minimized"
    return None


def grab_frame(hwnd):
    frames, _size, _border = _capture_frames_impl(hwnd, 2, False)
    if not frames:
        return None
    return frames[-1][1]


def frame_diff(before, after):
    if before is None or after is None or before.shape != after.shape:
        return None
    delta = np.abs(before[:, :, :3].astype(int) - after[:, :, :3].astype(int))
    return float((delta > DIFF_PIXEL_THRESHOLD).any(axis=2).mean())


def run_foreground_sendinput(resolution, output_dir):
    """SendInput test with fail-closed foreground verification."""
    hwnd = resolution.target.hwnd
    print("mode: foreground SendInput; move focus to the game window within the countdown")
    print(f"starting in {FOREGROUND_TIMEOUT_SECONDS:.0f}s; click the game window NOW...")
    time.sleep(FOREGROUND_TIMEOUT_SECONDS)
    foreground = user32.GetForegroundWindow()
    if foreground != hwnd:
        print(f"foreground gate failed: foreground={foreground} expected={hwnd}; no input was sent")
        return 1
    print("foreground gate passed; sending two key taps (Esc, then E)")
    sent_esc = sendinput_key(0x01, True)
    time.sleep(0.08)
    sendinput_key(0x01, False)
    time.sleep(1.5)
    sent_e = sendinput_key(0x12, True)
    time.sleep(0.08)
    sendinput_key(0x12, False)
    matrix = {
        "mode": "foreground_sendinput",
        "target": {"hwnd": hwnd, "class": resolution.target.class_name,
                   "title": resolution.target.title, "process": resolution.target.process_name},
        "foreground_verified": True,
        "entries": [
            {"action": "key:Esc", "sent": sent_esc},
            {"action": "key:E", "sent": sent_e},
        ],
    }
    path = os.path.join(output_dir, "input_sendinput_matrix.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(matrix, handle, ensure_ascii=False, indent=2)
    print(f"sent Esc={sent_esc} E={sent_e}; matrix saved: {path}")
    print("human checklist: did the Esc menu open/close? did E trigger?")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output-dir", required=True,
        help="matrix JSON and frames; must stay outside this repository",
    )
    parser.add_argument(
        "--target-hwnd", type=lambda value: int(value, 0), default=0,
        help="override the input target; default resolves the main window",
    )
    parser.add_argument("--countdown", type=float, default=3.0, help="seconds before first input")
    parser.add_argument(
        "--mode",
        choices=["background-postmessage", "foreground-sendinput"],
        default="background-postmessage",
        help="input injection path to test",
    )
    args = parser.parse_args(argv)

    from src.interaction import cloud_window as cw

    records = cw.collect_windows()
    resolution = cw.resolve_cloud_target(records)
    if resolution.status != "resolved":
        print(f"target not resolved (status={resolution.status}); no input was sent")
        return 1
    hwnd = resolution.target.hwnd
    error = verify_target(hwnd)
    if error:
        print(f"target gate failed: {error}; no input was sent")
        return 1
    target = resolution.target
    print(f"target: hwnd={hwnd} class={target.class_name!r} title={target.title!r}")

    output_dir = probe._ensure_untracked_output(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    if args.mode == "foreground-sendinput":
        return run_foreground_sendinput(resolution, output_dir)

    print(f"starting in {args.countdown:.0f}s; switch to watch the game...")
    time.sleep(args.countdown)

    held = set()
    matrix = []
    baseline = grab_frame(hwnd)

    def gated(hwnd_=hwnd):
        problem = verify_target(hwnd_)
        if problem:
            print(f"  gate blocked dispatch: {problem}")
        return problem is None

    def record(action, sent, diff, note=""):
        received = diff is not None and diff >= RECEIVED_RATIO_HINT
        verdict = "likely received" if received else "no visible change"
        entry = {
            "action": action,
            "sent": sent,
            "diff_ratio": None if diff is None else round(diff, 4),
            "verdict": verdict,
            "note": note,
        }
        matrix.append(entry)
        diff_text = "n/a" if diff is None else f"{diff:.4f}"
        print(f"  {action}: sent={sent} diff={diff_text} -> {verdict}")

    try:
        for name, vk in KEY_TAPS:
            if not gated():
                break
            sent = post_key(hwnd, vk, True)
            time.sleep(0.08)
            if sent:
                post_key(hwnd, vk, False)
            else:
                held.discard(vk)
            time.sleep(INPUT_WAIT_SECONDS)
            after = grab_frame(hwnd)
            record(f"key:{name}", bool(sent), frame_diff(baseline, after))
            baseline = after

        if gated():
            sent_down = post_key(hwnd, 0x57, True)  # W
            if sent_down:
                held.add(0x57)
            time.sleep(1.0)
            post_key(hwnd, 0x57, False)
            held.discard(0x57)
            time.sleep(INPUT_WAIT_SECONDS)
            after = grab_frame(hwnd)
            record("key:W hold 1s", bool(sent_down), frame_diff(baseline, after))
            baseline = after

        if gated():
            client = win32gui.GetClientRect(hwnd)
            cx, cy = client[2] // 2, client[3] // 2
            sent = post_scroll(hwnd, cx, cy, 3)
            time.sleep(INPUT_WAIT_SECONDS)
            after = grab_frame(hwnd)
            record("wheel:+3 at center", bool(sent), frame_diff(baseline, after))
            baseline = after

        if gated():
            client = win32gui.GetClientRect(hwnd)
            cx, cy = client[2] // 2, client[3] // 2
            sent = post_click(hwnd, cx, cy, True)
            time.sleep(0.06)
            post_click(hwnd, cx, cy, False)
            time.sleep(INPUT_WAIT_SECONDS)
            after = grab_frame(hwnd)
            record("left click at center", bool(sent), frame_diff(baseline, after))
    finally:
        for vk in list(held):
            post_key(hwnd, vk, False)
        held.clear()

    matrix_path = os.path.join(output_dir, "input_capability_matrix.json")
    with open(matrix_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "target": {"hwnd": hwnd, "class": target.class_name,
                           "title": target.title, "process": target.process_name},
                "mode": "background_postmessage",
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "entries": matrix,
            },
            handle, ensure_ascii=False, indent=2,
        )
    print(f"matrix saved: {matrix_path}")
    print("human checklist: menu toggling (Esc), camera/character movement (W hold),")
    print("skill effects (E/Q/R), wheel zoom, click response; the diff is a hint only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
