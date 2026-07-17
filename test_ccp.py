import tempfile
import unittest
from pathlib import Path

from ccp import GestureProfileStore, RotaryControlPanel, TouchEventRecorder


class GestureProfileStoreTests(unittest.TestCase):
    def test_round_trip_profile(self) -> None:
        profile = {
            "name": "Morning route",
            "screen_width": 1920,
            "screen_height": 720,
            "gestures": [
                {"type": "tap", "offset_ms": 125, "duration_ms": 70, "x": 500, "y": 300},
                {
                    "type": "swipe",
                    "offset_ms": 750,
                    "duration_ms": 420,
                    "x1": 800,
                    "y1": 500,
                    "x2": 300,
                    "y2": 500,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            store = GestureProfileStore(path)
            clean = store.validate_profile(profile)
            store.profiles["Morning route"] = clean
            store.save()

            restored = GestureProfileStore(path)
            self.assertEqual(restored.load(), [])
            self.assertEqual(restored.profiles["Morning route"]["gestures"], clean["gestures"])

    def test_rejects_coordinate_outside_screen(self) -> None:
        profile = {
            "name": "Unsafe",
            "screen_width": 100,
            "screen_height": 50,
            "gestures": [
                {"type": "tap", "offset_ms": 0, "duration_ms": 50, "x": 100, "y": 10},
            ],
        }
        with self.assertRaises(ValueError):
            GestureProfileStore.validate_profile(profile)


class GestureCommandTests(unittest.TestCase):
    def test_swipe_command_uses_recorded_duration(self) -> None:
        command = RotaryControlPanel._gesture_adb_command(
            ["adb", "-s", "device"],
            {
                "type": "swipe",
                "offset_ms": 100,
                "duration_ms": 375,
                "x1": 10,
                "y1": 20,
                "x2": 30,
                "y2": 40,
            },
        )
        self.assertEqual(
            command,
            ["adb", "-s", "device", "shell", "input", "touchscreen", "swipe", "10", "20", "30", "40", "375"],
        )

    def test_zoom_command_starts_two_swipes(self) -> None:
        command = RotaryControlPanel._gesture_adb_command(
            ["adb"],
            {
                "type": "zoom_in",
                "offset_ms": 0,
                "duration_ms": 500,
                "p1x1": 45,
                "p1y1": 50,
                "p1x2": 20,
                "p1y2": 50,
                "p2x1": 55,
                "p2y1": 50,
                "p2x2": 80,
                "p2y2": 50,
            },
        )
        self.assertEqual(command[:4], ["adb", "shell", "sh", "-c"])
        self.assertIn(" & ", command[-1])
        self.assertTrue(command[-1].endswith("& wait"))


class TouchEventRecorderTests(unittest.TestCase):
    CAPABILITIES = """add device 1: /dev/input/event2
  name:     \"gpio-keys\"
add device 2: /dev/input/event5
  name:     \"AAOS touchscreen\"
  events:
    KEY (0001): BTN_TOUCH
    ABS (0003):
      ABS_MT_POSITION_X  : value 0, min 0, max 1000, fuzz 0
      ABS_MT_POSITION_Y  : value 0, min 0, max 500, fuzz 0
"""

    def test_finds_multitouch_device_and_ranges(self) -> None:
        path, x_range, y_range = RotaryControlPanel._parse_touch_device(self.CAPABILITIES)
        self.assertEqual(path, "/dev/input/event5")
        self.assertEqual(x_range, (0, 1000))
        self.assertEqual(y_range, (0, 500))

    @staticmethod
    def _touch(recorder: TouchEventRecorder, started: float, x: int, y: int, ended: float) -> None:
        recorder.feed("[ 1.0] EV_ABS ABS_MT_TRACKING_ID 00000001", started)
        recorder.feed(f"[ 1.0] EV_ABS ABS_MT_POSITION_X {x:08x}", started)
        recorder.feed(f"[ 1.0] EV_ABS ABS_MT_POSITION_Y {y:08x}", started)
        recorder.feed("[ 1.0] EV_SYN SYN_REPORT 00000000", started)
        recorder.feed("[ 1.1] EV_ABS ABS_MT_TRACKING_ID ffffffff", ended)
        recorder.feed("[ 1.1] EV_SYN SYN_REPORT 00000000", ended)

    def test_two_nearby_taps_become_double_tap(self) -> None:
        recorder = TouchEventRecorder((1001, 501), (0, 1000), (0, 500), 0, 10.0)
        self._touch(recorder, 10.1, 200, 100, 10.18)
        self._touch(recorder, 10.35, 205, 102, 10.43)
        gestures = recorder.finish(10.5)
        self.assertEqual(len(gestures), 1)
        self.assertEqual(gestures[0]["type"], "double_tap")
        self.assertEqual(gestures[0]["gap_ms"], 250)

    def test_drag_becomes_swipe_with_recorded_timing(self) -> None:
        recorder = TouchEventRecorder((1001, 501), (0, 1000), (0, 500), 0, 20.0)
        recorder.feed("[ 2.0] EV_ABS ABS_MT_TRACKING_ID 00000001", 20.2)
        recorder.feed("[ 2.0] EV_ABS ABS_MT_POSITION_X 00000064", 20.2)
        recorder.feed("[ 2.0] EV_ABS ABS_MT_POSITION_Y 00000064", 20.2)
        recorder.feed("[ 2.0] EV_SYN SYN_REPORT 00000000", 20.2)
        recorder.feed("[ 2.3] EV_ABS ABS_MT_POSITION_X 00000384", 20.5)
        recorder.feed("[ 2.3] EV_SYN SYN_REPORT 00000000", 20.5)
        recorder.feed("[ 2.4] EV_ABS ABS_MT_TRACKING_ID ffffffff", 20.6)
        recorder.feed("[ 2.4] EV_SYN SYN_REPORT 00000000", 20.6)
        gesture = recorder.finish()[0]
        self.assertEqual(gesture["type"], "swipe")
        self.assertEqual(gesture["offset_ms"], 200)
        self.assertEqual(gesture["duration_ms"], 400)
        self.assertEqual((gesture["x1"], gesture["x2"]), (100, 900))


if __name__ == "__main__":
    unittest.main()
