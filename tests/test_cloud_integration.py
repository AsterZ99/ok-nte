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
        import time

        from src.interaction.cloud_mouse import CloudMessagePointer

        interaction = CloudNTEInteraction.__new__(CloudNTEInteraction)
        interaction._input_lock = threading.RLock()
        interaction._pointer = CloudMessagePointer(Mock(return_value=True))
        interaction._generation = 0
        interaction._last_target = (0, 0)
        interaction._frame_health = CloudFrameHealth.OK
        interaction._frame_observed_at = time.time()
        interaction._frame_size = (0, 0)
        interaction.hwnd_window = Mock()
        interaction.hwnd_window.hwnd = 100
        interaction.capture = Mock()
        interaction.capture.width = 1920
        interaction.capture.height = 1080
        return interaction

    def _gate_patches(self):
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(
            patch("src.interaction.CloudNTEInteraction.find_cloud_input_child", return_value=300)
        )
        stack.enter_context(patch("win32gui.IsWindow", return_value=True))
        stack.enter_context(patch("win32process.GetWindowThreadProcessId", return_value=(1, 42)))
        stack.enter_context(patch("win32gui.GetClientRect", return_value=(0, 0, 1920, 1080)))
        return stack

    def test_send_key_dispatches_through_fake_activation(self):
        interaction = self._make_interaction()

        with (
            self._gate_patches(),
            patch("src.interaction.CloudNTEInteraction.NTEInteraction") as parent,
            patch("win32gui.SendMessage") as send_message,
            patch("win32gui.PostMessage") as post_message,
        ):
            interaction.send_key("e")

            parent.send_key.assert_called_once_with(interaction, "e", 0.01)
            # per-dispatch lease: sync activate, queued release
            self.assertEqual(send_message.call_count, 1)
            self.assertEqual(post_message.call_count, 1)

    def test_send_key_down_and_up_dispatch(self):
        interaction = self._make_interaction()

        with (
            self._gate_patches(),
            patch("src.interaction.CloudNTEInteraction.NTEInteraction") as parent,
            patch("win32gui.SendMessage"),
            patch("win32gui.PostMessage"),
        ):
            interaction.send_key_down("w")
            interaction.send_key_up("w")

        parent.send_key_down.assert_called_once_with(interaction, "w", activate=False)
        parent.send_key_up.assert_called_once_with(interaction, "w")

    def test_send_key_blocked_when_leaf_chain_missing(self):
        interaction = self._make_interaction()

        with (
            patch(
                "src.interaction.CloudNTEInteraction.find_cloud_input_child",
                return_value=0,
            ),
            patch("win32gui.IsWindow", return_value=True),
            patch("src.interaction.CloudNTEInteraction.NTEInteraction") as parent,
            patch("win32gui.SendMessage") as send_message,
        ):
            interaction.send_key("e")

        # fail closed: no parent dispatch, no activation, no fallback target
        parent.send_key.assert_not_called()
        send_message.assert_not_called()


class TestMouseMessageBackend(TestKeyboardDispatch):
    """Audit A-01/A-02/A-04/A-10: background message backend behavior."""

    def _make_interaction(self):
        interaction = TestKeyboardDispatch._make_interaction(self)
        return interaction

    def test_module_has_no_physical_input_api(self):
        import src.interaction.CloudNTEInteraction as module

        self.assertFalse(hasattr(module, "win32api"), "physical input API imported")

    def test_click_posts_message_sequence_to_leaf(self):
        import win32con

        interaction = self._make_interaction()
        poster = interaction._pointer._poster

        with (
            self._gate_patches(),
            patch("win32gui.SendMessage"),
            patch("win32gui.PostMessage"),
            patch("time.sleep"),
        ):
            result = interaction.click(960, 540)

        self.assertTrue(result.ok)
        posted = [call.args for call in poster.call_args_list]
        self.assertEqual(len(posted), 3)
        self.assertEqual(posted[0][0], 300)
        self.assertEqual(posted[0][1], win32con.WM_MOUSEMOVE)
        self.assertEqual(posted[1][1], win32con.WM_LBUTTONDOWN)
        self.assertEqual(posted[1][2], win32con.MK_LBUTTON)
        self.assertEqual(posted[2][1], win32con.WM_LBUTTONUP)
        self.assertEqual(posted[1][3], posted[2][3])  # same lparam

    def test_click_blocked_outside_content_rect_sends_nothing(self):
        interaction = self._make_interaction()
        poster = interaction._pointer._poster

        with (
            self._gate_patches(),
            patch("win32gui.SendMessage") as send_message,
            patch("win32gui.PostMessage") as post_message,
        ):
            result = interaction.click(5000, 5000)

        self.assertTrue(result.blocked)
        self.assertEqual(result.reason.value, "out_of_bounds")
        poster.assert_not_called()
        send_message.assert_not_called()
        post_message.assert_not_called()

    def test_click_blocked_when_leaf_chain_missing(self):
        interaction = self._make_interaction()
        poster = interaction._pointer._poster

        with (
            patch(
                "src.interaction.CloudNTEInteraction.find_cloud_input_child",
                return_value=0,
            ),
            patch("win32gui.IsWindow", return_value=True),
            patch("win32gui.SendMessage") as send_message,
        ):
            result = interaction.click(100, 100)

        self.assertTrue(result.blocked)
        self.assertEqual(result.reason.value, "no_target")
        poster.assert_not_called()
        send_message.assert_not_called()

    def test_pid_mismatch_blocks_dispatch(self):
        interaction = self._make_interaction()
        poster = interaction._pointer._poster

        with (
            self._gate_patches(),
            patch("win32process.GetWindowThreadProcessId", side_effect=[(1, 42), (1, 43)]),
            patch("win32gui.SendMessage") as send_message,
        ):
            result = interaction.click(100, 100)

        self.assertTrue(result.blocked)
        self.assertEqual(result.reason.value, "no_target")
        poster.assert_not_called()
        send_message.assert_not_called()

    def test_unhealthy_frame_blocks_dispatch(self):
        interaction = self._make_interaction()
        interaction._frame_health = CloudFrameHealth.BLACK
        poster = interaction._pointer._poster

        with (
            self._gate_patches(),
            patch("win32gui.SendMessage") as send_message,
        ):
            result = interaction.click(100, 100)

        self.assertTrue(result.blocked)
        self.assertEqual(result.reason.value, "unhealthy_frame")
        poster.assert_not_called()
        send_message.assert_not_called()

    def test_mouse_down_up_are_paired_on_leaf(self):
        import win32con

        interaction = self._make_interaction()
        poster = interaction._pointer._poster

        with (
            self._gate_patches(),
            patch("win32gui.SendMessage"),
            patch("win32gui.PostMessage"),
        ):
            down = interaction.mouse_down(100, 200)
            self.assertTrue(down.ok)
            self.assertTrue(interaction._pointer.buttons.is_pressed("left"))
            up = interaction.mouse_up()
            self.assertTrue(up.ok)

        self.assertEqual(poster.call_args_list[0].args[1], win32con.WM_LBUTTONDOWN)
        self.assertEqual(poster.call_args_list[1].args[1], win32con.WM_LBUTTONUP)
        self.assertFalse(interaction._pointer.buttons.is_pressed("left"))

    def test_on_destroy_releases_pressed_buttons(self):
        import win32con

        interaction = self._make_interaction()
        interaction._pointer.buttons.press("left", 300)
        interaction._last_target = (100, 300)

        with (
            patch("win32gui.IsWindow", return_value=True),
            patch("win32gui.PostMessage") as post_message,
            patch("win32gui.SendMessage"),
            patch(
                "src.interaction.NTEInteraction.NTEInteraction.on_destroy"
            ) as parent_destroy,
        ):
            interaction.on_destroy()

        released = [call.args for call in post_message.call_args_list]
        self.assertIn((300, win32con.WM_LBUTTONUP, 0, 0), released)
        self.assertEqual(interaction._pointer.buttons.pressed_buttons(), {})
        parent_destroy.assert_called_once()

    def test_scroll_is_unsupported(self):
        interaction = self._make_interaction()

        with patch("src.interaction.CloudNTEInteraction.logger") as log_mock:
            result = interaction.scroll(100, 100, 3)

        self.assertTrue(result.blocked)
        self.assertEqual(result.reason.value, "unsupported")
        log_mock.warning.assert_called()


if __name__ == "__main__":
    unittest.main()
