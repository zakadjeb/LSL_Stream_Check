import time
import threading
import queue
import tkinter as tk
from tkinter.scrolledtext import ScrolledText
from pylsl import resolve_streams, StreamInlet


# -----------------------------
# Settings
# -----------------------------
CHECK_INTERVAL = 0.5
RESOLVE_TIMEOUT = 0.2
FORCED_RESOLVE_TIMEOUT = 1.0
MARKER_PULL_TIMEOUT = 0.0


class LSLMonitorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("LSL Stream and Marker Monitor")
        self.root.geometry("1050x800")

        self.stop_event = threading.Event()
        self.monitor_thread = None

        self.marker_queue = queue.Queue()
        self.stream_update_queue = queue.Queue()

        self.force_update_requested = threading.Event()

        self.selected_lock = threading.Lock()
        self.selected_identity = None
        self.selected_display_name = None

        self.current_stream_records = []

        # -----------------------------
        # Buttons
        # -----------------------------
        button_frame = tk.Frame(root)
        button_frame.pack(fill=tk.X, padx=10, pady=10)

        self.start_button = tk.Button(
            button_frame,
            text="Start monitoring",
            command=self.start_monitoring,
            height=2,
            width=20
        )
        self.start_button.pack(side=tk.LEFT, padx=5)

        self.cancel_button = tk.Button(
            button_frame,
            text="Cancel / Stop",
            command=self.stop_monitoring,
            height=2,
            width=20,
            state=tk.DISABLED
        )
        self.cancel_button.pack(side=tk.LEFT, padx=5)

        self.force_update_button = tk.Button(
            button_frame,
            text="Force stream update",
            command=self.force_stream_update,
            height=2,
            width=20
        )
        self.force_update_button.pack(side=tk.LEFT, padx=5)

        # -----------------------------
        # Marker area
        # -----------------------------
        marker_frame = tk.LabelFrame(root, text="Markers / samples from selected stream")
        marker_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.marker_box = ScrolledText(marker_frame, wrap=tk.WORD, height=15)
        self.marker_box.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # -----------------------------
        # Stream selection area
        # -----------------------------
        stream_frame = tk.LabelFrame(root, text="Discovered LSL streams — click one to monitor")
        stream_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Status row
        status_frame = tk.Frame(stream_frame)
        status_frame.pack(fill=tk.X, padx=5, pady=(5, 0))

        tk.Label(status_frame, text="Selected stream connection:").pack(side=tk.LEFT)

        self.status_canvas = tk.Canvas(
            status_frame,
            width=18,
            height=18,
            highlightthickness=0
        )
        self.status_canvas.pack(side=tk.LEFT, padx=6)

        self.status_dot = self.status_canvas.create_oval(
            3, 3, 15, 15,
            fill="grey",
            outline="black"
        )

        self.status_label = tk.Label(status_frame, text="No stream selected")
        self.status_label.pack(side=tk.LEFT)

        # Stream list and details
        stream_content_frame = tk.Frame(stream_frame)
        stream_content_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.stream_listbox = tk.Listbox(
            stream_content_frame,
            height=12,
            exportselection=False
        )
        self.stream_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.stream_listbox.bind("<<ListboxSelect>>", self.on_stream_selected)

        list_scrollbar = tk.Scrollbar(
            stream_content_frame,
            orient=tk.VERTICAL,
            command=self.stream_listbox.yview
        )
        list_scrollbar.pack(side=tk.LEFT, fill=tk.Y)
        self.stream_listbox.config(yscrollcommand=list_scrollbar.set)

        self.stream_details_box = ScrolledText(
            stream_content_frame,
            wrap=tk.WORD,
            width=55,
            height=12
        )
        self.stream_details_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))

        self.root.after(100, self.process_marker_queue)
        self.root.after(100, self.process_stream_update_queue)

    # -----------------------------
    # Utility
    # -----------------------------
    def timestamp(self):
        return time.strftime("%H:%M:%S")

    def log_marker(self, message):
        self.marker_queue.put(f"[{self.timestamp()}] {message}")

    def queue_stream_update(self, records, status_text, status_color, status_label):
        self.stream_update_queue.put(
            (records, status_text, status_color, status_label)
        )

    def make_record(self, stream):
        return {
            "uid": stream.uid(),
            "name": stream.name(),
            "type": stream.type(),
            "channels": stream.channel_count(),
            "source_id": stream.source_id(),
            "hostname": stream.hostname()
        }

    def record_label(self, record):
        return (
            f"{record['name']} | "
            f"type={record['type']} | "
            f"channels={record['channels']} | "
            f"source={record['source_id']}"
        )

    def identity_matches_stream(self, identity, stream):
        if identity is None:
            return False

        if stream.uid() == identity["uid"]:
            return True

        return (
            stream.name() == identity["name"]
            and stream.type() == identity["type"]
            and stream.source_id() == identity["source_id"]
        )

    # -----------------------------
    # GUI queue processing
    # -----------------------------
    def process_marker_queue(self):
        while not self.marker_queue.empty():
            message = self.marker_queue.get()
            self.marker_box.insert(tk.END, message + "\n")
            self.marker_box.see(tk.END)

        self.root.after(100, self.process_marker_queue)

    def process_stream_update_queue(self):
        latest = None

        while not self.stream_update_queue.empty():
            latest = self.stream_update_queue.get()

        if latest is not None:
            records, status_text, status_color, status_label = latest
            self.update_stream_listbox(records)
            self.update_stream_details(status_text)
            self.update_status_indicator(status_color, status_label)

        self.root.after(100, self.process_stream_update_queue)

    def update_status_indicator(self, color, label):
        self.status_canvas.itemconfig(self.status_dot, fill=color)
        self.status_label.config(text=label)

    def update_stream_listbox(self, records):
        self.current_stream_records = records

        with self.selected_lock:
            selected_identity = self.selected_identity

        previous_selection_index = None

        for i, record in enumerate(records):
            if selected_identity is not None:
                same_uid = record["uid"] == selected_identity["uid"]
                same_signature = (
                    record["name"] == selected_identity["name"]
                    and record["type"] == selected_identity["type"]
                    and record["source_id"] == selected_identity["source_id"]
                )

                if same_uid or same_signature:
                    previous_selection_index = i
                    break

        self.stream_listbox.delete(0, tk.END)

        for record in records:
            self.stream_listbox.insert(tk.END, self.record_label(record))

        if previous_selection_index is not None:
            self.stream_listbox.selection_set(previous_selection_index)
            self.stream_listbox.activate(previous_selection_index)

    def update_stream_details(self, status_text):
        self.stream_details_box.delete("1.0", tk.END)
        self.stream_details_box.insert(tk.END, status_text)

    # -----------------------------
    # Button actions
    # -----------------------------
    def start_monitoring(self):
        if self.monitor_thread is not None and self.monitor_thread.is_alive():
            return

        self.stop_event.clear()

        self.start_button.config(state=tk.DISABLED)
        self.cancel_button.config(state=tk.NORMAL)

        self.log_marker("Monitoring started. Select a stream below.")

        self.monitor_thread = threading.Thread(
            target=self.monitor_lsl,
            daemon=True
        )
        self.monitor_thread.start()

    def stop_monitoring(self):
        self.stop_event.set()

        self.start_button.config(state=tk.NORMAL)
        self.cancel_button.config(state=tk.DISABLED)

        self.log_marker("Monitoring cancelled by user.")

    def force_stream_update(self):
        self.force_update_requested.set()
        self.log_marker("Forced LSL stream update requested.")

    def on_stream_selected(self, event):
        selection = self.stream_listbox.curselection()

        if not selection:
            return

        index = selection[0]

        if index >= len(self.current_stream_records):
            return

        record = self.current_stream_records[index]

        identity = {
            "uid": record["uid"],
            "name": record["name"],
            "type": record["type"],
            "source_id": record["source_id"]
        }

        with self.selected_lock:
            self.selected_identity = identity
            self.selected_display_name = self.record_label(record)

        self.log_marker("")
        self.log_marker(f"Selected stream: {self.selected_display_name}")
        self.log_marker("Now showing markers/samples only from this stream.")

    # -----------------------------
    # LSL logic
    # -----------------------------
    def discover_streams(self, forced=False):
        timeout = FORCED_RESOLVE_TIMEOUT if forced else RESOLVE_TIMEOUT
        return resolve_streams(wait_time=timeout)

    def build_status_text(
        self,
        records,
        selected_identity,
        selected_display_name,
        connected,
        disconnect_start
    ):
        lines = []

        lines.append(f"Last updated: {self.timestamp()}")
        lines.append(f"Number of discovered streams: {len(records)}")
        lines.append("")

        if selected_identity is None:
            lines.append("Selected stream: NONE")
            lines.append("Click a stream in the list to monitor its markers/samples.")
        else:
            lines.append(f"Selected stream: {selected_display_name}")

            if connected:
                lines.append("Selected stream status: CONNECTED")
            else:
                if disconnect_start is None:
                    lines.append("Selected stream status: WAITING")
                else:
                    duration = time.time() - disconnect_start
                    lines.append(
                        f"Selected stream status: DISCONNECTED for {duration:.2f} seconds"
                    )

        lines.append("")
        lines.append("-" * 70)
        lines.append("")

        if not records:
            lines.append("No LSL streams currently discovered.")
            return "\n".join(lines)

        for i, record in enumerate(records):
            selected_marker = ""

            if selected_identity is not None:
                same_uid = record["uid"] == selected_identity["uid"]
                same_signature = (
                    record["name"] == selected_identity["name"]
                    and record["type"] == selected_identity["type"]
                    and record["source_id"] == selected_identity["source_id"]
                )

                if same_uid or same_signature:
                    selected_marker = "  <-- SELECTED"

            lines.append(f"Stream {i + 1}{selected_marker}")
            lines.append(f"  Name:      {record['name']}")
            lines.append(f"  Type:      {record['type']}")
            lines.append(f"  Channels:  {record['channels']}")
            lines.append(f"  Source ID: {record['source_id']}")
            lines.append(f"  UID:       {record['uid']}")
            lines.append(f"  Hostname:  {record['hostname']}")
            lines.append("")

        return "\n".join(lines)

    def monitor_lsl(self):
        connected = False
        disconnect_start = None

        inlet = None
        active_uid = None

        while not self.stop_event.is_set():
            try:
                forced = self.force_update_requested.is_set()

                if forced:
                    self.force_update_requested.clear()

                streams = self.discover_streams(forced=forced)
                records = [self.make_record(stream) for stream in streams]

                with self.selected_lock:
                    selected_identity = self.selected_identity
                    selected_display_name = self.selected_display_name

                selected_stream = None

                if selected_identity is not None:
                    for stream in streams:
                        if self.identity_matches_stream(selected_identity, stream):
                            selected_stream = stream
                            break

                # ---------------------------------------------
                # Selected-stream connection status
                # ---------------------------------------------
                if selected_identity is None:
                    connected = False
                    disconnect_start = None
                    inlet = None
                    active_uid = None
                    status_color = "grey"
                    status_label = "No stream selected"

                elif selected_stream is not None:
                    selected_uid = selected_stream.uid()

                    status_color = "green"
                    status_label = "Connected"

                    if not connected:
                        connected = True

                        if disconnect_start is not None:
                            duration = time.time() - disconnect_start
                            self.log_marker(
                                f"Selected stream reconnected after {duration:.2f} seconds."
                            )
                            disconnect_start = None

                        inlet = StreamInlet(selected_stream)
                        active_uid = selected_uid

                    elif selected_uid != active_uid:
                        self.log_marker(
                            "Selected stream instance changed. Reconnecting inlet."
                        )
                        inlet = StreamInlet(selected_stream)
                        active_uid = selected_uid

                else:
                    status_color = "red"
                    status_label = "Disconnected"

                    if connected:
                        connected = False
                        disconnect_start = time.time()
                        inlet = None
                        active_uid = None
                        self.log_marker("Selected stream disappeared.")

                    elif disconnect_start is None:
                        disconnect_start = time.time()

                # ---------------------------------------------
                # Update GUI stream area and status dot
                # ---------------------------------------------
                status_text = self.build_status_text(
                    records=records,
                    selected_identity=selected_identity,
                    selected_display_name=selected_display_name,
                    connected=connected,
                    disconnect_start=disconnect_start
                )

                self.queue_stream_update(
                    records=records,
                    status_text=status_text,
                    status_color=status_color,
                    status_label=status_label
                )

                # ---------------------------------------------
                # Pull samples only from selected stream
                # ---------------------------------------------
                if inlet is not None:
                    while True:
                        sample, lsl_timestamp = inlet.pull_sample(
                            timeout=MARKER_PULL_TIMEOUT
                        )

                        if sample is None:
                            break

                        self.log_marker(
                            f"Marker at LSL time {lsl_timestamp:.5f}: {sample}"
                        )

                time.sleep(CHECK_INTERVAL)

            except Exception as e:
                self.log_marker(f"ERROR: {e}")

                if connected:
                    connected = False
                    disconnect_start = time.time()
                    self.log_marker("Disconnected due to error.")

                inlet = None
                active_uid = None

                self.queue_stream_update(
                    records=[],
                    status_text=f"Error: {e}",
                    status_color="red",
                    status_label="Error / disconnected"
                )

                time.sleep(CHECK_INTERVAL)

        self.queue_stream_update(
            records=[],
            status_text="Monitoring stopped.",
            status_color="grey",
            status_label="Stopped"
        )
        self.log_marker("Monitoring stopped.")


if __name__ == "__main__":
    root = tk.Tk()
    app = LSLMonitorApp(root)
    root.mainloop()