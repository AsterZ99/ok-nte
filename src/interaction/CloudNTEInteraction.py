"""Interaction for the PC cloud NTE client.

Phase 3/4 of the cloud NTE adaptation (see
docs/zh-CN/development/云异环适配开发设计文档.md). The cloud client processes
posted input only on its nested input surface
(``WLCloudGameClient``/``WLCloudGame``) and only while it believes itself
active, so this class:

- re-resolves the dynamically reparented child chain before every dispatch
  (never caches HWNDs);
- keeps a background thread sending a fake ``WM_ACTIVATE`` every few seconds;
- posts keys with plain lparam (verified on a live client) instead of the
  scan-code lparam used for the local game;
- posts mouse clicks/moves directly to the child; the child client area maps
  1:1 to the captured main-window content (same size and position).

Known limits, verified on a live client:

- camera rotation via relative mouse movement does not reach the streamed
  game; tasks that require it (AutoCombat aiming) are not supported in cloud
  mode yet;
- background scroll is unreliable (the prior art also falls back to physical
  scrolling); messages are still posted, with a warning;
- while fake activation is running the client captures the user's real mouse
  in the background. The thread stops and the fake activation is released on
  destroy.
"""

import threading
import time

import win32api
import win32con
import win32gui
from ok.util.logger import Logger

from src.interaction.cloud_window import find_cloud_input_child
from src.interaction.NTEInteraction import NTEInteraction

logger = Logger.get_logger(__name__)


class CloudNTEInteraction(NTEInteraction):
    """NTEInteraction variant targeting the cloud client's input surface.

    Input dispatch pattern: fake-activate the input surface, run the dispatch,
    then immediately release the activation (``DEACTIVATE_AFTER_DISPATCH``).
    The client only forwards input while it believes itself active; releasing
    right after each dispatch stops it from capturing the user's real mouse
    between dispatches (otherwise real mouse movement anywhere rotates the
    in-game camera).
    """

    FAKE_ACTIVATE_INTERVAL = 3.0
    DEACTIVATE_AFTER_DISPATCH = False
    MIN_CLICK_DOWN_TIME = 0.08
    CURSOR_SETTLE_SECONDS = 0.4

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fake_activate_stop = threading.Event()
        self._fake_activate_thread = None
        if not self.DEACTIVATE_AFTER_DISPATCH:
            # Sustained activation mode: keep the client always-active. The
            # real mouse stays captured while this runs (legacy behavior).
            self._fake_activate_thread = threading.Thread(
                target=self._fake_activate_loop, name="cloud-fake-activate", daemon=True
            )
            self._fake_activate_thread.start()

    def _dispatch_with_activation(self, action):
        """Fake-activate, run the dispatch, then release the activation."""

        def run():
            self.try_activate()
            return action()

        if self.DEACTIVATE_AFTER_DISPATCH:
            try:
                result = run()
            finally:
                try:
                    # Queued (PostMessage) so the client processes the input
                    # messages first, then deactivates.
                    self.release_fake_activation(post=True)
                except Exception as error:
                    logger.warning(f"release fake activation failed: {error!r}")
            return result
        return run()

    # -- target resolution ---------------------------------------------------

    def cloud_input_child(self):
        """Current input surface hwnd, or 0 when the chain is absent."""
        return find_cloud_input_child(self.hwnd_window.hwnd)

    @property
    def hwnd(self):
        child = self.cloud_input_child()
        if child:
            return child
        return super().hwnd

    # -- fake activation -------------------------------------------------------

    def fake_activate(self):
        child = self.cloud_input_child()
        if child:
            win32gui.SendMessage(child, win32con.WM_ACTIVATE, win32con.WA_ACTIVE, 0)
        return child

    def release_fake_activation(self, post=True):
        """Release the fake activation.

        ``post=True`` queues the WA_INACTIVE *after* already-posted input
        messages, so the client processes them while still active. A
        synchronous SendMessage here would jump the queue and deactivate the
        client before it processes the queued clicks/keys, dropping them.
        """
        child = self.cloud_input_child()
        if not child:
            return
        if post:
            win32gui.PostMessage(child, win32con.WM_ACTIVATE, win32con.WA_INACTIVE, 0)
        else:
            win32gui.SendMessage(child, win32con.WM_ACTIVATE, win32con.WA_INACTIVE, 0)

    def _fake_activate_loop(self):
        while not self._fake_activate_stop.wait(self.FAKE_ACTIVATE_INTERVAL):
            try:
                self.fake_activate()
            except Exception as error:
                logger.warning(f"cloud fake activation failed: {error!r}")

    def try_activate(self):
        # The sustained background activation covers activation needs; the
        # Qt main window itself ignores WM_ACTIVATE for game input purposes.
        self.fake_activate()

    def on_destroy(self):
        self._fake_activate_stop.set()
        try:
            self.release_fake_activation(post=False)
        except Exception as error:
            logger.warning(f"release cloud fake activation failed: {error!r}")
        super().on_destroy()

    # -- keyboard ---------------------------------------------------------------

    def make_lparam(self, vk_code, is_up=False):
        # Live-client verified: the child processes posted keys with plain
        # lparam. Scan-code lparam is not required (Noki-compatible recipe).
        return 0xC0000000 if is_up else 0

    def send_key(self, key, down_time=0.01):
        def dispatch():
            # Zero-arg super() does not work inside a lambda ("super(): no
            # arguments"), so the parent call must be explicit.
            return NTEInteraction.send_key(self, key, down_time)

        self._dispatch_with_activation(dispatch)

    def send_key_down(self, key, activate=True):
        def dispatch():
            return NTEInteraction.send_key_down(self, key, activate=False)

        self._dispatch_with_activation(dispatch)

    def send_key_up(self, key):
        def dispatch():
            return NTEInteraction.send_key_up(self, key)

        self._dispatch_with_activation(dispatch)

    # -- mouse -------------------------------------------------------------------

    def _leaf_post(self, message, wparam, x, y):
        hwnd = self.hwnd
        lparam = (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)
        try:
            win32gui.PostMessage(hwnd, message, wparam, lparam)
            logger.info(
                f"cloud mouse: hwnd={hwnd} msg=0x{message:04X} wparam=0x{wparam:04X}"
                f" pos=({int(x)},{int(y)}) lparam=0x{lparam & 0xFFFFFFFF:08X}"
            )
            return True
        except Exception as error:
            logger.error(f"cloud input post failed hwnd={hwnd}: {error!r}")
            return False

    def _scale_to_child(self, x, y):
        """Map capture-frame coords to the child window's client coords."""
        child = self.hwnd
        try:
            _left, _top, right, bottom = win32gui.GetClientRect(child)
            client_w, client_h = right - _left, bottom - _top
            frame_w, frame_h = self.capture.width, self.capture.height
            if frame_w and frame_h and (client_w, client_h) != (frame_w, frame_h):
                return round(x * client_w / frame_w), round(y * client_h / frame_h)
        except Exception as error:
            logger.warning(f"scale to child failed: {error!r}")
        return int(x), int(y)

    def _teleport_cursor(self, child, x, y):
        """Save the real cursor once, then move it to the target.

        A pending delayed restore is cancelled: consecutive clicks must keep
        the cursor at their targets until the client has processed them.
        """
        timer = getattr(self, "_restore_timer", None)
        if timer is not None:
            timer.cancel()
            self._restore_timer = None
        if getattr(self, "_saved_cursor_pos", None) is None:
            self._saved_cursor_pos = win32api.GetCursorPos()
        screen = win32gui.ClientToScreen(child, (int(x), int(y)))
        win32api.SetCursorPos(screen)

    def _restore_cursor(self):
        timer = getattr(self, "_restore_timer", None)
        if timer is not None:
            timer.cancel()
            self._restore_timer = None
        pos = getattr(self, "_saved_cursor_pos", None)
        if pos is not None:
            self._saved_cursor_pos = None
            try:
                win32api.SetCursorPos(pos)
            except Exception as error:
                logger.warning(f"restore cursor failed: {error!r}")

    def _with_real_cursor(self, child, x, y, action, restore=True):
        """Teleport the real cursor to the target client point, run, restore.

        x/y must already be in the child window's client coords (see
        _scale_to_child). The fake-activated client tracks the REAL OS cursor
        position for its in-game cursor ("异环只能通过传递真实鼠标坐标实现
        鼠标模拟"), so every mouse dispatch needs the cursor physically at
        the target first.
        """
        try:
            self._teleport_cursor(child, x, y)
        except Exception as error:
            logger.warning(f"teleport cursor failed: {error!r}")
        try:
            return action()
        finally:
            if restore:
                self._restore_cursor()

    def move(self, x, y, down_btn=0):
        x, y = self._scale_to_child(x, y)
        self._leaf_post(win32con.WM_MOUSEMOVE, down_btn, x, y)
        self.mouse_pos = (x, y)
        return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)

    _BUTTON_FLAGS = {
        "left": (0x0002, 0x0004),  # MOUSEEVENTF_LEFTDOWN / LEFTUP
        "middle": (0x0020, 0x0040),  # MOUSEEVENTF_MIDDLEDOWN / MIDDLEUP
        "right": (0x0008, 0x0010),  # MOUSEEVENTF_RIGHTDOWN / RIGHTUP
    }

    def _bring_cloud_to_front(self):
        """Bring the cloud window to the foreground for a physical click.

        Returns the previously active window hwnd, or None when no switch is
        needed/possible.
        """
        main_hwnd = self.hwnd_window.hwnd
        if not main_hwnd or not win32gui.IsWindow(main_hwnd):
            return None
        previous = win32gui.GetForegroundWindow()
        if previous == main_hwnd:
            return None
        try:
            win32gui.ShowWindow(main_hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(main_hwnd)
        except Exception as error:
            logger.warning(f"SetForegroundWindow failed: {error!r}")
            return None
        time.sleep(0.15)
        if win32gui.GetForegroundWindow() != main_hwnd:
            logger.warning("cloud window did not become the foreground window")
            return None
        return previous

    def _restore_foreground(self, previous):
        if previous and win32gui.IsWindow(previous):
            try:
                win32gui.SetForegroundWindow(previous)
            except Exception as error:
                logger.warning(f"restore foreground failed: {error!r}")

    def _physical_button(self, child, x, y, down_flag, up_flag, down_time):
        """Physical click: cursor teleport + real button events.

        The cloud client forwards raw-input button events to the streamed
        game; posted button messages never reach it. The cursor teleports to
        the target and the click happens at OS level, so the game window must
        be visible/uncovered at the target point.
        """
        screen = win32gui.ClientToScreen(child, (int(x), int(y)))
        previous = self._bring_cloud_to_front()
        try:
            win32api.SetCursorPos(screen)
            time.sleep(self.CURSOR_SETTLE_SECONDS)
            win32api.mouse_event(down_flag, 0, 0, 0, 0)
            time.sleep(down_time)
            win32api.mouse_event(up_flag, 0, 0, 0, 0)
            # Give the client time to process the button-up before switching
            # the foreground away, otherwise the release can be dropped by
            # its activity gate (press without release).
            time.sleep(0.25)
        finally:
            self._restore_foreground(previous)

    def click(self, x=-1, y=-1, move_back=False, name=None, down_time=0.01, move=True, key="left"):
        with self._input_lock:
            self.try_activate()
            if x < 0 or y < 0:
                x, y = round(self.capture.width * 0.5), round(self.capture.height * 0.5)
            child = self.hwnd
            x, y = self._scale_to_child(x, y)
            # Streaming latency needs a more deliberate press than local play.
            down_time = max(float(down_time), self.MIN_CLICK_DOWN_TIME)
            down_flag, up_flag = self._BUTTON_FLAGS.get(key, self._BUTTON_FLAGS["left"])

            def dispatch():
                self._physical_button(child, x, y, down_flag, up_flag, down_time)

            self._dispatch_with_activation(dispatch)

    def right_click(self, x=-1, y=-1, move_back=False, name=None):
        self.click(x, y, move_back=move_back, name=name, key="right")

    def mouse_down(self, x=-1, y=-1, name=None, key="left"):
        with self._input_lock:
            self.try_activate()
            if x < 0 or y < 0:
                x, y = round(self.capture.width * 0.5), round(self.capture.height * 0.5)
            child = self.hwnd
            x, y = self._scale_to_child(x, y)
            down_flag, _up_flag = self._BUTTON_FLAGS.get(
                key, self._BUTTON_FLAGS["left"]
            )

            def dispatch():
                screen = win32gui.ClientToScreen(child, (int(x), int(y)))
                self._bring_cloud_to_front()
                win32api.SetCursorPos(screen)
                time.sleep(self.CURSOR_SETTLE_SECONDS)
                win32api.mouse_event(down_flag, 0, 0, 0, 0)
                self.mouse_pos = (x, y)

            self._dispatch_with_activation(dispatch)

    def mouse_up(self, key="left"):
        with self._input_lock:
            _down_flag, up_flag = self._BUTTON_FLAGS.get(key, self._BUTTON_FLAGS["left"])
            win32api.mouse_event(up_flag, 0, 0, 0, 0)

    def scroll(self, x, y, scroll_amount):
        # Live-client status: background wheel is unconfirmed; the prior art
        # falls back to physical scrolling. Physical wheel at the cursor.
        with self._input_lock:
            self.try_activate()
            child = self.hwnd
            x, y = self._scale_to_child(x, y)

            def dispatch():
                screen = win32gui.ClientToScreen(child, (int(x), int(y)))
                previous = self._bring_cloud_to_front()
                try:
                    win32api.SetCursorPos(screen)
                    time.sleep(self.CURSOR_SETTLE_SECONDS)
                    delta = win32con.WHEEL_DELTA if scroll_amount > 0 else -win32con.WHEEL_DELTA
                    for _ in range(abs(scroll_amount)):
                        win32api.mouse_event(0x0800, 0, 0, delta, 0)  # MOUSEEVENTF_WHEEL
                        time.sleep(0.05)
                finally:
                    self._restore_foreground(previous)

            self._dispatch_with_activation(dispatch)

    def move_mouse_relative(self, dx, dy):
        # Camera rotation does not reach the streamed game via injected or
        # posted input; combat tasks that require it are unsupported in cloud
        # mode for now (see the design document).
        logger.warning(
            "cloud mode: relative mouse movement (camera rotation) is not "
            "supported and was ignored"
        )
