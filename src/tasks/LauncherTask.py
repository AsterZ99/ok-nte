import os
import re
import time

import psutil
import win32con
import win32gui
import win32process
from ok import TaskDisabledException, og
from ok.util.process import execute, is_admin

from src import CLOUD_EXE, GAME_EXE, LAUNCHER_EXE
from src.events import communicate
from src.interaction.cloud_window import CLOUD_MAIN_TITLE
from src.interaction.CloudNTEInteraction import CloudNTEInteraction
from src.interaction.NTEInteraction import NTEInteraction
from src.Labels import Labels
from src.tasks.BaseNTETask import BaseNTETask
from src.utils.screensaver import dismiss_screensaver


def _exe_name_list(exe_names):
    if isinstance(exe_names, (list, tuple, set)):
        return [str(exe_name) for exe_name in exe_names if exe_name]
    if exe_names:
        return [str(exe_names)]
    return []


def _format_exe_names(exe_names):
    names = _exe_name_list(exe_names)
    return ", ".join(names) if names else "<empty>"


class DynamicConfig(dict):
    @property
    def GAME_CAPTURE_CONFIG(self):
        return {
            "windows": {
                "exe": GAME_EXE,
                "hwnd_class": "UnrealWindow",
                "interaction": NTEInteraction,
                "capture_method": og.device_manager.config.get("capture", "WGC"),
            },
        }

    @property
    def LAUNCHER_CAPTURE_CONFIG(self):
        return {
            "windows": {
                "exe": LAUNCHER_EXE,
                "hwnd_class": "Qt51517QWindowOwnDC",
                "top_hwnd_class": ["Qt51517QWindowToolSaveBitsOwnDC"],
                "interaction": "PostMessage",
                "capture_method": og.device_manager.config.get("capture", "WGC"),
            },
        }

    @property
    def CLOUD_CAPTURE_CONFIG(self):
        return {
            "windows": {
                "exe": CLOUD_EXE,
                "hwnd_class": "Qt51517QWindowIcon",
                "title": CLOUD_MAIN_TITLE,
                "interaction": CloudNTEInteraction,
                # 后台截图只支持 WGC; 云客户端为 D3D 表面, BitBlt 不可用
                "capture_method": ["WGC"],
            },
        }


class LauncherTask(BaseNTETask):
    CONF_PATH = "Launcher Path"
    CONF_RUN_TARGET = "Run Target"
    RUN_TARGET_CHOICES = ("auto", "local", "cloud")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "Start Game"
        self.default_config.update({self.CONF_PATH: "", self.CONF_RUN_TARGET: "auto"})
        self.enable_after_start = True  # auto run after start
        self.visible = False  # False to hide from the UI
        self.capture_config = DynamicConfig()

    def _get_run_target(self):
        """Explicit run target (audit A-08): auto | local | cloud.

        ``auto`` refuses to pick between a simultaneously running local game
        and cloud client — the user must choose explicitly.
        """
        value = str(self.config.get(self.CONF_RUN_TARGET, "auto") or "auto").strip().lower()
        if value not in self.RUN_TARGET_CHOICES:
            self.log_warning(
                f"invalid run target {value!r}; falling back to 'auto'"
            )
            value = "auto"
        return value

    def _resolve_cloud_now(self):
        """Resolve the cloud target with the shared fail-closed resolver (A-07)."""
        from src.interaction import cloud_window as cw

        records = cw.collect_windows()
        resolution = cw.resolve_cloud_target(records)
        if resolution.status == "resolved":
            return resolution.target
        if resolution.status == "ambiguous":
            # fail closed: never pick between multiple valid targets
            raise TaskDisabledException(
                "Multiple cloud game windows match the target identity; "
                "close the extra clients or select the target manually"
            )
        return None

    def run(self):
        self.scene.set_game_capture_ready(False)
        self.log_info("Launcher task started")
        dismiss_screensaver()

        if not self._check_admin():
            return

        run_target = self._get_run_target()
        cloud_target = None
        game_proc = None
        if run_target != "local":
            # audit A-07: use the shared fail-closed resolver, never a
            # first-match process scan
            cloud_target = self._resolve_cloud_now()
            self.log_info(f"Cloud target resolution: {self._describe_cloud(cloud_target)}")
        if run_target != "cloud":
            game_proc = self._find_process(GAME_EXE)
            self.log_info(f"Game process check: {self._format_process(game_proc)}")

        if run_target == "auto" and cloud_target is not None and game_proc:
            # audit A-08: never silently prefer one of two running targets
            self.log_error(
                "Both the cloud client and the local game are running; "
                "set 'Run Target' to local or cloud to choose explicitly"
            )
            raise TaskDisabledException(
                "Ambiguous run target: both local game and cloud client are "
                "running. Set Run Target to local or cloud"
            )

        if cloud_target is not None and run_target in ("auto", "cloud"):
            self.log_info("Cloud client is running; preparing cloud capture")
            self._wait_for_cloud_and_capture()
            return
        if run_target == "cloud":
            self.log_info("Cloud target not resolved yet; waiting for the cloud client")
            self._wait_for_cloud_and_capture()
            return

        if game_proc:
            self.log_info("Game is already running; preparing game capture")
            self._update_launcher_path_from_game(game_proc.get("exe"))
            self._wait_for_game_and_capture(time_out=120, settle_window=False)
            return

        self.scene.set_logged_in(False)
        launcher_proc = self._find_process(LAUNCHER_EXE)
        self.log_info(f"Launcher process check: {self._format_process(launcher_proc)}")
        if launcher_proc:
            self.log_info("Launcher is already running; preparing launcher capture")
            self._update_launcher_path(launcher_proc.get("exe"))
            if not self._wait_for_process(LAUNCHER_EXE):
                raise TaskDisabledException("Timed out waiting for launcher window")
            self._capture_launcher()
            if not self._click_start_game():
                raise TaskDisabledException("Timed out waiting for launcher to minimize")
            self._wait_for_game_and_capture()
            return

        launcher_path = self._get_launcher_path()
        if not launcher_path:
            self.log_error("Launcher path was not found in config or registry")
            raise TaskDisabledException(
                "Launcher path not found. Please set Launcher Path to a launcher executable"
            )

        self.log_info(f"Starting launcher from configured path: {launcher_path}")
        if not execute(launcher_path):
            self.log_error(f"Failed to start launcher from path: {launcher_path}")
            raise TaskDisabledException(f"Invalid launcher path: {launcher_path}")

        self.log_info("Game did not appear directly; waiting for launcher process")
        if not self._wait_for_process(LAUNCHER_EXE, settle_window=True):
            self.log_error("Timed out waiting for launcher process")
            raise TaskDisabledException("Timed out waiting for launcher process")

        launcher_proc = self._find_process(LAUNCHER_EXE)
        self.log_info(f"Launcher process after start: {self._format_process(launcher_proc)}")
        if launcher_proc:
            self._update_launcher_path(launcher_proc.get("exe"))
        self._capture_launcher()
        if not self._click_start_game():
            raise TaskDisabledException("Timed out waiting for launcher to minimize")
        self._wait_for_game_and_capture()

    @staticmethod
    def _describe_cloud(target):
        if target is None:
            return "not running"
        return f"hwnd={target.hwnd} class={target.class_name!r} title={target.title!r}"

    def _capture_game(self):
        self.log_info(
            f"Switching capture to game window: {self.capture_config.GAME_CAPTURE_CONFIG}"
        )
        self.executor.device_manager.ensure_capture(self.capture_config.GAME_CAPTURE_CONFIG)
        self.log_info("Game capture is ready; activating game window")

    def _capture_launcher(self):
        self.log_info(
            f"Switching capture to launcher window: {self.capture_config.LAUNCHER_CAPTURE_CONFIG}"
        )
        if not self._ensure_launcher_visible():
            raise TaskDisabledException("Launcher window is not visible")
        self._log_task_state("before launcher ensure_capture")
        self.executor.device_manager.ensure_capture(self.capture_config.LAUNCHER_CAPTURE_CONFIG)
        self._log_task_state("after launcher ensure_capture")
        self.log_info("Launcher capture is ready; activating launcher window")

    def _capture_cloud(self):
        self.log_info(
            f"Switching capture to cloud window: {self.capture_config.CLOUD_CAPTURE_CONFIG}"
        )
        self.executor.device_manager.ensure_capture(self.capture_config.CLOUD_CAPTURE_CONFIG)
        self.log_info("Cloud capture is ready")

    def _wait_for_cloud_and_capture(self, time_out=120):
        if not self._wait_for_process(CLOUD_EXE, time_out=time_out, settle_window=False):
            self.log_error("Timed out waiting for cloud game window")
            raise TaskDisabledException("Timed out waiting for cloud game window")
        deadline = time.time() + time_out
        target = None
        while time.time() < deadline:
            # re-resolve with the shared resolver; raises on ambiguity (A-07)
            target = self._resolve_cloud_now()
            if target is not None:
                break
            self.sleep(1)
        if target is None:
            self.log_error("Timed out waiting for the cloud main window")
            raise TaskDisabledException(
                "Timed out waiting for the cloud main window (云·异环)"
            )
        self.log_info(f"Cloud game window resolved: {self._describe_cloud(target)}")
        self._capture_cloud()
        self._wait_for_capture_connection()
        resolution_error = og.app.start_controller.check_resolution()
        if resolution_error:
            self.log_error(f"resolution_error: {resolution_error}")
            raise TaskDisabledException(f"Resolution Error: {resolution_error}")
        self.scene.set_game_capture_ready(True)

    def _wait_for_capture_connection(self, time_out=10):
        deadline = time.time() + time_out
        while time.time() < deadline:
            if not self.executor.connected():
                self.log_info("executor not connected try refresh")
                self.executor.device_manager.refresh()
                time.sleep(1.5)
            else:
                return
        self.log_warning(
            f"try refresh timeout {time_out}s, executor connect {self.executor.connected()}"
        )
        raise TaskDisabledException("Timed out waiting for game capture connection")

    def _log_task_state(self, point):
        current_task = getattr(self.executor, "current_task", None)
        self.log_info(
            f"Launcher task state at {point}: "
            f"enabled={self._enabled}, "
            f"running={self.running}, "
            f"paused={self.paused}, "
            f"executor_paused={getattr(self.executor, 'paused', None)}, "
            f"is_current_task={current_task is self}, "
            f"current_task={current_task}"
        )

    def _click_start_game(self, time_out=120):
        self.log_info(f"Looking for launcher Start Game button for up to {time_out}s")
        deadline = time.time() + time_out
        start_click_pending = False
        while time.time() < deadline:
            loop_start = time.time()
            if self._find_process(GAME_EXE):
                self.log_info(
                    "Game process appeared while checking launcher; treating launch as successful"
                )
                return True

            if not start_click_pending:
                if not self._ensure_launcher_visible():
                    self.log_warning(
                        "Launcher window is not visible; waiting for it to be restored"
                    )
                    self.sleep(1)
                    continue
            elif self._is_launcher_hidden_or_minimized():
                self.log_info("Launcher minimized after Start Game click")
                return True

            try:
                button_ready, button = self._launcher_button_state()
            except AttributeError as e:
                self.log_warning(
                    f"Launcher frame was unavailable while checking launcher button {e}"
                )
                time.sleep(1)
                continue

            box = self.box_of_screen(0.644, 0.214, 0.784, 0.378)
            if btn := self.find_one(Labels.launcher_popup_close, box=box):
                self.click(btn, after_sleep=2)
                continue

            if not button:
                self.log_info_gated("launcher button not found", interval=10)
                self.sleep(1)
                continue

            if button_ready:
                if button.name == Labels.launcher_start:
                    self.log_info(f"Found launcher start button: {button}")
                    self.click(button, after_sleep=1)
                    start_click_pending = True
                elif button.name == Labels.launcher_update:
                    self.log_info(f"Found launcher update button: {button}")
                    self.click(button, after_sleep=1)
            else:
                if button.name == Labels.launcher_start:
                    self.log_info_gated(
                        "Found launcher start button; waiting for ready", interval=10
                    )
                    self.sleep(1)
                elif button.name == Labels.launcher_update:
                    self.log_info_gated(
                        "Game update is in progress",
                        interval=10,
                    )
                    self.sleep(1)
                    deadline = self._extend_deadline_for_update(deadline, loop_start)
                    continue

            self.sleep(1)
        self.log_warning("click start game timeout")
        return False

    def _launcher_button_state(self):
        self.next_frame()
        button = self._find_launcher_button()
        is_ready = self._launcher_button_ready()
        return is_ready, button

    def _extend_deadline_for_update(self, deadline, start_time):
        return deadline + time.time() - start_time

    def _find_launcher_button(self):
        to_find = [Labels.launcher_start, Labels.launcher_update]
        for feature_name in to_find:
            if box := self.find_one(
                feature_name,
                horizontal_variance=0.1,
                vertical_variance=0.1,
                threshold=0.85,
            ):
                return box

    def _launcher_button_ready(self):
        box = self.box_of_screen(0.8137, 0.8678, 0.8387, 0.9022, name="launcher_button")
        per = self.calculate_color_percentage(launcher_btn_ready_color, box)
        self.log_info_gated(f"launcher_button color {per}", interval=10, changed=True)
        return per > 0.8

    def _ensure_launcher_visible(self):
        _, launcher_hwnd = self._find_process_window(LAUNCHER_EXE, require_title=True)
        if not launcher_hwnd:
            return False
        return self._restore_window_if_minimized(launcher_hwnd, LAUNCHER_EXE)

    def _is_launcher_hidden_or_minimized(self):
        _, launcher_hwnd = self._find_process_window(LAUNCHER_EXE, require_title=True)
        if not launcher_hwnd:
            return False
        return bool(win32gui.IsIconic(launcher_hwnd) or not win32gui.IsWindowVisible(launcher_hwnd))

    def _wait_for_game_and_capture(self, time_out=600, settle_window=True):
        _attempt = 3
        for i in range(_attempt):
            self.log_info(f"Waiting for game process for up to {time_out}s")
            if not self._wait_for_process(GAME_EXE, time_out=time_out, settle_window=settle_window):
                self.log_error("Timed out waiting for game process")
                raise TaskDisabledException("Timed out waiting for game process")
            self.log_info("Game process found; switching capture to game")
            try:
                self._capture_game()
                break
            except Exception as e:
                if str(e) == "Cannot find window":
                    if i < _attempt:
                        self.log_error(f"Cannot find window, attempt for {i + 1}/3")
                        continue
                    else:
                        raise e
                else:
                    raise e
        self._wait_for_capture_connection()
        resolution_error = og.app.start_controller.check_resolution()
        if resolution_error:
            self.log_error(f"resolution_error: {resolution_error}")
            raise TaskDisabledException(f"Resolution Error: {resolution_error}")

        self.scene.set_game_capture_ready(True)

    def _wait_for_process(self, exe_name, time_out=120, settle_window=False):
        exe_label = _format_exe_names(exe_name)
        self.log_info(
            f"Waiting for process and window {exe_label} for up to {time_out}s "
            f"(settle_window={settle_window})"
        )
        start = time.time()
        while time.time() - start < time_out:
            proc, hwnd = self._find_process_window(
                exe_name,
                require_title=exe_name == LAUNCHER_EXE,
            )
            if proc:
                if hwnd:
                    self._restore_window_if_minimized(hwnd, exe_name)
                    size = self._get_window_size(hwnd)
                    if not self._is_usable_window_size(size):
                        elapsed = int(time.time() - start)
                        self.log_info(
                            f"Window for {exe_label} exists but is too small; "
                            f"hwnd={hwnd}, size={size[0]}x{size[1]}, elapsed={elapsed}s",
                        )
                        self.sleep(1)
                        continue

                    if settle_window:
                        if not self._wait_for_window_size_to_settle(
                            hwnd, exe_label, start, time_out
                        ):
                            return False
                        size = self._get_window_size(hwnd)

                    self.log_info(
                        f"Found process and window {exe_label}: "
                        f"{self._format_process(proc)}, hwnd={hwnd}, size={size[0]}x{size[1]}",
                    )
                    return True

            elapsed = int(time.time() - start)
            if proc:
                self.log_info(
                    f"Process {exe_label} exists, waiting for window; elapsed={elapsed}s",
                )
            else:
                self.log_info(
                    f"Still waiting for {exe_label}; elapsed={elapsed}s",
                )
            self.sleep(1)
        self.log_warning(f"Process/window {exe_label} was not found within {time_out}s")
        return False

    def _find_process(self, exe_name):
        exe_names = {name.lower() for name in _exe_name_list(exe_name)}
        if not exe_names:
            return None
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                name = proc.info.get("name") or ""
                if name.lower() in exe_names:
                    return proc.info
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return None

    def _find_process_window(self, exe_name, require_title=False):
        proc = self._find_process(exe_name)
        if not proc:
            return None, 0

        if exe_name == CLOUD_EXE:
            capture_config = self.capture_config.CLOUD_CAPTURE_CONFIG["windows"]
            return proc, self._find_window_for_process(
                proc,
                hwnd_class=capture_config["hwnd_class"],
                require_title=require_title,
                exact_title=CLOUD_MAIN_TITLE,
            )

        capture_config = (
            self.capture_config.GAME_CAPTURE_CONFIG
            if exe_name == GAME_EXE
            else self.capture_config.LAUNCHER_CAPTURE_CONFIG
        )["windows"]
        return proc, self._find_window_for_process(
            proc, hwnd_class=capture_config["hwnd_class"], require_title=require_title
        )

    def _find_window_for_process(
        self, proc_info, hwnd_class=None, require_title=False, exact_title=None
    ):
        pid = proc_info.get("pid")
        if not pid:
            return 0

        matches = []

        def callback(hwnd, _):
            if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowEnabled(hwnd):
                return True
            try:
                _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                return True
            if window_pid != pid:
                return True
            if hwnd_class and win32gui.GetClassName(hwnd) != hwnd_class:
                return True
            if require_title and not win32gui.GetWindowText(hwnd):
                return True
            if exact_title and win32gui.GetWindowText(hwnd) != exact_title:
                return True

            matches.append(hwnd)
            # EnumWindows treats False as an aborted enumeration and pywin32 raises an error.
            return True

        win32gui.EnumWindows(callback, None)
        if not matches:
            return 0

        visible = [hwnd for hwnd in matches if win32gui.IsWindowVisible(hwnd)]
        return visible[0] if visible else matches[0]

    def _get_window_size(self, hwnd):
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            return max(0, right - left), max(0, bottom - top)
        except Exception as e:
            self.log_debug_gated(
                f"Failed to get window size for hwnd={hwnd}: {e}", interval=10, changed=True
            )
            return 0, 0

    def _is_usable_window_size(self, size):
        width, height = size
        return width > 200 and height > 200

    def _wait_for_window_size_to_settle(self, hwnd, exe_name, wait_start, time_out, settle_time=8):
        self.log_info(f"Waiting for {exe_name} window size to settle for {settle_time}s")
        stable_start = time.time()
        last_size = self._get_window_size(hwnd)

        while time.time() - wait_start < time_out:
            if not win32gui.IsWindow(hwnd):
                self.log_warning(f"Window for {exe_name} disappeared while settling; hwnd={hwnd}")
                return False

            self._restore_window_if_minimized(hwnd, exe_name)
            size = self._get_window_size(hwnd)
            if not self._is_usable_window_size(size):
                stable_start = time.time()
            elif size != last_size:
                self.log_info(
                    f"Window size for {exe_name} changed while settling: "
                    f"{last_size[0]}x{last_size[1]} -> {size[0]}x{size[1]}"
                )
                stable_start = time.time()
                last_size = size
            elif time.time() - stable_start >= settle_time:
                self.log_info(f"Window size for {exe_name} settled at {size[0]}x{size[1]}")
                return True

            stable_for = max(0, time.time() - stable_start)
            self.log_info_gated(
                f"Waiting for {exe_name} window to settle; "
                f"size={size[0]}x{size[1]}, stable_for={stable_for:.1f}s/{settle_time}s",
                interval=2,
            )
            self.sleep(0.5)

        self.log_warning(f"Timed out while waiting for {exe_name} window size to settle")
        return False

    def _restore_window_if_minimized(self, hwnd, exe_name):
        is_minimized = bool(win32gui.IsIconic(hwnd))
        is_visible = bool(win32gui.IsWindowVisible(hwnd))
        if not is_minimized and is_visible:
            return True

        state = "minimized" if is_minimized else "hidden"
        self.log_info(f"Window for {_format_exe_names(exe_name)} is {state}; restoring hwnd={hwnd}")
        if is_minimized:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

        restored = not win32gui.IsIconic(hwnd) and win32gui.IsWindowVisible(hwnd)
        if not restored:
            self.log_info(
                f"Window for {_format_exe_names(exe_name)} is still not visible after restore; "
                f"hwnd={hwnd}"
            )
        return restored

    def _get_launcher_path(self):
        configured_path = self.config.get(self.CONF_PATH, "").strip()
        self.log_info(f"Configured Launcher Path: {configured_path or '<empty>'}")
        launcher_path = configured_path
        launcher_path = self._extract_launcher_path(launcher_path)
        if launcher_path:
            self.log_info(f"Using Launcher Path from config: {launcher_path}")
            self._update_launcher_path(launcher_path)
            return launcher_path

        if configured_path:
            self.log_warning(
                f"Configured Launcher Path does not exist; clearing it: {configured_path}"
            )
            self.config[self.CONF_PATH] = ""  # type: ignore

        self.log_info("Launcher Path config is empty or invalid; checking Windows registry")
        launcher_path = self._find_launcher_path_from_registry()
        if launcher_path:
            self.log_info(f"Using Launcher Path from registry: {launcher_path}")
            self._update_launcher_path(launcher_path)
            return launcher_path

        self.log_warning("Launcher Path could not be resolved")
        return ""

    def _update_launcher_path(self, path):
        if path and self._is_launcher_exe_path(path):
            old_path = self.config.get(self.CONF_PATH, "")
            if old_path != path:
                self.log_info(f"Updating Launcher Path config: {path}")
                self.config[self.CONF_PATH] = path  # type: ignore
            else:
                self.log_info(f"Launcher Path config is already current: {path}")
        elif path:
            self.log_warning(f"Skip updating Launcher Path; path is not valid: {path}")

    def _update_launcher_path_from_game(self, game_path):
        if not game_path:
            self.log_warning("Game process path is unavailable; cannot derive launcher path")
            return

        path = os.path.abspath(game_path)
        self.log_info(f"Trying to derive launcher path from game path: {path}")
        parts = path.split(os.sep)
        lowered = [part.lower() for part in parts]
        if "client" in lowered:
            client_index = lowered.index("client")
            root = os.sep.join(parts[:client_index])
            launcher_path = self._launcher_path_from_install_root(root)
            if launcher_path:
                self.log_info(f"Derived launcher path candidate: {launcher_path}")
                self._update_launcher_path(launcher_path)
            else:
                self.log_warning(f"Could not derive launcher path from game path: {path}")
        else:
            self.log_warning(f"Could not derive launcher path from game path: {path}")

    def _find_launcher_path_from_registry(self):
        try:
            import winreg
        except ImportError:
            self.log_warning("winreg is unavailable; registry lookup skipped")
            return ""

        self.log_info("Scanning Windows uninstall registry keys for NTE launcher")
        roots = [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]
        uninstall_keys = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ]
        views = [0, winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY]

        for root in roots:
            for key_path in uninstall_keys:
                for view in views:
                    launcher_path = self._scan_uninstall_registry(root, key_path, view, winreg)
                    if launcher_path:
                        self.log_info(f"Found launcher path in registry: {launcher_path}")
                        return launcher_path
        self.log_warning("No launcher path found in registry")
        return ""

    def _scan_uninstall_registry(self, root, key_path, view, winreg):
        try:
            with winreg.OpenKey(root, key_path, 0, winreg.KEY_READ | view) as key:
                subkey_count = winreg.QueryInfoKey(key)[0]
                for index in range(subkey_count):
                    try:
                        subkey_name = winreg.EnumKey(key, index)
                        with winreg.OpenKey(key, subkey_name) as subkey:
                            launcher_path = self._launcher_path_from_registry_values(subkey, winreg)
                            if launcher_path:
                                return launcher_path
                    except OSError:
                        continue
        except OSError:
            self.log_debug(f"Registry key unavailable: root={root}, key={key_path}, view={view}")
            return ""
        return ""

    def _launcher_path_from_registry_values(self, subkey, winreg):
        values = {}
        for name in ("DisplayName", "InstallLocation", "DisplayIcon", "UninstallString"):
            try:
                values[name] = str(winreg.QueryValueEx(subkey, name)[0])
            except OSError:
                values[name] = ""

        combined = " ".join(values.values()).lower()
        if not any(token in combined for token in ("neverness", "ntegame", "ntelauncher")):
            return ""

        self.log_info(f"Potential NTE registry entry found: {values.get('DisplayName')}")
        for value in values.values():
            launcher_path = self._extract_launcher_path(value)
            if launcher_path:
                return launcher_path

        install_location = values.get("InstallLocation", "").strip().strip('"')
        for base in (install_location, os.path.dirname(install_location)):
            launcher_path = self._launcher_path_from_install_root(base)
            if launcher_path:
                return launcher_path
        return ""

    def _extract_launcher_path(self, value):
        if not value:
            return ""

        launcher_pattern = "|".join(re.escape(name) for name in _exe_name_list(LAUNCHER_EXE))
        match = re.search(rf'"?([a-zA-Z]:\\[^"]*?(?:{launcher_pattern}))"?', value)
        if match and os.path.exists(match.group(1)):
            return match.group(1)

        path = value.strip().strip('"')
        if self._is_launcher_exe_path(path):
            return path

        return self._launcher_path_from_install_root(path)

    def _launcher_path_from_install_root(self, path):
        if not path:
            return ""
        candidates = []
        for exe_name in _exe_name_list(LAUNCHER_EXE):
            candidates.extend(
                [
                    os.path.join(path, "NTELauncher", exe_name),
                    os.path.join(path, exe_name),
                ]
            )
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return ""

    def _is_launcher_exe_path(self, path):
        launcher_names = {name.lower() for name in _exe_name_list(LAUNCHER_EXE)}
        return os.path.basename(path).lower() in launcher_names and os.path.exists(path)

    def _format_process(self, proc_info):
        if not proc_info:
            return "not found"
        name = proc_info.get("name") or "<unknown>"
        exe = proc_info.get("exe") or "<path unavailable>"
        return f"name={name}, exe={exe}"

    def _check_admin(self):
        if not is_admin():
            communicate.starting_emulator.emit(
                True,
                "PC version requires admin privileges, Please restart this app with admin privileges!",  # noqa: E501
                0,
            )
            communicate.restart_admin.emit()
            return False
        return True


launcher_btn_ready_color = {
    "r": (215, 225),
    "g": (215, 225),
    "b": (215, 225),
}
