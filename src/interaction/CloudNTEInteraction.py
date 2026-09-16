"""Interaction for the PC cloud NTE client (audit WP-2 backend).

The cloud client processes posted input only on its nested input surface
(``WLCloudGameClient``/``WLCloudGame``) and only while it believes itself
active. This class is the ok-script adapter; the input contracts live in
``src/interaction/cloud_mouse.py``:

- every dispatch builds ONE immutable :class:`CloudInputSnapshot` and is
  gated (:func:`validate_dispatch`) before anything is sent;
- keyboard uses the live-client-verified recipe: plain lparam keys posted to
  the leaf window behind a fake activation;
- mouse uses the message backend only: posted MOVE/DOWN/UP to the verified
  leaf window. No foreground switching, no real-cursor movement, no
  ``mouse_event``/``SendInput`` — blocked or failing dispatches send nothing
  (audit A-01/A-02, fail closed);
- logically pressed buttons are tracked and force-released on destroy
  (audit A-05).

Known limits (honest failures, audit WP-2):

- scroll is ``UNSUPPORTED`` until the real-client matrix proves it;
- camera rotation via relative mouse movement does not reach the streamed
  game; combat tasks requiring it are unsupported in cloud mode;
- the physical/foreground experimental backend was removed (see the audit
  report, A-01); it lives only in git history.
"""

import time

import win32con
import win32gui
import win32process
from ok.util.logger import Logger

from src.interaction.cloud_mouse import (
    BUTTON_MESSAGES,
    CloudDispatchReason,
    CloudDispatchResult,
    CloudDispatchStatus,
    CloudInputSnapshot,
    CloudMessagePointer,
    validate_dispatch,
)
from src.interaction.cloud_window import CloudFrameHealth, find_cloud_input_child
from src.interaction.NTEInteraction import NTEInteraction

logger = Logger.get_logger(__name__)


class CloudNTEInteraction(NTEInteraction):
    """NTEInteraction variant targeting the cloud client's input surface."""

    #: activation lease strategy (audit §7.4): activate per dispatch, release
    #: queued after the input. The sustained background thread was removed:
    #: the real-client matrix must prove it necessary before it returns.
    DEACTIVATE_AFTER_DISPATCH = True
    MIN_CLICK_DOWN_TIME = 0.08
    #: gap between the posted MOVE and the button DOWN. Live-client matrix:
    #: the DOWN must arrive within ~0.15s of the WM_ACTIVATE to be forwarded,
    #: so this gap stays tiny (a long settle dropped every click).
    CURSOR_SETTLE_SECONDS = 0.05

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pointer = CloudMessagePointer(self._post_message)
        self._generation = 0
        self._last_target = (0, 0)
        self._frame_health = CloudFrameHealth.OK
        self._frame_observed_at = time.time()
        self._frame_size = (0, 0)

    # -- lifecycle -------------------------------------------------------------

    def record_frame_health(self, health, capture_size, observed_at=None):
        """Receive the latest capture-health observation (audit A-06).

        Called by the task layer's periodic health check; the input gate uses
        it to reject dispatches on unhealthy/stale frames.
        """
        self._frame_health = health
        self._frame_size = (int(capture_size[0]), int(capture_size[1]))
        self._frame_observed_at = time.time() if observed_at is None else observed_at

    def on_destroy(self):
        for leaf, message, wparam in self._pointer.cleanup():
            try:
                if win32gui.IsWindow(leaf):
                    win32gui.PostMessage(leaf, message, wparam, 0)
            except Exception as error:
                logger.warning(f"cloud button cleanup failed: {error!r}")
        try:
            self.release_fake_activation(post=False)
        except Exception as error:
            logger.warning(f"release cloud fake activation failed: {error!r}")
        super().on_destroy()

    # -- target resolution -----------------------------------------------------

    def _build_snapshot(self):
        """Resolve one verified target snapshot, or None (fail closed).

        No fallback to parent/middle windows (audit A-02): the leaf chain
        must resolve completely and both windows must share one PID.
        """
        main = self.hwnd_window.hwnd if self.hwnd_window else 0
        if not main or not win32gui.IsWindow(main):
            return None
        leaf = find_cloud_input_child(main)
        if not leaf or not win32gui.IsWindow(leaf):
            return None
        try:
            _tid, main_pid = win32process.GetWindowThreadProcessId(main)
            _tid, leaf_pid = win32process.GetWindowThreadProcessId(leaf)
        except Exception as error:
            logger.warning(f"cloud pid check failed: {error!r}")
            return None
        if main_pid != leaf_pid or main_pid <= 0:
            logger.warning("cloud target rejected: pid mismatch across the chain")
            return None
        try:
            left, top, right, bottom = win32gui.GetClientRect(leaf)
        except Exception as error:
            logger.warning(f"cloud leaf client rect failed: {error!r}")
            return None
        leaf_size = (right - left, bottom - top)
        if leaf_size[0] <= 0 or leaf_size[1] <= 0:
            return None
        if (main, leaf) != self._last_target:
            self._last_target = (main, leaf)
            self._generation += 1
        capture_w = int(getattr(self.capture, "width", 0) or 0)
        capture_h = int(getattr(self.capture, "height", 0) or 0)
        return CloudInputSnapshot(
            main_hwnd=main,
            leaf_hwnd=leaf,
            process_id=main_pid,
            capture_size=(capture_w, capture_h),
            content_rect=(0, 0, capture_w, capture_h),
            leaf_client_size=leaf_size,
            frame_health=self._frame_health,
            frame_observed_at=self._frame_observed_at,
            generation=self._generation,
        )

    @property
    def hwnd(self):
        """The verified leaf input surface, or 0 — never a fallback (A-02)."""
        return self._last_target[1] if self._last_target[1] else 0

    # -- activation lease --------------------------------------------------------

    def fake_activate(self, leaf=None):
        leaf = leaf or self.hwnd
        if leaf:
            win32gui.SendMessage(leaf, win32con.WM_ACTIVATE, win32con.WA_ACTIVE, 0)
        return leaf

    def release_fake_activation(self, post=True, leaf=None):
        """Release the fake activation.

        ``post=True`` queues the WA_INACTIVE *after* already-posted input
        messages, so the client processes them while still active. A
        synchronous SendMessage here would jump the queue and deactivate the
        client before it processes the queued clicks/keys, dropping them.
        """
        leaf = leaf or self.hwnd
        if not leaf:
            return
        if post:
            win32gui.PostMessage(leaf, win32con.WM_ACTIVATE, win32con.WA_INACTIVE, 0)
        else:
            win32gui.SendMessage(leaf, win32con.WM_ACTIVATE, win32con.WA_INACTIVE, 0)

    def try_activate(self):
        self.fake_activate()

    def _dispatch_with_activation(self, leaf, action):
        """Activate, run the dispatch, then queue the release (audit §7.4)."""

        def run():
            self.fake_activate(leaf)
            return action()

        if self.DEACTIVATE_AFTER_DISPATCH:
            try:
                return run()
            finally:
                try:
                    self.release_fake_activation(post=True, leaf=leaf)
                except Exception as error:
                    logger.warning(f"release fake activation failed: {error!r}")
        return run()

    # -- dispatch plumbing ---------------------------------------------------------

    def _post_message(self, hwnd, message, wparam, lparam):
        try:
            win32gui.PostMessage(hwnd, message, wparam, lparam)
            logger.info(
                f"cloud input: hwnd={hwnd} msg=0x{message:04X} wparam=0x{wparam:04X} "
                f"lparam=0x{lparam & 0xFFFFFFFF:08X}"
            )
            return True
        except Exception as error:
            logger.error(f"cloud input post failed hwnd={hwnd}: {error!r}")
            return False

    def _gate(self, capture_point=None):
        """Resolve + gate one dispatch. Returns (result, snapshot)."""
        snapshot = self._build_snapshot()
        if snapshot is None:
            result = CloudDispatchResult(
                CloudDispatchStatus.BLOCKED,
                CloudDispatchReason.NO_TARGET,
                detail="cloud input chain missing or inconsistent",
            )
            logger.warning(f"cloud input blocked: {result.detail}")
            return result, None
        if capture_point is None:
            return (
                CloudDispatchResult(
                    CloudDispatchStatus.PENDING, CloudDispatchReason.NONE
                ),
                snapshot,
            )
        expected_size = self._frame_size if self._frame_size[0] > 0 else None
        result, leaf_point = validate_dispatch(
            snapshot,
            capture_point,
            expected_capture_size=expected_size,
        )
        if result.blocked:
            logger.warning(f"cloud input blocked: {result.reason.value} {result.detail}")
        else:
            result = CloudDispatchResult(
                CloudDispatchStatus.PENDING,
                CloudDispatchReason.NONE,
                leaf_point=leaf_point,
            )
        return result, snapshot

    # -- keyboard (verified recipe) ----------------------------------------------

    def make_lparam(self, vk_code, is_up=False):
        # Live-client verified: the child processes posted keys with plain
        # lparam. Scan-code lparam is not required (Noki-compatible recipe).
        return 0xC0000000 if is_up else 0

    def send_key(self, key, down_time=0.01):
        result, snapshot = self._gate()
        if result.blocked or snapshot is None:
            return
        self._dispatch_with_activation(
            snapshot.leaf_hwnd,
            lambda: NTEInteraction.send_key(self, key, down_time),
        )

    def send_key_down(self, key, activate=True):
        result, snapshot = self._gate()
        if result.blocked or snapshot is None:
            return
        self._dispatch_with_activation(
            snapshot.leaf_hwnd,
            lambda: NTEInteraction.send_key_down(self, key, activate=False),
        )

    def send_key_up(self, key):
        result, snapshot = self._gate()
        if result.blocked or snapshot is None:
            return
        self._dispatch_with_activation(
            snapshot.leaf_hwnd, lambda: NTEInteraction.send_key_up(self, key)
        )

    # -- mouse (message backend) ---------------------------------------------------

    def move(self, x, y, down_btn=0):
        with self._input_lock:
            result, snapshot = self._gate((x, y))
            if result.blocked or snapshot is None:
                return result
            if not down_btn and self._pointer.buttons.is_pressed("left"):
                # held-drag: every move must carry the button flag (audit A-04)
                down_btn = win32con.MK_LBUTTON
            move_result = self._pointer.move(snapshot, result.leaf_point, down_btn=down_btn)
            self.mouse_pos = result.leaf_point
            return move_result

    def click(self, x=-1, y=-1, move_back=False, name=None, down_time=0.01, move=True, key="left"):
        if key not in BUTTON_MESSAGES:
            logger.warning(f"cloud click refused: unknown button {key!r}")
            return CloudDispatchResult(
                CloudDispatchStatus.BLOCKED, CloudDispatchReason.UNSUPPORTED
            )
        with self._input_lock:
            if x < 0 or y < 0:
                x, y = round(self.capture.width * 0.5), round(self.capture.height * 0.5)
            result, snapshot = self._gate((x, y))
            if result.blocked or snapshot is None:
                return result

            def run():
                # Live-client matrix (2026-09-16): the button event must reach
                # the leaf within ~0.15s of the activation to be forwarded.
                # A long settle between activation and DOWN drops the click.
                if move:
                    self._pointer.move(snapshot, result.leaf_point)
                    time.sleep(self.CURSOR_SETTLE_SECONDS)
                return self._pointer.click(
                    snapshot,
                    result.leaf_point,
                    button=key,
                    down_time=max(float(down_time), self.MIN_CLICK_DOWN_TIME),
                )

            return self._dispatch_with_activation(snapshot.leaf_hwnd, run)

    def right_click(self, x=-1, y=-1, move_back=False, name=None):
        return self.click(x, y, move_back=move_back, name=name, key="right")

    def mouse_down(self, x=-1, y=-1, name=None, key="left"):
        with self._input_lock:
            result, snapshot = self._gate((x, y))
            if result.blocked or snapshot is None:
                return result

            def run():
                press_result = self._pointer.press(snapshot, result.leaf_point, button=key)
                if press_result.ok:
                    self.mouse_pos = result.leaf_point
                return press_result

            # Held press: the lease stays open (no queued deactivate) until
            # mouse_up releases it, so the held button keeps being forwarded.
            self.fake_activate(snapshot.leaf_hwnd)
            return run()

    def mouse_up(self, key="left"):
        with self._input_lock:
            result, snapshot = self._gate()
            if result.blocked or snapshot is None:
                return result
            release_result = self._pointer.release(snapshot, button=key)
            self.release_fake_activation(post=True, leaf=snapshot.leaf_hwnd)
            return release_result

    def scroll(self, x, y, scroll_amount):
        # Not proven on a real client (the prior art also fails here); honest
        # failure until the capability matrix says otherwise (audit WP-2).
        logger.warning(
            "cloud mode: scroll is not supported yet (pending real-client "
            "matrix); request ignored"
        )
        return CloudDispatchResult(
            CloudDispatchStatus.BLOCKED, CloudDispatchReason.UNSUPPORTED
        )

    def move_mouse_relative(self, dx, dy):
        # Camera rotation does not reach the streamed game via injected or
        # posted input; combat tasks that require it are unsupported in cloud
        # mode for now (see the design document).
        logger.warning(
            "cloud mode: relative mouse movement (camera rotation) is not "
            "supported and was ignored"
        )
        return CloudDispatchResult(
            CloudDispatchStatus.BLOCKED, CloudDispatchReason.UNSUPPORTED
        )
