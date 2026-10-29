"""Signal-processing checks and a live Tk/LSL EEG + marker integration test."""

import threading
import time
import tkinter as tk
import unittest
import uuid

import numpy as np
import pylsl

from eeg_viewer import EEGViewer, SignalBuffer


class SignalBufferTests(unittest.TestCase):
    def test_filter_continuity_and_frequency_rejection(self):
        rate = 250
        times = np.arange(rate * 8) / rate
        samples = (np.sin(2 * np.pi * 10 * times) + np.sin(2 * np.pi * 80 * times))[:, None]
        whole, chunked = SignalBuffer(rate, 1), SignalBuffer(rate, 1)
        for buffer in (whole, chunked):
            buffer.configure_filter(True, 5, 30)
        whole.append(samples, times)
        for start in range(0, len(times), 37):
            chunked.append(samples[start:start + 37], times[start:start + 37])
        np.testing.assert_allclose(whole.filtered[whole.indices()], chunked.filtered[chunked.indices()], atol=1e-12)
        filtered = whole.filtered[whole.indices()][500:, 0]
        spectrum = np.abs(np.fft.rfft(filtered))
        frequencies = np.fft.rfftfreq(len(filtered), 1 / rate)
        amplitude_10 = spectrum[np.argmin(abs(frequencies - 10))]
        amplitude_80 = spectrum[np.argmin(abs(frequencies - 80))]
        self.assertGreater(amplitude_10, 500)
        self.assertLess(amplitude_80, amplitude_10 / 30)
        whole.configure_filter(False, 5, 30)
        np.testing.assert_allclose(whole.filtered[whole.indices()], samples)

    def test_validation_ring_buffer_and_gaps(self):
        buffer = SignalBuffer(10, 2)
        for low, high in ((0, 4), (2, 2), (1, 5), (float("nan"), 4)):
            with self.subTest(low=low, high=high), self.assertRaises(ValueError):
                buffer.configure_filter(True, low, high)
        times = np.arange(400) / 10
        samples = np.column_stack((times, times * 2))
        buffer.append(samples, times)
        self.assertEqual(buffer.size, 300)
        shown_times, shown = buffer.window(39.9, 2, 1, 1)
        np.testing.assert_allclose(shown[:, 0], shown_times * 2)
        self.assertLessEqual(len(shown), 21)
        buffer.configure_filter(True, 0.5, 4)
        after_gap = np.full((10, 2), 25.0)
        buffer.append(after_gap, 50 + np.arange(10) / 10)
        # A new constant segment starts at steady state instead of ringing from the previous segment.
        np.testing.assert_allclose(buffer.filtered[buffer.indices()][-10:], 0, atol=1e-10)
        buffer.append([[float("nan"), 1]], [52])
        self.assertAlmostEqual(buffer.last_time, 50.9)


class LiveViewerTests(unittest.TestCase):
    def test_live_eeg_markers_filter_scale_and_scroll(self):
        unique = str(uuid.uuid4())
        info = pylsl.StreamInfo("Test EEG", "EEG", 12, 200, "float32", "eeg-" + unique)
        channels = info.desc().append_child("channels")
        for number in range(12):
            channel = channels.append_child("channel")
            channel.append_child_value("label", f"EEG {number + 1}")
            channel.append_child_value("unit", "uV")
        eeg_outlet = pylsl.StreamOutlet(info)
        marker_outlet = pylsl.StreamOutlet(pylsl.StreamInfo("Test events", "Markers", 1, 0, "string", "markers-" + unique))
        root = tk.Tk()
        root.geometry("1000x720+0+0")
        viewer = EEGViewer(root, pylsl, lambda: None)
        viewer.pack(fill="both", expand=True)
        errors = []
        root.report_callback_exception = lambda *args: errors.append(args)
        producer_stop = threading.Event()
        producer = None

        def wait_until(condition, timeout=8):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                root.update()
                if condition():
                    return
                time.sleep(0.01)
            self.fail(f"Timed out: {viewer.status.get()} / {viewer.display_status.get()}")

        try:
            eeg = pylsl.resolve_byprop("source_id", "eeg-" + unique, timeout=5)
            markers = pylsl.resolve_byprop("source_id", "markers-" + unique, timeout=5)
            self.assertTrue(eeg and markers)
            viewer.set_streams(eeg + markers)
            self.assertTrue(viewer.running, "The first discovered EEG should connect automatically")
            viewer.marker_choice.set(next(key for key, stream in viewer.streams.items() if stream.type() == "Markers"))
            viewer.connect()
            wait_until(lambda: eeg_outlet.have_consumers() and marker_outlet.have_consumers())

            def produce():
                origin = pylsl.local_clock()
                index = 0
                marked = False
                while not producer_stop.is_set():
                    end = int((pylsl.local_clock() - origin) * 200)
                    if end > index:
                        times = origin + np.arange(index, end) / 200
                        values = 40 * np.sin(2 * np.pi * 10 * (times[:, None] - origin) + np.arange(12)[None, :] / 10)
                        eeg_outlet.push_chunk(values.tolist(), timestamp=float(times[-1]))
                        index = end
                    if index >= 80 and not marked:
                        marker_outlet.push_sample(["trial_start"], timestamp=pylsl.local_clock())
                        marked = True
                    producer_stop.wait(0.02)

            producer = threading.Thread(target=produce, daemon=True)
            producer.start()
            wait_until(lambda: viewer.buffer.size >= 200 and bool(viewer.plot.find_withtag("marker")))
            self.assertEqual(viewer.labels[0], "EEG 1")
            self.assertEqual(viewer.units[0], "uV")
            self.assertTrue(viewer.plot.find_withtag("trace"))
            self.assertEqual(viewer.markers[0][1], "trial_start")
            # A real received marker falls within the EEG timestamp interval.
            self.assertLess(abs(viewer.markers[0][0] - viewer.buffer.last_time), 2)
            self.assertIn("Live", viewer.display_status.get())
            previous_size = viewer.buffer.size
            wait_until(lambda: viewer.buffer.size > previous_size + 40)

            viewer.low.set("2")
            viewer.high.set("30")
            viewer.filter_on.set(True)
            viewer.apply_filter()
            self.assertIsNotNone(viewer.buffer.sos)
            self.assertEqual(viewer.filter_settings, (True, 2.0, 30.0))
            viewer.high.set("101")
            viewer.apply_filter()
            self.assertIn("Nyquist", viewer.status.get())
            self.assertEqual(viewer.filter_settings, (True, 2.0, 30.0))
            viewer.span_var.set("50")
            viewer.seconds_var.set("5")
            viewer.visible_var.set("4")
            viewer.apply_view()
            self.assertEqual(viewer.span, 50)
            viewer.scroll_channels("scroll", 1, "pages")
            self.assertEqual(viewer.first_channel, 4)
            viewer.draw()
            self.assertIn("Channels 5–8/12", viewer.display_status.get())
            viewer.scroll_channels("moveto", 1)
            self.assertEqual(viewer.first_channel, 8)
            viewer.autoscale()
            self.assertGreater(viewer.span, 0)

            viewer.stop()
            viewer.worker.join(timeout=4)
            self.assertFalse(viewer.worker.is_alive())
            viewer.draw()
            self.assertIn("Stopped", viewer.display_status.get())
            viewer.connect()
            wait_until(lambda: viewer.buffer.size > 20)
            self.assertEqual(errors, [])
        finally:
            producer_stop.set()
            if producer is not None:
                producer.join(timeout=2)
            viewer.close()
            if viewer.worker is not None:
                viewer.worker.join(timeout=4)
            root.destroy()


if __name__ == "__main__":
    unittest.main()
