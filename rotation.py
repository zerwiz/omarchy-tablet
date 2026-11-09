#!/usr/bin/env python3
"""Automatic display rotation for the internal panel.

iio-sensor-proxy publishes the accelerometer orientation over D-Bus; its
``monitor-sensor`` helper prints every change as one line. This module reads
that stream and asks Hyprland to rotate the internal output and its touch
devices to match.

Only the current orientation string is used. Nothing is stored, and no motion,
position or device identity is kept. The compositor owns the display; this
module never guesses a mode, a scale or a position — it reads them first.

Hyprland 0.55 replaced the legacy keyword parser with a Lua one, so
``hyprctl keyword`` is refused on current releases. The Lua call is tried
first and the legacy keyword is kept as the fallback for older compositors.
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

INTERNAL = re.compile(r"^(eDP|DSI|LVDS)")
ORIENTATIONS = {0: "normal", 1: "left-up", 2: "bottom-up", 3: "right-up"}
BY_NAME = {name: index for index, name in ORIENTATIONS.items()}
DEFAULT_TRANSFORMS = (0, 1, 2, 3)
SENSOR = "monitor-sensor"
SENSOR_EVENT = re.compile(r"orientation changed:\s*([a-z-]+)", re.IGNORECASE)


def run(*args, check=True, timeout=12):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:300])
    return result


def transforms(text):
    """Parse the accelerometer-to-transform permutation, e.g. "3,0,1,2"."""
    if text is None:
        return DEFAULT_TRANSFORMS
    parts = [part.strip() for part in str(text).split(",")]
    if len(parts) != 4:
        raise ValueError("Expected four comma-separated transforms")
    values = tuple(int(part) for part in parts)
    if sorted(values) != [0, 1, 2, 3]:
        raise ValueError("Transforms must be a permutation of 0,1,2,3")
    return values


def sensor_orientation(line):
    """Orientation named by one monitor-sensor line, or None."""
    match = SENSOR_EVENT.search(line or "")
    return match.group(1).lower() if match else None


def internal_monitor(monitors):
    """The built-in panel, whatever this machine calls it."""
    return next((m for m in monitors if INTERNAL.match(m.get("name", ""))), None)


def monitor_mode(monitor):
    return "{}x{}@{:g}".format(monitor["width"], monitor["height"],
                               round(float(monitor["refreshRate"]), 3))


def monitor_lua(monitor, transform):
    return ('hl.monitor({{ output = "{name}", mode = "{mode}", position = "{x}x{y}", '
            'scale = {scale:g}, transform = {transform} }})').format(
                name=monitor["name"], mode=monitor_mode(monitor), x=monitor["x"],
                y=monitor["y"], scale=float(monitor["scale"]), transform=transform)


def monitor_keyword(monitor, transform):
    return "{},{},{}x{},{:g},transform,{}".format(
        monitor["name"], monitor_mode(monitor), monitor["x"], monitor["y"],
        float(monitor["scale"]), transform)


def device_lua(name, transform):
    return 'hl.device({{ name = "{name}", transform = {transform} }})'.format(
        name=name, transform=transform)


def device_keyword(name, transform):
    return "{}:transform,{}".format(name, transform)


def apply_hyprctl(hyprctl, lua, keyword_args):
    """Lua first, legacy keyword as the fallback. Returns the working form."""
    result = run(hyprctl, "eval", lua, check=False)
    output = (result.stdout + result.stderr).lower()
    if result.returncode == 0 and "can't" not in output and "unknown" not in output:
        return "eval"
    run(hyprctl, "keyword", *keyword_args)
    return "keyword"


class Rotation:
    """Keep the internal panel and its touch devices square with gravity."""

    def __init__(self, permutation=DEFAULT_TRANSFORMS, hyprctl="hyprctl", sensor=SENSOR,
                 on_change=None):
        self.permutation = permutation
        self.hyprctl = hyprctl
        self.sensor = sensor
        self.on_change = on_change
        self.process = None
        self.thread = None
        self.error = ""
        self.transform = None
        self.stopping = False

    def available(self):
        return bool(shutil.which(self.sensor))

    def monitors(self):
        return json.loads(run(self.hyprctl, "-j", "monitors").stdout)

    def touch_devices(self):
        try:
            devices = json.loads(run(self.hyprctl, "-j", "devices").stdout)
        except (ValueError, RuntimeError, subprocess.SubprocessError):
            return []
        return [device["name"] for device in devices.get("touch", []) if device.get("name")]

    def panel(self):
        monitor = internal_monitor(self.monitors())
        if monitor is None:
            raise RuntimeError("No internal display found; rotation needs eDP, DSI or LVDS.")
        return monitor

    def apply(self, transform):
        """Rotate the internal panel and every touch device to one transform."""
        monitor = self.panel()
        apply_hyprctl(self.hyprctl, monitor_lua(monitor, transform),
                      ("monitor", monitor_keyword(monitor, transform)))
        for name in self.touch_devices():
            # hl.device only exists on the Lua parser; older compositors take
            # device:name:transform as a keyword.
            result = run(self.hyprctl, "eval", device_lua(name, transform), check=False)
            if result.returncode or "can't" in (result.stdout + result.stderr).lower():
                run(self.hyprctl, "keyword", "device", device_keyword(name, transform),
                    check=False)
        self.transform = transform
        if self.on_change:
            self.on_change(transform)
        return transform

    def apply_orientation(self, orientation):
        index = BY_NAME.get(orientation)
        if index is None:
            return None
        return self.apply(self.permutation[index])

    def cycle(self):
        current = self.current()
        return self.apply((current + 1) % 4)

    def current(self):
        monitor = self.panel()
        return int(monitor.get("transform", 0))

    def watch(self, retry=30):
        """Follow monitor-sensor until stop() is called."""
        self.stopping = False
        while not self.stopping:
            if not self.available():
                self.error = "Screen rotation needs iio-sensor-proxy (monitor-sensor)."
                return
            try:
                self.process = subprocess.Popen([shutil.which(self.sensor)],
                                                stdout=subprocess.PIPE,
                                                stderr=subprocess.DEVNULL, text=True)
            except (OSError, ValueError):
                self.error = "Cannot start monitor-sensor."
                return
            try:
                for line in self.process.stdout:
                    orientation = sensor_orientation(line)
                    if orientation and not self.stopping:
                        try:
                            self.apply_orientation(orientation)
                        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                            self.error = str(exc)
            finally:
                self.process.terminate()
                self.process = None
            if self.stopping:
                return
            time.sleep(retry)

    def start(self):
        """Idempotent: one reader thread, clean skip when the sensor is absent."""
        if self.thread and self.thread.is_alive():
            return True
        if not self.available():
            self.error = "Screen rotation needs iio-sensor-proxy (monitor-sensor)."
            return False
        self.error = ""
        self.thread = threading.Thread(target=self.watch, name="tablet-rotation", daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.stopping = True
        process, self.process = self.process, None
        if process:
            try:
                process.terminate()
            except OSError:
                pass
        self.thread = None

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "rotate", "set", "watch"], nargs="?",
                        default="status")
    parser.add_argument("--transform", help="permutation, e.g. 3,0,1,2")
    parser.add_argument("--value", type=int, choices=[0, 1, 2, 3], help="transform to set")
    args = parser.parse_args(argv)
    rotation = Rotation(permutation=transforms(args.transform))
    if args.action == "status":
        panel = internal_monitor(rotation.monitors())
        print(json.dumps({"available": rotation.available(),
                          "monitor": panel and panel["name"],
                          "transform": panel and panel.get("transform")}, indent=2))
        return 0
    if args.action == "rotate":
        print(rotation.cycle())
        return 0
    if args.action == "set":
        if args.value is None:
            parser.error("set needs --value")
        print(rotation.apply(args.value))
        return 0
    rotation.watch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
