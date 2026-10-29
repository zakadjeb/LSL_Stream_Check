"""Run with python -m unittest -v. Integration test requires a desktop and pylsl."""

import tempfile
import time
import tkinter as tk
import unittest
import uuid
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import pylsl

from lsl_marker_sender import MarkerApp, Settings, load_settings


class SettingsTests(unittest.TestCase):
    def test_roundtrip_and_stable_source_id(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            original, warning = load_settings(path)
            self.assertIsNone(warning)
            original.name = "Test markers"
            original.clear_after_send = True
            original.bindings = {"F1": "trial_start", "Ctrl+Shift+A": "æøå"}
            original.shortcuts_enabled = False
            original.save(path)
            loaded, warning = load_settings(path)
            self.assertIsNone(warning)
            self.assertEqual(original, loaded)

    def test_invalid_settings_fall_back_without_overwriting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text('{"discovery_seconds": -1}', encoding="utf-8")
            settings, warning = load_settings(path)
            self.assertIsNotNone(warning)
            settings.validate()
            self.assertIn("-1", path.read_text(encoding="utf-8"))

    def test_invalid_durations(self):
        for duration in (float("nan"), float("inf"), 0, 31, True, "2"):
            with self.subTest(duration=duration):
                with self.assertRaises(ValueError):
                    Settings(source_id="test", discovery_seconds=duration).validate()


class LSLIntegrationTests(unittest.TestCase):
    def test_tk_sender_discovery_and_real_marker_delivery(self):
        root = tk.Tk()
        root.withdraw()
        settings = Settings(source_id=f"test-{uuid.uuid4()}", discovery_seconds=0.3,
                            clear_after_send=True, bindings={"F12": "previously saved marker", "Ctrl+Shift+H": "saved H"})
        app = None
        inlet = None
        try:
            with patch("lsl_marker_sender.load_settings", return_value=(settings, None)):
                app = MarkerApp(root, pylsl)
            self.assertEqual(app.binding_table.item("F12", "values"), ("F12", "previously saved marker"))
            self.assertTrue(app.shortcuts_enabled.get())
            self.assertFalse(any(isinstance(w, tk.Toplevel) for w in root.winfo_children()))
            root.deiconify()
            self.assertEqual([app.options_tabs.tab(tab, "text") for tab in app.options_tabs.tabs()],
                             ["Bindings", "Settings"])
            self.assertEqual(app.options_tabs.select(), str(app.bindings_tab))
            for geometry, mode in (("720x480", "narrow"), ("1060x650", "wide")):
                root.geometry(geometry + "+0+0")
                root.update()
                self.assertEqual(app.layout_mode, mode)
                self.assertGreaterEqual(float(app.canvas.cget("scrollregion").split()[3]), app.content.winfo_reqheight())
                app.canvas.yview_moveto(1.0)
                root.update_idletasks()
                self.assertAlmostEqual(app.canvas.yview()[1], 1.0)
            self.assertEqual(int(app.stream_table.cget("height")), 2)
            app.canvas.yview_moveto(0)
            root.lift()
            root.attributes("-topmost", True)
            root.update()
            # Check real visibility/stacking, not just whether data exists in Tk.
            app.binding_table.see("F12")
            root.update_idletasks()
            bounds = app.binding_table.bbox("F12", "marker")
            self.assertTrue(bounds)
            x, y, width, height = bounds
            visible_widget = root.winfo_containing(app.binding_table.winfo_rootx() + x + 5,
                                                   app.binding_table.winfo_rooty() + y + height // 2)
            self.assertIs(visible_widget, app.binding_table,
                          f"Saved rows must be visible: bounds={bounds}, table position="
                          f"{app.binding_table.winfo_rootx()},{app.binding_table.winfo_rooty()}")
            app.options_tabs.select(app.settings_tab)
            root.update()
            self.assertFalse(app.binding_table.winfo_ismapped())
            app.options_tabs.select(app.bindings_tab)
            root.update()
            self.assertTrue(app.binding_table.winfo_ismapped())
            self.assertEqual(app.binding_table.item("F12", "values"), ("F12", "previously saved marker"))
            root.withdraw()
            streams = pylsl.resolve_byprop("source_id", settings.source_id, timeout=5)
            self.assertTrue(streams, "The test outlet must be discoverable")
            inlet = pylsl.StreamInlet(streams[0])
            inlet.open_stream(timeout=5)
            self.assertTrue(app.outlet.wait_for_consumers(5))
            # Keep the Tk event loop alive until background discovery completes.
            deadline = time.monotonic() + 5
            while app.scanning and time.monotonic() < deadline:
                root.update()
                time.sleep(0.02)
            self.assertFalse(app.scanning)
            rows = [app.stream_table.item(item, "values") for item in app.stream_table.get_children()]
            self.assertTrue(any(row[-1] == settings.source_id for row in rows))
            marker = "  Trial start – æøå 🏃  "
            app.marker.set(marker)
            before = pylsl.local_clock()
            app.send_marker()
            sample, timestamp = inlet.pull_sample(timeout=5)
            self.assertEqual(sample, [marker])
            self.assertGreaterEqual(timestamp, before)
            self.assertLessEqual(timestamp, pylsl.local_clock())
            self.assertEqual(app.marker.get(), "")
            self.assertEqual(app.sent, 1)
            self.assertEqual(len(app.log.get_children()), 1)
            with patch.object(Settings, "save", autospec=True) as save:
                app.binding_key.set("F1")
                app.binding_text.set("preset marker")
                app.save_binding()
                save.assert_called_once()
            self.assertEqual(app.binding_table.item("F1", "values"), ("F1", "preset marker"))
            app.marker.set("unfinished draft")
            key = SimpleNamespace(keysym="F1", keycode=112, state=0)
            app.shortcuts_enabled.set(False)
            with patch.object(Settings, "save", autospec=True) as saved:
                app.save_shortcut_preference()
                self.assertFalse(saved.call_args.args[0].shortcuts_enabled)
            app.key_press(key)
            self.assertEqual(app.sent, 1)  # Explicitly disabled.
            app.shortcuts_enabled.set(True)
            with patch.object(Settings, "save", autospec=True) as saved:
                app.save_shortcut_preference()
                self.assertTrue(saved.call_args.args[0].shortcuts_enabled)
            app.key_press(key)
            app.key_press(key)  # Holding a key must not repeat markers.
            sample, timestamp = inlet.pull_sample(timeout=5)
            self.assertEqual(sample, ["preset marker"])
            self.assertEqual(app.sent, 2)
            self.assertEqual(app.marker.get(), "unfinished draft")
            app.key_release(key)
            root.update_idletasks()
            app.key_press(key)
            self.assertEqual(inlet.pull_sample(timeout=5)[0], ["preset marker"])
            app.key_release(key)
            root.update_idletasks()
            with patch.object(Settings, "save", autospec=True):
                app.binding_key.set("Ctrl+Shift+A")
                app.binding_text.set("modified shortcut")
                app.save_binding()
            app.key_press(SimpleNamespace(keysym="A", keycode=65, state=5))
            self.assertEqual(inlet.pull_sample(timeout=5)[0], ["modified shortcut"])
            self.assertEqual(app.sent, 4)
            app.release_key(65)
            # Exercise actual Tk bindings, not only direct Python callbacks.
            callback_errors = []
            root.report_callback_exception = lambda *error: callback_errors.append(error)
            root.deiconify()
            app.canvas.yview_moveto(0)
            root.update()
            app.entry.focus_force()
            root.update()
            for keysym, state, expected in (("F1", 0, "preset marker"), ("H", 5, "saved H")):
                app.entry.event_generate("<KeyPress>", keysym=keysym, state=state)
                app.entry.event_generate("<KeyRelease>", keysym=keysym, state=state)
                root.update()
                self.assertEqual(inlet.pull_sample(timeout=5)[0], [expected])
            self.assertEqual(app.marker.get(), "unfinished draft")
            # Reproduce focus on ttk's Tcl-only popdown listbox.
            root.tk.call("ttk::combobox::Post", str(app.binding_combo))
            popup = root.tk.call("ttk::combobox::PopdownWindow", str(app.binding_combo))
            listbox = f"{popup}.f.l"
            root.tk.call("focus", "-force", listbox)
            root.update()
            self.assertEqual(str(root.tk.call("focus", "-displayof", root._w)), listbox)
            app.reset_keys_if_unfocused()
            root.tk.call("event", "generate", listbox, "<KeyPress-F1>")
            root.tk.call("event", "generate", listbox, "<KeyRelease-F1>")
            root.update()
            self.assertEqual(inlet.pull_sample(timeout=5)[0], ["preset marker"])
            root.tk.call("ttk::combobox::Unpost", str(app.binding_combo))
            root.update()
            self.assertEqual(callback_errors, [])
            self.assertEqual(app.sent, 7)
            events = [app.log.item(item, "values") for item in app.log.get_children()]
            self.assertEqual(len(events), 7)
            self.assertEqual(events[0][2:], ("F1", "preset marker"))
            self.assertEqual(events[-1][2:], ("Send button", marker))
            self.assertTrue(any(row[2:] == ("Ctrl+Shift+A", "modified shortcut") for row in events))
            self.assertEqual(app.log.selection(), (app.log.get_children()[0],))
            self.assertIn("preset marker", app.last_sent.get())
            # Windows Num Lock is Mod1 (0x8), not Alt. It must not block shortcuts.
            if root.tk.call("tk", "windowingsystem") == "win32":
                app.settings.bindings.update({"F6": "TEST", "Ctrl+Shift+J": "TEST"})
                app.entry.focus_force()
                root.update()
                for locks in (0, 0x8, 0xA, 0x2A):
                    for keysym, modifiers, binding in (("F6", 0, "F6"), ("J", 5, "Ctrl+Shift+J")):
                        with self.subTest(locks=locks, binding=binding):
                            previous = app.sent
                            app.entry.event_generate("<KeyPress>", keysym=keysym, state=modifiers | locks)
                            app.entry.event_generate("<KeyRelease>", keysym=keysym, state=modifiers | locks)
                            root.update()
                            self.assertEqual(app.sent, previous + 1)
                            self.assertEqual(inlet.pull_sample(timeout=5)[0], ["TEST"])
                            latest = app.log.item(app.log.get_children()[0], "values")
                            self.assertEqual(latest[2:], (binding, "TEST"))
                # An actual extra Alt modifier must still prevent a match.
                previous = app.sent
                app.entry.event_generate("<KeyPress>", keysym="F6", state=0x20000)
                app.entry.event_generate("<KeyRelease>", keysym="F6", state=0x20000)
                root.update()
                self.assertEqual(app.sent, previous)
            expected_sent = app.sent
            with patch.object(Settings, "save", autospec=True):
                app.fields["discovery_seconds"].set("1.5")
                app.save_settings()
                self.assertEqual(app.settings.discovery_seconds, 1.5)
                self.assertEqual(app.settings.bindings["F1"], "preset marker")
                app.binding_table.selection_set("F1")
                app.remove_binding()
                self.assertNotIn("F1", app.settings.bindings)
                self.assertNotIn("F1", app.binding_table.get_children())
            inlet.close_stream()
            app.stop_outlet()
            app.marker.set("not sent")
            app.send_marker()
            self.assertEqual(app.sent, expected_sent)
            app.start_outlet()
            self.assertIsNotNone(app.outlet)
        finally:
            if inlet is not None:
                inlet.close_stream()
            if app is not None:
                app.close()
            else:
                root.destroy()


if __name__ == "__main__":
    unittest.main()
