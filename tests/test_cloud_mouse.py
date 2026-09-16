"""Pure-logic tests for the cloud mouse contracts (audit WP-1 / A-10).

No real windows involved: everything runs against synthetic snapshots and a
recorded poster.
"""

import time
import unittest

import win32con

from src.interaction.cloud_mouse import (
    ButtonState,
    CloudDispatchReason,
    CloudDispatchStatus,
    CloudInputSnapshot,
    CloudMessagePointer,
    map_capture_to_leaf,
    pack_client_lparam,
    pack_screen_lparam,
    validate_dispatch,
)
from src.interaction.cloud_window import CloudFrameHealth


def make_snapshot(**overrides):
    values = dict(
        main_hwnd=100,
        leaf_hwnd=300,
        process_id=42,
        capture_size=(1920, 1080),
        content_rect=(0, 0, 1920, 1080),
        leaf_client_size=(1920, 1080),
        frame_health=CloudFrameHealth.OK,
        frame_observed_at=time.time(),
        generation=1,
    )
    values.update(overrides)
    return CloudInputSnapshot(**values)


class MapCaptureToLeafTests(unittest.TestCase):
    def test_identity_when_sizes_match(self):
        self.assertEqual(
            map_capture_to_leaf((960, 540), (1920, 1080), (0, 0, 1920, 1080), (1920, 1080)),
            (960, 540),
        )

    def test_scales_proportionally(self):
        point = map_capture_to_leaf(
            (960, 540), (1920, 1080), (0, 0, 1920, 1080), (1280, 720)
        )
        self.assertEqual(point, (640, 360))

    def test_content_offset_is_removed(self):
        point = map_capture_to_leaf(
            (970, 560), (1920, 1080), (10, 20, 1930, 1100), (1920, 1080)
        )
        self.assertEqual(point, (960, 540))

    def test_letterbox_black_bars_are_not_clickable(self):
        # content rect is the 1920x1080 picture inside a 2560x1440 frame
        self.assertIsNone(
            map_capture_to_leaf(
                (100, 700), (2560, 1440), (320, 180, 2240, 1260), (1920, 1080)
            )
        )
        self.assertIsNotNone(
            map_capture_to_leaf(
                (1280, 720), (2560, 1440), (320, 180, 2240, 1260), (1920, 1080)
            )
        )

    def test_rejects_negative_and_nan(self):
        base = ((1920, 1080), (0, 0, 1920, 1080), (1920, 1080))
        self.assertIsNone(map_capture_to_leaf((-1, 5), *base))
        self.assertIsNone(map_capture_to_leaf((5, -1), *base))
        self.assertIsNone(map_capture_to_leaf((float("nan"), 5), *base))

    def test_half_open_bounds(self):
        base = ((1920, 1080), (0, 0, 1920, 1080), (1920, 1080))
        self.assertIsNotNone(map_capture_to_leaf((1919, 1079), *base))
        self.assertIsNone(map_capture_to_leaf((1920, 540), *base))
        self.assertIsNone(map_capture_to_leaf((960, 1080), *base))

    def test_no_silent_clamping_outside_leaf(self):
        # a point inside the content but beyond the smaller leaf must scale,
        # not clamp: (1900, 1070) -> (1267, 713) in a 1280x720 leaf
        point = map_capture_to_leaf(
            (1900, 1070), (1920, 1080), (0, 0, 1920, 1080), (1280, 720)
        )
        self.assertEqual(point, (1267, 713))

    def test_degenerate_rects_rejected(self):
        self.assertIsNone(map_capture_to_leaf((5, 5), (0, 0), (0, 0, 10, 10), (10, 10)))
        self.assertIsNone(map_capture_to_leaf((5, 5), (1920, 1080), (0, 0, 0, 10), (10, 10)))
        self.assertIsNone(map_capture_to_leaf((5, 5), (1920, 1080), (0, 0, 10, 10), (0, 0)))


class ValidateDispatchTests(unittest.TestCase):
    def test_valid_point_is_approved_with_leaf_point(self):
        result, leaf = validate_dispatch(make_snapshot(), (960, 540), now=time.time())
        self.assertEqual(result.status, CloudDispatchStatus.PENDING)
        self.assertEqual(leaf, (960, 540))

    def test_incomplete_target_blocked(self):
        for overrides in ({"leaf_hwnd": 0}, {"main_hwnd": 0}, {"process_id": 0}):
            result, point = validate_dispatch(make_snapshot(**overrides), (10, 10))
            self.assertEqual(result.status, CloudDispatchStatus.BLOCKED)
            self.assertEqual(result.reason, CloudDispatchReason.NO_TARGET)
            self.assertIsNone(point)

    def test_unhealthy_frame_blocked(self):
        result, _ = validate_dispatch(
            make_snapshot(frame_health=CloudFrameHealth.BLACK), (10, 10)
        )
        self.assertEqual(result.reason, CloudDispatchReason.UNHEALTHY_FRAME)

    def test_size_changed_blocked(self):
        result, _ = validate_dispatch(
            make_snapshot(frame_health=CloudFrameHealth.SIZE_CHANGED), (10, 10)
        )
        self.assertEqual(result.reason, CloudDispatchReason.SIZE_CHANGED)

    def test_stale_frame_blocked(self):
        result, _ = validate_dispatch(
            make_snapshot(frame_observed_at=time.time() - 120), (10, 10), now=time.time()
        )
        self.assertEqual(result.reason, CloudDispatchReason.STALE_FRAME)

    def test_fresh_frame_passes(self):
        result, _ = validate_dispatch(
            make_snapshot(frame_observed_at=time.time() - 5), (10, 10), now=time.time()
        )
        self.assertEqual(result.status, CloudDispatchStatus.PENDING)

    def test_capture_size_mismatch_blocked(self):
        result, _ = validate_dispatch(
            make_snapshot(capture_size=(1600, 900)), (10, 10), expected_capture_size=(1920, 1080)
        )
        self.assertEqual(result.reason, CloudDispatchReason.SIZE_CHANGED)

    def test_out_of_bounds_blocked(self):
        result, _ = validate_dispatch(make_snapshot(), (5000, 5000))
        self.assertEqual(result.reason, CloudDispatchReason.OUT_OF_BOUNDS)


class PackingTests(unittest.TestCase):
    def test_client_lparam_layout(self):
        self.assertEqual(pack_client_lparam(960, 540), (540 << 16) | 960)

    def test_screen_lparam_supports_negative_virtual_screen(self):
        packed = pack_screen_lparam(-1920, -100)
        lo = packed & 0xFFFF
        hi = (packed >> 16) & 0xFFFF
        self.assertEqual(lo, (-1920) & 0xFFFF)
        self.assertEqual(hi, (-100) & 0xFFFF)
        # Windows reads these halves as signed shorts
        signed_lo = lo - 0x10000 if lo >= 0x8000 else lo
        signed_hi = hi - 0x10000 if hi >= 0x8000 else hi
        self.assertEqual((signed_lo, signed_hi), (-1920, -100))


class ButtonStateTests(unittest.TestCase):
    def test_press_release_pairing(self):
        state = ButtonState()
        self.assertTrue(state.press("left", 300))
        self.assertFalse(state.press("left", 300))  # double press refused
        self.assertTrue(state.is_pressed("left"))
        self.assertEqual(state.release("left"), 300)
        self.assertFalse(state.is_pressed("left"))

    def test_cleanup_releases_all_pressed(self):
        state = ButtonState()
        state.press("left", 300)
        state.press("right", 300)
        messages = state.cleanup_messages()
        self.assertEqual(len(messages), 2)
        ups = {msg for _leaf, msg, _w in messages}
        self.assertEqual(ups, {win32con.WM_LBUTTONUP, win32con.WM_RBUTTONUP})
        self.assertEqual(state.pressed_buttons(), {})


class CloudMessagePointerTests(unittest.TestCase):
    def _pointer(self):
        posts = []

        def poster(hwnd, message, wparam, lparam):
            posts.append((hwnd, message, wparam, lparam))
            return True

        return CloudMessagePointer(poster), posts

    def test_click_sequence_left(self):
        pointer, posts = self._pointer()
        snapshot = make_snapshot()
        result = pointer.click(snapshot, (960, 540), "left", down_time=0)
        self.assertEqual(result.status, CloudDispatchStatus.SENT)
        self.assertEqual(len(posts), 2)
        hwnd, down_msg, down_w, lparam = posts[0]
        self.assertEqual(
            (hwnd, down_msg, down_w), (300, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON)
        )
        self.assertEqual(lparam, pack_client_lparam(960, 540))
        hwnd, up_msg, up_w, up_lparam = posts[1]
        self.assertEqual((hwnd, up_msg, up_w), (300, win32con.WM_LBUTTONUP, 0))
        self.assertEqual(up_lparam, lparam)

    def test_right_click_uses_right_messages(self):
        pointer, posts = self._pointer()
        result = pointer.click(make_snapshot(), (10, 20), "right", down_time=0)
        self.assertTrue(result.ok)
        self.assertEqual(posts[0][1], win32con.WM_RBUTTONDOWN)
        self.assertEqual(posts[0][2], win32con.MK_RBUTTON)
        self.assertEqual(posts[1][1], win32con.WM_RBUTTONUP)

    def test_press_then_move_carries_button_flag(self):
        pointer, posts = self._pointer()
        snapshot = make_snapshot()
        pointer.press(snapshot, (100, 100), "left")
        pointer.move(snapshot, (200, 200), down_btn=win32con.MK_LBUTTON)
        self.assertEqual(posts[1][1], win32con.WM_MOUSEMOVE)
        self.assertEqual(posts[1][2], win32con.MK_LBUTTON)

    def test_release_targets_recorded_leaf(self):
        pointer, posts = self._pointer()
        snapshot = make_snapshot()
        pointer.press(snapshot, (5, 5), "left")
        result = pointer.release(snapshot, "left")
        self.assertTrue(result.ok)
        self.assertEqual(posts[-1][0], 300)
        self.assertEqual(posts[-1][1], win32con.WM_LBUTTONUP)

    def test_post_failure_reports_failed(self):
        def failing_poster(hwnd, message, wparam, lparam):
            return False

        pointer = CloudMessagePointer(failing_poster)
        result = pointer.click(make_snapshot(), (5, 5), "left", down_time=0)
        self.assertEqual(result.status, CloudDispatchStatus.FAILED)
        self.assertEqual(result.reason, CloudDispatchReason.POST_FAILED)
        self.assertFalse(pointer.buttons.is_pressed("left"))

    def test_cleanup_after_pressed(self):
        pointer, posts = self._pointer()
        pointer.press(make_snapshot(), (5, 5), "left")
        cleanup = pointer.cleanup()
        self.assertEqual(len(cleanup), 1)
        self.assertEqual(cleanup[0][1], win32con.WM_LBUTTONUP)
        self.assertEqual(pointer.buttons.pressed_buttons(), {})

    def test_wheel_uses_screen_coords(self):
        pointer, posts = self._pointer()
        result = pointer.wheel(make_snapshot(), (-100, -200), 3)
        self.assertTrue(result.ok)
        _hwnd, msg, wparam, lparam = posts[0]
        self.assertEqual(msg, win32con.WM_MOUSEWHEEL)
        lo, hi = lparam & 0xFFFF, (lparam >> 16) & 0xFFFF

        def signed(value):
            return value - 0x10000 if value >= 0x8000 else value

        self.assertEqual((signed(lo), signed(hi)), (-100, -200))
        self.assertEqual((wparam >> 16) & 0xFFFF, win32con.WHEEL_DELTA)

    def test_unknown_button_blocked(self):
        pointer, posts = self._pointer()
        result = pointer.click(make_snapshot(), (5, 5), "hyper", down_time=0)
        self.assertEqual(result.status, CloudDispatchStatus.BLOCKED)
        self.assertEqual(result.reason, CloudDispatchReason.UNSUPPORTED)
        self.assertEqual(posts, [])


if __name__ == "__main__":
    unittest.main()
