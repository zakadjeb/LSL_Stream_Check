# LSL Marker Sender

An app for discovering Lab Streaming Layer (LSL) streams, sending timestamped text markers, and viewing live EEG. Includes saved keyboard bindings, separate Bindings and Settings tabs, and a recent-event history.

## Installation

Requires Python 3.10+

The app uses Tkinter, pylsl, NumPy, and SciPy. Update dependencies when upgrading an existing installation.

```sh
python -m pip install -r requirements.txt
python lsl_marker_sender.py
```

Standard Windows Python installations include Tkinter. On Linux, you may need your distribution's `python3-tk` package. See the [pylsl installation guide](https://github.com/labstreaminglayer/pylsl) if the native LSL library cannot be loaded.

## Sending markers

1. The app starts an outlet named **PythonEventMarkers**, with type **Markers**. Use **Refresh** to discover streams.
2. Select the marker stream in your recorder alongside your measurement streams and start recording.
3. Type a marker and press **Enter** or click **Send marker**.
4. In **Bindings**, assign text to F1–F12 or Ctrl+Shift+A–Z and click **Save binding**. Select an existing row to edit or remove it.

Shortcuts work while the app is focused and **Enable shortcuts** is checked. Holding a key sends one marker until you release it. Shortcuts preserve the manual text field. The enable preference is remembered across launches.

**Sent events** shows the last 500 published markers, newest first, including their triggering key and timestamp. This history is held in memory and cleared on exit.

The window supports resizing and scrolling. The Bindings and Settings tabs move below the sender in narrow windows. The compact stream list has its own scrollbars.

## Settings

The Settings tab controls the stream name, type, source ID, discovery duration, clearing text after sending, and automatic outlet startup. Click **Apply and save settings** to apply changes. Changing an active stream's identity restarts its outlet; refresh and reconnect your recorder afterward.

Preferences and bindings are generated locally in `lsl_marker_settings.json`, which is excluded from Git. A unique source ID is generated on first launch. Use different source IDs for simultaneous sender instances.

## Live EEG

Open the **Live EEG** tab. Stream discovery selects and connects the first available numeric stream whose type is `EEG`. For multiple devices, choose the desired EEG stream and click **Connect / restart**. EEG must declare a positive nominal sample rate.

- **Marker overlay:** select a marker stream (including this app's sender), then click **Connect / restart**. Newly received markers appear as labeled orange vertical lines. Select **None** for EEG alone. Changing streams restarts viewing and clears the display buffer.
- **Band-pass:** enter low/high cutoff frequencies and click **Apply filter**. Cutoffs must satisfy `0 < low < high < sample rate / 2`. Uncheck and apply to return to the raw display. The fourth-order Butterworth design is applied causally with state preserved between chunks; it introduces phase delay. Marker timestamps remain unchanged.
- **Y range:** set the full amplitude range per channel, then click **Apply view**. Smaller values enlarge the traces; **Auto scale** fits the currently visible channels. Values use each channel's declared units (for example, uV or V); there is no implicit unit conversion. Traces are centered on their median, and peaks outside the chosen range are clipped visually.
- **Channels:** choose 1–32 visible traces. Scroll up/down over the plot or use its vertical scrollbar to browse the remaining channels.
- **Seconds:** choose a live time window from 1 to 30 seconds. The viewer keeps at most 30 seconds, with a memory cap that can shorten retention for large streams. Missing samples are drawn as gaps.
- **Stop viewing:** disconnect the viewer and freeze the last received view. Use **Connect / restart** to resume.

The display updates approximately 20 times per second. Both EEG and marker inlets use LSL clock synchronization to place received timestamps on the local clock. Accurate overlays still depend on correct timestamps supplied by the source applications. Markers are overlaid visually, not inserted into the EEG stream. Viewing/filtering does not change other receivers' data or save a recording. The overlay retains the latest 2,000 received events and limits text labels to 40 per view to reduce clutter.

## LSL behavior

Markers are published on a **separate stream**, not injected into discovered streams. Record the marker stream alongside your measurement streams for time alignment.

The outlet uses one string channel, an irregular sample rate, and `pylsl.local_clock()` timestamps taken immediately before publication. These timestamps describe the software send action, not physical input onset.

A connected receiver does not confirm recording, and the event history does not acknowledge delivery. Markers sent before a receiver connects may be missed. If discovery is empty, check the source application and local network/firewall settings.

## Tests

```sh
python -B -m unittest -v
```

Tests require a desktop session and local LSL networking. They check the sender, shortcuts, filtering across chunks, frequency rejection, live synthetic EEG/marker delivery, overlays, scrolling, and amplitude controls.

