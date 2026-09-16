"""Tests for the cloud NTE window probe. Synthetic fixtures only, no real windows."""

import json
import unittest

from src.interaction import cloud_window as cw
from tools import cloud_nte_probe as probe


def make_record(**overrides):
    fields = {
        "pid": 1000,
        "process_name": "viewer.exe",
        "hwnd": 100,
        "parent_hwnd": 0,
        "root_hwnd": 100,
        "class_name": "ViewerWindow",
        "title": "Viewer",
        "visible": True,
        "enabled": True,
        "minimized": False,
        "client_rect": (0, 0, 0, 0),
        "window_rect": (0, 0, 0, 0),
        "dpi": 96,
        "depth": 0,
        "child_count": 0,
    }
    fields.update(overrides)
    return probe.WindowRecord(**fields)


def cloud_window(hwnd=200, parent_hwnd=0, depth=0, **overrides):
    fields = {
        "pid": 2000,
        "process_name": "WLCloudGameClient.exe",
        "hwnd": hwnd,
        "parent_hwnd": parent_hwnd,
        "root_hwnd": hwnd,
        "class_name": "Qt51514QWindowIcon",
        "title": "云异环",
        "visible": True,
        "enabled": True,
        "minimized": False,
        "client_rect": (0, 0, 1920, 1080),
        "window_rect": (0, 0, 1920, 1080),
        "dpi": 96,
        "depth": depth,
        "child_count": 0,
    }
    fields.update(overrides)
    return probe.WindowRecord(**fields)


def local_game_window(hwnd=300, **overrides):
    fields = {
        "pid": 3000,
        "process_name": "HTGame.exe",
        "hwnd": hwnd,
        "parent_hwnd": 0,
        "root_hwnd": hwnd,
        "class_name": "UnrealWindow",
        "title": "异环",
        "visible": True,
        "enabled": True,
        "minimized": False,
        "client_rect": (0, 0, 1920, 1080),
        "window_rect": (0, 0, 1920, 1080),
        "dpi": 96,
        "depth": 0,
        "child_count": 0,
    }
    fields.update(overrides)
    return probe.WindowRecord(**fields)


class QtClassPatternTests(unittest.TestCase):
    def test_matches_known_qt_window_icon_class_shapes(self):
        self.assertTrue(probe.qt_class_pattern("Qt51514QWindowIcon"))
        self.assertTrue(probe.qt_class_pattern("Qt5QWindowIcon"))
        self.assertTrue(probe.qt_class_pattern("Qt661QWindowIcon"))
        self.assertTrue(probe.qt_class_pattern(" qt661qwindowicon "))

    def test_does_not_match_other_classes(self):
        self.assertFalse(probe.qt_class_pattern("UnrealWindow"))
        self.assertFalse(probe.qt_class_pattern("Qt5QWindow"))
        self.assertFalse(probe.qt_class_pattern(""))
        self.assertFalse(probe.qt_class_pattern(None))


class RedactionTests(unittest.TestCase):
    def test_redact_title_replaces_any_non_empty_title(self):
        self.assertEqual(probe.redact_title("异环"), probe.REDACTED_TITLE)
        self.assertEqual(probe.redact_title("contains user name"), probe.REDACTED_TITLE)
        self.assertEqual(probe.redact_title(""), "")
        self.assertEqual(probe.redact_title(None), "")

    def test_redact_path_keeps_basename_only(self):
        self.assertEqual(
            probe.redact_path("C:\\Users\\me\\WLCloudGameClient.exe"), "WLCloudGameClient.exe"
        )
        self.assertEqual(probe.redact_path("/opt/games/ntecloud"), "ntecloud")
        self.assertEqual(probe.redact_path(""), "")

    def test_report_redacts_titles_by_default_and_keeps_them_on_request(self):
        records = [cloud_window(), make_record(title="")]
        captured_at = "2026-09-16T00:00:00+08:00"

        default_report = probe.build_report(records, probe.evaluate_windows(records), captured_at)
        titled_report = probe.build_report(
            records, probe.evaluate_windows(records), captured_at, include_titles=True
        )

        titles = {window["hwnd"]: window["title"] for window in default_report["windows"]}
        self.assertEqual(titles[200], probe.REDACTED_TITLE)
        self.assertEqual(titles[100], "")
        titled = {window["hwnd"]: window["title"] for window in titled_report["windows"]}
        self.assertEqual(titled[200], "云异环")

    def test_process_name_is_already_stored_as_basename(self):
        record = cloud_window(process_name=probe.redact_path("C:\\apps\\WLCloudGameClient.exe"))
        captured_at = "2026-09-16T00:00:00+08:00"
        report = probe.build_report([record], probe.evaluate_windows([record]), captured_at)
        self.assertEqual(report["windows"][0]["process_name"], "WLCloudGameClient.exe")


class ScoringTests(unittest.TestCase):
    def evaluate(self, records):
        return probe.evaluate_windows(records)

    def test_cloud_streaming_window_combination_is_candidate(self):
        evaluation = self.evaluate([cloud_window()])[200]
        self.assertTrue(evaluation.is_candidate)
        self.assertIn("process_marker:cloud", evaluation.reasons)
        self.assertIn("qt_class_marker_combination", evaluation.reasons)
        self.assertIn("size_16_9", evaluation.reasons)

    def test_qt_class_pattern_alone_is_not_enough(self):
        record = make_record(
            process_name="viewer.exe",
            class_name="Qt51514QWindowIcon",
            client_rect=(0, 0, 1920, 1080),
        )
        evaluation = self.evaluate([record])[100]
        self.assertFalse(evaluation.is_candidate)
        self.assertNotIn("qt_class_marker_combination", evaluation.reasons)

    def test_title_marker_alone_is_not_enough(self):
        record = make_record(title="云异环", client_rect=(0, 0, 1920, 1080))
        evaluation = self.evaluate([record])[100]
        self.assertFalse(evaluation.is_candidate)
        self.assertIn("title_marker:异环", evaluation.reasons)

    def test_small_window_with_marker_process_is_not_candidate(self):
        record = cloud_window(client_rect=(0, 0, 640, 480))
        evaluation = self.evaluate([record])[200]
        self.assertFalse(evaluation.is_candidate)

    def test_hidden_window_is_never_candidate(self):
        for field in ("visible", "enabled"):
            record = cloud_window(**{field: False})
            self.assertFalse(self.evaluate([record])[200].is_candidate, field)
        record = cloud_window(minimized=True)
        self.assertFalse(self.evaluate([record])[200].is_candidate)

    def test_local_game_window_is_also_reported_as_candidate(self):
        evaluation = self.evaluate([local_game_window()])[300]
        self.assertTrue(evaluation.is_candidate)
        self.assertIn("class_exact:unrealwindow", evaluation.reasons)
        self.assertIn("process_marker:htgame", evaluation.reasons)

    def test_child_of_marked_parent_gets_structural_reason(self):
        parent = cloud_window(hwnd=200)
        child = cloud_window(
            hwnd=201,
            parent_hwnd=200,
            depth=1,
            class_name="Qt5QWindowIcon",
            title="",
            client_rect=(0, 0, 1600, 900),
        )
        evaluation = self.evaluate([parent, child])[201]
        self.assertTrue(evaluation.is_candidate)
        self.assertIn("parent_class_marker", evaluation.reasons)

    def test_non_16_9_large_window_lacks_size_reason(self):
        record = cloud_window(client_rect=(0, 0, 1200, 1080))
        evaluation = self.evaluate([record])[200]
        self.assertNotIn("size_16_9", evaluation.reasons)


class SelectionTests(unittest.TestCase):
    def test_no_candidates(self):
        selection = probe.select_candidates(probe.evaluate_windows([make_record()]))
        self.assertEqual(selection["status"], "none")
        self.assertEqual(selection["hwnds"], [])

    def test_single_candidate(self):
        selection = probe.select_candidates(probe.evaluate_windows([cloud_window()]))
        self.assertEqual(selection["status"], "single")
        self.assertEqual(selection["hwnds"], [200])

    def test_multiple_candidates_are_ambiguous_and_ranked(self):
        records = [cloud_window(hwnd=200), local_game_window(), make_record()]
        selection = probe.select_candidates(probe.evaluate_windows(records))
        self.assertEqual(selection["status"], "ambiguous")
        self.assertEqual(len(selection["hwnds"]), 2)


class ReportSchemaTests(unittest.TestCase):
    REQUIRED_WINDOW_KEYS = (
        "pid",
        "process_name",
        "hwnd",
        "parent_hwnd",
        "root_hwnd",
        "class_name",
        "title",
        "visible",
        "enabled",
        "minimized",
        "client_rect",
        "window_rect",
        "dpi",
        "depth",
        "child_count",
        "score",
        "candidate_reasons",
        "is_candidate",
    )

    def build(self, records):
        return probe.build_report(
            records, probe.evaluate_windows(records), "2026-09-16T00:00:00+08:00"
        )

    def test_schema_version_and_selection_are_present(self):
        report = self.build([cloud_window(), make_record()])
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["captured_at"], "2026-09-16T00:00:00+08:00")
        self.assertEqual(report["selection"]["status"], "single")
        self.assertEqual(report["selection"]["hwnds"], [200])

    def test_every_window_has_the_contract_fields(self):
        report = self.build([cloud_window(), make_record(minimized=True)])
        self.assertEqual(len(report["windows"]), 2)
        for window in report["windows"]:
            for key in self.REQUIRED_WINDOW_KEYS:
                self.assertIn(key, window)
            self.assertIsInstance(window["candidate_reasons"], list)

    def test_serialization_is_deterministic(self):
        records = [cloud_window(), local_game_window(), make_record()]
        first = probe.serialize_report(self.build(records))
        second = probe.serialize_report(self.build(records))
        self.assertEqual(first, second)
        self.assertEqual(
            [window["hwnd"] for window in json.loads(first)["windows"]], [100, 200, 300]
        )


class FrameContentStatsTests(unittest.TestCase):
    def test_alpha_only_pixels_count_as_black(self):
        pixels = bytes([0, 0, 0, 255] * 4)
        self.assertEqual(probe.frame_content_stats(pixels), (0, 4))

    def test_visible_content_pixels_are_counted(self):
        pixels = bytes([1, 0, 0, 255, 0, 2, 0, 255, 0, 0, 0, 255, 0, 0, 3, 255])
        self.assertEqual(probe.frame_content_stats(pixels), (3, 4))

    def test_empty_buffer(self):
        self.assertEqual(probe.frame_content_stats(b""), (0, 0))


def cloud_main_window(hwnd=12586332, **overrides):
    """The real main window confirmed by the Phase 0 probe."""
    fields = {
        "pid": 43532,
        "process_name": "NTECloudGame.exe",
        "hwnd": hwnd,
        "parent_hwnd": 0,
        "root_hwnd": hwnd,
        "class_name": "Qt51517QWindowIcon",
        "title": "云·异环",
        "visible": True,
        "enabled": True,
        "minimized": False,
        "client_rect": (0, 0, 1920, 1080),
        "window_rect": (0, 0, 1920, 1080),
        "dpi": 96,
        "depth": 0,
        "child_count": 0,
    }
    fields.update(overrides)
    return probe.WindowRecord(**fields)


def cloud_ghost_window(hwnd=7211582, **overrides):
    """A ghost render surface of the real client (not WGC-capturable)."""
    fields = {
        "pid": 43532,
        "process_name": "NTECloudGame.exe",
        "hwnd": hwnd,
        "parent_hwnd": 0,
        "root_hwnd": hwnd,
        "class_name": "Qt51517QWindowIcon",
        "title": "NTECloudGame",
        "visible": True,
        "enabled": True,
        "minimized": False,
        "client_rect": (0, 0, 1920, 1080),
        "window_rect": (0, 0, 1920, 1080),
        "dpi": 96,
        "depth": 0,
        "child_count": 0,
    }
    fields.update(overrides)
    return probe.WindowRecord(**fields)


def ghost_browser_surface(hwnd=12062950):
    return cloud_ghost_window(hwnd, class_name="WLCloudGameClient", title="WLCloudGame")


class ResolveCloudTargetTests(unittest.TestCase):
    def test_resolves_the_real_main_window(self):
        records = [cloud_main_window(), cloud_ghost_window(), ghost_browser_surface()]
        resolution = cw.resolve_cloud_target(records)
        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.target.hwnd, 12586332)
        self.assertEqual(resolution.target.kind, cw.CloudTargetKind.CLOUD)
        self.assertEqual(resolution.target.process_name, "NTECloudGame.exe")

    def test_ghost_windows_alone_are_never_resolved(self):
        records = [cloud_ghost_window(), ghost_browser_surface()]
        resolution = cw.resolve_cloud_target(records)
        self.assertEqual(resolution.status, "none")
        self.assertIsNone(resolution.target)

    def test_rebuild_overlap_is_ambiguous_and_fails_closed(self):
        records = [
            cloud_main_window(12586332),
            cloud_main_window(15000000, pid=43600),
        ]
        resolution = cw.resolve_cloud_target(records)
        self.assertEqual(resolution.status, "ambiguous")
        self.assertIsNone(resolution.target)
        self.assertEqual(resolution.hwnds, (12586332, 15000000))

    def test_local_game_is_never_resolved_as_cloud(self):
        records = [local_game_window()]
        resolution = cw.resolve_cloud_target(records)
        self.assertEqual(resolution.status, "none")

    def test_hidden_main_window_is_not_resolved(self):
        records = [cloud_main_window(visible=False)]
        resolution = cw.resolve_cloud_target(records)
        self.assertEqual(resolution.status, "none")


class FrameHealthTests(unittest.TestCase):
    def test_healthy_frame(self):
        health = cw.assess_frame_health(1920, 1080, content_ratio=0.96)
        self.assertEqual(health, cw.CloudFrameHealth.OK)

    def test_invalid_hwnd_wins_over_everything(self):
        health = cw.assess_frame_health(0, 0, hwnd_valid=False)
        self.assertEqual(health, cw.CloudFrameHealth.HWND_INVALID)

    def test_missing_frame(self):
        health = cw.assess_frame_health(0, 0)
        self.assertEqual(health, cw.CloudFrameHealth.NO_FRAME)

    def test_size_change_against_expected(self):
        health = cw.assess_frame_health(1600, 900, expected_size=(1920, 1080))
        self.assertEqual(health, cw.CloudFrameHealth.SIZE_CHANGED)

    def test_size_below_project_contract(self):
        health = cw.assess_frame_health(1600, 900)
        self.assertEqual(health, cw.CloudFrameHealth.SIZE_TOO_SMALL)

    def test_aspect_mismatch(self):
        health = cw.assess_frame_health(2560, 1080)
        self.assertEqual(health, cw.CloudFrameHealth.ASPECT_MISMATCH)

    def test_black_frame(self):
        health = cw.assess_frame_health(1920, 1080, content_ratio=0.0005)
        self.assertEqual(health, cw.CloudFrameHealth.BLACK)

    def test_size_check_runs_before_black_check(self):
        health = cw.assess_frame_health(1600, 900, content_ratio=0.0)
        self.assertEqual(health, cw.CloudFrameHealth.SIZE_TOO_SMALL)


class TreeRenderTests(unittest.TestCase):
    def test_children_are_indented_under_their_parent(self):
        parent = cloud_window(hwnd=200)
        child = cloud_window(hwnd=201, parent_hwnd=200, depth=1)
        tree = probe.render_tree([parent, child])
        lines = tree.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("hwnd=200", lines[0])
        self.assertTrue(lines[1].startswith("  "))
        self.assertIn("hwnd=201", lines[1])

    def test_orphan_parent_is_treated_as_top_level(self):
        record = cloud_window(hwnd=200, parent_hwnd=999)
        tree = probe.render_tree([record])
        self.assertIn("hwnd=200", tree)
        self.assertFalse(tree.splitlines()[0].startswith(" "))


if __name__ == "__main__":
    unittest.main()
