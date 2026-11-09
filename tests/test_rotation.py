from types import SimpleNamespace
import unittest
from unittest.mock import patch

import rotation
from rotation import Rotation, transforms


MONITORS = [
    {"name": "HDMI-A-1", "width": 2560, "height": 1440, "refreshRate": 60.0,
     "x": 1920, "y": 0, "scale": 1.0, "transform": 0},
    {"name": "eDP-1", "width": 1920, "height": 1200, "refreshRate": 120.003,
     "x": 0, "y": 0, "scale": 1.5, "transform": 0},
]

DEVICES = {"touch": [{"name": "elan9008:00-04f3:2c82", "address": "x"}], "tablets": []}


def result(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class SensorLineTests(unittest.TestCase):
    def test_accelerometer_and_compass_wordings_are_both_read(self):
        self.assertEqual(rotation.sensor_orientation("Accelerometer orientation changed: left-up"), "left-up")
        self.assertEqual(rotation.sensor_orientation("Compass orientation changed: bottom-up"), "bottom-up")

    def test_unrelated_lines_are_ignored(self):
        for line in ("Waiting for iio-sensor-proxy to appear",
                     "IIO sensor proxy service vanished.",
                     "",
                     None):
            self.assertIsNone(rotation.sensor_orientation(line))

    def test_permutation_parsing(self):
        self.assertEqual(transforms(None), (0, 1, 2, 3))
        self.assertEqual(transforms("3,0,1,2"), (3, 0, 1, 2))
        for bad in ("1,2,3", "0,0,1,2", "a,b,c,d"):
            with self.assertRaises(ValueError):
                transforms(bad)


class CommandBuildingTests(unittest.TestCase):
    def test_internal_panel_is_found_among_other_outputs(self):
        self.assertEqual(rotation.internal_monitor(MONITORS)["name"], "eDP-1")
        self.assertIsNone(rotation.internal_monitor(MONITORS[:1]))

    def test_mode_keeps_the_reported_refresh(self):
        self.assertEqual(rotation.monitor_mode(MONITORS[1]), "1920x1200@120.003")
        self.assertEqual(rotation.monitor_mode(MONITORS[0]), "2560x1440@60")

    def test_lua_keeps_mode_scale_and_position(self):
        lua = rotation.monitor_lua(MONITORS[1], 1)
        self.assertIn('output = "eDP-1"', lua)
        self.assertIn('mode = "1920x1200@120.003"', lua)
        self.assertIn("scale = 1.5", lua)
        self.assertIn("transform = 1", lua)

    def test_legacy_keyword_carries_scale_before_transform(self):
        self.assertEqual(rotation.monitor_keyword(MONITORS[1], 3),
                         "eDP-1,1920x1200@120.003,0x0,1.5,transform,3")

    def test_device_forms(self):
        self.assertEqual(rotation.device_lua("touch0", 2),
                         'hl.device({ name = "touch0", transform = 2 })')
        self.assertEqual(rotation.device_keyword("touch0", 2), "touch0:transform,2")


class ApplyTests(unittest.TestCase):
    def rotation(self):
        return Rotation(hyprctl="hyprctl", sensor="monitor-sensor")

    def test_lua_path_rotates_the_panel_and_every_touch_device(self):
        calls = []
        def fake_run(*args, **kwargs):
            calls.append(args)
            if args[1:3] == ("-j", "monitors"):
                return result(stdout=__import__("json").dumps(MONITORS))
            if args[1:3] == ("-j", "devices"):
                return result(stdout=__import__("json").dumps(DEVICES))
            return result(stdout="ok")
        with patch("rotation.run", side_effect=fake_run):
            self.rotation().apply(1)
        self.assertIn(("hyprctl", "eval", rotation.monitor_lua(MONITORS[1], 1)), calls)
        self.assertIn(("hyprctl", "eval", 'hl.device({ name = "elan9008:00-04f3:2c82", transform = 1 })'), calls)

    def test_legacy_compositor_falls_back_to_the_keyword_form(self):
        calls = []
        def fake_run(*args, **kwargs):
            calls.append(args)
            if args[1:3] == ("-j", "monitors"):
                return result(stdout=__import__("json").dumps(MONITORS))
            if args[1:3] == ("-j", "devices"):
                return result(stdout=__import__("json").dumps(DEVICES))
            if args[1] == "eval":
                return result(stderr="keyword can't work with non-legacy parsers. Use eval.")
            return result()
        with patch("rotation.run", side_effect=fake_run):
            self.rotation().apply(1)
        self.assertIn(("hyprctl", "keyword", "monitor",
                       "eDP-1,1920x1200@120.003,0x0,1.5,transform,1"), calls)
        self.assertIn(("hyprctl", "keyword", "device", "elan9008:00-04f3:2c82:transform,1"), calls)

    def test_orientation_is_mapped_through_the_configured_permutation(self):
        seen = []
        rotation_ = Rotation(permutation=(3, 0, 1, 2))
        with patch.object(rotation_, "apply", side_effect=lambda t: seen.append(t)):
            rotation_.apply_orientation("normal")
            rotation_.apply_orientation("left-up")
            self.assertIsNone(rotation_.apply_orientation("upside-down"))
        self.assertEqual(seen, [3, 0])

    def test_no_internal_display_is_a_named_refusal(self):
        with patch("rotation.run", return_value=result(stdout=__import__("json").dumps(MONITORS[:1]))):
            with self.assertRaisesRegex(RuntimeError, "internal display"):
                self.rotation().apply(1)

    def test_absent_sensor_is_a_clean_skip(self):
        rotation_ = Rotation(sensor="monitor-sensor")
        with patch("rotation.shutil.which", return_value=None):
            self.assertFalse(rotation_.available())
            self.assertFalse(rotation_.start())
            self.assertFalse(rotation_.running)
        self.assertIn("iio-sensor-proxy", rotation_.error)


if __name__ == "__main__":
    unittest.main()
