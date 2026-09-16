"""Phase 1 spike: bind the real ok-script runtime to the cloud NTE window.

Read-only diagnostic for the cloud NTE adaptation (Phase 1). It starts a
headless OK runtime whose Windows config points at the cloud client
(exe/class/title), waits for HwndWindow binding and WGC connection, grabs
frames through the framework's own capture lifecycle, tests close/rebind, and
quits cleanly. It never sends input and never modifies the target window.
"""

import os
import sys
import tempfile
import time

import win32gui

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import cloud_nte_probe as probe

CLOUD_EXE = "NTECloudGame.exe"
CLOUD_CLASS = "Qt51517QWindowIcon"
CLOUD_TITLE = "云·异环"


def wait_for_hwnd_window(device_manager, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd_window = device_manager.hwnd_window
        if hwnd_window is not None and hwnd_window.exists and hwnd_window.hwnd:
            return hwnd_window
        time.sleep(0.5)
    raise TimeoutError(f"HwndWindow did not bind within {timeout}s")


def select_main_window():
    records = probe.collect_windows()
    evaluations = probe.evaluate_windows(records)
    for hwnd in probe.select_candidates(evaluations)["hwnds"]:
        try:
            if win32gui.GetWindowText(hwnd) == CLOUD_TITLE:
                return hwnd
        except win32gui.error:
            continue
    return 0


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--interact", action="store_true",
        help="bind CloudNTEInteraction and send a small input sequence (game must be watched)",
    )
    args = parser.parse_args(argv)

    hwnd = select_main_window()
    if not hwnd:
        print("no candidate window with the expected title found; is the cloud NTE client running?")
        return 1
    title = win32gui.GetWindowText(hwnd)
    if title != CLOUD_TITLE:
        print(f"candidate hwnd={hwnd} title={title!r} does not match expected {CLOUD_TITLE!r}")
        return 1
    print(f"expecting runtime to bind hwnd={hwnd} class={win32gui.GetClassName(hwnd)!r}")

    from ok import OK

    config_folder = tempfile.mkdtemp(prefix="ok_nte_cloud_spike_")
    from ok import ConfigOption

    key_config_option = ConfigOption(
        "Game Hotkey Config",
        {"Skill Key": "e", "Ultimate Key": "q", "Arc Key": "r", "Use QWERTY Physical Keys": False},
    )
    interaction_list = []
    if args.interact:
        from src.interaction.CloudNTEInteraction import CloudNTEInteraction

        interaction_list = [CloudNTEInteraction]
    config = {
        "windows": {
            "exe": CLOUD_EXE,
            "hwnd_class": CLOUD_CLASS,
            "title": CLOUD_TITLE,
            "capture_method": ["WGC"],
            "interaction": interaction_list,
            "require_bg": True,
            "start_exe": False,
            "check_hdr": False,
            "force_no_hdr": False,
        },
        "gui": None,
        "use_gui": False,
        "debug": True,
        "onetime_tasks": [],
        "trigger_tasks": [],
        "custom_tasks": False,
        "custom_tabs": [],
        "analytics": None,
        "check_mutex": False,
        "disable_file_log": True,
        "locale": "zh_CN",
        "config_folder": config_folder,
        "start_timeout": 20,
        "global_configs": [key_config_option],
    }
    runtime = OK(config)
    try:
        device_manager = runtime.device_manager
        hwnd_window = wait_for_hwnd_window(device_manager)
        bound = hwnd_window.hwnd
        print(f"bound hwnd={bound} expected={hwnd} match={bound == hwnd}")
        if bound != hwnd:
            print("FAIL: runtime bound a different window")
            return 1

        from ok.device.capture_methods.windows_graphics import WindowsGraphicsCaptureMethod

        capture = WindowsGraphicsCaptureMethod(hwnd_window)
        for round_index in range(3):
            frame = capture.do_get_frame()
            if frame is None:
                print(f"round {round_index}: no frame")
                return 1
            height, width = frame.shape[:2]
            colored = int((frame[:, :, :3].any(axis=2)).sum())
            ratio = colored / (width * height)
            print(f"round {round_index}: frame {width}x{height} content_ratio={ratio:.4f}")
            if (width, height) != (1920, 1080):
                print("FAIL: unexpected frame size")
                return 1
            if round_index == 1:
                capture.close()
                print("capture closed; recreating to verify the rebind path")
                capture = WindowsGraphicsCaptureMethod(hwnd_window)

        out_dir = probe._ensure_untracked_output(
            os.path.join(tempfile.gettempdir(), "ok_nte_cloud_runtime_spike")
        )
        os.makedirs(out_dir, exist_ok=True)
        import cv2

        path = os.path.join(out_dir, "runtime_frame.png")
        cv2.imwrite(path, frame)
        print(f"saved last frame: {path}")

        if args.interact:
            from src.interaction.CloudNTEInteraction import CloudNTEInteraction

            interaction = CloudNTEInteraction(device_manager.capture_method, hwnd_window)
            device_manager.interaction = interaction
            print(f"interaction: {type(interaction).__name__}")
            print("sending via CloudNTEInteraction: E tap, W hold 1s, center click")
            interaction.send_key("e")
            time.sleep(1.2)
            interaction.send_key_down("w")
            time.sleep(1.0)
            interaction.send_key_up("w")
            time.sleep(1.2)
            width, height = frame.shape[1], frame.shape[0]
            interaction.click(width // 2, height // 2)
            time.sleep(1.0)
            print("input sequence done")

        print("runtime lifecycle spike: OK")
        return 0
    finally:
        try:
            runtime.quit()
        except Exception as error:
            print(f"quit error (ignored): {error!r}")


if __name__ == "__main__":
    raise SystemExit(main())
