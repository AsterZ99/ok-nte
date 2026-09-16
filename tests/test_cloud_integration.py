"""Tests for the cloud mode runtime integration (Phase 4)."""

import unittest
from unittest.mock import Mock, patch

import numpy as np

from src import CLOUD_EXE
from src.interaction.cloud_window import CLOUD_MAIN_TITLE, CloudFrameHealth
from src.interaction.CloudNTEInteraction import CloudNTEInteraction
from src.tasks.LauncherTask import DynamicConfig, LauncherTask


class TestCloudCaptureConfig(unittest.TestCase):
    def test_cloud_capture_config_targets_the_cloud_client(self):
        config = DynamicConfig().CLOUD_CAPTURE_CONFIG["windows"]
        self.assertEqual(config["exe"], CLOUD_EXE)
        self.assertEqual(config["hwnd_class"], "Qt51517QWindowIcon")
        self.assertEqual(config["title"], CLOUD_MAIN_TITLE)
        self.assertIs(config["interaction"], CloudNTEInteraction)
        self.assertEqual(config["capture_method"], ["WGC"])


class _LauncherTaskBase(unittest.TestCase):
    def _make_task(self):
        task = object.__new__(LauncherTask)
        task.log_info = Mock()
        task.log_warning = Mock()
        task.log_error = Mock()
        task.log_info_gated = Mock()
        task.sleep = Mock()
        task.scene = Mock()
        task._check_admin = Mock(return_value=True)
        task._wait_for_cloud_and_capture = Mock()
        task._wait_for_game_and_capture = Mock()
        task._update_launcher_path_from_game = Mock()
        task.capture_config = DynamicConfig()
        return task


class TestCloudAutoDetection(_LauncherTaskBase):
    def test_run_uses_cloud_capture_when_cloud_client_is_running(self):
        task = self._make_task()
        task._find_process = Mock(return_value={"pid": 43532, "name": CLOUD_EXE})

        task.run()

        task._wait_for_cloud_and_capture.assert_called_once_with()
        task._wait_for_game_and_capture.assert_not_called()

    def test_run_falls_back_to_local_flow_without_cloud_client(self):
        task = self._make_task()
        task._find_process = Mock(side_effect=[None, {"pid": 1, "name": "HTGame.exe"}])
        task._update_launcher_path = Mock()
        task._wait_for_process = Mock(return_value=True)

        task.run()

        task._wait_for_cloud_and_capture.assert_not_called()
        task._wait_for_game_and_capture.assert_called_once_with(time_out=120, settle_window=False)

    def test_find_process_window_filters_cloud_windows_by_exact_title(self):
        task = self._make_task()
        task._find_process = Mock(return_value={"pid": 43532})
        task._find_window_for_process = Mock(return_value=12586332)

        task._find_process_window(CLOUD_EXE)

        task._find_window_for_process.assert_called_once_with(
            {"pid": 43532},
            hwnd_class="Qt51517QWindowIcon",
            require_title=False,
            exact_title=CLOUD_MAIN_TITLE,
        )

    def test_cloud_window_wait_aborts_when_window_never_appears(self):
        from ok import TaskDisabledException

        task = self._make_task()
        task._wait_for_process = Mock(return_value=False)

        with self.assertRaisesRegex(TaskDisabledException, "cloud game window"):
            LauncherTask._wait_for_cloud_and_capture(task, time_out=1)


class TestCaptureHealth(_LauncherTaskBase):
    def _make_health_task(self, frame):
        from src.tasks.BaseNTETask import BaseNTETask

        task = object.__new__(BaseNTETask)
        task.scene = Mock()
        task.scene.game_capture_ready = Mock(return_value=False)
        task.scene.set_game_capture_ready = Mock()
        task.log_info = Mock()
        task.log_warning = Mock()
        task.log_error = Mock()
        task._capture_health_checked_at = 0.0
        task._health_frame = frame
        return task

    def _patch_og(self):
        device_manager = Mock()
        device_manager.hwnd_window.hwnd = 12586332
        og_mock = Mock()
        og_mock.device_manager = device_manager
        return patch("src.tasks.BaseNTETask.og", og_mock), patch(
            "win32gui.IsWindow", return_value=True
        )

    def test_healthy_frame_keeps_capture_ready(self):
        frame = np.full((1080, 1920, 3), 120, dtype=np.uint8)
        task = self._make_health_task(frame)
        og_patch, iswindow_patch = self._patch_og()

        with og_patch, iswindow_patch:
            health = task.update_capture_health(frame=frame)

        self.assertEqual(health, CloudFrameHealth.OK)
        task.scene.set_game_capture_ready.assert_called_once_with(True)

    def test_black_frame_disables_capture_ready(self):
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        task = self._make_health_task(frame)
        task.scene.game_capture_ready = Mock(return_value=True)
        og_patch, iswindow_patch = self._patch_og()

        with og_patch, iswindow_patch:
            health = task.update_capture_health(frame=frame)

        self.assertEqual(health, CloudFrameHealth.BLACK)
        task.scene.set_game_capture_ready.assert_called_once_with(False)

    def test_small_frame_flags_resolution_contract(self):
        frame = np.full((900, 1600, 3), 120, dtype=np.uint8)
        task = self._make_health_task(frame)
        task.scene.game_capture_ready = Mock(return_value=True)
        og_patch, iswindow_patch = self._patch_og()

        with og_patch, iswindow_patch:
            health = task.update_capture_health(frame=frame)

        self.assertEqual(health, CloudFrameHealth.SIZE_TOO_SMALL)
        task.scene.set_game_capture_ready.assert_called_once_with(False)

    def test_dead_hwnd_wins_over_missing_frame(self):
        task = self._make_health_task(None)
        task._executor = Mock()
        task._executor.frame = None
        og_patch, iswindow_patch = self._patch_og()

        with og_patch, iswindow_patch as iswindow_mock:
            iswindow_mock.return_value = False
            health = task.update_capture_health(frame=None)

        self.assertEqual(health, CloudFrameHealth.HWND_INVALID)

    def test_missing_frame_with_valid_hwnd(self):
        task = self._make_health_task(None)
        task._executor = Mock()
        task._executor.frame = None
        og_patch, iswindow_patch = self._patch_og()

        with og_patch, iswindow_patch:
            health = task.update_capture_health(frame=None)

        self.assertEqual(health, CloudFrameHealth.NO_FRAME)

    def test_throttle_skips_repeated_checks(self):
        frame = np.full((1080, 1920, 3), 120, dtype=np.uint8)
        task = self._make_health_task(frame)
        og_patch, iswindow_patch = self._patch_og()

        with og_patch, iswindow_patch:
            first = task.update_capture_health(frame=frame)
            second = task.update_capture_health(frame=frame)

        self.assertEqual(first, CloudFrameHealth.OK)
        self.assertIsNone(second)
        task.scene.set_game_capture_ready.assert_called_once_with(True)


class TestOneTimeTaskHealthGate(unittest.TestCase):
    def test_one_time_task_runs_health_check_before_ready_gate(self):
        from src.tasks.NTEOneTimeTask import NTEOneTimeTask

        calls = []

        class _Stub:
            def update_capture_health(self):
                calls.append("health")

            def log_warning(self, message):
                pass

            def set_check_monthly_card(self):
                pass

            def sleep(self, seconds):
                pass

            def run(self, *args, **kwargs):
                return "ran"

        class _Task(NTEOneTimeTask, _Stub):
            pass

        task = _Task()
        task.scene = Mock()
        task.scene.game_capture_ready = Mock(return_value=True)
        task.executor = Mock()
        task.executor.connected = Mock(return_value=True)
        task.executor.interaction = object()

        self.assertEqual(task.run(), "ran")
        self.assertEqual(calls, ["health"])


class TestKeyboardDispatch(unittest.TestCase):
    """Regression: zero-arg super() inside a lambda raises 'super(): no
    arguments', which silently broke every cloud keyboard dispatch."""

    def _make_interaction(self):
        import threading

        from src.interaction.CloudNTEInteraction import CloudNTEInteraction

        interaction = CloudNTEInteraction.__new__(CloudNTEInteraction)
        interaction._input_lock = threading.RLock()
        interaction._fake_activate_stop = threading.Event()
        interaction.hwnd_window = Mock()
        interaction.hwnd_window.hwnd = 123
        return interaction

    def test_send_key_dispatches_through_fake_activation(self):
        interaction = self._make_interaction()

        with (
            patch("src.interaction.CloudNTEInteraction.find_cloud_input_child", return_value=456),
            patch("src.interaction.CloudNTEInteraction.NTEInteraction") as parent,
            patch("win32gui.SendMessage") as send_message,
            patch("win32gui.PostMessage") as post_message,
        ):
            interaction.send_key("e")

            parent.send_key.assert_called_once_with(interaction, "e", 0.01)
            # fake activate (sync) + queued release (async, after the input)
            self.assertEqual(send_message.call_count, 1)
            self.assertEqual(post_message.call_count, 1)

    def test_send_key_down_and_up_dispatch(self):
        interaction = self._make_interaction()

        with (
            patch("src.interaction.CloudNTEInteraction.find_cloud_input_child", return_value=456),
            patch("src.interaction.CloudNTEInteraction.NTEInteraction") as parent,
            patch("win32gui.SendMessage"),
        ):
            interaction.send_key_down("w")
            interaction.send_key_up("w")

        parent.send_key_down.assert_called_once_with(interaction, "w", activate=False)
        parent.send_key_up.assert_called_once_with(interaction, "w")


if __name__ == "__main__":
    unittest.main()
