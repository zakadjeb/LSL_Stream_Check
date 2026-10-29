"""Live LSL EEG display, streaming filter, and bounded sample buffer."""

from collections import deque
import math
import queue
import threading
import tkinter as tk
from tkinter import ttk

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi


class SignalBuffer:
    """Keep raw and display samples; preserve filter state across LSL chunks."""

    def __init__(self, rate, channels):
        if not math.isfinite(rate) or rate <= 0 or channels < 1:
            raise ValueError("EEG needs a positive nominal sample rate and numeric channels.")
        self.rate, self.channels = rate, channels
        # At most 30 seconds or about 32 MB of raw + filtered channel values.
        self.capacity = max(2, min(int(math.ceil(rate * 30)), 2_000_000 // channels))
        self.raw = np.empty((self.capacity, channels))
        self.filtered = np.empty_like(self.raw)
        self.times = np.empty(self.capacity)
        self.position = self.size = 0
        self.sos = self.zi = None
        self.last_time = None

    def indices(self):
        return (np.arange(self.size) + self.position - self.size) % self.capacity

    def configure_filter(self, enabled, low, high):
        if enabled and not (math.isfinite(low) and math.isfinite(high) and 0 < low < high < self.rate / 2):
            raise ValueError(f"Filter requires 0 < low < high < {self.rate / 2:g} Hz (Nyquist).")
        sos = butter(4, [low, high], btype="bandpass", fs=self.rate, output="sos") if enabled else None
        self.sos, self.zi, self.last_time = sos, None, None
        idx = self.indices()
        if len(idx):
            self.filtered[idx] = self._filter(self.raw[idx], self.times[idx])

    def _filter(self, samples, times):
        output = samples.copy()
        if self.sos is not None:
            # Restart at acquisition gaps so missing data is not treated as continuous.
            breaks = np.flatnonzero(np.diff(times) > 3 / self.rate) + 1
            for segment in np.split(np.arange(len(times)), breaks):
                if not len(segment):
                    continue
                first = segment[0]
                if self.zi is None or (self.last_time is not None and times[first] - self.last_time > 3 / self.rate):
                    self.zi = sosfilt_zi(self.sos)[:, :, None] * samples[first][None, None, :]
                output[segment], self.zi = sosfilt(self.sos, samples[segment], axis=0, zi=self.zi)
                self.last_time = times[segment[-1]]
        if len(times):
            self.last_time = times[-1]
        return output

    def append(self, samples, timestamps):
        samples, timestamps = np.asarray(samples, dtype=float), np.asarray(timestamps, dtype=float)
        if samples.ndim != 2 or samples.shape != (len(timestamps), self.channels):
            raise ValueError("Received EEG samples do not match the declared channel count.")
        valid = np.isfinite(timestamps) & np.all(np.isfinite(samples), axis=1)
        if self.last_time is not None:
            valid &= timestamps > self.last_time
        samples, timestamps = samples[valid], timestamps[valid]
        if len(timestamps) == 0:
            return
        if np.any(np.diff(timestamps) <= 0):
            raise ValueError("EEG timestamps must increase; reconnect the stream.")
        filtered = self._filter(samples, timestamps)
        samples, timestamps, filtered = samples[-self.capacity:], timestamps[-self.capacity:], filtered[-self.capacity:]
        idx = (np.arange(len(timestamps)) + self.position) % self.capacity
        self.raw[idx], self.filtered[idx], self.times[idx] = samples, filtered, timestamps
        self.position = (self.position + len(timestamps)) % self.capacity
        self.size = min(self.capacity, self.size + len(timestamps))

    def window(self, now, seconds, first, count):
        idx = self.indices()
        idx = idx[(self.times[idx] >= now - seconds) & (self.times[idx] <= now)]
        return self.times[idx], self.filtered[idx, first:first + count]


def receive_streams(lsl, eeg_info, marker_info, messages, stop):
    """Own both inlets in a worker. Only messages cross into the Tk thread."""
    inlets = []

    def emit(kind, value):
        if not stop.is_set():
            messages.put((kind, value), timeout=0.5)

    try:
        eeg = lsl.StreamInlet(eeg_info, max_buflen=5, processing_flags=lsl.proc_clocksync, recover=False)
        inlets.append(eeg)
        eeg.open_stream(timeout=3)
        full_info = eeg.info(timeout=3)
        node = full_info.desc().child("channels").child("channel")
        labels, units = [], []
        for index in range(eeg_info.channel_count()):
            labels.append(node.child_value("label") or f"Ch {index + 1}")
            units.append(node.child_value("unit") or "stream units")
            node = node.next_sibling("channel")
        marker = None
        if marker_info is not None:
            marker = lsl.StreamInlet(marker_info, max_buflen=5, processing_flags=lsl.proc_clocksync, recover=False)
            inlets.append(marker)
            marker.open_stream(timeout=3)
        emit("ready", (labels, units))
        chunk_size = max(1, min(1024, 65536 // eeg_info.channel_count()))
        while not stop.is_set():
            samples, timestamps = eeg.pull_chunk(timeout=0.0, max_samples=chunk_size)
            if timestamps:
                emit("eeg", (samples, timestamps))
            if marker is not None:
                samples_m, times_m = marker.pull_chunk(timeout=0.0, max_samples=256)
                if times_m:
                    emit("markers", [(stamp, " | ".join(map(str, sample))) for stamp, sample in zip(times_m, samples_m)])
            stop.wait(0.01)
    except Exception as exc:
        if not stop.is_set():
            # Always surface failures, even if a slow display exhausted the queue.
            if messages.full():
                messages.get_nowait()
            message = "Display cannot keep up with this stream; reduce its rate/channel count." if isinstance(exc, queue.Full) else str(exc)
            messages.put_nowait(("error", message))
    finally:
        for inlet in inlets:
            inlet.close_stream()


class EEGViewer(ttk.Frame):
    def __init__(self, parent, lsl, refresh):
        super().__init__(parent, padding=8)
        self.lsl = lsl
        self.streams = {}
        self.buffer = None
        self.running = False
        self.auto_connect = True
        self.stop_event = threading.Event()
        self.messages = queue.Queue(maxsize=32)
        self.markers = deque(maxlen=2000)
        self.labels, self.units = [], []
        self.first_channel = 0
        self.span, self.seconds, self.visible = 100.0, 10.0, 8
        self.worker = None
        self.filter_settings = (False, 1.0, 40.0)
        self.needs_autoscale = False
        self.last_received = None
        self.eeg_choice = tk.StringVar()
        self.marker_choice = tk.StringVar(value="None")
        self.status = tk.StringVar(value="Refresh streams to find EEG (stream type EEG).")
        self.display_status = tk.StringVar()

        selectors = ttk.Frame(self)
        selectors.pack(fill="x")
        ttk.Label(selectors, text="EEG stream").grid(row=0, column=0, sticky="w")
        self.eeg_combo = ttk.Combobox(selectors, textvariable=self.eeg_choice, state="readonly")
        self.eeg_combo.grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        ttk.Label(selectors, text="Marker overlay").grid(row=1, column=0, sticky="w")
        self.marker_combo = ttk.Combobox(selectors, textvariable=self.marker_choice, state="readonly")
        self.marker_combo.grid(row=1, column=1, sticky="ew", padx=6, pady=3)
        selectors.columnconfigure(1, weight=1)
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=4)
        ttk.Button(buttons, text="Refresh streams", command=refresh).pack(side="left")
        ttk.Button(buttons, text="Connect / restart", command=self.connect).pack(side="left", padx=6)
        ttk.Button(buttons, text="Stop viewing", command=self.stop).pack(side="left")
        ttk.Label(self, text="Choose streams, then Connect / restart. Viewing does not modify or record either stream.", wraplength=750).pack(anchor="w")

        # Two short control rows fit even when the main window is narrow.
        filtering = ttk.Frame(self)
        filtering.pack(fill="x", pady=(8, 3))
        self.filter_on = tk.BooleanVar(value=False)
        self.low, self.high = tk.StringVar(value="1"), tk.StringVar(value="40")
        ttk.Checkbutton(filtering, text="Band-pass", variable=self.filter_on).pack(side="left")
        for label, variable in (("Low Hz", self.low), ("High Hz", self.high)):
            ttk.Label(filtering, text=label).pack(side="left", padx=(6, 3))
            ttk.Entry(filtering, textvariable=variable, width=6).pack(side="left")
        ttk.Button(filtering, text="Apply filter", command=self.apply_filter).pack(side="left", padx=6)

        scaling = ttk.Frame(self)
        scaling.pack(fill="x", pady=3)
        self.span_var, self.seconds_var, self.visible_var = tk.StringVar(value="100"), tk.StringVar(value="10"), tk.StringVar(value="8")
        for label, variable in (("Y range", self.span_var), ("Seconds", self.seconds_var), ("Channels", self.visible_var)):
            ttk.Label(scaling, text=label).pack(side="left", padx=(0, 3))
            ttk.Entry(scaling, textvariable=variable, width=6).pack(side="left", padx=(0, 6))
        ttk.Button(scaling, text="Apply view", command=self.apply_view).pack(side="left")
        ttk.Button(scaling, text="Auto scale", command=self.autoscale).pack(side="left", padx=6)
        ttk.Label(self, text="Y range = full height of each trace in its declared units; traces are centered. Wheel / scrollbar: channels.", wraplength=750).pack(anchor="w")
        ttk.Label(self, textvariable=self.status, wraplength=850).pack(anchor="w", pady=3)
        ttk.Label(self, textvariable=self.display_status, wraplength=850).pack(anchor="w")
        plot_frame = ttk.Frame(self)
        plot_frame.pack(fill="both", expand=True)
        self.plot = tk.Canvas(plot_frame, background="#101923", highlightthickness=0, height=300, width=1)
        self.plot.pack(side="left", fill="both", expand=True)
        self.channel_scroll = ttk.Scrollbar(plot_frame, orient="vertical", command=self.scroll_channels)
        self.channel_scroll.pack(side="right", fill="y")
        self.plot.bind("<MouseWheel>", self.wheel)
        self.plot.bind("<Button-4>", self.wheel)
        self.plot.bind("<Button-5>", self.wheel)
        self.poll_id = self.after(50, self.poll)

    def set_streams(self, streams):
        self.streams = {f"{s.name()} | {s.hostname()} | {s.uid()}": s for s in streams}
        eeg = [key for key, s in self.streams.items() if s.type().strip().lower() == "eeg" and s.channel_format() != self.lsl.cf_string]
        markers = [key for key, s in self.streams.items() if s.type().strip().lower() in ("marker", "markers", "event", "events") or s.channel_format() == self.lsl.cf_string]
        self.eeg_combo.configure(values=eeg)
        self.marker_combo.configure(values=["None"] + markers)
        if not self.running:
            if self.eeg_choice.get() not in eeg:
                self.eeg_choice.set(eeg[0] if eeg else "")
            if self.marker_choice.get() not in markers:
                self.marker_choice.set("None")
            if eeg and self.auto_connect:
                self.connect()

    def connect(self):
        info = self.streams.get(self.eeg_choice.get())
        marker = self.streams.get(self.marker_choice.get())
        try:
            if info is None:
                raise ValueError("Select an available EEG stream first.")
            if self.marker_choice.get() != "None" and marker is None:
                raise ValueError("The selected marker stream is no longer available. Refresh streams.")
            buffer = SignalBuffer(info.nominal_srate(), info.channel_count())
            low, high = float(self.low.get()), float(self.high.get())
            buffer.configure_filter(self.filter_on.get(), low, high)
        except (ValueError, OverflowError) as exc:
            self.status.set(str(exc))
            return
        self.stop(auto=False)
        self.auto_connect = False
        self.buffer = buffer
        self.filter_settings = (self.filter_on.get(), low, high)
        self.first_channel = 0
        self.markers.clear()
        self.labels = [f"Ch {i + 1}" for i in range(buffer.channels)]
        self.units = ["stream units"] * buffer.channels
        self.needs_autoscale = True
        self.last_received = None
        self.running = True
        self.status.set(f"Connecting to {info.name()}…")
        self.messages = queue.Queue(maxsize=32)
        self.stop_event = threading.Event()
        self.worker = threading.Thread(target=receive_streams, args=(self.lsl, info, marker, self.messages, self.stop_event), daemon=True)
        self.worker.start()

    def stop(self, auto=True):
        self.stop_event.set()
        self.running = False
        if auto:
            self.auto_connect = False
        self.status.set("Viewing stopped. Connect to resume.")

    def apply_filter(self):
        try:
            if self.buffer is None:
                raise ValueError("Connect an EEG stream before applying a filter.")
            low, high = float(self.low.get()), float(self.high.get())
            self.buffer.configure_filter(self.filter_on.get(), low, high)
            self.filter_settings = (self.filter_on.get(), low, high)
            self.status.set("Display filter applied. Causal filtering introduces phase delay; marker times are unchanged.")
        except ValueError as exc:
            self.status.set(str(exc))

    def apply_view(self):
        try:
            span, seconds, visible = float(self.span_var.get()), float(self.seconds_var.get()), int(self.visible_var.get())
            if not math.isfinite(span) or span <= 0 or not 1 <= seconds <= 30 or not 1 <= visible <= 32:
                raise ValueError("Use a positive Y range, 1–30 seconds, and 1–32 visible channels.")
            self.span, self.seconds, self.visible = span, seconds, visible
            self.first_channel = min(self.first_channel, max(0, len(self.labels) - visible))
            self.status.set("View updated.")
        except ValueError as exc:
            self.status.set(str(exc))

    def autoscale(self):
        if self.buffer is None or not self.buffer.size:
            return
        _, values = self.buffer.window(self.lsl.local_clock(), self.seconds, self.first_channel, self.visible)
        if len(values):
            centered = values - np.median(values, axis=0)
            self.span = max(float(np.percentile(np.abs(centered), 98)) * 2.5, 1e-12)
            self.span_var.set(f"{self.span:.6g}")
            self.needs_autoscale = False

    def scroll_channels(self, operation, value, units=None):
        count = len(self.labels)
        if operation == "moveto":
            self.first_channel = round(float(value) * count)
        else:
            self.first_channel += int(value) * (self.visible if units == "pages" else 1)
        self.first_channel = min(max(0, self.first_channel), max(0, count - self.visible))

    def wheel(self, event):
        direction = -1 if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0 else 1
        self.scroll_channels("scroll", direction)
        return "break"

    def poll(self):
        try:
            if self.running:
                for _ in range(16):
                    try:
                        kind, value = self.messages.get_nowait()
                    except queue.Empty:
                        break
                    if kind == "ready":
                        self.labels, self.units = value
                        self.status.set("Connected. Waiting for EEG samples…")
                    elif kind == "eeg":
                        self.buffer.append(*value)
                        self.last_received = self.lsl.local_clock()
                        if self.status.get() == "Connected. Waiting for EEG samples…":
                            self.status.set("Receiving EEG. Marker lines use synchronized LSL timestamps.")
                    elif kind == "markers":
                        self.markers.extend(value)
                    elif kind == "error":
                        self.stop()
                        self.status.set(f"Stream error: {value}")
                        break
                if self.needs_autoscale and self.buffer.size >= min(20, self.buffer.capacity):
                    self.autoscale()
            if self.winfo_viewable():
                self.draw()
        except Exception as exc:
            self.stop()
            self.status.set(f"Display error: {exc}")
        self.poll_id = self.after(50, self.poll)

    def draw(self):
        self.plot.delete("all")
        width, height = self.plot.winfo_width(), self.plot.winfo_height()
        if self.buffer is None:
            self.plot.create_text(width / 2, height / 2, text="No EEG connected. Refresh streams or choose an EEG stream above.", fill="white", width=max(100, width - 40))
            return
        now = self.lsl.local_clock() if self.running else (self.buffer.last_time or self.lsl.local_clock())
        times, values = self.buffer.window(now, self.seconds, self.first_channel, self.visible)
        count = min(self.visible, self.buffer.channels - self.first_channel)
        self.channel_scroll.set(self.first_channel / self.buffer.channels, (self.first_channel + count) / self.buffer.channels)
        enabled, low, high = self.filter_settings
        mode = f"Band-pass {low:g}–{high:g} Hz (causal)" if enabled else "Unfiltered"
        live = "Stopped" if not self.running else "Waiting / no recent EEG" if self.last_received is None or self.lsl.local_clock() - self.last_received > 2 else "Live"
        self.display_status.set(f"{live} · {mode} · {self.buffer.rate:g} Hz · Channels {self.first_channel + 1}–{self.first_channel + count}/{self.buffer.channels} · Y range {self.span:g}")
        left, right, top, bottom = 105, max(106, width - 12), 30, max(31, height - 28)
        row_height = (bottom - top) / max(1, count)
        x = left + (times - (now - self.seconds)) / self.seconds * (right - left)
        for seconds_ago in np.linspace(self.seconds, 0, 6):
            tick = right - seconds_ago / self.seconds * (right - left)
            self.plot.create_line(tick, top, tick, bottom, fill="#273544")
            self.plot.create_text(tick, bottom + 14, text=f"-{seconds_ago:g}s" if seconds_ago else "now", fill="#aab8c5")
        for channel in range(count):
            center = top + (channel + 0.5) * row_height
            index = self.first_channel + channel
            self.plot.create_text(4, center, text=f"{self.labels[index]}\n{self.units[index]}", anchor="w", fill="#cbd5e1", width=96)
            self.plot.create_line(left, center, right, center, fill="#273544")
            if len(times) < 2:
                continue
            signal = values[:, channel] - np.median(values[:, channel])
            y = center - np.clip(signal / self.span, -0.48, 0.48) * row_height
            # Do not draw a continuous trace over missing acquisition samples.
            segments = np.split(np.arange(len(times)), np.flatnonzero(np.diff(times) > 3 / self.buffer.rate) + 1)
            for segment in segments:
                if len(segment) < 2:
                    continue
                # Retain min/max extrema in each pixel bucket instead of dropping spikes.
                stride = max(1, int(math.ceil(len(segment) / max(1, right - left))))
                if stride == 1:
                    indices = segment
                else:
                    complete = len(segment) // stride * stride
                    buckets = segment[:complete].reshape(-1, stride)
                    offsets = np.arange(len(buckets))
                    minima = buckets[offsets, np.argmin(y[buckets], axis=1)]
                    maxima = buckets[offsets, np.argmax(y[buckets], axis=1)]
                    indices = np.concatenate((np.sort(np.column_stack((minima, maxima)), axis=1).ravel(), segment[complete:]))
                coords = np.column_stack((x[indices], y[indices])).ravel().tolist()
                if len(coords) >= 4:
                    self.plot.create_line(*coords, fill="#67e8c2", width=1, tags="trace")
        visible_markers = [(stamp, text) for stamp, text in self.markers if now - self.seconds <= stamp <= now]
        for index, (stamp, text) in enumerate(visible_markers):
            marker_x = left + (stamp - (now - self.seconds)) / self.seconds * (right - left)
            self.plot.create_line(marker_x, top, marker_x, bottom, fill="#ffbc66", dash=(3, 2), tags="marker")
            if index < 40:
                self.plot.create_text(marker_x + 2, 5 + (index % 2) * 12, text=text[:45], anchor="nw", fill="#ffbc66", tags="marker_label")
        if not len(times):
            self.plot.create_text((left + right) / 2, (top + bottom) / 2, text="Waiting for EEG samples in this time window…", fill="white")

    def close(self):
        self.stop_event.set()
        self.after_cancel(self.poll_id)
