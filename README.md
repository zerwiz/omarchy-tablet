# Omarchy Tablet

**Turn Omarchy into a touch-friendly tablet interface, and return to the desktop with one tap.**

Omarchy Tablet is a native Quickshell plugin for tablets and detachable computers running Omarchy. It adds a touch launcher, favorite apps, window switching, an on-screen keyboard and dictation shortcuts while keeping Omarchy's themes and native status widgets.

Developed on a **Microsoft Surface Pro 4 with the linux-surface kernel**. Other tablets and convertibles may work when they can run a compatible Omarchy installation and their hardware is supported by Linux; they have not yet been validated.

[Présentation française](README.fr.md) · [Release review](docs/release-readiness.md) · [Publishing guide](docs/marketplace.md)

![Omarchy Tablet Home on a Surface Pro 4](preview.png)

## Tablet, Desktop and Automatic

| Mode | What happens |
| --- | --- |
| **Tablet** | Touch controls appear in the top bar. **Single app** maximizes the active eligible window on the internal display, keeping navigation accessible. |
| **Desktop** | The plugin restores the window states it changed and returns to Omarchy's normal Hyprland tiling. Native widgets and the mode button remain. |
| **Automatic** | A detected physical keyboard selects Desktop; removing it selects Tablet. Virtual keyboards are excluded from detection. |

**Hyprland runs in both modes.** Single app is a window arrangement, not a separate desktop session or a kiosk: several applications can remain open. Tap the leftmost monitor/tablet icon to switch manually. This selects an explicit mode; choose **Settings → Mode → Automatic** to resume keyboard-based switching.

The plugin replaces the whole bar with one top row. Desktop mode is not an exact copy of every stock bar setting: the top placement and navigation button remain. External displays retain their window arrangement, although they also receive the replacement bar.

## Features

- **Home and applications:** favorites, search and a touch grid. Hold an app or use **All apps → Edit** to manage favorites.
- **Window switcher:** tap Windows, or swipe up/hold the bottom grip. Cards show icons and titles; tap to focus or use ✕ to close. Tap the grip for Home.
- **One top bar:** native widgets keep their actions and popups. Workspace numbers hide on the tablet display in Tablet mode and return in Desktop. Swipe the system widget area horizontally on narrow displays.
- **On-screen keyboard:** Squeekboard opens automatically in compatible Wayland text fields in Tablet mode, or only on request with **Button only**. Choose Omarchy, Rounded or High contrast appearance; all follow the active theme. Style changes apply after hiding and reopening the keyboard.
- **Dictation shortcuts:** microphone buttons in the bar and above the keyboard invoke Murmure, or your configured command, without requesting keyboard focus. Speech recognition is provided by that application.
- **Omarchy integration:** live colors and typography, clipboard/emoji/menu shortcuts and an on-demand opaque Home surface. The wallpaper remains unchanged; window cards use no live thumbnails.

## Compatibility and dependencies

Reference machine inspected for this release: **Surface Pro 4**, **Omarchy 4.0.4-1**, **Hyprland 0.56.2-2** (Lua API), **Quickshell 0.3.1-1**, **linux-surface 6.19.8-arch1-3-surface**, display scale 2×. These are observed versions, not a promise that every earlier or later release works.

This requires the **Quickshell-based Omarchy shell**, its `qs.Commons`/`qs.Ui` components, app library and widget registry. It is not compatible with the older Waybar setup.

| Dependency | Purpose |
| --- | --- |
| Python **3.11+** | Backend, installer and `tomllib` theme parsing; no pip packages required |
| `python-gobject` | D-Bus keyboard visibility watcher |
| `hyprctl`, `omarchy-shell`, Quickshell | Window management and shell integration |
| systemd user session, `systemctl`, `systemd-run`, `busctl` | Owned keyboard service and D-Bus control |
| `gsettings` (`glib2`), desktop schemas | Keyboard accessibility and input-source settings |
| `gtk-launch` (`gtk3`), `uwsm-app` (`uwsm`) | Launch installed desktop applications |
| `squeekboard` (`extra`) | Optional for the shell UI; required for the on-screen keyboard. Without it the bar button stays disabled. |
| `iio-sensor-proxy` (`extra`) | Optional; required for automatic screen rotation |
| Murmure or another dictation executable | Optional; install and configure separately |

The plugin installs no packages or hardware drivers. Touchscreen, stylus, rotation and suspend support depend on the device's Linux setup. The linux-surface kernel is the development machine's configuration, not a universal requirement.

## Screen rotation

Rotation needs an accelerometer that Linux already exposes and `iio-sensor-proxy`, which publishes the orientation over D-Bus. Install it and the automatic mode follows the device in Tablet mode only:

```sh
sudo pacman -S iio-sensor-proxy
```

Two behaviours are deliberate. Automatic rotation is bound to **Tablet mode**, because a desktop display that turns while the lid is open fights the keyboard and the user. And the panel keeps the mode, scale and position it already had: the backend reads them from the compositor before it asks for a new transform, so a docked external monitor and a scaled panel are left untouched.

Hyprland 0.55 replaced the legacy keyword parser with a Lua one, so `hyprctl keyword` is refused on current releases. This backend calls the Lua API first and keeps the keyword form as the fallback for older compositors. The pointer and the touch devices are rotated with the panel.

Without the sensor nothing is broken: the **Rotate** button turns the internal panel by hand, and the settings page names the missing dependency instead of failing silently. A device whose accelerometer axes do not match its panel can be corrected with `rotation.py --transform 3,0,1,2`, the same permutation iio-hyprland exposes.

## Install from GitHub

Install this public repository with Omarchy's standard installer:

```sh
omarchy plugin add https://github.com/varlet99/omarchy-tablet.git --enable
```

Review the source and dependencies before enabling. Enabling selects the replacement bar and may immediately enter Tablet mode if no physical keyboard is detected. Note the name of your previous bar if you use a custom one.

Update and temporarily disable:

```sh
omarchy plugin update surface.tablet
omarchy plugin disable surface.tablet
# Enable again:
omarchy plugin enable surface.tablet
```

Disabling returns to the stock Omarchy bar and unloads the tablet service. To return to another custom bar, enable that bar's plugin ID. Standard installation does not use `install.py` or create its previous-bar backup. If the shell retains an old component after updating, run `omarchy restart shell`.

Remove the plugin:

```sh
omarchy plugin remove surface.tablet
```

This unloads it and removes its installation (or backs up a non-git installation). Preferences remain in `${XDG_STATE_HOME:-$HOME/.local/state}/omarchy-tablet` for reuse. If you used the optional keyboard-language helper, run `python3 scripts/keyboard_language.py --restore` **from your clone before removing it** to restore its saved input sources.

The standard Omarchy commands use `~/.config/omarchy/plugins` in the inspected version. The alternate installer below honors `XDG_CONFIG_HOME`.

## Local development installation

From a separate clone outside the watched plugin directory:

```sh
python3 install.py
```

This explicitly selects the top tablet bar, preserving configured widgets and saving the previous bar selection. It copies a content-versioned release to `${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/surface.tablet` to avoid stale QML components during development. Run it again after changing or pulling source. No root privileges are required.

Restore the prior bar before switching to a standard GitHub installation:

```sh
python3 install.py --restore
omarchy plugin remove surface.tablet
```

Restoration changes only the bar ID and position, preserving later widget and unrelated setting edits. With no usable previous-bar backup it falls back to the stock top bar. You can also restore using the installed entry point:

```sh
python3 "${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/surface.tablet/install.py" --restore
```

Do not mix update methods: the development installer refuses to overwrite a git-managed marketplace checkout. It does not edit Hyprland configuration, keybindings, rotation services or packaged Omarchy files.

## Keyboard and dictation behavior

The interface is English. Keyboard language is independent; the development Surface uses French Canadian. The optional helper changes GNOME input sources explicitly and saves a backup:

```sh
python3 scripts/keyboard_language.py ca
python3 scripts/keyboard_language.py --restore
```

Squeekboard may fall back to a US terminal layout where a Canadian terminal variant is missing. Apps launched from Home receive `GTK_IM_MODULE=wayland` and `QT_IM_MODULE=wayland`; existing apps may need restarting. Automatic activation depends on the app's Wayland text-input support. The manual button can show the keyboard, but does not guarantee input compatibility with every application.

Automatic activation keeps the keyboard service listening after Hide; a subsequent text-input activation can reopen it. **Button only** stops the service when hidden. Desktop uses manual activation. The keyboard toolbar provides microphone and Hide controls.

While the keyboard is owned by this plugin, the backend temporarily stops the known `omarchy-fcitx5.service` and enables the accessibility keyboard setting, then restores their previous states on cleanup. Avoid running another input method concurrently. Theme CSS is scoped to the Squeekboard process through a GLib resource overlay; no global GTK CSS or system keyboard layouts are changed.

The default command is `murmure --transcription`; the plugin adds `--hidden` for Murmure. Other commands are parsed into arguments without shell evaluation. Use a wrapper executable for pipelines. Dictation does not start a dismissed keyboard, does not alter F9 and does not provide a recording indicator. Hiding the keyboard does not stop recording in the external app. Transcription storage, microphone access and any network use are controlled by that app.

## Window handling and stored data

Single app changes fullscreen/maximized states on the internal `eDP`, `DSI` or `LVDS` display. Floating dialogs, pinned/grouped windows and special workspaces are excluded; existing application fullscreen requests are respected. Without a matching internal display, window maximization is skipped. The launcher falls back to the first screen.

Desktop restores states recorded by the plugin. It cannot reconstruct a tiling tree you rearranged yourself. A session-scoped recovery journal supports cleanup after reloads. Hardware detection checks Linux input topology every two seconds; unusual keyboards may need manual mode.

Preferences live in `$XDG_STATE_HOME/omarchy-tablet` (default `~/.local/state/omarchy-tablet`); runtime state lives in `$XDG_RUNTIME_DIR/omarchy-tablet`. The switcher displays window titles, but the plugin does not save their contents, typed text, dictated text or audio. Recovery records window identities and layout states. Like other shell plugins, it runs with the user's permissions. Native widget commands retain their normal shell behavior.

## Validation and contributing

Run from the repository root:

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile *.py scripts/*.py tests/*.py
omarchy plugin validate .
python3 scripts/lint_qml.py  # requires Qt development tools and the installed Omarchy shell
git diff --check
```

See the [release review](docs/release-readiness.md) for fresh results and outstanding device checks. GitHub Actions runs Python tests on 3.11 and 3.14; this does not exercise a graphical Omarchy session.

Historical [validation](docs/validation.md), [keyboard work](docs/mode-keyboard-validation.md) and [architecture](docs/architecture.md) explain development decisions. Older screenshots and scripts can refer to earlier bar layouts. `scripts/live_smoke.py` is a Surface-specific developer harness with historical pointer coordinates and widget assumptions, not a portable certification test. It modifies the running session; inspect it before use. `scripts/check_input.py` also changes session state and needs GTK4, PyGObject and `wtype`.

Bug reports should include Omarchy, Hyprland and Quickshell versions, device model, display scale/orientation, selected mode, reproduction steps and relevant errors. Remove private window titles and other personal information from logs or screenshots.

## License

[MIT](LICENSE), copyright Charles Rivest. Native Omarchy widgets are loaded from the installed shell, not bundled. Application icons and themes visible in screenshots belong to their respective projects. This community plugin is not a Microsoft or Omarchy endorsement.
