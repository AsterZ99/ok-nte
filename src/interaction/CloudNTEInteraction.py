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
    """NTEInteraction variant targeting the cloud client's input surface."""

    FAKE_ACTIVATE_INTERVAL = 3.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fake_activate_stop = threading.Event()
        self._fake_activate_thread = threading.Thread(
            target=self._fake_activate_loop, name="cloud-fake-activate", daemon=True
        )
        self._fake_activate_thread.start()

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

    def move(self, x, y, down_btn=0):
        self._leaf_post(win32con.WM_MOUSEMOVE, down_btn, x, y)
        self.mouse_pos = (x, y)
        return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)

    def click(self, x=-1, y=-1, move_back=False, name=None, down_time=0.01, move=True, key="left"):
        # Background clicks verified on a live client (attack and dodge work).
        # The real cursor is never moved, so no cursor sync or restore here.
        with self._input_lock:
            self.try_activate()
            if x < 0 or y < 0:
                x, y = round(self.capture.width * 0.5), round(self.capture.height * 0.5)
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
            if move:
                self._leaf_post(win32con.WM_MOUSEMOVE, 0, x, y)
                time.sleep(down_time)
            self._leaf_post(btn_down, btn_mk, x, y)
            time.sleep(down_time)
            self._leaf_post(btn_up, 0, x, y)

    def right_click(self, x=-1, y=-1, move_back=False, name=None):
        self.click(x, y, move_back=move_back, name=name, key="right")

    def mouse_down(self, x=-1, y=-1, name=None, key="left"):
        with self._input_lock:
            self.try_activate()
            if x < 0 or y < 0:
                x, y = round(self.capture.width * 0.5), round(self.capture.height * 0.5)
            btn = {"left": win32con.MK_LBUTTON, "middle": win32con.MK_MBUTTON}.get(
                key, win32con.MK_RBUTTON
            )
            action = {"left": win32con.WM_LBUTTONDOWN, "middle": win32con.WM_MBUTTONDOWN}.get(
                key, win32con.WM_RBUTTONDOWN
            )
            self._leaf_post(action, btn, x, y)

    def mouse_up(self, key="left"):
        with self._input_lock:
            action = {"left": win32con.WM_LBUTTONUP, "middle": win32con.WM_MBUTTONUP}.get(
                key, win32con.WM_RBUTTONUP
            )
            x, y = getattr(self, "mouse_pos", (0, 0))
            self._leaf_post(action, 0, x, y)

    def scroll(self, x, y, scroll_amount):
        # Live-client status: background wheel is unconfirmed; the prior art
        # falls back to physical scrolling. We still post the message.
        with self._input_lock:
            self.try_activate()
            wparam = win32api.MAKELONG(0, win32con.WHEEL_DELTA * scroll_amount)
            if x > 0 and y > 0:
                self._leaf_post(win32con.WM_MOUSEWHEEL, wparam, x, y)
            else:
                self._leaf_post(win32con.WM_MOUSEWHEEL, wparam, 0, 0)

    def move_mouse_relative(self, dx, dy):
        # Camera rotation does not reach the streamed game via injected or
        # posted input; combat tasks that require it are unsupported in cloud
        # mode for now (see the design document).
        logger.warning(
            "cloud mode: relative mouse movement (camera rotation) is not "
            "supported and was ignored"
        )
