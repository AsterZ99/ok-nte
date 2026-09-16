"""Pure contracts for cloud mouse input (audit WP-1).

Everything in this module is testable without a real game window:

- :class:`CloudInputSnapshot` — the immutable per-transaction target state;
- :class:`CloudDispatchResult` — structured dispatch outcome, never a bare bool;
- :func:`map_capture_to_leaf` — the only sanctioned capture->leaf coordinate
  transform (content-rect aware, half-open bounds, no silent clamping);
- :func:`validate_dispatch` — the lightweight input gate checked inside the
  interaction lock before any message is posted;
- :class:`CloudMessagePointer` — message-sequence construction with button
  state tracking and cleanup, given an injected poster callable.

Security contract (audit §4.1): the message backend never touches the
foreground, never moves the real cursor, and never calls mouse_event /
SendInput. Blocked dispatches send nothing.
"""

import enum
import time
from dataclasses import dataclass, field

import win32con

from src.interaction.cloud_window import CloudFrameHealth

FRAME_MAX_AGE_SECONDS = 30.0


class CloudDispatchStatus(enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    BLOCKED = "blocked"
    FAILED = "failed"


class CloudDispatchReason(enum.Enum):
    NONE = "none"
    NO_TARGET = "no_target"
    AMBIGUOUS_TARGET = "ambiguous_target"
    STALE_FRAME = "stale_frame"
    UNHEALTHY_FRAME = "unhealthy_frame"
    SIZE_CHANGED = "size_changed"
    OUT_OF_BOUNDS = "out_of_bounds"
    WINDOW_REBUILT = "window_rebuilt"
    POST_FAILED = "post_failed"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class CloudDispatchResult:
    status: CloudDispatchStatus
    reason: CloudDispatchReason = CloudDispatchReason.NONE
    leaf_point: tuple[int, int] | None = None
    detail: str = ""

    @property
    def ok(self):
        return self.status == CloudDispatchStatus.SENT

    @property
    def blocked(self):
        return self.status == CloudDispatchStatus.BLOCKED


@dataclass(frozen=True)
class CloudInputSnapshot:
    """Immutable per-transaction target state (audit §5.1).

    One input transaction must use exactly one snapshot. ``content_rect`` is
    in capture coordinates and describes the real streamed picture (letterbox
    bars are not clickable). ``generation`` increments whenever the target is
    rebuilt (hwnd change, resize, rebind).
    """

    main_hwnd: int
    leaf_hwnd: int
    process_id: int
    capture_size: tuple[int, int]
    content_rect: tuple[int, int, int, int]
    leaf_client_size: tuple[int, int]
    frame_health: CloudFrameHealth = CloudFrameHealth.OK
    frame_observed_at: float = field(default_factory=time.time)
    generation: int = 0


def map_capture_to_leaf(point, capture_size, content_rect, leaf_client_size):
    """Map a capture-frame point to leaf-client coords, or ``None``.

    Contract (audit §6):

    - rejects negative coordinates and points outside ``content_rect``
      (half-open: left/top inclusive, right/bottom exclusive);
    - normalizes inside the content rect, then scales to the leaf client
      size (round-half-up on the scaled value);
    - the mapped point must satisfy ``0 <= x < width``, ``0 <= y < height``;
    - never silently clamps.
    """
    x, y = point
    if not (isinstance(x, (int, float)) and isinstance(y, (int, float))):
        return None
    x, y = float(x), float(y)
    if x < 0 or y < 0 or x != x or y != y:  # negative or NaN
        return None
    left, top, right, bottom = content_rect
    if right <= left or bottom <= top:
        return None
    if not (left <= x < right and top <= y < bottom):
        return None
    leaf_w, leaf_h = leaf_client_size
    if leaf_w <= 0 or leaf_h <= 0:
        return None
    content_w, content_h = right - left, bottom - top
    if capture_size[0] <= 0 or capture_size[1] <= 0:
        return None
    lx = round((x - left) * leaf_w / content_w)
    ly = round((y - top) * leaf_h / content_h)
    if not (0 <= lx < leaf_w and 0 <= ly < leaf_h):
        return None
    return int(lx), int(ly)


def validate_dispatch(
    snapshot,
    capture_point,
    now=None,
    frame_max_age=FRAME_MAX_AGE_SECONDS,
    expected_capture_size=None,
):
    """Run the per-dispatch gate (audit §5.3) on pure snapshot data.

    Returns ``(result, leaf_point)``: on success the result carries
    ``status=PENDING`` (the caller still has to post and report SENT/FAILED)
    and the mapped leaf point; every failure returns ``BLOCKED`` with a
    specific reason and no point.
    """
    def blocked(reason, detail=""):
        return (
            CloudDispatchResult(CloudDispatchStatus.BLOCKED, reason, detail=detail),
            None,
        )

    if snapshot is None:
        return blocked(CloudDispatchReason.NO_TARGET, "no snapshot")
    if snapshot.main_hwnd <= 0 or snapshot.leaf_hwnd <= 0 or snapshot.process_id <= 0:
        return blocked(CloudDispatchReason.NO_TARGET, "incomplete target")
    if snapshot.leaf_client_size[0] <= 0 or snapshot.leaf_client_size[1] <= 0:
        return blocked(CloudDispatchReason.NO_TARGET, "leaf client size invalid")
    if snapshot.frame_health == CloudFrameHealth.SIZE_CHANGED:
        return blocked(CloudDispatchReason.SIZE_CHANGED, "frame size changed")
    if snapshot.frame_health != CloudFrameHealth.OK:
        return blocked(
            CloudDispatchReason.UNHEALTHY_FRAME, snapshot.frame_health.value
        )
    if expected_capture_size is not None and tuple(snapshot.capture_size) != tuple(
        expected_capture_size
    ):
        return blocked(
            CloudDispatchReason.SIZE_CHANGED,
            f"capture {snapshot.capture_size} != expected {expected_capture_size}",
        )
    if now is None:
        now = time.time()
    if now - snapshot.frame_observed_at > frame_max_age:
        return blocked(
            CloudDispatchReason.STALE_FRAME,
            f"frame age {now - snapshot.frame_observed_at:.1f}s > {frame_max_age:.1f}s",
        )
    leaf_point = map_capture_to_leaf(
        capture_point, snapshot.capture_size, snapshot.content_rect, snapshot.leaf_client_size
    )
    if leaf_point is None:
        return blocked(
            CloudDispatchReason.OUT_OF_BOUNDS,
            f"capture point {capture_point!r} outside content_rect "
            f"{snapshot.content_rect}",
        )
    return (
        CloudDispatchResult(
            CloudDispatchStatus.PENDING, CloudDispatchReason.NONE, leaf_point=leaf_point
        ),
        leaf_point,
    )


def pack_client_lparam(x, y):
    """Pack leaf-client coords into an lparam (LOWORD=x, HIWORD=y)."""
    return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)


def pack_screen_lparam(x, y):
    """Pack screen coords for WM_MOUSEWHEEL (signed 16-bit halves)."""
    # Masking to 16 bits yields the two's-complement bits Windows expects for
    # negative virtual-screen coordinates.
    return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)


# button -> (down message, down wparam, up message)
BUTTON_MESSAGES = {
    "left": (win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, win32con.WM_LBUTTONUP),
    "middle": (win32con.WM_MBUTTONDOWN, win32con.MK_MBUTTON, win32con.WM_MBUTTONUP),
    "right": (win32con.WM_RBUTTONDOWN, win32con.MK_RBUTTON, win32con.WM_RBUTTONUP),
}


class ButtonState:
    """Track logically pressed buttons for paired-release cleanup (A-04/A-05)."""

    def __init__(self):
        self._pressed = {}  # button -> leaf hwnd it was pressed on

    def press(self, button, leaf_hwnd):
        if button in self._pressed:
            return False
        self._pressed[button] = leaf_hwnd
        return True

    def release(self, button):
        return self._pressed.pop(button, None)

    def is_pressed(self, button):
        return button in self._pressed

    def pressed_buttons(self):
        return dict(self._pressed)

    def cleanup_messages(self):
        """UP messages (leaf_hwnd, message, wparam=0) for every pressed button."""
        messages = []
        for button, leaf in list(self._pressed.items()):
            _down, _mk, up = BUTTON_MESSAGES[button]
            messages.append((leaf, up, 0))
            del self._pressed[button]
        return messages


class CloudMessagePointer:
    """Background message mouse backend (audit WP-2).

    Builds and posts mouse sequences to one verified leaf window using a
    single snapshot per transaction. The poster is injected so tests can
    assert exact message flows. Never touches the foreground, the real
    cursor, or global input APIs.
    """

    def __init__(self, poster):
        self._poster = poster
        self.buttons = ButtonState()

    def move(self, snapshot, leaf_point, down_btn=0):
        lparam = pack_client_lparam(*leaf_point)
        return self._post(snapshot, win32con.WM_MOUSEMOVE, down_btn, lparam)

    def click(self, snapshot, leaf_point, button="left", down_time=0.08):
        """Full press/release on one snapshot. Returns the final result."""
        if button not in BUTTON_MESSAGES:
            return CloudDispatchResult(
                CloudDispatchStatus.BLOCKED, CloudDispatchReason.UNSUPPORTED,
                detail=f"unknown button {button!r}",
            )
        down, mk, up = BUTTON_MESSAGES[button]
        lparam = pack_client_lparam(*leaf_point)
        if not self._post(snapshot, down, mk, lparam):
            return CloudDispatchResult(
                CloudDispatchStatus.FAILED, CloudDispatchReason.POST_FAILED
            )
        time.sleep(down_time)
        if not self._post(snapshot, up, 0, lparam):
            return CloudDispatchResult(
                CloudDispatchStatus.FAILED, CloudDispatchReason.POST_FAILED
            )
        return CloudDispatchResult(CloudDispatchStatus.SENT)

    def press(self, snapshot, leaf_point, button="left"):
        down, mk, _up = BUTTON_MESSAGES[button]
        lparam = pack_client_lparam(*leaf_point)
        if not self._post(snapshot, down, mk, lparam):
            return CloudDispatchResult(
                CloudDispatchStatus.FAILED, CloudDispatchReason.POST_FAILED
            )
        self.buttons.press(button, snapshot.leaf_hwnd)
        return CloudDispatchResult(CloudDispatchStatus.SENT)

    def release(self, snapshot, button="left"):
        leaf = self.buttons.release(button)
        if leaf is None:
            return CloudDispatchResult(
                CloudDispatchStatus.BLOCKED, CloudDispatchReason.NONE,
                detail=f"{button} not pressed",
            )
        _down, _mk, up = BUTTON_MESSAGES[button]
        lparam = pack_client_lparam(0, 0)
        if not self._post(snapshot, up, 0, lparam):
            return CloudDispatchResult(
                CloudDispatchStatus.FAILED, CloudDispatchReason.POST_FAILED
            )
        return CloudDispatchResult(CloudDispatchStatus.SENT)

    def wheel(self, snapshot, screen_point, delta):
        """WM_MOUSEWHEEL takes SCREEN coordinates (audit §6 exception)."""
        lparam = pack_screen_lparam(*screen_point)
        wparam = (win32con.WHEEL_DELTA & 0xFFFF) << 16
        if not self._post(snapshot, win32con.WM_MOUSEWHEEL, wparam, lparam):
            return CloudDispatchResult(
                CloudDispatchStatus.FAILED, CloudDispatchReason.POST_FAILED
            )
        return CloudDispatchResult(CloudDispatchStatus.SENT)

    def cleanup(self):
        """Release every logically pressed button; returns cleanup messages."""
        return self.buttons.cleanup_messages()

    def _post(self, snapshot, message, wparam, lparam):
        return self._poster(snapshot.leaf_hwnd, message, wparam, lparam)
