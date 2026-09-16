"""Cloud NTE window resolution and capture health checks (Phase 2).

Single source of truth for cloud window identification logic. The Phase 0
probe (tools/cloud_nte_probe.py) imports the pure functions from here so the
CLI tool and the application never drift apart.

Evidence-based facts from the real cloud client (see the design document
section 3.1, confirmed by read-only probes on a live client):

- Streaming game process: ``NTECloudGame.exe``; platform pages live in a
  separate ``NTECloudBrowser.exe`` process.
- The game picture is carried by the visible top-level window titled
  ``云·异环`` (class ``Qt51517QWindowIcon``). Sibling "ghost" render surfaces
  in the same process are not WGC-capturable (CreateForWindow fails with
  E_INVALIDARG), so capturability is a strong discriminator.
- HWNDs are rebuilt across session state transitions (home -> streaming) but
  survive resolution/fullscreen changes.
"""

import ctypes
import os
import sys
from dataclasses import dataclass
from enum import Enum

try:
    from ok.util.logger import Logger

    logger = Logger.get_logger(__name__)
except ImportError:
    # Pure-logic tests run without the ok-script runtime installed.
    import logging

    logger = logging.getLogger(__name__)

try:
    import win32gui  # noqa: F401
    import win32process  # noqa: F401

    HAVE_PYWIN32 = True
except ImportError:
    HAVE_PYWIN32 = False

# Evidence-based identification constants confirmed by the Phase 0 probe.
CLOUD_PROCESS = "NTECloudGame.exe"
CLOUD_MAIN_CLASS = "Qt51517QWindowIcon"
CLOUD_MAIN_TITLE = "云·异环"

# Marker words are hints only. They must never become formal matching rules
# on their own (see the design document, "先取证, 后实现").
MARKER_PROCESS_KEYWORDS = ("cloud", "htgame", "neverness", "nte")
MARKER_TITLE_KEYWORDS = ("cloud", "neverness", "异环")
MARKER_CLASS_EXACT = ("unrealwindow",)

SCORE_PROCESS_MARKER = 2
SCORE_CLASS_EXACT = 2
SCORE_QT_COMBINATION = 1
SCORE_TITLE_MARKER = 1
SCORE_SIZE_16_9 = 2
SCORE_PARENT_CLASS = 1

# A plausible game surface needs combined evidence. 5 means a bare process
# marker plus auxiliary hints is not enough; size evidence or additional
# markers must participate, so tiny helper windows are not candidates.
CANDIDATE_MIN_SCORE = 5
PRIMARY_REASON_PREFIXES = ("class_exact:", "process_marker:", "size_16_9")

MIN_CANDIDATE_WIDTH = 1280
MIN_CANDIDATE_HEIGHT = 720
ASPECT_TARGET = 16 / 9
ASPECT_TOLERANCE = 0.05

# Project resolution contract: 16:9, at least 1920x1080.
MIN_FRAME_WIDTH = 1920
MIN_FRAME_HEIGHT = 1080
BLACK_CONTENT_RATIO = 0.001

REDACTED_TITLE = "<redacted>"


@dataclass(frozen=True)
class WindowRecord:
    pid: int
    process_name: str
    hwnd: int
    parent_hwnd: int
    root_hwnd: int
    class_name: str
    title: str
    visible: bool
    enabled: bool
    minimized: bool
    client_rect: tuple[int, int, int, int]
    window_rect: tuple[int, int, int, int]
    dpi: int
    depth: int
    child_count: int


@dataclass(frozen=True)
class WindowEvaluation:
    score: int
    reasons: tuple[str, ...]
    is_candidate: bool


class CloudTargetKind(Enum):
    LOCAL = "local"
    CLOUD = "cloud"


@dataclass(frozen=True)
class CloudTarget:
    kind: CloudTargetKind
    hwnd: int
    process_name: str
    class_name: str
    title: str


@dataclass(frozen=True)
class CloudTargetResolution:
    """Result of cloud target resolution. Never guesses under ambiguity."""

    status: str  # "none" | "resolved" | "ambiguous"
    target: CloudTarget | None
    hwnds: tuple[int, ...]


class CloudFrameHealth(Enum):
    OK = "ok"
    NO_FRAME = "no_frame"
    HWND_INVALID = "hwnd_invalid"
    SIZE_CHANGED = "size_changed"
    SIZE_TOO_SMALL = "size_too_small"
    ASPECT_MISMATCH = "aspect_mismatch"
    BLACK = "black"


def redact_title(title):
    if not title:
        return ""
    return REDACTED_TITLE


def redact_path(path):
    if not path:
        return ""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def qt_class_pattern(class_name):
    """Mark Qt streaming-window-like class names. Marker only, never a rule."""
    if not class_name:
        return False
    lowered = class_name.strip().lower()
    return lowered.startswith("qt") and lowered.endswith("qwindowicon")


def distinct_markers(text, keywords):
    if not text:
        return []
    lowered = text.lower()
    return sorted({keyword for keyword in keywords if keyword in lowered})


def is_large_16_9(client_rect):
    left, _top, right, bottom = client_rect
    width = right - left
    height = bottom - _top
    if width < MIN_CANDIDATE_WIDTH or height < MIN_CANDIDATE_HEIGHT or height <= 0:
        return False
    return abs(width / height - ASPECT_TARGET) <= ASPECT_TARGET * ASPECT_TOLERANCE


def evaluate_windows(records):
    """Score every window from combined evidence. Pure function over records."""
    by_hwnd = {record.hwnd: record for record in records}
    results = {}
    for record in records:
        score = 0
        reasons = []
        process_hits = distinct_markers(record.process_name, MARKER_PROCESS_KEYWORDS)
        for keyword in process_hits:
            score += SCORE_PROCESS_MARKER
            reasons.append(f"process_marker:{keyword}")
        lowered_class = (record.class_name or "").strip().lower()
        if lowered_class in MARKER_CLASS_EXACT:
            score += SCORE_CLASS_EXACT
            reasons.append(f"class_exact:{lowered_class}")
        title_hits = distinct_markers(record.title, MARKER_TITLE_KEYWORDS)
        if qt_class_pattern(record.class_name) and (process_hits or title_hits):
            # Qt class names only count in combination, never on their own.
            score += SCORE_QT_COMBINATION
            reasons.append("qt_class_marker_combination")
        for keyword in title_hits:
            score += SCORE_TITLE_MARKER
            reasons.append(f"title_marker:{keyword}")
        if is_large_16_9(record.client_rect):
            score += SCORE_SIZE_16_9
            reasons.append("size_16_9")
        parent = by_hwnd.get(record.parent_hwnd)
        if parent is not None and (
            qt_class_pattern(parent.class_name)
            or (parent.class_name or "").strip().lower() in MARKER_CLASS_EXACT
        ):
            score += SCORE_PARENT_CLASS
            reasons.append("parent_class_marker")
        eligible = (
            record.visible
            and record.enabled
            and not record.minimized
            and score >= CANDIDATE_MIN_SCORE
            and any(reason.startswith(PRIMARY_REASON_PREFIXES) for reason in reasons)
        )
        results[record.hwnd] = WindowEvaluation(
            score=score, reasons=tuple(reasons), is_candidate=eligible
        )
    return results


def select_candidates(evaluations):
    """Rank candidates. Any ambiguity is reported, never silently resolved."""
    ranked = sorted(
        (
            (evaluation.score, hwnd)
            for hwnd, evaluation in evaluations.items()
            if evaluation.is_candidate
        ),
        key=lambda item: (-item[0], item[1]),
    )
    hwnds = [hwnd for _score, hwnd in ranked]
    if not hwnds:
        status = "none"
    elif len(hwnds) == 1:
        status = "single"
    else:
        status = "ambiguous"
    return {"status": status, "hwnds": hwnds}


def resolve_cloud_target(records, evaluations=None):
    """Pick the cloud main window from evidence, or fail closed on ambiguity.

    The real client exposes one visible window that combines the confirmed
    process identity, main class and title. Ghost render surfaces differ in
    class or title. Any residual ambiguity returns status "ambiguous" with no
    target instead of guessing.
    """
    if evaluations is None:
        evaluations = evaluate_windows(records)
    by_hwnd = {record.hwnd: record for record in records}
    matches = []
    for hwnd, evaluation in evaluations.items():
        if not evaluation.is_candidate:
            continue
        record = by_hwnd[hwnd]
        if (
            record.process_name.lower() == CLOUD_PROCESS.lower()
            and record.class_name == CLOUD_MAIN_CLASS
            and record.title == CLOUD_MAIN_TITLE
        ):
            matches.append((evaluation.score, hwnd))
    matches.sort(key=lambda item: (-item[0], item[1]))
    hwnds = tuple(hwnd for _score, hwnd in matches)
    if not hwnds:
        return CloudTargetResolution("none", None, ())
    if len(hwnds) > 1:
        return CloudTargetResolution("ambiguous", None, hwnds)
    hwnd = hwnds[0]
    record = by_hwnd[hwnd]
    target = CloudTarget(
        kind=CloudTargetKind.CLOUD,
        hwnd=hwnd,
        process_name=record.process_name,
        class_name=record.class_name,
        title=record.title,
    )
    return CloudTargetResolution("resolved", target, hwnds)


def assess_frame_health(
    width,
    height,
    content_ratio=1.0,
    hwnd_valid=True,
    expected_size=None,
):
    """Classify one captured frame against the screenshot contract. Pure."""
    if not hwnd_valid:
        return CloudFrameHealth.HWND_INVALID
    if not width or not height:
        return CloudFrameHealth.NO_FRAME
    if expected_size is not None and (width, height) != tuple(expected_size):
        return CloudFrameHealth.SIZE_CHANGED
    if width < MIN_FRAME_WIDTH or height < MIN_FRAME_HEIGHT:
        return CloudFrameHealth.SIZE_TOO_SMALL
    if abs(width / height - ASPECT_TARGET) > ASPECT_TARGET * ASPECT_TOLERANCE:
        return CloudFrameHealth.ASPECT_MISMATCH
    if content_ratio <= BLACK_CONTENT_RATIO:
        return CloudFrameHealth.BLACK
    return CloudFrameHealth.OK


def frame_content_stats(pixel_bytes):
    """Count non-black pixels ignoring the alpha channel. Pure function.

    Direct3D windows often capture as BGRA with B=G=R=0 and A=255, which a
    naive all-bytes check would mistake for real content.
    """
    total_pixels = len(pixel_bytes) // 4
    blue = pixel_bytes[0::4]
    green = pixel_bytes[1::4]
    red = pixel_bytes[2::4]
    nonzero = sum(1 for b, g, r in zip(blue, green, red) if b or g or r)
    return nonzero, total_pixels


def find_cloud_input_child(main_hwnd, finder=None):
    """Resolve the input surface chain under the visible main window.

    Real client structure (dynamically reparented, never cache the result):
    main ``云·异环`` window -> child ``Qt51517QWindowIcon``/``NTECloudGame``
    -> child ``WLCloudGameClient``/``WLCloudGame``. The leaf is the surface
    that actually carries the streamed game and processes posted input.

    ``finder`` defaults to ``win32gui.FindWindowEx`` and is injectable for
    tests. Returns the leaf hwnd, or 0 when the chain is absent.
    """
    if finder is None:
        import win32gui

        finder = win32gui.FindWindowEx
    if not main_hwnd:
        return 0
    mid = finder(main_hwnd, 0, CLOUD_MAIN_CLASS, "NTECloudGame")
    if not mid:
        return 0
    leaf = finder(mid, 0, "WLCloudGameClient", "WLCloudGame")
    return leaf or 0


def wgc_capturable(hwnd):
    """Probe whether WGC can capture this window.

    Returns True/False from a real GraphicsCaptureItem creation attempt, or
    None when the WGC stack is unavailable. Ghost render surfaces of the cloud
    client fail cleanly with E_INVALIDARG while the main window succeeds.
    """
    try:
        from ok.rotypes.roapi import GetActivationFactory
        from ok.rotypes.Windows.Graphics.Capture import (
            IGraphicsCaptureItem,
            IGraphicsCaptureItemInterop,
        )
    except Exception:
        return None
    try:
        interop = GetActivationFactory(
            "Windows.Graphics.Capture.GraphicsCaptureItem"
        ).astype(IGraphicsCaptureItemInterop)
        interop.CreateForWindow(hwnd, IGraphicsCaptureItem.GUID)
        return True
    except OSError:
        return False
    except Exception as error:
        logger.warning(f"wgc_capturable unknown failure for hwnd={hwnd}: {error!r}")
        return None


def enable_dpi_awareness():
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def _window_dpi(hwnd):
    try:
        get_dpi = getattr(ctypes.windll.user32, "GetDpiForWindow", None)
        if get_dpi is not None:
            return int(get_dpi(hwnd))
    except Exception:
        pass
    return 96


def _process_basename(pid):
    if not pid:
        return ""
    try:
        import psutil

        return redact_path(psutil.Process(pid).name())
    except Exception:
        pass
    try:
        import win32process

        process_query_limited_information = 0x1000
        process_vm_read = 0x0010
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information | process_vm_read, False, pid
        )
        if not handle:
            return ""
        try:
            return redact_path(win32process.GetModuleFileNameEx(handle, 0))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def _record_from_hwnd(hwnd, parent_hwnd, depth, child_count):
    import win32gui
    import win32process

    try:
        class_name = win32gui.GetClassName(hwnd)
    except win32gui.error:
        class_name = ""
    try:
        title = win32gui.GetWindowText(hwnd)
    except win32gui.error:
        title = ""
    try:
        _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
    except win32gui.error:
        pid = 0
    try:
        client_rect = tuple(win32gui.GetClientRect(hwnd))
    except win32gui.error:
        client_rect = (0, 0, 0, 0)
    try:
        window_rect = tuple(win32gui.GetWindowRect(hwnd))
    except win32gui.error:
        window_rect = (0, 0, 0, 0)
    try:
        root_hwnd = win32gui.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT == 2
    except win32gui.error:
        root_hwnd = hwnd
    return WindowRecord(
        pid=pid,
        process_name=_process_basename(pid),
        hwnd=hwnd,
        parent_hwnd=parent_hwnd,
        root_hwnd=root_hwnd,
        class_name=class_name,
        title=title,
        visible=bool(win32gui.IsWindowVisible(hwnd)),
        enabled=bool(win32gui.IsWindowEnabled(hwnd)),
        minimized=bool(win32gui.IsIconic(hwnd)),
        client_rect=client_rect,
        window_rect=window_rect,
        dpi=_window_dpi(hwnd),
        depth=depth,
        child_count=child_count,
    )


def collect_windows():
    """Enumerate top-level windows and all descendants. Read-only Win32 access."""
    import win32gui

    tops = []
    win32gui.EnumWindows(lambda hwnd, acc: acc.append(hwnd), tops)
    descendants = []
    for hwnd in tops:
        try:
            win32gui.EnumChildWindows(hwnd, lambda child, acc: acc.append(child), descendants)
        except win32gui.error:
            continue
    children_map = {}
    for child in descendants:
        try:
            parent = win32gui.GetParent(child) or 0
        except win32gui.error:
            parent = 0
        children_map.setdefault(parent, []).append(child)

    records = []
    visited = set()

    def add(hwnd, parent_hwnd, depth):
        try:
            record = _record_from_hwnd(
                hwnd, parent_hwnd, depth, child_count=len(children_map.get(hwnd, []))
            )
        except Exception as error:
            logger.warning(f"skipped hwnd={hwnd} during enumeration: {error!r}")
            return
        records.append(record)
        visited.add(hwnd)

    queue = sorted(tops)
    depth_by_hwnd = {hwnd: 0 for hwnd in queue}
    index = 0
    while index < len(queue):
        parent = queue[index]
        index += 1
        depth = depth_by_hwnd[parent]
        if parent not in visited:
            add(parent, 0, depth)
        for child in sorted(children_map.get(parent, [])):
            if child not in visited:
                depth_by_hwnd[child] = depth + 1
                add(child, parent, depth + 1)
                queue.append(child)
    for child in descendants:
        if child not in visited:
            add(child, 0, 0)
    return records


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def ensure_repo_root_on_path():
    root = repo_root()
    if root not in sys.path:
        sys.path.insert(0, root)
