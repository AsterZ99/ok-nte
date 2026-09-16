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
        task.config = {"Run Target": "auto"}
        task._resolve_cloud_now = Mock(return_value=None)
        task._find_process = Mock(return_value=None)
        return task


class TestRunTargetConfig(unittest.TestCase):
    def test_invalid_run_target_falls_back_to_auto(self):
        task = _LauncherTaskBase()._make_task()
        task.config = {"Run Target": "banana"}

        self.assertEqual(task._get_run_target(), "auto")
        task.log_warning.assert_called_once()

    def test_run_target_values_are_respected(self):
        task = _LauncherTaskBase()._make_task()
        for value in ("auto", "local", "cloud"):
            task.config = {"Run Target": value}
            self.assertEqual(task._get_run_target(), value)


class TestCloudAutoDetection(_LauncherTaskBase):
    def test_run_uses_cloud_capture_when_cloud_client_is_running(self):
        task = self._make_task()
        cloud_target = Mock()
        task._resolve_cloud_now = Mock(return_value=cloud_target)

        task.run()

        task._wait_for_cloud_and_capture.assert_called_once_with()
        task._wait_for_game_and_capture.assert_not_called()

    def test_run_fails_closed_when_cloud_and_local_both_running(self):
        from ok import TaskDisabledException

        task = self._make_task()
        task._resolve_cloud_now = Mock(return_value=Mock())
        task._find_process = Mock(return_value={"pid": 1, "name": "HTGame.exe"})

        with self.assertRaisesRegex(TaskDisabledException, "Ambiguous run target"):
            task.run()
        task._wait_for_cloud_and_capture.assert_not_called()

    def test_run_target_local_skips_cloud_detection(self):
        from ok import TaskDisabledException

        task = self._make_task()
        task.config = {"Run Target": "local"}
        task._find_process = Mock(return_value=None)
        # never touch the real registry/launcher in tests
        task._get_launcher_path = Mock(return_value=None)

        with self.assertRaisesRegex(TaskDisabledException, "Launcher path not found"):
            task.run()

        task._resolve_cloud_now.assert_not_called()

    def test_run_target_cloud_does_not_check_local_process(self):
        task = self._make_task()
        task.config = {"Run Target": "cloud"}

        task.run()

        task._wait_for_cloud_and_capture.assert_called_once_with()
        task._find_process.assert_not_called()

    def test_run_falls_back_to_local_flow_without_cloud_client(self):
        task = self._make_task()
        task._find_process = Mock(return_value={"pid": 1, "name": "HTGame.exe"})
        task._update_launcher_path = Mock()
        task._wait_for_process = Mock(return_value=True)

        task.run()

        task._wait_for_cloud_and_capture.assert_not_called()
        task._wait_for_game_and_capture.assert_called_once_with(time_out=120, settle_window=False)

    def test_resolve_cloud_now_raises_on_ambiguity(self):
        from ok import TaskDisabledException

        task = self._make_task()
        del task._resolve_cloud_now  # use the real method, not the stub

        with (
            patch("src.interaction.cloud_window.collect_windows", return_value=[]),
            patch(
                "src.interaction.cloud_window.resolve_cloud_target",
                return_value=Mock(status="ambiguous"),
            ),
        ):
            with self.assertRaisesRegex(TaskDisabledException, "Multiple cloud game windows"):
                task._resolve_cloud_now()

    def test_resolve_cloud_now_returns_target_when_resolved(self):
        task = self._make_task()
        del task._resolve_cloud_now
        target = Mock()

        with (
            patch("src.interaction.cloud_window.collect_windows", return_value=[]),
            patch(
                "src.interaction.cloud_window.resolve_cloud_target",
                return_value=Mock(status="resolved", target=target),
            ),
        ):
            self.assertIs(task._resolve_cloud_now(), target)

    def test_resolve_cloud_now_returns_none_when_not_running(self):
        task = self._make_task()
        del task._resolve_cloud_now

        with (
            patch("src.interaction.cloud_window.collect_windows", return_value=[]),
            patch(
                "src.interaction.cloud_window.resolve_cloud_target",
                return_value=Mock(status="none"),
            ),
        ):
            self.assertIsNone(task._resolve_cloud_now())

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

    def _patch_og(self, selected_exe=CLOUD_EXE, interaction=None):
        device_manager = Mock()
        device_manager.hwnd_window.hwnd = 12586332
        device_manager.config = {"selected_exe": selected_exe}
        device_manager.interaction = interaction
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

    def test_health_check_skipped_for_local_target(self):
        frame = np.full((1080, 1920, 3), 120, dtype=np.uint8)
        task = self._make_health_task(frame)
        og_patch, iswindow_patch = self._patch_og(selected_exe="HTGame.exe")

        with og_patch, iswindow_patch:
            health = task.update_capture_health(frame=frame)

        # audit A-06: the cloud health gate must not change local behavior
        self.assertIsNone(health)
        task.scene.set_game_capture_ready.assert_not_called()

    def test_health_pushes_observation_to_cloud_interaction(self):
        frame = np.full((1080, 1920, 3), 120, dtype=np.uint8)
        task = self._make_health_task(frame)
        interaction = Mock()
        og_patch, iswindow_patch = self._patch_og(interaction=interaction)

        with og_patch, iswindow_patch:
            health = task.update_capture_health(frame=frame)

        self.assertEqual(health, CloudFrameHealth.OK)
        interaction.record_frame_health.assert_called_once_with(
            CloudFrameHealth.OK, (1920, 1080)
        )


class TestFrameObservationHook(_LauncherTaskBase):
    """The frame property must refresh the cloud health observation (A-06).

    Live failure (2026-09-16): the observation was pushed only once at task
    start, so every task longer than the staleness window had all input
    rejected as ``stale_frame`` while the click coordinates were correct.
    """

    # reuse the fixtures without re-running the parent class's test methods
    _make_health_task = TestCaptureHealth._make_health_task
    _patch_og = TestCaptureHealth._patch_og

    def _make_frame_task(self, frame):
        task = self._make_health_task(frame)
        task._executor = Mock()
        task._executor.frame = frame
        return task

    def test_frame_property_pushes_cloud_observation(self):
        frame = np.full((1080, 1920, 3), 120, dtype=np.uint8)
        task = self._make_frame_task(frame)
        interaction = Mock()
        og_patch, iswindow_patch = self._patch_og(interaction=interaction)

        with og_patch, iswindow_patch:
            returned = task.frame

        self.assertIs(returned, frame)
        interaction.record_frame_health.assert_called_once_with(
            CloudFrameHealth.OK, (1920, 1080)
        )

    def test_frame_property_passes_frame_without_rereading(self):
        """The hook must pass the frame down, not re-enter self.frame."""
        frame = np.full((1080, 1920, 3), 120, dtype=np.uint8)
        task = self._make_frame_task(frame)
        task.update_capture_health = Mock(return_value=CloudFrameHealth.OK)

        returned = task.frame

        self.assertIs(returned, frame)
        task.update_capture_health.assert_called_once_with(frame=frame)

    def test_frame_property_skips_local_target(self):
        frame = np.full((1080, 1920, 3), 120, dtype=np.uint8)
        task = self._make_frame_task(frame)
        interaction = Mock()
        og_patch, iswindow_patch = self._patch_og(
            selected_exe="HTGame.exe", interaction=interaction
        )

        with og_patch, iswindow_patch:
            returned = task.frame

        # local play must keep its exact previous behavior (audit A-06)
        self.assertIs(returned, frame)
        interaction.record_frame_health.assert_not_called()
        task.scene.set_game_capture_ready.assert_not_called()

    def test_frame_property_survives_health_failure(self):
        frame = np.full((1080, 1920, 3), 120, dtype=np.uint8)
        task = self._make_frame_task(frame)
        task.update_capture_health = Mock(side_effect=RuntimeError("probe exploded"))

        self.assertIs(task.frame, frame)
        task.log_warning.assert_called_once()

    def test_repeated_frame_reads_stay_throttled(self):
        frame = np.full((1080, 1920, 3), 120, dtype=np.uint8)
        task = self._make_frame_task(frame)
        interaction = Mock()
        og_patch, iswindow_patch = self._patch_og(interaction=interaction)

        with og_patch, iswindow_patch:
            for _ in range(5):
                task.frame

        # 5s throttle: five immediate reads produce exactly one observation
        interaction.record_frame_health.assert_called_once()
        task.scene.set_game_capture_ready.assert_called_once_with(True)


class TestActivationLease(_LauncherTaskBase):
    """Live matrix 2026-09-16: sustained activation is what makes clicks land.

    A single synchronous WA_ACTIVE before the button is not enough — the
    client drops the button. The lease keeps re-asserting activation while
    input is flowing and releases it once input goes idle, so no WA_INACTIVE
    may ever be queued between dispatches.
    """

    # resolved lazily: TestMouseMessageBackend is defined later in this module
    def _make_interaction(self):
        return TestMouseMessageBackend._make_interaction(self)

    def _gate_patches(self):
        return TestMouseMessageBackend._gate_patches(self)

    def test_dispatch_never_queues_deactivate(self):
        import win32con

        interaction = self._make_interaction()
        poster = interaction._pointer._poster

        with (
            self._gate_patches(),
            patch("win32gui.SendMessage") as send_message,
            patch("win32gui.PostMessage") as post_message,
            patch("src.interaction.CloudNTEInteraction.threading.Thread"),
            patch("time.sleep"),
        ):
            interaction.click(960, 540)
            interaction.mouse_down(900, 500)
            interaction.mouse_up()

        # activation is asserted, never released between dispatches
        for call in send_message.call_args_list:
            self.assertEqual(call.args[1], win32con.WM_ACTIVATE)
            self.assertEqual(call.args[2], win32con.WA_ACTIVE)
        self.assertEqual(post_message.call_count, 0)
        posted_messages = [call.args[1] for call in poster.call_args_list]
        self.assertNotIn(win32con.WM_ACTIVATE, posted_messages)

    def test_lease_is_reused_within_the_idle_window(self):
        interaction = self._make_interaction()
        started = []

        class _FakeThread:
            def __init__(self, *args, **kwargs):
                started.append(kwargs.get("name"))

            def start(self):
                pass

        with (
            patch("src.interaction.CloudNTEInteraction.threading.Thread", _FakeThread),
            patch("win32gui.SendMessage"),
            patch("time.sleep"),
        ):
            interaction._ensure_lease(300)
            interaction._ensure_lease(300)
            interaction._ensure_lease(300)

        self.assertEqual(started, ["cloud-activation-lease"])
        self.assertTrue(interaction._lease_active)

    def test_lease_refreshes_then_releases_when_idle(self):
        import win32con

        interaction = self._make_interaction()
        interaction.LEASE_REFRESH_SECONDS = 0
        interaction._lease_active = True
        interaction._lease_leaf = 300
        interaction._lease_deadline = 0  # already idle -> expire immediately

        with (
            patch.object(interaction._lease_wake, "wait", return_value=False),
            patch("win32gui.IsWindow", return_value=True),
            patch("win32gui.SendMessage") as send_message,
            patch("win32gui.PostMessage") as post_message,
        ):
            interaction._lease_loop()

        post_message.assert_called_once_with(
            300, win32con.WM_ACTIVATE, win32con.WA_INACTIVE, 0
        )
        send_message.assert_not_called()
        self.assertFalse(interaction._lease_active)

    def test_lease_loop_refreshes_while_hot(self):
        import time

        interaction = self._make_interaction()
        interaction.LEASE_REFRESH_SECONDS = 0
        interaction._lease_active = True
        interaction._lease_leaf = 300
        interaction._lease_deadline = time.time() + 60

        calls = []

        def _wait(timeout):
            calls.append(timeout)
            if len(calls) >= 2:
                interaction._lease_active = False  # end the loop
            return False

        with (
            patch.object(interaction._lease_wake, "wait", side_effect=_wait),
            patch("win32gui.IsWindow", return_value=True),
            patch("win32gui.SendMessage") as send_message,
            patch("win32gui.PostMessage") as post_message,
        ):
            interaction._lease_loop()

        # hot lease: re-asserts activation, never releases
        self.assertGreaterEqual(send_message.call_count, 1)
        post_message.assert_not_called()

    def test_stop_lease_releases_synchronously(self):
        import win32con

        interaction = self._make_interaction()
        interaction._lease_active = True
        interaction._lease_leaf = 300

        with patch("win32gui.SendMessage") as send_message, patch(
            "win32gui.PostMessage"
        ) as post_message, patch("win32gui.IsWindow", return_value=True):
            interaction.stop_lease()

        send_message.assert_called_once_with(
            300, win32con.WM_ACTIVATE, win32con.WA_INACTIVE, 0
        )
        post_message.assert_not_called()
        self.assertFalse(interaction._lease_active)
        self.assertTrue(interaction._lease_wake.is_set())

    def test_on_destroy_stops_the_lease(self):
        from src.interaction.NTEInteraction import NTEInteraction

        interaction = self._make_interaction()
        interaction._lease_active = True
        interaction._lease_leaf = 300

        with (
            patch("win32gui.IsWindow", return_value=False),
            patch("win32gui.SendMessage"),
            patch.object(NTEInteraction, "on_destroy"),
            patch("win32gui.PostMessage"),
        ):
            interaction.on_destroy()

        self.assertFalse(interaction._lease_active)


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

    def setUp(self):
        # keep the activation lease from spawning a real background thread in
        # tests (it would outlive the mocks and talk to a fake hwnd)
        thread_patch = patch("src.interaction.CloudNTEInteraction.threading.Thread")
        thread_patch.start()
        self.addCleanup(thread_patch.stop)

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
        interaction._lease_lock = threading.Lock()
        interaction._lease_active = False
        interaction._lease_leaf = None
        interaction._lease_deadline = 0.0
        interaction._lease_thread = None
        interaction._lease_wake = threading.Event()
        interaction._engagement = None
        interaction._engagement_counts = {}
        interaction._cursor_outside_count = 0
        interaction._background_warned = False
        interaction.hwnd_window = Mock()
        interaction.hwnd_window.hwnd = 100
        interaction.capture = Mock()
        interaction.capture.width = 1920
        interaction.capture.height = 1080
        return interaction

    def _gate_patches(self):
        from contextlib import ExitStack

        from src.interaction.cloud_window import CloudEngagement, CloudEngagementState

        stack = ExitStack()
        stack.enter_context(
            patch("src.interaction.CloudNTEInteraction.find_cloud_input_child", return_value=300)
        )
        stack.enter_context(patch("win32gui.IsWindow", return_value=True))
        stack.enter_context(patch("win32process.GetWindowThreadProcessId", return_value=(1, 42)))
        stack.enter_context(patch("win32gui.GetClientRect", return_value=(0, 0, 1920, 1080)))
        # dispatch tests assert message behaviour, not engagement sampling; the
        # engagement suite patches this again with its own sequence.
        stack.enter_context(
            patch(
                "src.interaction.CloudNTEInteraction.observe_engagement",
                return_value=CloudEngagementState(CloudEngagement.ENGAGED, 100, True),
            )
        )
        return stack

    def test_send_key_dispatches_through_fake_activation(self):
        interaction = self._make_interaction()

        with (
            self._gate_patches(),
            patch("src.interaction.CloudNTEInteraction.NTEInteraction") as parent,
            patch("win32gui.SendMessage") as send_message,
            patch("win32gui.PostMessage") as post_message,
            patch("src.interaction.CloudNTEInteraction.threading.Thread"),
            patch("time.sleep"),
        ):
            interaction.send_key("e")

            parent.send_key.assert_called_once_with(interaction, "e", 0.01)
            # lease model (2026-09-16 live matrix): activation is asserted for
            # the dispatch and must NOT be released between dispatches.
            self.assertEqual(send_message.call_count, 1)
            self.assertEqual(post_message.call_count, 0)

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


class TestEngagementObservation(TestMouseMessageBackend):
    """Real-client report 2026-09-16: input seems gated behind genuine
    engagement (a real click in the window). The dispatch path must record
    read-only evidence for that, and must never steal the foreground by
    default (audit A-01).
    """

    def _state(self, state, cursor_inside=True, foreground_hwnd=555):
        from src.interaction.cloud_window import CloudEngagementState

        return CloudEngagementState(
            state=state, foreground_hwnd=foreground_hwnd, cursor_inside=cursor_inside
        )

    def test_gate_records_every_observation(self):
        from src.interaction.cloud_window import CloudEngagement

        interaction = self._make_interaction()
        states = [CloudEngagement.ENGAGED, CloudEngagement.BACKGROUND]

        with (
            self._gate_patches(),
            patch(
                "src.interaction.CloudNTEInteraction.observe_engagement",
                side_effect=lambda *a, **k: self._state(states.pop(0)),
            ),
            patch("src.interaction.CloudNTEInteraction.logger"),
            patch("win32gui.PostMessage"),
            patch("win32gui.SendMessage"),
            patch("time.sleep"),
        ):
            interaction._gate((10, 10))
            interaction._gate((10, 10))

        report = interaction.engagement_report()
        self.assertEqual(report["counts"], {"engaged": 1, "background": 1})
        self.assertEqual(report["last_state"], "background")

    def test_background_warns_once_per_episode_then_again_after_engage(self):
        from src.interaction.cloud_window import CloudEngagement

        interaction = self._make_interaction()
        sequence = [
            CloudEngagement.BACKGROUND,
            CloudEngagement.BACKGROUND,
            CloudEngagement.ENGAGED,
            CloudEngagement.BACKGROUND,
        ]

        with (
            self._gate_patches(),
            patch(
                "src.interaction.CloudNTEInteraction.observe_engagement",
                side_effect=lambda *a, **k: self._state(sequence.pop(0)),
            ),
            patch("src.interaction.CloudNTEInteraction.logger") as log_mock,
        ):
            for _ in range(4):
                interaction._gate((10, 10))

        warnings = [call for call in log_mock.warning.call_args_list]
        self.assertEqual(len(warnings), 2, "background must warn once per episode")
        self.assertIn("does not own the foreground", warnings[0].args[0])

    def test_cursor_outside_window_is_counted(self):
        from src.interaction.cloud_window import CloudEngagement

        interaction = self._make_interaction()

        with (
            self._gate_patches(),
            patch(
                "src.interaction.CloudNTEInteraction.observe_engagement",
                return_value=self._state(CloudEngagement.ENGAGED, cursor_inside=False),
            ),
            patch("src.interaction.CloudNTEInteraction.logger"),
        ):
            interaction._gate((10, 10))
            interaction._gate((10, 10))

        self.assertEqual(interaction.engagement_report()["cursor_outside_count"], 2)

    def test_probe_failure_never_breaks_dispatch(self):
        interaction = self._make_interaction()

        with (
            self._gate_patches(),
            patch(
                "src.interaction.CloudNTEInteraction.observe_engagement",
                side_effect=RuntimeError("probe exploded"),
            ),
            patch("src.interaction.CloudNTEInteraction.logger") as log_mock,
        ):
            result, snapshot = interaction._gate((10, 10))

        self.assertFalse(result.blocked)
        self.assertIsNotNone(snapshot)
        log_mock.warning.assert_called()

    def test_foreground_is_never_taken_by_default(self):
        from src.interaction.cloud_window import CloudEngagement

        interaction = self._make_interaction()

        with (
            self._gate_patches(),
            patch(
                "src.interaction.CloudNTEInteraction.observe_engagement",
                return_value=self._state(CloudEngagement.BACKGROUND, cursor_inside=False),
            ),
            patch("src.interaction.CloudNTEInteraction.force_foreground") as steal,
            patch("src.interaction.CloudNTEInteraction.logger"),
            patch("win32gui.SendMessage"),
            patch("time.sleep"),
        ):
            interaction._gate((10, 10))
            interaction.activate()

        steal.assert_not_called()
        self.assertFalse(CloudNTEInteraction.ENGAGE_FOREGROUND_ON_START)

    def test_opt_in_engagement_takes_the_foreground(self):
        from src.interaction.cloud_window import CloudEngagement

        interaction = self._make_interaction()
        interaction.ENGAGE_FOREGROUND_ON_START = True

        with (
            self._gate_patches(),
            patch(
                "src.interaction.CloudNTEInteraction.observe_engagement",
                return_value=self._state(CloudEngagement.BACKGROUND),
            ),
            patch(
                "src.interaction.CloudNTEInteraction.force_foreground",
                return_value=True,
            ) as steal,
            patch("win32gui.IsWindow", return_value=True),
            patch("src.interaction.CloudNTEInteraction.logger"),
        ):
            self.assertTrue(interaction.engage_foreground())

        steal.assert_called_once_with(100)

    def test_engage_foreground_fails_closed_without_main_window(self):
        interaction = self._make_interaction()
        interaction.hwnd_window = None

        with patch("src.interaction.CloudNTEInteraction.force_foreground") as steal:
            self.assertFalse(interaction.engage_foreground())

        steal.assert_not_called()

    def test_destroy_logs_engagement_summary(self):
        interaction = self._make_interaction()
        interaction._engagement_counts = {"background": 12}

        with (
            patch("win32gui.IsWindow", return_value=True),
            patch("win32gui.PostMessage"),
            patch("win32gui.SendMessage"),
            patch("src.interaction.NTEInteraction.NTEInteraction.on_destroy"),
            patch("src.interaction.CloudNTEInteraction.logger") as log_mock,
        ):
            interaction.on_destroy()

        summary = [call for call in log_mock.info.call_args_list if "summary" in call.args[0]]
        self.assertEqual(len(summary), 1)
        self.assertIn("background", summary[0].args[0])


if __name__ == "__main__":
    unittest.main()
