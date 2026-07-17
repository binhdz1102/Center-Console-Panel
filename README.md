# AAOS Rotary Control Panel

A compact Python Tkinter panel for simulating Android Automotive OS rotary input through ADB.

## Preview

<p align="center">
  <img src="preview.png" width="420"/>
</p>

## Features

- Rotate left / right
- Tilt / nudge up, down, left, right
- Enter / center button
- Home and Back buttons
- Capture device screenshot to the computer Downloads folder using `screencap` + `adb pull`
- Check connected ADB devices
- Repeat and delay controls
- Always-on-top floating window
- Vertically resizable panel while keeping the width compact
- Custom command runner loaded from `comands.txt`
- Automatic touchscreen recording from the connected device (`getevent`)
- Conversion of recorded touches to safe `input touchscreen` commands
- Timed replay of tap, double-tap, long-click, swipe, and two-finger zoom gestures
- Named touch profiles with inline selection, rename, and delete controls
- Windows no-terminal launcher via `ccp.pyw`

## Touch recording and replay

The **Touch recorder** is embedded in the main panel.

1. Connect and authorize one Android device. Set **Serial** when more than one device is connected.
2. Enter a **Record name**, then click **Record**.
3. Use the real Android touchscreen normally. The recorder listens to the touchscreen event stream automatically; no gesture type needs to be selected.
4. Click **Stop**. Complete touch sessions are classified and converted to `input touchscreen` commands, then saved automatically.
5. Choose a saved profile and click **Replay**. Edit **Record name** and click **Rename** to rename the selected profile.

Profiles are stored in `gesture_profiles.json` beside `ccp.py`. Writes are atomic and loaded data is validated before it can be replayed. Before replay, the program captures the connected screen and cancels if its pixel dimensions do not match the recorded profile.

Recording requires the device build to allow `adb shell getevent`. Two-finger zoom replay uses two concurrent touchscreen swipe commands; support depends on the Android input implementation. Timing is preserved from the recording, subject to normal ADB/device scheduling latency.

## Custom commands

The tool reads extra commands from `comands.txt`. Each command should be written on one line.

Example:

```text
adb shell dumpsys window
adb shell dumpsys meminfo
adb devices
```

After editing `comands.txt`, click **Reload**, choose one command from the dropdown, then click **Run**.

Notes:

- Blank lines are ignored.
- Lines starting with `#` are ignored.
- If a command starts with `adb`, the tool uses the current `ADB` field.
- If `Serial` is set, the tool automatically adds `-s <serial>` for most `adb` commands.
- `adb devices` is kept as a global command and does not use the selected serial.
- The command runner uses argument parsing, so simple ADB commands work best. Shell operators such as `>`, `|`, `&&`, and `;` are not intended for this runner.

## Run

Recommended on Windows, without terminal window:

```bash
pythonw ccp.pyw
```

Or double-click:

```text
ccp.pyw
```

Development mode:

```bash
python ccp.py
```

## Notes

- Drag the bottom edge or bottom-right resize grip to change the panel height.
- The width is locked to keep the panel compact beside Android Studio or an emulator.
- Set `ADB` to a full adb path if `adb` is not available in PATH.
- Screenshots are saved as `aaos_screenshot_yyyyMMdd_HHmmss.png` in your computer `Downloads` folder.
- Screenshot capture first creates the PNG on the Android device, then pulls it to the computer to avoid corrupted PNG output on Windows.
