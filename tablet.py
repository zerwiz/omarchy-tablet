#!/usr/bin/env python3
"""Small session backend: hardware presence, preferences and the Wayland OSK.

No text, keystrokes, window contents or credentials are read or stored.
The QML service owns this process through stdin/stdout JSON messages.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import struct
import sys
import time
from keyboard_theme import KeyboardTheme, STYLES
from rotation import Rotation

DEFAULT_FAVORITES = ["chromium", "org.gnome.Nautilus", "com.github.xournalpp.xournalpp",
                     "libreoffice-writer", "YouTube", "org.gnome.Calculator", "murmure", "localsend"]
MODES = {"auto", "tablet", "desktop"}


def valid_command(value):
    return (isinstance(value, list) and bool(value)
            and all(isinstance(v, str) and v and "\0" not in v for v in value))


def wallpaper():
    path = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omarchy/current/background"
    return path.resolve().as_uri() if path.exists() else ""


def physical_keyboard_present(devices):
    """Use Linux input topology, not virtual-keyboard counts from Hyprland."""
    for block in devices.split("\n\n"):
        name = re.search(r'^N: Name="(.*)"$', block, re.M)
        path = re.search(r"^S: Sysfs=(.*)$", block, re.M)
        handlers = re.search(r"^H: Handlers=(.*)$", block, re.M)
        if not (name and path and handlers):
            continue
        if "kbd" not in handlers[1].split() or "/virtual/" in path[1]:
            continue
        label = name[1].lower()
        if any(word in label for word in ("virtual", "video bus", "buttons", "power button", "speaker", "consumer control", "system control")):
            continue
        keys = re.search(r"^B: KEY=(.*)$", block, re.M)
        if keys:
            bits = 0
            for word in keys[1].split():
                bits = (bits << (struct.calcsize("L") * 8)) | int(word, 16)
            if all(bits & (1 << code) for code in (28, 30, 44, 57)):
                return True
            continue
        if "keyboard" in label or "type cover" in label:
            return True
    return False


def tablet_mode(mode, attached):
    return mode == "tablet" or (mode == "auto" and not attached)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def run(*args, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=12)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:300])
    return result


class Backend:
    def __init__(self, state_dir=None):
        self.state_dir = Path(state_dir or Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omarchy-tablet")
        self.preferences_path = self.state_dir / "preferences.json"
        self.preferences = {"mode": "auto", "favorites": DEFAULT_FAVORITES.copy(), "layout": "single",
                            "dictationCommand": ["murmure", "--transcription"], "keyboardStyle": "omarchy", "keyboardActivation": "auto",
                            "rotation": "auto"}
        if self.preferences_path.exists():
            try:
                saved = json.loads(self.preferences_path.read_text())
            except (ValueError, OSError):
                saved = {}
            if not isinstance(saved, dict):
                saved = {}
            if isinstance(saved.get("layout"), str) and saved["layout"] in {"single", "tiling"}:
                self.preferences["layout"] = saved["layout"]
            if valid_command(saved.get("dictationCommand")):
                self.preferences["dictationCommand"] = saved["dictationCommand"]
            if isinstance(saved.get("mode"), str) and saved["mode"] in MODES:
                self.preferences["mode"] = saved["mode"]
            if isinstance(saved.get("keyboardStyle"), str) and saved["keyboardStyle"] in STYLES:
                self.preferences["keyboardStyle"] = saved["keyboardStyle"]
            if isinstance(saved.get("keyboardActivation"), str) and saved["keyboardActivation"] in {"auto", "manual"}:
                self.preferences["keyboardActivation"] = saved["keyboardActivation"]
            if isinstance(saved.get("rotation"), str) and saved["rotation"] in {"auto", "off"}:
                self.preferences["rotation"] = saved["rotation"]
            if isinstance(saved.get("favorites"), list):
                self.preferences["favorites"] = [s for s in saved["favorites"] if isinstance(s, str)]
        self.runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "omarchy-tablet"
        self.runtime.mkdir(mode=0o700, exist_ok=True)
        self.keyboard_theme = KeyboardTheme(self.runtime)
        self.keyboard_css_applied = None
        self.lease = self.runtime / "keyboard-lease.json"
        self.manual_keyboard = False
        self.keep_keyboard_open = False
        self.keep_keyboard_until = 0
        self.keyboard_started = False
        self.error = ""
        self.last_mode = None
        self.retry_after = 0
        self.running = True
        from layout import SingleApp
        self.layout = SingleApp(self.runtime / "window-lease.json")
        self.dictation_process = None
        self.rotation = Rotation()
        self.animations = True
        self.next_style_check = 0
        self.next_keyboard_check = 0
        self.layout_dirty = True
        self.last_focus_event = None
        self.dismiss_keyboard_on_focus = False

    def active(self, unit):
        return run("systemctl", "--user", "is-active", "--quiet", unit, check=False).returncode == 0

    def restore_keyboard(self):
        if not self.lease.exists():
            return
        lease = json.loads(self.lease.read_text())
        run("systemctl", "--user", "stop", "omarchy-tablet-keyboard.service", check=False)
        if "oskEnabled" in lease:
            run("gsettings", "set", "org.gnome.desktop.a11y.applications", "screen-keyboard-enabled", lease["oskEnabled"])
        if lease.get("fcitx"):
            run("systemctl", "--user", "start", "omarchy-fcitx5.service")
        self.lease.unlink(missing_ok=True)
        self.keyboard_started = False

    def start_keyboard(self):
        # Only yield the known Omarchy input-method service, never kill arbitrary IMEs.
        if self.active("omarchy-tablet-keyboard.service"):
            self.keyboard_started = True
            return
        css = self.keyboard_theme.prepare(self.preferences["keyboardStyle"])
        fcitx = self.active("omarchy-fcitx5.service")
        osk_enabled = run("gsettings", "get", "org.gnome.desktop.a11y.applications", "screen-keyboard-enabled").stdout.strip()
        if osk_enabled not in {"true", "false"}:
            raise RuntimeError("Cannot read the on-screen keyboard accessibility setting")
        atomic_json(self.lease, {"fcitx": fcitx, "oskEnabled": osk_enabled})
        try:
            run("gsettings", "set", "org.gnome.desktop.a11y.applications", "screen-keyboard-enabled", "true")
            if fcitx:
                run("systemctl", "--user", "stop", "omarchy-fcitx5.service")
            run("systemd-run", "--user", "--collect", "--unit=omarchy-tablet-keyboard",
                "--property=PartOf=graphical-session.target", "--", "/usr/bin/env",
                *self.keyboard_theme.environment(), shutil.which("squeekboard"))
            self.keyboard_css_applied = css
            self.keyboard_started = True
        except Exception:
            self.restore_keyboard()
            raise

    def visible(self):
        result = run("busctl", "--user", "get-property", "sm.puri.OSK0", "/sm/puri/OSK0",
                     "sm.puri.OSK0", "Visible", check=False)
        return result.returncode == 0 and result.stdout.strip() == "b true"

    def show_keyboard(self, visible):
        run("busctl", "--user", "call", "sm.puri.OSK0", "/sm/puri/OSK0",
            "sm.puri.OSK0", "SetVisible", "b", "true" if visible else "false")

    def automatic_keyboard(self, tablet=None):
        if tablet is None:
            tablet = self.state()["tablet"]
        return tablet and self.preferences["keyboardActivation"] == "auto"

    def hide_keyboard(self):
        self.manual_keyboard = False
        self.keep_keyboard_open = False
        if self.keyboard_started:
            try:
                self.show_keyboard(False)
            finally:
                # Automatic mode keeps the input method listening for the next
                # text-input activation; manual mode stays stopped until a tap.
                if not self.automatic_keyboard():
                    self.restore_keyboard()

    def _effective_layout(self, tablet):
        """Layout follows the mode: tablets use Single app, desktop uses tiling.

        No manual Single-app/Tiling switch; switching Desktop ⇄ Tablet swaps it.
        """
        return "single" if tablet else "tiling"

    def state(self):
        attached = physical_keyboard_present(Path("/proc/bus/input/devices").read_text())
        tablet = tablet_mode(self.preferences["mode"], attached)
        state = dict(self.preferences, attached=attached, tablet=tablet)
        state["layout"] = self._effective_layout(tablet)
        state.update(
            keyboardAvailable=bool(shutil.which("squeekboard")),
            rotationAvailable=self.rotation.available(), rotationActive=self.rotation.running,
            rotationError=self.rotation.error,
            keyboardRunning=self.keyboard_started, error=self.error,
            dictationCommandText=shlex.join(self.preferences["dictationCommand"]),
            dictationAvailable=bool(shutil.which(self.preferences["dictationCommand"][0])),
            animations=self.animations,
            wallpaper=wallpaper(),
        )
        return state

    def command(self, data):
        if not isinstance(data, dict):
            raise ValueError("Expected a command object")
        action, value = data.get("action"), data.get("value")
        if action == "mode":
            if value == "toggle":
                value = "desktop" if self.state()["tablet"] else "tablet"
            if not isinstance(value, str) or value not in MODES:
                raise ValueError("Unknown mode")
            self.preferences["mode"] = value
            self.manual_keyboard = False
            self.keep_keyboard_open = False
        elif action == "layout":
            if not isinstance(value, str) or value not in {"single", "tiling"}:
                raise ValueError("Unknown layout")
            # Kept for older IPC clients: layout follows the selected mode.
            self.preferences["mode"] = "tablet" if value == "single" else "desktop"
            self.preferences["layout"] = value
        elif action == "keyboardActivation":
            if not isinstance(value, str) or value not in {"auto", "manual"}:
                raise ValueError("Unknown keyboard activation")
            self.preferences["keyboardActivation"] = value
            self.hide_keyboard()
        elif action == "keyboardStyle":
            if not isinstance(value, str) or value not in STYLES:
                raise ValueError("Unknown keyboard style")
            self.preferences["keyboardStyle"] = value
        elif action == "dictationCommand":
            argv = shlex.split(value) if isinstance(value, str) else value
            if not valid_command(argv):
                raise ValueError("Enter a command and optional arguments")
            self.preferences["dictationCommand"] = argv
        elif action == "dictation":
            if self.dictation_process and self.dictation_process.poll() is None:
                return
            # Dictation may retain an explicitly opened keyboard, but must not
            # reopen one the user dismissed.
            if self.keyboard_started and (self.manual_keyboard or self.visible()):
                self.keep_keyboard_open = True
                # Cover the speech app's startup focus transition, then give
                # visibility back to Wayland text-input (including auto-hide).
                self.keep_keyboard_until = time.monotonic() + 2
            argv = list(self.preferences["dictationCommand"])
            if Path(argv[0]).name == "murmure" and "--hidden" not in argv:
                argv.append("--hidden")
            self.dictation_process = subprocess.Popen(argv,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif action == "favorite":
            if not isinstance(value, str) or not value or len(value) > 256:
                raise ValueError("Invalid application ID")
            favorites = self.preferences["favorites"]
            favorites.remove(value) if value in favorites else favorites.append(value)
        elif action == "hideKeyboard":
            self.hide_keyboard()
        elif action == "keyboard":
            if not shutil.which("squeekboard"):
                raise RuntimeError("Keyboard unavailable: install squeekboard.")
            if self.keyboard_started and self.visible():
                self.hide_keyboard()
            else:
                self.manual_keyboard = True
                self.start_keyboard()
                # D-Bus registration happens just after process startup.
                for attempt in range(20):
                    try:
                        self.show_keyboard(True)
                        break
                    except RuntimeError:
                        if attempt == 19:
                            raise
                        time.sleep(0.1)
        elif action == "rotate":
            self.rotation.cycle()
        elif action == "rotation":
            if not isinstance(value, str) or value not in {"auto", "off"}:
                raise ValueError("Unknown screen rotation mode")
            self.preferences["rotation"] = value
            if value == "off":
                self.rotation.stop()
        else:
            raise ValueError("Unknown command")
        if action in {"mode", "layout", "dictationCommand", "favorite", "keyboardStyle", "keyboardActivation", "rotation"}:
            atomic_json(self.preferences_path, self.preferences)
        if action in {"mode", "layout"}:
            self.layout_dirty = True
        self.error = ""

    def reconcile(self):
        state = self.state()
        if self.last_mode is not None and state["tablet"] != self.last_mode:
            self.manual_keyboard = False
            self.keep_keyboard_open = False
            self.layout_dirty = True
        self.last_mode = state["tablet"]
        wanted = state["keyboardAvailable"] and (self.manual_keyboard or self.automatic_keyboard(state["tablet"]))
        if wanted and not self.keyboard_started and time.monotonic() >= self.retry_after:
            try:
                self.start_keyboard()
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                self.error = str(exc)
                self.retry_after = time.monotonic() + 30
        elif not wanted and self.keyboard_started:
            self.restore_keyboard()
        if self.keyboard_started and time.monotonic() >= self.next_keyboard_check:
            self.next_keyboard_check = time.monotonic() + 5
            if not self.active("omarchy-tablet-keyboard.service"):
                self.restore_keyboard()
                self.error = "Keyboard stopped. Check journalctl --user -u omarchy-tablet-keyboard."
                self.retry_after = time.monotonic() + 30
        if self.dismiss_keyboard_on_focus:
            if self.keyboard_started and self.automatic_keyboard(state["tablet"]) and not self.keep_keyboard_open:
                # A new window may inherit the previous client's OSK visibility.
                # Dismiss it once; subsequent field activations remain automatic.
                self.show_keyboard(False)
                self.manual_keyboard = False
            self.dismiss_keyboard_on_focus = False
        if self.keep_keyboard_open and time.monotonic() >= self.keep_keyboard_until:
            self.keep_keyboard_open = False
        if self.keyboard_started and self.keep_keyboard_open and not self.visible():
            self.show_keyboard(True)
        if self.keyboard_started:
            css = self.keyboard_theme.prepare(self.preferences["keyboardStyle"])
            # Reload only while hidden: never interrupt an in-progress touch or
            # composing sequence. Keep the input-method recovery lease intact.
            if css != self.keyboard_css_applied and not self.visible():
                run("systemctl", "--user", "restart", "omarchy-tablet-keyboard.service")
                self.keyboard_css_applied = css
        # Rotation belongs to tablet mode: a rotating desktop display would
        # fight the physical lid, so it stays still there.
        if state["tablet"] and self.preferences["rotation"] == "auto":
            self.rotation.start()
        elif self.rotation.running:
            self.rotation.stop()
        if self.layout_dirty:
            # Keep dirty on failure: a failed dispatch must be retried.
            self.layout.reconcile(self._effective_layout(state["tablet"]) == "single")
            self.layout_dirty = False
        if time.monotonic() >= self.next_style_check:
            self.next_style_check = time.monotonic() + 30
            result = run("hyprctl", "-j", "getoption", "animations:enabled", check=False)
            if result.returncode == 0:
                self.animations = bool(json.loads(result.stdout).get("int", 1))
        if self.dictation_process and self.dictation_process.poll() is not None:
            if self.dictation_process.returncode:
                self.error = "Dictation command failed. Check your dictation application."
            self.dictation_process = None
        return self.state()

    def stop(self, *_):
        self.running = False

    def layout_event(self, line):
        """Ignore title churn: Hyprland repeats activewindowv2 for the same ID."""
        name, _, data = line.partition(b">>")
        if name == b"activewindowv2":
            if data == self.last_focus_event:
                return False
            self.last_focus_event = data
            self.dismiss_keyboard_on_focus = True
            return True
        if name == b"configreloaded":
            self.next_style_check = 0
        return name in {b"openwindow", b"closewindow", b"movewindow", b"movewindowv2",
                        b"changefloatingmode", b"fullscreen", b"workspace", b"workspacev2",
                        b"focusedmon", b"monitoradded", b"monitorremoved", b"configreloaded",
                        b"togglegroup", b"pin"}

    def daemon(self):
        lock = (self.runtime / "backend.lock").open("w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        previous = ""
        buffer = b""
        events = None
        event_buffer = b""
        next_connect = 0
        next_maintenance = 0
        layout_due = 0
        try:
            self.layout.restore()
            self.restore_keyboard()  # recover after a previous shell crash
            while self.running:
                now = time.monotonic()
                if events is None and now >= next_connect:
                    next_connect = now + 5
                    candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    try:
                        candidate.connect(str(self.runtime.parent / "hypr" /
                            os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "") / ".socket2.sock"))
                        candidate.setblocking(False)
                        events = candidate
                        event_buffer = b""
                        self.last_focus_event = None
                        self.layout_dirty = True
                    except OSError:
                        candidate.close()
                if now >= next_maintenance or (self.layout_dirty and now >= layout_due):
                    try:
                        # Slow fallback keeps hardware detection and recovery alive
                        # even if the compositor event socket is unavailable.
                        if events is None:
                            self.layout_dirty = True
                        state = json.dumps(self.reconcile(), ensure_ascii=False)
                        if state != previous:
                            print(state, flush=True)
                            previous = state
                    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                        self.error = str(exc)
                        print(json.dumps(self.state(), ensure_ascii=False), flush=True)
                        previous = ""
                        layout_due = time.monotonic() + 2
                    next_maintenance = time.monotonic() + (0.5 if self.keep_keyboard_open else 2)
                timeout = max(0, next_maintenance - time.monotonic())
                if self.layout_dirty:
                    timeout = min(timeout, max(0, layout_due - time.monotonic()))
                readable, _, _ = select.select([sys.stdin] + ([events] if events else []), [], [], timeout)
                if events and events in readable:
                    try:
                        chunk = events.recv(65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        events.close()
                        events = None
                    else:
                        event_buffer += chunk
                        while b"\n" in event_buffer:
                            line, event_buffer = event_buffer.split(b"\n", 1)
                            if self.layout_event(line):
                                if not self.layout_dirty:
                                    layout_due = time.monotonic() + .04
                                self.layout_dirty = True
                if sys.stdin in readable:
                    chunk = os.read(sys.stdin.fileno(), 65536)
                    if not chunk:
                        break
                    buffer += chunk
                    if len(buffer) > 1024 * 1024:
                        buffer = b""
                        self.error = "Command too large"
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        try:
                            self.command(json.loads(line))
                        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                            self.error = str(exc)
                    next_maintenance = 0
                    layout_due = 0
        finally:
            if events:
                events.close()
            try:
                self.layout.restore()
            finally:
                self.rotation.stop()
                self.restore_keyboard()
                lock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["daemon", "status"])
    args = parser.parse_args()
    if args.action == "daemon":
        Backend().daemon()
    else:
        print(json.dumps(Backend().state(), ensure_ascii=False, indent=2))
