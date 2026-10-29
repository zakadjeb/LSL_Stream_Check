"""Tkinter LSL marker sender. Run with: python lsl_marker_sender.py"""

from __future__ import annotations

import json
import math
import queue
import threading
import tkinter as tk
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

SETTINGS_PATH = Path(__file__).resolve().with_name("lsl_marker_settings.json")
HOTKEYS = tuple(f"F{i}" for i in range(1, 13)) + tuple(f"Ctrl+Shift+{chr(i)}" for i in range(65, 91))


@dataclass
class Settings:
    name: str = "PythonEventMarkers"
    stream_type: str = "Markers"
    source_id: str = ""
    discovery_seconds: float = 2.0
    clear_after_send: bool = False
    start_automatically: bool = True
    shortcuts_enabled: bool = True
    bindings: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        for key in ("name", "stream_type", "source_id"):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"{key.replace('_', ' ').title()} must be nonempty text without null characters.")
        if isinstance(self.discovery_seconds, bool) or not isinstance(self.discovery_seconds, (int, float)):
            raise ValueError("Discovery duration must be a number.")
        if not math.isfinite(self.discovery_seconds) or not 0.2 <= self.discovery_seconds <= 30:
            raise ValueError("Discovery duration must be between 0.2 and 30 seconds.")
        for key in ("clear_after_send", "start_automatically", "shortcuts_enabled"):
            if not isinstance(getattr(self, key), bool):
                raise ValueError(f"{key} must be true or false.")
        if not isinstance(self.bindings, dict):
            raise ValueError("Key bindings must be a key-to-marker mapping.")
        for key, marker in self.bindings.items():
            if key not in HOTKEYS or not isinstance(marker, str) or not marker.strip() or "\x00" in marker:
                raise ValueError("Each binding needs a supported key and nonempty marker text without null characters.")

    def save(self, path: Path = SETTINGS_PATH) -> None:
        self.validate()
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)


def load_settings(path: Path = SETTINGS_PATH) -> tuple[Settings, str | None]:
    defaults = Settings(source_id=f"python-markers-{uuid.uuid4()}")
    try:
        if not path.exists():
            defaults.save(path)
            return defaults, None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Settings must be a JSON object.")
        settings = Settings(**(asdict(defaults) | data))
        settings.validate()
        return settings, None
    except (OSError, ValueError, TypeError) as exc:
        return defaults, f"Using default settings: {exc}\nSave settings to repair the settings file."


def scrollable_table(parent, columns, height, **options):
    """Create a table whose scrollbars share its actual parent."""
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    table = ttk.Treeview(frame, columns=columns, show="headings", height=height, **options)
    vertical = ttk.Scrollbar(frame, orient="vertical", command=table.yview)
    horizontal = ttk.Scrollbar(frame, orient="horizontal", command=table.xview)
    table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    table.grid(row=0, column=0, sticky="nsew")
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    return table


class MarkerApp:
    def __init__(self, root: tk.Tk, lsl):
        from eeg_viewer import EEGViewer

        self.root = root
        window_system = root.tk.call("tk", "windowingsystem")
        # Tk on Windows uses Mod1 (0x8) for Num Lock, not Alt.
        # ALT_MASK is 0x20000; Mod2 is also accepted as the Windows Alt alias.
        self.alt_mask = {"win32": 0x20010, "aqua": 0x10}.get(window_system, 0x8)
        self.lsl = lsl
        self.settings, warning = load_settings()
        self.outlet = None
        self.pressed_keys = set()
        self.release_jobs = {}
        self.scanning = False
        self.results = queue.Queue()
        self.sent = 0
        root.title("LSL Event Marker Sender")
        width = min(1060, max(520, root.winfo_screenwidth() - 100))
        height = min(700, max(360, root.winfo_screenheight() - 140))
        root.geometry(f"{width}x{height}")
        root.minsize(520, 360)
        root.resizable(True, True)
        root.protocol("WM_DELETE_WINDOW", self.close)

        self.main_tabs = ttk.Notebook(root)
        self.main_tabs.pack(fill="both", expand=True)
        workspace = ttk.Frame(self.main_tabs)
        self.main_tabs.add(workspace, text="Marker sender")
        self.eeg_viewer = EEGViewer(self.main_tabs, lsl, self.discover)
        self.main_tabs.add(self.eeg_viewer, text="Live EEG")
        self.canvas = tk.Canvas(workspace, highlightthickness=0, width=1, height=1)
        vertical = ttk.Scrollbar(workspace, orient="vertical", command=self.canvas.yview)
        horizontal = ttk.Scrollbar(workspace, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        workspace.rowconfigure(0, weight=1)
        workspace.columnconfigure(0, weight=1)
        frame = self.content = ttk.Frame(self.canvas, padding=12)
        self.content_id = self.canvas.create_window(0, 0, window=frame, anchor="nw")
        ttk.Label(frame, text="Publish text markers on a separate, single-channel LSL stream.").pack(anchor="w", pady=(4, 12))
        columns = self.columns = ttk.Frame(frame)
        columns.pack(fill="both", expand=True)
        sender = self.sender = ttk.Frame(columns)
        sidebar = self.sidebar = ttk.Frame(columns)
        self.layout_mode = None
        self.canvas.bind("<Configure>", self.resize_layout)
        self.content.bind("<Configure>", self.update_scroll_region)
        root.bind("<MouseWheel>", self.scroll_workspace, add="+")
        root.bind("<Button-4>", self.scroll_workspace, add="+")
        root.bind("<Button-5>", self.scroll_workspace, add="+")
        frame = sender
        controls = ttk.Frame(frame)
        controls.pack(fill="x")
        self.toggle = ttk.Button(controls, text="Start outlet", command=self.toggle_outlet)
        self.toggle.pack(side="left")
        self.shortcuts_enabled = tk.BooleanVar(value=self.settings.shortcuts_enabled)
        ttk.Checkbutton(controls, text="Enable shortcuts", variable=self.shortcuts_enabled,
                        command=self.save_shortcut_preference).pack(side="left", padx=(12, 0))
        self.shortcut_status = tk.StringVar()
        ttk.Label(frame, textvariable=self.shortcut_status, wraplength=460).pack(anchor="w", pady=(6, 0))
        self.status = tk.StringVar(value="Outlet stopped")
        ttk.Label(frame, textvariable=self.status, wraplength=460).pack(anchor="w", pady=(12, 4))
        self.consumers = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.consumers, wraplength=460).pack(anchor="w", pady=(0, 12))
        ttk.Label(frame, text="Event marker text (Enter or Send publishes one marker):").pack(anchor="w")
        entry_row = ttk.Frame(frame)
        entry_row.pack(fill="x", pady=6)
        self.marker = tk.StringVar()
        self.entry = ttk.Entry(entry_row, textvariable=self.marker)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", self.send_marker)
        self.send_button = ttk.Button(entry_row, text="Send marker", command=self.send_marker, state="disabled")
        self.send_button.pack(side="left", padx=(8, 0))
        self.last_sent = tk.StringVar(value="No markers sent yet.")
        ttk.Label(frame, textvariable=self.last_sent, wraplength=460).pack(anchor="w", pady=(4, 0))
        ttk.Label(frame, text="Sent events (newest first; last 500)").pack(anchor="w", pady=(10, 4))
        self.log = scrollable_table(
            frame, ("time", "lsl", "trigger", "text"), height=6,
            displaycolumns=("time", "trigger", "text", "lsl"))
        for key, title, width in (("time", "Local time", 95), ("lsl", "LSL timestamp (s)", 130),
                                  ("trigger", "Sent using", 105), ("text", "Marker text", 220)):
            self.log.heading(key, text=title)
            self.log.column(key, width=width, stretch=key == "text")
        self.count = tk.StringVar(value="0 markers sent")
        ttk.Label(frame, textvariable=self.count, wraplength=460).pack(anchor="w", pady=(8, 0))
        self.build_browser(frame)
        self.options_tabs = ttk.Notebook(sidebar)
        self.options_tabs.pack(fill="both", expand=True)
        self.bindings_tab = ttk.Frame(self.options_tabs, padding=6)
        self.settings_tab = ttk.Frame(self.options_tabs, padding=6)
        self.options_tabs.add(self.bindings_tab, text="Bindings")
        self.options_tabs.add(self.settings_tab, text="Settings")
        self.build_bindings(self.bindings_tab)
        self.build_settings(self.settings_tab)
        self.options_tabs.select(self.bindings_tab)
        # An early bind tag consumes shortcuts before widget/class bindings run.
        self.hotkey_tag = f"MarkerHotkeys{id(self)}"
        root.bind_class(self.hotkey_tag, "<KeyPress>", self.key_press)
        root.bind_class(self.hotkey_tag, "<KeyRelease>", self.key_release)
        self.install_hotkeys(root)
        root.bind("<FocusOut>", self.focus_out, add="+")
        self.poll_id = root.after(100, self.poll)
        if warning:
            messagebox.showwarning("Settings", warning, parent=root)
        if self.settings.start_automatically:
            self.start_outlet()
        self.discover()
        self.entry.focus_set()

    def resize_layout(self, event):
        width = max(560, event.width)
        mode = "wide" if width >= 940 else "narrow"
        if mode != self.layout_mode:
            self.layout_mode = mode
            self.sender.grid_forget()
            self.sidebar.grid_forget()
            self.columns.columnconfigure(0, weight=3)
            self.columns.columnconfigure(1, weight=2 if mode == "wide" else 0)
            self.columns.rowconfigure(0, weight=1 if mode == "wide" else 0)
            self.sender.grid(row=0, column=0, sticky="nsew")
            self.sidebar.grid(row=0 if mode == "wide" else 1,
                              column=1 if mode == "wide" else 0, sticky="nsew",
                              padx=(12, 0) if mode == "wide" else 0,
                              pady=0 if mode == "wide" else (12, 0))
        self.canvas.itemconfigure(self.content_id, width=width)
        self.update_scroll_region()
        # Grid computes its new requested height after processing geometry changes.
        self.root.after_idle(self.update_scroll_region)

    def update_scroll_region(self, event=None):
        height = max(self.content.winfo_reqheight(), self.canvas.winfo_height())
        self.canvas.configure(scrollregion=(0, 0, max(560, self.canvas.winfo_width()), height))

    def scroll_workspace(self, event):
        # Tables retain their own scrolling; elsewhere the wheel scrolls the page.
        if isinstance(event.widget, (ttk.Treeview, ttk.Combobox)):
            return
        if self.canvas.yview() == (0.0, 1.0):
            return
        if getattr(event, "num", None) in (4, 5):
            steps = -1 if event.num == 4 else 1
        else:
            steps = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(steps * 3, "units")
        return "break"

    def start_outlet(self):
        try:
            info = self.lsl.StreamInfo(self.settings.name, self.settings.stream_type,
                                       1, 0.0, "string", self.settings.source_id)
            desc = info.desc()
            desc.append_child_value("manufacturer", "Python Tkinter Marker Sender")
            channel = desc.append_child("channels").append_child("channel")
            channel.append_child_value("label", "Marker")
            channel.append_child_value("type", "Marker")
            self.outlet = self.lsl.StreamOutlet(info, chunk_size=1)
        except Exception as exc:
            messagebox.showerror("Cannot start LSL outlet", str(exc), parent=self.root)
            return
        self.toggle.configure(text="Stop outlet")
        self.send_button.configure(state="normal")
        self.status.set(f"Publishing: {self.settings.name} | Type: {self.settings.stream_type}")

    def stop_outlet(self):
        self.outlet = None
        self.toggle.configure(text="Start outlet")
        self.send_button.configure(state="disabled")
        self.status.set("Outlet stopped")
        self.consumers.set("")

    def toggle_outlet(self):
        if self.outlet is None:
            self.start_outlet()
        else:
            self.stop_outlet()

    def send_marker(self, event=None, text=None, trigger=None):
        if self.outlet is None:
            return "break"
        manual = text is None
        if manual:
            text = self.marker.get()
        if not text.strip() or "\x00" in text:
            messagebox.showwarning("Invalid marker", "Enter nonempty text without null characters.", parent=self.root)
            return "break"
        try:
            timestamp = self.lsl.local_clock()
            self.outlet.push_sample([text], timestamp=timestamp, pushthrough=True)
        except Exception as exc:
            messagebox.showerror("Marker failed", str(exc), parent=self.root)
            return "break"
        self.sent += 1
        trigger = trigger or ("Enter" if event is not None else "Send button")
        local_time = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        item = self.log.insert("", 0, values=(local_time, f"{timestamp:.6f}", trigger, text))
        children = self.log.get_children()
        if len(children) > 500:
            self.log.delete(children[-1])
        self.log.selection_set(item)
        self.log.see(item)
        self.last_sent.set(f"Last sent: {text} ({trigger}, {local_time})")
        self.count.set(f"{self.sent} markers sent — publication does not confirm recording")
        if manual and self.settings.clear_after_send:
            self.marker.set("")
        if manual:
            self.entry.focus_set()
        return "break"

    def build_browser(self, parent):
        frame = ttk.LabelFrame(parent, text="Available LSL streams", padding=8)
        frame.pack(fill="x", expand=False, pady=(8, 0))
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=(0, 4))
        self.scan_button = ttk.Button(toolbar, text="Refresh", command=self.discover)
        self.scan_button.pack(anchor="w")
        self.scan_status = tk.StringVar(value="")
        ttk.Label(toolbar, textvariable=self.scan_status, wraplength=420).pack(anchor="w")
        columns = ("name", "type", "channels", "rate", "host", "source")
        self.stream_table = scrollable_table(frame, columns, height=2)
        for key, title, width in zip(columns, ("Name", "Type", "Channels", "Rate (Hz)", "Host", "Source ID"), (110, 65, 60, 65, 80, 100)):
            self.stream_table.heading(key, text=title)
            self.stream_table.column(key, width=width, minwidth=60)

    def discover(self):
        if self.scanning:
            return
        self.scanning = True
        self.scan_button.configure(state="disabled")
        self.scan_status.set("Searching…")
        duration = self.settings.discovery_seconds

        def worker():
            # Background discovery never accesses Tk widgets.
            try:
                streams = self.lsl.resolve_streams(wait_time=duration)
                self.results.put((streams, None))
            except Exception as exc:
                self.results.put(([], str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        self.shortcut_status.set("Shortcuts disabled — enable them above." if not self.shortcuts_enabled.get()
                                 else "Shortcuts enabled — start the outlet to send." if self.outlet is None
                                 else "Shortcuts enabled — keep this app focused.")
        try:
            streams, error = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.scanning = False
            self.scan_button.configure(state="normal")
            self.eeg_viewer.set_streams(streams)
            rows = [(s.name(), s.type(), s.channel_count(), s.nominal_srate() or "Irregular", s.hostname(), s.source_id()) for s in streams]
            for item in self.stream_table.get_children():
                self.stream_table.delete(item)
            for row in sorted(rows, key=lambda row: (row[0], row[1])):
                self.stream_table.insert("", "end", values=row)
            self.scan_status.set(f"Discovery failed: {error}" if error else f"Found {len(rows)} stream(s). Refresh to update.")
        if self.outlet is not None:
            try:
                connected = self.outlet.have_consumers()
                self.consumers.set("LSL receiver connected (recording status unknown)." if connected else "No receiver connected. Start your recorder before sending markers.")
            except Exception as exc:
                self.consumers.set(f"Receiver status unavailable: {exc}")
        self.poll_id = self.root.after(200, self.poll)

    def build_settings(self, parent):
        frame = ttk.LabelFrame(parent, text="Marker outlet settings", padding=10)
        frame.pack(fill="x")
        self.fields = {}
        labels = (("name", "Stream name"), ("stream_type", "Stream type"), ("source_id", "Source ID"), ("discovery_seconds", "Discovery duration (0.2–30 s)"))
        for row, (key, label) in enumerate(labels):
            ttk.Label(frame, text=label).grid(row=row * 2, column=0, sticky="w")
            self.fields[key] = tk.StringVar(value=str(getattr(self.settings, key)))
            ttk.Entry(frame, textvariable=self.fields[key], width=36).grid(row=row * 2 + 1, column=0, sticky="ew", pady=(0, 6))
        self.clear_setting = tk.BooleanVar(value=self.settings.clear_after_send)
        self.auto_setting = tk.BooleanVar(value=self.settings.start_automatically)
        ttk.Checkbutton(frame, text="Clear text after sending", variable=self.clear_setting).grid(row=8, sticky="w")
        ttk.Checkbutton(frame, text="Start outlet automatically on launch", variable=self.auto_setting).grid(row=9, sticky="w")
        ttk.Label(frame, text="One string channel · irregular rate · LSL timestamps.\nIdentity changes restart the outlet; reconnect your recorder.", wraplength=370).grid(row=10, sticky="w", pady=8)
        ttk.Button(frame, text="Apply and save settings", command=self.save_settings).grid(row=11, sticky="w")
        self.settings_status = tk.StringVar()
        ttk.Label(frame, textvariable=self.settings_status, wraplength=370).grid(row=12, sticky="w")
        frame.columnconfigure(0, weight=1)

    def save_settings(self):
        try:
            settings = Settings(
                name=self.fields["name"].get().strip(),
                stream_type=self.fields["stream_type"].get().strip(),
                source_id=self.fields["source_id"].get().strip(),
                discovery_seconds=float(self.fields["discovery_seconds"].get()),
                clear_after_send=self.clear_setting.get(),
                start_automatically=self.auto_setting.get(),
                shortcuts_enabled=self.shortcuts_enabled.get(),
                bindings=dict(self.settings.bindings))
            settings.save()
        except (ValueError, OSError) as exc:
            self.settings_status.set(f"Cannot save: {exc}")
            return
        changed = any(getattr(settings, key) != getattr(self.settings, key) for key in ("name", "stream_type", "source_id"))
        restart = changed and self.outlet is not None
        if restart:
            self.stop_outlet()
        self.settings = settings
        if restart:
            self.start_outlet()
        self.settings_status.set("Settings saved." + (" Reconnect your recorder." if restart else ""))

    def build_bindings(self, parent):
        frame = ttk.LabelFrame(parent, text="Bound markers — saved shortcuts", padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Checkbutton(frame, text="Enable marker shortcuts", variable=self.shortcuts_enabled,
                        command=self.save_shortcut_preference).pack(anchor="w")
        ttk.Label(frame, text="Active only while this app has focus. One marker per key press. Disable while editing if needed.", wraplength=370).pack(anchor="w", pady=4)
        self.binding_summary = tk.StringVar()
        ttk.Label(frame, textvariable=self.binding_summary, wraplength=370).pack(anchor="w", pady=(0, 4))
        self.binding_table = scrollable_table(frame, ("key", "marker"), height=6, selectmode="browse")
        self.binding_table.heading("key", text="Key")
        self.binding_table.heading("marker", text="Marker text")
        self.binding_table.column("key", width=115, stretch=False)
        self.binding_table.column("marker", width=200)
        self.binding_table.bind("<<TreeviewSelect>>", self.select_binding)
        editor = ttk.Frame(frame)
        editor.pack(fill="x", pady=6)
        self.binding_key = tk.StringVar(value="F1")
        self.binding_text = tk.StringVar()
        self.binding_combo = ttk.Combobox(editor, textvariable=self.binding_key, values=HOTKEYS,
                                         state="readonly", width=15, postcommand=self.prepare_dropdown)
        self.binding_combo.pack(side="left")
        ttk.Entry(editor, textvariable=self.binding_text, width=20).pack(side="left", fill="x", expand=True, padx=(6, 0))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Save binding", command=self.save_binding).pack(side="left")
        ttk.Button(buttons, text="Remove selected", command=self.remove_binding).pack(side="left", padx=6)
        self.binding_status = tk.StringVar(value="Choose a key and enter its marker text.")
        ttk.Label(frame, textvariable=self.binding_status, wraplength=370).pack(anchor="w", pady=(4, 0))
        self.refresh_bindings()

    def refresh_bindings(self):
        count = len(self.settings.bindings)
        self.binding_summary.set(f"{count} saved binding(s). Select a row to edit." if count else
                                 "No bindings saved. Choose a key and marker below, then click Save binding.")
        for item in self.binding_table.get_children():
            self.binding_table.delete(item)
        for key in HOTKEYS:
            if key in self.settings.bindings:
                self.binding_table.insert("", "end", iid=key, values=(key, self.settings.bindings[key]))

    def save_shortcut_preference(self):
        settings = replace(self.settings, shortcuts_enabled=self.shortcuts_enabled.get())
        try:
            settings.save()
        except (ValueError, OSError) as exc:
            self.binding_status.set(f"Shortcut toggle applies now but could not be saved: {exc}")
            return
        self.settings = settings
        self.binding_status.set("Shortcut preference saved.")

    def select_binding(self, event=None):
        selected = self.binding_table.selection()
        if selected:
            key = selected[0]
            self.binding_key.set(key)
            self.binding_text.set(self.settings.bindings[key])

    def persist_bindings(self, bindings):
        settings = replace(self.settings, bindings=bindings)
        try:
            settings.save()
        except (ValueError, OSError) as exc:
            self.binding_status.set(f"Cannot save: {exc}")
            return
        self.settings = settings
        self.refresh_bindings()
        key = self.binding_key.get()
        if key in bindings:
            self.binding_table.selection_set(key)
            self.binding_table.see(key)
        self.binding_status.set("Key bindings saved.")

    def save_binding(self):
        self.persist_bindings(self.settings.bindings | {self.binding_key.get(): self.binding_text.get()})

    def remove_binding(self):
        selected = self.binding_table.selection()
        if selected:
            bindings = dict(self.settings.bindings)
            del bindings[selected[0]]
            self.persist_bindings(bindings)

    def install_hotkeys(self, widget):
        widget.bindtags((self.hotkey_tag,) + widget.bindtags())
        for child in widget.winfo_children():
            self.install_hotkeys(child)

    def prepare_dropdown(self):
        # ttk creates its popup entirely in Tcl, without Python widget objects.
        # Include those widgets in shortcut handling before their class bindings.
        popup = self.root.tk.call("ttk::combobox::PopdownWindow", str(self.binding_combo))

        def install(path):
            tags = self.root.tk.splitlist(self.root.tk.call("bindtags", path))
            if self.hotkey_tag not in tags:
                self.root.tk.call("bindtags", path, (self.hotkey_tag,) + tags)
            for child in self.root.tk.splitlist(self.root.tk.call("winfo", "children", path)):
                install(child)

        install(popup)

    def key_press(self, event):
        # Keycode identifies the physical key even if modifiers change while held.
        code = event.keycode
        pending = self.release_jobs.pop(code, None)
        if pending is not None:
            self.root.after_cancel(pending)
        if code in self.pressed_keys:
            return "break"
        if not self.shortcuts_enabled.get():
            return
        key = event.keysym
        modifiers = event.state & (0x5 | self.alt_mask)  # Ignore lock-key state.
        if modifiers == 5:
            key = "Ctrl+Shift+" + key.upper()
        elif modifiers != 0:
            return
        text = self.settings.bindings.get(key)
        if text is not None:
            self.pressed_keys.add(code)
            self.send_marker(text=text, trigger=key)
            return "break"

    def key_release(self, event):
        code = event.keycode
        if code in self.pressed_keys:
            # Defer release so auto-repeat release/press pairs are suppressed.
            if code in self.release_jobs:
                self.root.after_cancel(self.release_jobs[code])
            self.release_jobs[code] = self.root.after_idle(lambda: self.release_key(code))
            return "break"

    def release_key(self, code):
        self.release_jobs.pop(code, None)
        self.pressed_keys.discard(code)

    def focus_out(self, event):
        self.root.after_idle(self.reset_keys_if_unfocused)

    def reset_keys_if_unfocused(self):
        # focus_displayof() tries to turn Tcl-only popup paths into Python widgets
        # and raises KeyError('popdown'). We only need to know whether focus exists.
        focused_path = self.root.tk.call("focus", "-displayof", self.root._w)
        if not focused_path:
            for job in self.release_jobs.values():
                self.root.after_cancel(job)
            self.release_jobs.clear()
            self.pressed_keys.clear()

    def close(self):
        self.root.after_cancel(self.poll_id)
        self.eeg_viewer.close()
        self.outlet = None
        self.root.destroy()


def main():
    root = tk.Tk()
    root.withdraw()
    try:
        import pylsl
        from eeg_viewer import EEGViewer  # Check plotting/filter dependencies before opening the app.
    except (ImportError, RuntimeError, OSError) as exc:
        messagebox.showerror("Dependency unavailable", f"Install dependencies with:\npython -m pip install -r requirements.txt\n\n{exc}", parent=root)
        root.destroy()
        return
    root.deiconify()
    MarkerApp(root, pylsl)
    root.mainloop()


if __name__ == "__main__":
    main()
