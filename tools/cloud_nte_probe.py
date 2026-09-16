"""Read-only window probe for the cloud NTE client.

Phase 0 tool of the cloud NTE adaptation (see
docs/zh-CN/development/云异环适配开发设计文档.md). It only collects facts about
the local window tree. It never activates, moves, resizes or closes windows,
never sends keyboard or mouse input, and never saves a screenshot unless the
user explicitly passes --capture-test together with --capture-output.

Privacy rules enforced by default:

- JSON titles are redacted; pass --include-titles to keep them locally.
- Only the process name basename is recorded, never the full path or command line.
- Capture output must stay outside this repository working tree.
"""

import argparse
import ctypes
import json
import os
import struct
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.interaction.cloud_window import (  # noqa: E402
    REDACTED_TITLE,  # noqa: F401
    WindowEvaluation,  # noqa: F401
    WindowRecord,  # noqa: F401
    collect_windows,
    enable_dpi_awareness,
    evaluate_windows,
    frame_content_stats,  # noqa: F401
    qt_class_pattern,  # noqa: F401
    redact_path,  # noqa: F401
    redact_title,
    select_candidates,
)

SCHEMA_VERSION = 1

try:
    import win32gui  # noqa: F401
    import win32process  # noqa: F401

    HAVE_PYWIN32 = True
except ImportError:
    HAVE_PYWIN32 = False


def build_report(records, evaluations, captured_at, include_titles=False):
    windows = []
    for record in sorted(records, key=lambda item: (item.pid, item.hwnd)):
        evaluation = evaluations.get(record.hwnd)
        windows.append(
            {
                "pid": record.pid,
                "process_name": record.process_name,
                "hwnd": record.hwnd,
                "parent_hwnd": record.parent_hwnd,
                "root_hwnd": record.root_hwnd,
                "class_name": record.class_name,
                "title": record.title if include_titles else redact_title(record.title),
                "visible": record.visible,
                "enabled": record.enabled,
                "minimized": record.minimized,
                "client_rect": list(record.client_rect),
                "window_rect": list(record.window_rect),
                "dpi": record.dpi,
                "depth": record.depth,
                "child_count": record.child_count,
                "score": evaluation.score if evaluation else 0,
                "candidate_reasons": list(evaluation.reasons) if evaluation else [],
                "is_candidate": bool(evaluation and evaluation.is_candidate),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": captured_at,
        "selection": select_candidates(evaluations),
        "windows": windows,
    }


def serialize_report(report):
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_tree(records):
    """Render the window tree for local diagnosis. Console may show raw titles."""
    hwnd_set = {record.hwnd for record in records}
    children = {}
    for record in records:
        children.setdefault(record.parent_hwnd, []).append(record)
    tops = [record for record in records if record.parent_hwnd not in hwnd_set]
    lines = []

    def append_line(record, indent):
        lines.append(
            f"{'  ' * indent}[pid {record.pid}] hwnd={record.hwnd}"
            f" parent={record.parent_hwnd} root={record.root_hwnd}"
            f" class={record.class_name!r} title={record.title!r}"
            f" visible={record.visible} enabled={record.enabled}"
            f" minimized={record.minimized} client={record.client_rect}"
            f" dpi={record.dpi} children={record.child_count}"
        )

    def walk(record, indent):
        append_line(record, indent)
        for child in sorted(children.get(record.hwnd, []), key=lambda item: item.hwnd):
            walk(child, indent + 1)

    for record in sorted(tops, key=lambda item: item.hwnd):
        walk(record, 0)
    return "\n".join(lines)


def write_bmp(path, width, height, pixel_bytes):
    """Write a 32bpp bottom-up BMP. pixel_bytes come from GetBitmapBits(True)."""
    data_size = len(pixel_bytes)
    header = struct.pack(
        "<2sIHHIIiiHHIIiiII",
        b"BM",
        14 + 40 + data_size,
        0,
        0,
        14 + 40,
        40,
        width,
        height,
        1,
        32,
        0,
        data_size,
        0,
        0,
        0,
        0,
    )
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(pixel_bytes)


def capture_test_windows(records, evaluations, output_dir, max_windows=3):
    """Capture one diagnostic frame per top candidate. Explicit user opt-in only.

    This uses PrintWindow with PW_RENDERFULLCONTENT, not WGC. Verifying WGC on
    candidate child windows is Phase 1 work (see the design document).
    """
    try:
        import win32ui
    except ImportError:
        return [{"status": "unavailable", "detail": "win32ui is required for --capture-test"}]
    by_hwnd = {record.hwnd: record for record in records}
    results = []
    os.makedirs(output_dir, exist_ok=True)
    for hwnd in select_candidates(evaluations)["hwnds"][:max_windows]:
        results.append(_print_window_frame(win32ui, hwnd, by_hwnd[hwnd], output_dir))
    return results


def _print_window_frame(win32ui, hwnd, record, output_dir):
    import win32gui

    left, top, right, bottom = record.window_rect
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return {"hwnd": hwnd, "status": "failed", "detail": "invalid window rect"}
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        pw_renderfullcontent = 2
        printed = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), pw_renderfullcontent)
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
    except Exception as error:
        return {"hwnd": hwnd, "status": "failed", "detail": repr(error)}
    if not printed:
        return {"hwnd": hwnd, "status": "failed", "detail": "PrintWindow returned 0"}
    path = os.path.join(output_dir, f"capture_hwnd_{hwnd}.bmp")
    write_bmp(path, info["bmWidth"], info["bmHeight"], bits)
    nonzero, total = frame_content_stats(bits)
    black = total > 0 and nonzero == 0
    return {
        "hwnd": hwnd,
        "status": "black_frame" if black else "saved",
        "path": path,
        "width": info["bmWidth"],
        "height": info["bmHeight"],
        "nonzero_pixel_ratio": round(nonzero / total, 4) if total else 0.0,
    }


def _ensure_untracked_output(path):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    full = os.path.abspath(path)
    directory = os.path.dirname(full)
    normalized_root = os.path.normcase(repo_root + os.sep)
    normalized_dir = os.path.normcase(directory)
    if normalized_dir == os.path.normcase(repo_root) or normalized_dir.startswith(normalized_root):
        raise SystemExit(
            "--capture-output must stay outside the repository working tree; "
            "captured frames and reports must never be committed"
        )
    return full


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only probe of the local window tree for cloud NTE adaptation."
    )
    parser.add_argument(
        "--output", help="write the JSON report to this file instead of stdout"
    )
    parser.add_argument(
        "--include-titles",
        action="store_true",
        help="keep original window titles in the JSON report (local diagnosis only)",
    )
    parser.add_argument(
        "--capture-test",
        action="store_true",
        help="capture one diagnostic frame per top candidate; disabled by default",
    )
    parser.add_argument(
        "--capture-output",
        help="output directory for --capture-test frames; must stay outside this repository",
    )
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    if not HAVE_PYWIN32:
        print("pywin32 is required; run this probe inside the project environment", file=sys.stderr)
        return 1
    if args.capture_test and not args.capture_output:
        parser.error("--capture-test requires --capture-output")
        return 2
    enable_dpi_awareness()
    records = collect_windows()
    evaluations = evaluate_windows(records)
    captured_at = datetime.now().astimezone().isoformat(timespec="seconds")
    report = build_report(records, evaluations, captured_at, include_titles=args.include_titles)
    json_text = serialize_report(report)
    if args.output:
        parent_dir = os.path.dirname(os.path.abspath(args.output))
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(json_text)
    else:
        print(json_text, end="")
    print(render_tree(records))
    selection = report["selection"]
    print(f"candidates: {len(selection['hwnds'])} status: {selection['status']}")
    for hwnd in selection["hwnds"]:
        evaluation = evaluations[hwnd]
        reasons = ", ".join(evaluation.reasons)
        print(f"  candidate hwnd={hwnd} score={evaluation.score} reasons={reasons}")
    print("candidates are not confirmed targets; verify the report manually before Phase 1")
    if args.include_titles:
        print("warning: the JSON report contains raw window titles; do not share it publicly")
    if args.capture_test:
        output_dir = _ensure_untracked_output(args.capture_output)
        for item in capture_test_windows(records, evaluations, output_dir):
            print(f"capture-test: {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
