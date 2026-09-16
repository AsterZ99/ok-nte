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
    DEACTIVATE_AFTER_DISPATCH = True
    MIN_CLICK_DOWN_TIME = 0.05

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
                    self.release_fake_activation()
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

    def release_fake_activation(self):
        child = self.cloud_input_child()
        if child:
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
            self.release_fake_activation()
        except Exception as error:
            logger.warning(f"release cloud fake activation failed: {error!r}")
        super().on_destroy()

    # -- keyboard ---------------------------------------------------------------

    def make_lparam(self, vk_code, is_up=False):
        # Live-client verified: the child processes posted keys with plain
        # lparam. Scan-code lparam is not required (Noki-compatible recipe).
        return 0xC0000000 if is_up else 0

    def send_key(self, key, down_time=0.01):
        self._dispatch_with_activation(lambda: super().send_key(key, down_time))

    def send_key_down(self, key, activate=True):
        self._dispatch_with_activation(lambda: super().send_key_down(key, activate=False))

    def send_key_up(self, key):
        self._dispatch_with_activation(lambda: super().send_key_up(key))

    # -- mouse -------------------------------------------------------------------

    def _leaf_post(self, message, wparam, x, y):
        hwnd = self.hwnd
        lparam = (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)
        try:
            win32gui.PostMessage(hwnd, message, wparam, lparam)
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
        """Save the real cursor once per operation, then move it to the target."""
        if getattr(self, "_saved_cursor_pos", None) is None:
            self._saved_cursor_pos = win32api.GetCursorPos()
        screen = win32gui.ClientToScreen(child, (int(x), int(y)))
        win32api.SetCursorPos(screen)

    def _restore_cursor(self):
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
        child = self.hwnd
        x, y = self._scale_to_child(x, y)

        def dispatch():
            self._leaf_post(win32con.WM_MOUSEMOVE, down_btn, x, y)
            self.mouse_pos = (x, y)

        def run():
            # No restore: drag/swipe sequences need the cursor to stay at the
            # moved position between steps; mouse_up releases it.
            self._with_real_cursor(child, x, y, dispatch, restore=False)

        self._dispatch_with_activation(run)
        return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)

    def click(self, x=-1, y=-1, move_back=False, name=None, down_time=0.01, move=True, key="left"):
        with self._input_lock:
            self.try_activate()
            if x < 0 or y < 0:
                x, y = round(self.capture.width * 0.5), round(self.capture.height * 0.5)
            child = self.hwnd
            x, y = self._scale_to_child(x, y)
            # Streaming latency needs a more deliberate press than local play.
            down_time = max(float(down_time), self.MIN_CLICK_DOWN_TIME)
            if key == "left":
                btn_down, btn_mk, btn_up = (
                    win32con.WM_LBUTTONDOWN,
                    win32con.MK_LBUTTON,
                    win32con.WM_LBUTTONUP,
                )
            elif key == "middle":
                btn_down, btn_mk, btn_up = (
                    win32con.WM_MBUTTONDOWN,
                    win32con.MK_MBUTTON,
                    win32con.WM_MBUTTONUP,
                )
            else:
                btn_down, btn_mk, btn_up = (
                    win32con.WM_RBUTTONDOWN,
                    win32con.MK_RBUTTON,
                    win32con.WM_RBUTTONUP,
                )

            def dispatch():
                if move:
                    # The client updates its in-game cursor from a stream of
                    # mouse events; a single teleport jump often does not
                    # settle it, so pulse the move message a few times.
                    self._leaf_post(win32con.WM_MOUSEMOVE, 0, x, y)
                    time.sleep(0.03)
                    self._leaf_post(win32con.WM_MOUSEMOVE, 0, x, y)
                    time.sleep(0.05)
                self._leaf_post(btn_down, btn_mk, x, y)
                time.sleep(down_time)
                self._leaf_post(btn_up, 0, x, y)

            def run():
                self._with_real_cursor(child, x, y, dispatch)

            self._dispatch_with_activation(run)

    def right_click(self, x=-1, y=-1, move_back=False, name=None):
        self.click(x, y, move_back=move_back, name=name, key="right")

    def mouse_down(self, x=-1, y=-1, name=None, key="left"):
        with self._input_lock:
            self.try_activate()
            if x < 0 or y < 0:
                x, y = round(self.capture.width * 0.5), round(self.capture.height * 0.5)
            child = self.hwnd
            x, y = self._scale_to_child(x, y)
            btn = {"left": win32con.MK_LBUTTON, "middle": win32con.MK_MBUTTON}.get(
                key, win32con.MK_RBUTTON
            )
            action = {"left": win32con.WM_LBUTTONDOWN, "middle": win32con.WM_MBUTTONDOWN}.get(
                key, win32con.WM_RBUTTONDOWN
            )

            def dispatch():
                self._leaf_post(action, btn, x, y)
                self.mouse_pos = (x, y)

            def run():
                # Held press: the cursor stays at the target until mouse_up.
                self._with_real_cursor(child, x, y, dispatch, restore=False)

            self._dispatch_with_activation(run)

    def mouse_up(self, key="left"):
        with self._input_lock:
            action = {"left": win32con.WM_LBUTTONUP, "middle": win32con.WM_MBUTTONUP}.get(
                key, win32con.WM_RBUTTONUP
            )
            x, y = self._scale_to_child(*getattr(self, "mouse_pos", (0, 0)))
            self._leaf_post(action, 0, x, y)
            self._restore_cursor()
            if self.DEACTIVATE_AFTER_DISPATCH:
                self.release_fake_activation()

    def scroll(self, x, y, scroll_amount):
        # Live-client status: background wheel is unconfirmed; the prior art
        # falls back to physical scrolling. We still post the message.
        with self._input_lock:
            self.try_activate()
            child = self.hwnd
            wparam = win32api.MAKELONG(0, win32con.WHEEL_DELTA * scroll_amount)

            def run():
                if x > 0 and y > 0:
                    scaled_x, scaled_y = self._scale_to_child(x, y)
                    self._with_real_cursor(
                        child,
                        scaled_x,
                        scaled_y,
                        lambda: self._leaf_post(
                            win32con.WM_MOUSEWHEEL, wparam, scaled_x, scaled_y
                        ),
                    )
                else:
                    self._leaf_post(win32con.WM_MOUSEWHEEL, wparam, 0, 0)

            self._dispatch_with_activation(run)

    def move_mouse_relative(self, dx, dy):
        # Camera rotation does not reach the streamed game via injected or
        # posted input; combat tasks that require it are unsupported in cloud
        # mode for now (see the design document).
        logger.warning(
            "cloud mode: relative mouse movement (camera rotation) is not "
            "supported and was ignored"
        )
