from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Optional

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")

from gi.repository import GdkPixbuf, GLib, Gtk

from .session import ConfigurationError, SessionConfig, StreamSession, describe_source
from .toolchain import ToolOverrideConfig, Toolchain


APP_TITLE = "dabstream2easydab"
CONFIG_PATH = Path.home() / ".config" / "dabstream2easydab" / "config.json"
STREAM_NAME_COLUMN = 0
STREAM_MODE_COLUMN = 1
STREAM_TYPE_COLUMN = 2
STREAM_URI_COLUMN = 3


DEFAULT_SETTINGS = {
    "source_mode": "auto",
    "output_mode": "tcp",
    "output_profile": "normal",
    "source_uri": "",
    "listen_host": "0.0.0.0",
    "listen_port": 18081,
    "edi2eti_path": "",
    "odr_edi2edi_path": "",
    "eti2zmq_path": "",
    "saved_streams": [],
}


AUTO_SOURCE_MODE = "auto"
LOGO_PATH = Path(__file__).resolve().parent / "assets" / "logo.svg"
FLOW_STALE_WARNING_SECONDS = 3.0
FLOW_STALE_OFFLINE_SECONDS = 8.0
CONNECTION_BUTTON_CSS = """
button.connection-toggle {
  background-image: none;
  background-color: #1677ff;
  border: 1px solid #0f5fd6;
  border-radius: 12px;
  color: #ffffff;
  font-weight: 700;
  font-size: 1.02em;
  padding: 6px 12px;
  box-shadow: 0 4px 12px rgba(22, 119, 255, 0.18);
}

button.connection-toggle:hover {
  background-color: #0f6ae6;
  box-shadow: 0 6px 14px rgba(22, 119, 255, 0.22);
}

button.connection-toggle:active {
  background-color: #0d5fcc;
  box-shadow: none;
}

button.connection-toggle.connected {
  background-color: #f97316;
  border: 1px solid #dd6b20;
  box-shadow: 0 4px 12px rgba(249, 115, 22, 0.18);
}

button.connection-toggle.connected:hover {
  background-color: #ea6a12;
  box-shadow: 0 6px 14px rgba(249, 115, 22, 0.22);
}

button.connection-toggle.connected:active {
  background-color: #d95f10;
  box-shadow: none;
}
"""


def load_settings() -> dict:
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(DEFAULT_SETTINGS)
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)
    merged = dict(DEFAULT_SETTINGS)
    merged.update(loaded)
    return merged


def save_settings(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def guess_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "LAN address to verify"


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title=APP_TITLE)
        self.set_default_size(860, 600)
        self.set_border_width(12)

        self.session: Optional[StreamSession] = None
        self._inputs_enabled = True
        self._guessed_ip = guess_lan_ip()
        self._toolchain = Toolchain.discover()
        self._apply_window_icon()

        self._build_ui()
        self._load_initial_settings()
        self._update_source_help()
        self._update_detected_type()
        self._update_output_help()
        self._update_easydab_hint()
        self._refresh_toolchain_status()

        GLib.timeout_add(500, self._refresh_status)
        self.connect("delete-event", self._on_delete_event)

    def _build_ui(self) -> None:
        outer_scrolled = Gtk.ScrolledWindow()
        outer_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        outer_scrolled.set_hexpand(True)
        outer_scrolled.set_vexpand(True)
        self.add(outer_scrolled)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_border_width(12)
        outer_scrolled.add(root)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        root.pack_start(header, False, False, 0)

        logo = self._create_logo_image()
        if logo is not None:
            header.pack_start(logo, False, False, 0)

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        header.pack_start(title_box, True, True, 0)

        title = Gtk.Label()
        title.set_markup(
            "<span size='x-large' weight='bold'>dabstream2easydab</span>"
        )
        title.set_xalign(0.0)
        title_box.pack_start(title, False, False, 0)

        subtitle = Gtk.Label(
            label=(
                "ETI relay and EDI -> ETI conversion to a local "
                "TCP or ZeroMQ output for EasyDABV2."
            )
        )
        subtitle.set_xalign(0.0)
        subtitle.set_line_wrap(True)
        title_box.pack_start(subtitle, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        root.pack_start(buttons, False, False, 0)

        self.connection_button = Gtk.Button(label="Connect")
        self.connection_button.set_size_request(116, -1)
        self.connection_button.connect("clicked", self._on_connection_button_clicked)
        buttons.pack_start(self.connection_button, False, False, 0)
        self._install_connection_button_style()
        self._update_connection_button()

        self.flow_status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.flow_status_box.set_margin_start(8)
        buttons.pack_start(self.flow_status_box, False, False, 0)

        self.flow_status_dot = Gtk.Label()
        self.flow_status_box.pack_start(self.flow_status_dot, False, False, 0)

        self.flow_status_label = Gtk.Label()
        self.flow_status_label.set_xalign(0.0)
        self.flow_status_box.pack_start(self.flow_status_label, False, False, 0)

        self._set_flow_status_indicator("offline")

        streams_frame = Gtk.Frame(label="Saved streams")
        root.pack_start(streams_frame, False, False, 0)

        streams_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        streams_frame.add(streams_box)

        streams_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        streams_box.pack_start(streams_header, False, False, 0)

        streams_help = Gtk.Label(
            label="Use `+` to add a new stream, `Edit` to change the selected one, then click `Connect`."
        )
        streams_help.set_xalign(0.0)
        streams_help.set_hexpand(True)
        streams_header.pack_start(streams_help, True, True, 0)

        self.add_stream_button = Gtk.Button(label="+")
        self.add_stream_button.connect("clicked", self._on_add_stream_clicked)
        streams_header.pack_start(self.add_stream_button, False, False, 0)

        self.edit_stream_button = Gtk.Button(label="Edit")
        self.edit_stream_button.set_sensitive(False)
        self.edit_stream_button.connect("clicked", self._on_edit_stream_clicked)
        streams_header.pack_start(self.edit_stream_button, False, False, 0)

        self.remove_stream_button = Gtk.Button(label="-")
        self.remove_stream_button.set_sensitive(False)
        self.remove_stream_button.connect("clicked", self._on_remove_stream_clicked)
        streams_header.pack_start(self.remove_stream_button, False, False, 0)

        self.saved_streams_store = Gtk.ListStore(str, str, str, str)
        self.saved_streams_view = Gtk.TreeView(model=self.saved_streams_store)
        self.saved_streams_view.set_headers_visible(True)
        selection = self.saved_streams_view.get_selection()
        selection.connect("changed", self._on_saved_stream_selection_changed)

        name_renderer = Gtk.CellRendererText()
        name_column = Gtk.TreeViewColumn("Name", name_renderer, text=STREAM_NAME_COLUMN)
        name_column.set_resizable(True)
        name_column.set_expand(False)
        self.saved_streams_view.append_column(name_column)

        type_renderer = Gtk.CellRendererText()
        type_column = Gtk.TreeViewColumn("Type", type_renderer, text=STREAM_TYPE_COLUMN)
        type_column.set_resizable(True)
        self.saved_streams_view.append_column(type_column)

        uri_renderer = Gtk.CellRendererText()
        uri_column = Gtk.TreeViewColumn("Stream", uri_renderer, text=STREAM_URI_COLUMN)
        uri_column.set_resizable(True)
        uri_column.set_expand(True)
        self.saved_streams_view.append_column(uri_column)

        streams_scrolled = Gtk.ScrolledWindow()
        streams_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        streams_scrolled.set_size_request(-1, 140)
        streams_scrolled.add(self.saved_streams_view)
        streams_box.pack_start(streams_scrolled, True, True, 0)

        status_frame = Gtk.Frame(label="Status")
        root.pack_start(status_frame, False, False, 0)

        status_grid = Gtk.Grid(column_spacing=12, row_spacing=6, margin=12)
        status_frame.add(status_grid)

        self.state_value = Gtk.Label(label="Stopped")
        self.state_value.set_xalign(0.0)
        self.clients_value = Gtk.Label(label="0")
        self.clients_value.set_xalign(0.0)
        self.bytes_value = Gtk.Label(label="0")
        self.bytes_value.set_xalign(0.0)
        self.recognized_type_value = Gtk.Label(label="-")
        self.recognized_type_value.set_xalign(0.0)
        self.error_value = Gtk.Label(label="-")
        self.error_value.set_xalign(0.0)
        self.error_value.set_line_wrap(True)

        status_grid.attach(Gtk.Label(label="Current state", xalign=0.0), 0, 0, 1, 1)
        status_grid.attach(self.state_value, 1, 0, 1, 1)
        status_grid.attach(Gtk.Label(label="EasyDAB clients", xalign=0.0), 0, 1, 1, 1)
        status_grid.attach(self.clients_value, 1, 1, 1, 1)
        status_grid.attach(Gtk.Label(label="Received ETI bytes", xalign=0.0), 0, 2, 1, 1)
        status_grid.attach(self.bytes_value, 1, 2, 1, 1)
        status_grid.attach(Gtk.Label(label="Recognized type", xalign=0.0), 0, 3, 1, 1)
        status_grid.attach(self.recognized_type_value, 1, 3, 1, 1)
        status_grid.attach(Gtk.Label(label="Last error", xalign=0.0), 0, 4, 1, 1)
        status_grid.attach(self.error_value, 1, 4, 1, 1)

        form_frame = Gtk.Frame(label="Configuration")
        root.pack_start(form_frame, False, False, 0)

        form_grid = Gtk.Grid(column_spacing=12, row_spacing=8, margin=12)
        form_frame.add(form_grid)

        self.output_combo = Gtk.ComboBoxText()
        self.output_combo.append("tcp", "Raw TCP")
        self.output_combo.append("zmq", "ZeroMQ")
        self.output_combo.connect("changed", self._on_output_changed)

        self.output_profile_combo = Gtk.ComboBoxText()
        self.output_profile_combo.append("normal", "Normal")
        self.output_profile_combo.append("stabilized", "Stabilized")
        self.output_profile_combo.connect("changed", self._on_output_changed)

        self.output_profile_help_label = Gtk.Label()
        self.output_profile_help_label.set_xalign(0.0)
        self.output_profile_help_label.set_line_wrap(True)

        self.source_entry = Gtk.Entry()
        self.source_entry.set_hexpand(True)
        self.source_entry.connect("changed", self._on_source_or_port_changed)

        self.source_help_label = Gtk.Label()
        self.source_help_label.set_xalign(0.0)

        self.detected_type_value = Gtk.Label(label="-")
        self.detected_type_value.set_xalign(0.0)

        self.listen_host_entry = Gtk.Entry()
        self.listen_host_entry.set_text("0.0.0.0")

        self.listen_port_spin = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.listen_port_spin.set_value(18081)
        self.listen_port_spin.connect("value-changed", self._on_source_or_port_changed)

        row = 0
        form_grid.attach(Gtk.Label(label="Output type", xalign=0.0), 0, row, 1, 1)
        form_grid.attach(self.output_combo, 1, row, 1, 1)
        row += 1
        form_grid.attach(Gtk.Label(label="Output mode", xalign=0.0), 0, row, 1, 1)
        form_grid.attach(self.output_profile_combo, 1, row, 1, 1)
        row += 1
        form_grid.attach(self.output_profile_help_label, 1, row, 1, 1)
        row += 1
        form_grid.attach(Gtk.Label(label="Stream address", xalign=0.0), 0, row, 1, 1)
        form_grid.attach(self.source_entry, 1, row, 1, 1)
        row += 1
        form_grid.attach(self.source_help_label, 1, row, 1, 1)
        row += 1
        form_grid.attach(Gtk.Label(label="Expected type", xalign=0.0), 0, row, 1, 1)
        form_grid.attach(self.detected_type_value, 1, row, 1, 1)
        row += 1
        form_grid.attach(Gtk.Label(label="Listen address", xalign=0.0), 0, row, 1, 1)
        form_grid.attach(self.listen_host_entry, 1, row, 1, 1)
        row += 1
        form_grid.attach(Gtk.Label(label="Listen port", xalign=0.0), 0, row, 1, 1)
        form_grid.attach(self.listen_port_spin, 1, row, 1, 1)

        tools_frame = Gtk.Frame(label="Integrated tools")
        root.pack_start(tools_frame, False, False, 0)

        tools_grid = Gtk.Grid(column_spacing=12, row_spacing=6, margin=12)
        tools_frame.add(tools_grid)

        tools_help = Gtk.Label(
            label=(
                "Leave empty for automatic detection. "
                "The application prefers tools/bin, then PATH."
            )
        )
        tools_help.set_xalign(0.0)
        tools_grid.attach(tools_help, 0, 0, 2, 1)

        self.edi2eti_path_entry = Gtk.Entry()
        self.edi2eti_path_entry.connect("changed", self._on_tool_paths_changed)
        self.odr_edi2edi_path_entry = Gtk.Entry()
        self.odr_edi2edi_path_entry.connect("changed", self._on_tool_paths_changed)
        self.eti2zmq_path_entry = Gtk.Entry()
        self.eti2zmq_path_entry.connect("changed", self._on_tool_paths_changed)

        self.edi2eti_status = Gtk.Label()
        self.edi2eti_status.set_xalign(0.0)
        self.edi2eti_status.set_line_wrap(True)
        self.odr_edi2edi_status = Gtk.Label()
        self.odr_edi2edi_status.set_xalign(0.0)
        self.odr_edi2edi_status.set_line_wrap(True)
        self.eti2zmq_status = Gtk.Label()
        self.eti2zmq_status.set_xalign(0.0)
        self.eti2zmq_status.set_line_wrap(True)

        tool_rows = [
            ("edi2eti", self.edi2eti_path_entry, self.edi2eti_status),
            ("odr-edi2edi", self.odr_edi2edi_path_entry, self.odr_edi2edi_status),
            ("eti2zmq", self.eti2zmq_path_entry, self.eti2zmq_status),
        ]
        row = 1
        for name, entry, status in tool_rows:
            tools_grid.attach(Gtk.Label(label=name, xalign=0.0), 0, row, 1, 1)
            tools_grid.attach(entry, 1, row, 1, 1)
            row += 1
            tools_grid.attach(status, 1, row, 1, 1)
            row += 1

        self.easydab_hint = Gtk.Label()
        self.easydab_hint.set_line_wrap(True)
        self.easydab_hint.set_xalign(0.0)
        root.pack_start(self.easydab_hint, False, False, 0)

        log_frame = Gtk.Frame(label="Log")
        root.pack_start(log_frame, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_size_request(-1, 180)
        log_frame.add(scrolled)

        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        scrolled.add(self.log_view)

        self.log_buffer = self.log_view.get_buffer()

    def _load_logo_pixbuf(self, size: int) -> Optional[GdkPixbuf.Pixbuf]:
        try:
            return GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(LOGO_PATH),
                width=size,
                height=size,
                preserve_aspect_ratio=True,
            )
        except Exception:
            return None

    def _create_logo_image(self) -> Optional[Gtk.Image]:
        pixbuf = self._load_logo_pixbuf(64)
        if pixbuf is None:
            return None
        image = Gtk.Image.new_from_pixbuf(pixbuf)
        image.set_halign(Gtk.Align.START)
        image.set_valign(Gtk.Align.START)
        return image

    def _apply_window_icon(self) -> None:
        if not LOGO_PATH.exists():
            return
        try:
            self.set_icon_from_file(str(LOGO_PATH))
        except Exception:
            pass

    def _load_initial_settings(self) -> None:
        settings = load_settings()
        self.output_combo.set_active_id(str(settings["output_mode"]))
        self.output_profile_combo.set_active_id(str(settings["output_profile"]))
        self.source_entry.set_text(str(settings["source_uri"]))
        self.listen_host_entry.set_text(str(settings["listen_host"]))
        self.listen_port_spin.set_value(int(settings["listen_port"]))
        self.edi2eti_path_entry.set_text(str(settings["edi2eti_path"]))
        self.odr_edi2edi_path_entry.set_text(str(settings["odr_edi2edi_path"]))
        self.eti2zmq_path_entry.set_text(str(settings["eti2zmq_path"]))
        self._load_saved_streams(settings["saved_streams"])
        self._select_saved_stream(
            AUTO_SOURCE_MODE,
            str(settings["source_uri"]),
        )

    def _serialize_settings(self) -> dict:
        return {
            "source_mode": AUTO_SOURCE_MODE,
            "output_mode": self.output_combo.get_active_id() or "tcp",
            "output_profile": self.output_profile_combo.get_active_id() or "normal",
            "source_uri": self.source_entry.get_text(),
            "listen_host": self.listen_host_entry.get_text(),
            "listen_port": self.listen_port_spin.get_value_as_int(),
            "edi2eti_path": self.edi2eti_path_entry.get_text(),
            "odr_edi2edi_path": self.odr_edi2edi_path_entry.get_text(),
            "eti2zmq_path": self.eti2zmq_path_entry.get_text(),
            "saved_streams": self._serialize_saved_streams(),
        }

    def _serialize_saved_streams(self) -> list[dict]:
        serialized: list[dict] = []
        for row in self.saved_streams_store:
            serialized.append(
                {
                    "name": row[STREAM_NAME_COLUMN],
                    "source_mode": AUTO_SOURCE_MODE,
                    "source_type": row[STREAM_TYPE_COLUMN],
                    "source_uri": row[STREAM_URI_COLUMN],
                }
            )
        return serialized

    def _load_saved_streams(self, saved_streams: list[dict]) -> None:
        self.saved_streams_store.clear()
        for item in saved_streams:
            name = str(item.get("name", "")).strip()
            source_mode = AUTO_SOURCE_MODE
            source_uri = str(item.get("source_uri", "")).strip()
            if not source_uri:
                continue
            source_type = str(item.get("source_type", "")).strip()
            if not source_type:
                try:
                    source_type = describe_source(source_mode, source_uri)
                except ConfigurationError:
                    source_type = "Invalid"
            if not name:
                name = source_uri
            self.saved_streams_store.append([name, source_mode, source_type, source_uri])

    def _save_settings_safely(self) -> None:
        try:
            save_settings(self._serialize_settings())
        except OSError as exc:
            self.append_log(f"Unable to save configuration: {exc}")

    def _on_delete_event(self, _window, _event):
        self._stop_session()
        self._save_settings_safely()
        return False

    def _on_output_changed(self, _combo) -> None:
        self._update_output_help()
        self._update_easydab_hint()

    def _on_source_or_port_changed(self, _widget) -> None:
        self._update_detected_type()
        self._update_easydab_hint()

    def _on_tool_paths_changed(self, _widget) -> None:
        self._refresh_toolchain_status()

    def _update_source_help(self) -> None:
        self.source_help_label.set_text(
            "Automatic detection: host:port, tcp://..., zmq+tcp://..., udp://... or http(s)://..."
        )
        self.source_entry.set_placeholder_text(
            "host:port, tcp://..., zmq+tcp://..., udp://... or http(s)://..."
        )

    def _update_detected_type(self) -> None:
        source_uri = self.source_entry.get_text().strip()
        if not source_uri:
            self.detected_type_value.set_text("-")
            return
        stored_type = self._saved_stream_type_for_uri(source_uri)
        if stored_type is not None:
            self.detected_type_value.set_text(stored_type)
            return
        if self.session is not None and self.session.config.source_uri == source_uri:
            recognized = self.session.snapshot().recognized_source_type
            if recognized:
                self.detected_type_value.set_text(recognized)
                return
        try:
            detected = describe_source(AUTO_SOURCE_MODE, source_uri)
        except ConfigurationError:
            detected = "Invalid"
        self.detected_type_value.set_text(detected)

    def _on_add_stream_clicked(self, _button) -> None:
        initial_uri = self._initial_uri_for_new_stream()
        added = self._ask_stream_details("", initial_uri, title="Add stream")
        if added is None:
            return
        stream_name, source_uri = added
        try:
            source_type = describe_source(AUTO_SOURCE_MODE, source_uri)
        except ConfigurationError as exc:
            self._show_error_dialog("Invalid stream", str(exc))
            return
        new_iter = self.saved_streams_store.append(
            [stream_name, AUTO_SOURCE_MODE, source_type, source_uri]
        )
        path = self.saved_streams_store.get_path(new_iter)
        self.saved_streams_view.get_selection().select_path(path)
        self.saved_streams_view.scroll_to_cell(path, None, False, 0.0, 0.0)
        self._save_settings_safely()
        self.append_log(f"Saved stream: {stream_name} ({source_type})")

    def _on_remove_stream_clicked(self, _button) -> None:
        selection = self.saved_streams_view.get_selection()
        model, tree_iter = selection.get_selected()
        if model is None or tree_iter is None:
            return
        stream_name = model.get_value(tree_iter, STREAM_NAME_COLUMN)
        source_uri = model.get_value(tree_iter, STREAM_URI_COLUMN)
        model.remove(tree_iter)
        self._save_settings_safely()
        self.append_log(f"Removed stream: {stream_name} ({source_uri})")

    def _on_edit_stream_clicked(self, _button) -> None:
        selection = self.saved_streams_view.get_selection()
        model, tree_iter = selection.get_selected()
        if model is None or tree_iter is None:
            return

        current_name = model.get_value(tree_iter, STREAM_NAME_COLUMN)
        current_uri = model.get_value(tree_iter, STREAM_URI_COLUMN)
        edited = self._ask_stream_details(current_name, current_uri, title="Edit stream")
        if edited is None:
            return

        stream_name, source_uri = edited
        try:
            source_type = describe_source(AUTO_SOURCE_MODE, source_uri)
        except ConfigurationError as exc:
            self._show_error_dialog("Invalid stream", str(exc))
            return

        model.set(
            tree_iter,
            STREAM_NAME_COLUMN,
            stream_name,
            STREAM_MODE_COLUMN,
            AUTO_SOURCE_MODE,
            STREAM_TYPE_COLUMN,
            source_type,
            STREAM_URI_COLUMN,
            source_uri,
        )
        self.source_entry.set_text(source_uri)
        self._update_detected_type()
        self._save_settings_safely()
        self.append_log(f"Edited stream: {stream_name} ({source_type})")

    def _on_saved_stream_selection_changed(self, selection) -> None:
        model, tree_iter = selection.get_selected()
        self._update_saved_stream_actions()
        if model is None or tree_iter is None:
            return
        source_uri = model.get_value(tree_iter, STREAM_URI_COLUMN)
        self.source_entry.set_text(source_uri)
        self._update_detected_type()

    def _find_saved_stream(self, source_mode: str, source_uri: str):
        tree_iter = self.saved_streams_store.get_iter_first()
        while tree_iter is not None:
            row_mode = self.saved_streams_store.get_value(tree_iter, STREAM_MODE_COLUMN)
            row_uri = self.saved_streams_store.get_value(tree_iter, STREAM_URI_COLUMN)
            if row_mode == source_mode and row_uri == source_uri:
                return tree_iter
            tree_iter = self.saved_streams_store.iter_next(tree_iter)
        return None

    def _saved_stream_type_for_uri(self, source_uri: str) -> Optional[str]:
        tree_iter = self._find_saved_stream(AUTO_SOURCE_MODE, source_uri)
        if tree_iter is None:
            return None
        return self.saved_streams_store.get_value(tree_iter, STREAM_TYPE_COLUMN)

    def _update_saved_stream_type(self, source_uri: str, source_type: str) -> None:
        updated = False
        tree_iter = self.saved_streams_store.get_iter_first()
        while tree_iter is not None:
            row_mode = self.saved_streams_store.get_value(tree_iter, STREAM_MODE_COLUMN)
            row_uri = self.saved_streams_store.get_value(tree_iter, STREAM_URI_COLUMN)
            if row_mode == AUTO_SOURCE_MODE and row_uri == source_uri:
                current_type = self.saved_streams_store.get_value(tree_iter, STREAM_TYPE_COLUMN)
                if current_type != source_type:
                    self.saved_streams_store.set_value(tree_iter, STREAM_TYPE_COLUMN, source_type)
                    updated = True
            tree_iter = self.saved_streams_store.iter_next(tree_iter)
        if updated:
            if self.source_entry.get_text().strip() == source_uri:
                self.detected_type_value.set_text(source_type)
            self._save_settings_safely()

    def _select_saved_stream(self, source_mode: str, source_uri: str) -> None:
        tree_iter = self._find_saved_stream(source_mode, source_uri)
        if tree_iter is None:
            return
        path = self.saved_streams_store.get_path(tree_iter)
        self.saved_streams_view.get_selection().select_path(path)
        self.saved_streams_view.scroll_to_cell(path, None, False, 0.0, 0.0)

    def _initial_uri_for_new_stream(self) -> str:
        source_uri = self.source_entry.get_text().strip()
        selected_uri = self._selected_saved_stream_uri()
        if selected_uri is not None and source_uri == selected_uri:
            return ""
        return source_uri

    def _selected_saved_stream_uri(self) -> Optional[str]:
        selection = self.saved_streams_view.get_selection()
        model, tree_iter = selection.get_selected()
        if model is None or tree_iter is None:
            return None
        return model.get_value(tree_iter, STREAM_URI_COLUMN)

    def _update_saved_stream_actions(self) -> None:
        selection = self.saved_streams_view.get_selection()
        model, tree_iter = selection.get_selected()
        has_selection = model is not None and tree_iter is not None
        self.saved_streams_view.set_sensitive(self._inputs_enabled)
        self.add_stream_button.set_sensitive(self._inputs_enabled)
        self.edit_stream_button.set_sensitive(self._inputs_enabled and has_selection)
        self.remove_stream_button.set_sensitive(self._inputs_enabled and has_selection)

    def _ask_stream_details(
        self,
        initial_name: str,
        initial_uri: str,
        *,
        title: str,
        address_visible: bool = True,
    ) -> Optional[tuple[str, str]]:
        dialog = Gtk.Dialog(
            title=title,
            transient_for=self,
            flags=0,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)

        content = dialog.get_content_area()
        content.set_spacing(8)

        label = Gtk.Label(label="Name to save for this stream:")
        label.set_xalign(0.0)
        content.pack_start(label, False, False, 0)

        name_entry = Gtk.Entry()
        name_entry.set_text(initial_name)
        name_entry.set_activates_default(True)
        content.pack_start(name_entry, False, False, 0)

        if address_visible:
            uri_label = Gtk.Label(label="Stream address:")
            uri_label.set_xalign(0.0)
            content.pack_start(uri_label, False, False, 0)

            uri_entry = Gtk.Entry()
            uri_entry.set_text(initial_uri)
            uri_entry.set_activates_default(True)
            content.pack_start(uri_entry, False, False, 0)
        else:
            uri_entry = None

        dialog.show_all()
        response = dialog.run()
        name_value = name_entry.get_text().strip()
        uri_value = initial_uri if uri_entry is None else uri_entry.get_text().strip()
        dialog.destroy()

        if response != Gtk.ResponseType.OK:
            return None
        if not name_value:
            self._show_error_dialog("Empty name", "Enter a name before saving this stream.")
            return None
        if address_visible and not uri_value:
            self._show_error_dialog("Empty stream", "Enter a stream address before saving this stream.")
            return None
        return name_value, uri_value

    def _update_output_help(self) -> None:
        output_mode = self.output_combo.get_active_id() or "tcp"
        output_profile = self.output_profile_combo.get_active_id() or "normal"
        if output_mode == "tcp":
            self.listen_host_entry.set_placeholder_text("0.0.0.0")
        else:
            self.listen_host_entry.set_placeholder_text("* or 0.0.0.0")
        if output_profile == "stabilized":
            self.output_profile_help_label.set_text(
                "Stabilized adds a prebuffer and smoother ETI output for sensitive EasyDAB or clone receivers."
            )
        else:
            self.output_profile_help_label.set_text(
                "Normal forwards the ETI stream directly with the lowest added delay."
            )

    def _update_easydab_hint(self) -> None:
        port = self.listen_port_spin.get_value_as_int()
        output_mode = self.output_combo.get_active_id() or "tcp"
        output_profile = self.output_profile_combo.get_active_id() or "normal"
        if output_mode == "tcp":
            text = (
                "EasyDABV2: set the device to TCP client mode, then enter "
                f"Remote IP = {self._guessed_ip} and Remote PORT = {port}."
            )
        else:
            text = (
                "Local output exposed in ZeroMQ on "
                f"zmq+tcp://*:{port}. On the EasyDAB side, keep your usual "
                f"ZeroMQ mode and point it to {self._guessed_ip}:{port}."
            )
        if output_profile == "stabilized":
            text += " Stabilized mode adds a software prebuffer before forwarding the ETI stream."
        self.easydab_hint.set_text(text)

    def _set_inputs_sensitive(self, enabled: bool) -> None:
        self._inputs_enabled = enabled
        self.output_combo.set_sensitive(enabled)
        self.output_profile_combo.set_sensitive(enabled)
        self.source_entry.set_sensitive(enabled)
        self.listen_host_entry.set_sensitive(enabled)
        self.listen_port_spin.set_sensitive(enabled)
        self.edi2eti_path_entry.set_sensitive(enabled)
        self.odr_edi2edi_path_entry.set_sensitive(enabled)
        self.eti2zmq_path_entry.set_sensitive(enabled)
        self._update_saved_stream_actions()
        self._update_connection_button()

    def _install_connection_button_style(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(CONNECTION_BUTTON_CSS.encode("utf-8"))
        style_context = self.connection_button.get_style_context()
        style_context.add_provider(
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        style_context.add_class("connection-toggle")

    def _update_connection_button(self) -> None:
        if self.session is None:
            label = "Connect"
            tooltip = "Start the relay and connect to the selected stream"
            connected = False
        else:
            label = "Disconnect"
            tooltip = "Stop the current relay session"
            connected = True

        self.connection_button.set_label(label)
        self.connection_button.set_tooltip_text(tooltip)
        self.connection_button.set_sensitive(True)

        style_context = self.connection_button.get_style_context()
        if connected:
            style_context.add_class("connected")
        else:
            style_context.remove_class("connected")

    def _set_flow_status_indicator(self, status: str, detail: str = "") -> None:
        if status == "online":
            color = "#2f9e44"
            label = "Online"
        elif status == "connecting":
            color = "#d97706"
            label = "Connecting"
        else:
            color = "#dc2626"
            label = "Offline"

        self.flow_status_dot.set_markup(
            f"<span foreground='{color}' size='x-large' weight='bold'>●</span>"
        )
        self.flow_status_label.set_text(label)

        tooltip = detail or label
        self.flow_status_box.set_tooltip_text(tooltip)
        self.flow_status_dot.set_tooltip_text(tooltip)
        self.flow_status_label.set_tooltip_text(tooltip)

    def _flow_status_from_stats(self, stats) -> tuple[str, str]:
        state = stats.state or ""
        lowered = state.lower()
        detail = stats.last_error or state

        if self.session is None:
            return "offline", "Session stopped"
        if any(keyword in lowered for keyword in ("prebuffer", "rebuffer")):
            return "connecting", state or "Stabilizing output"
        if any(keyword in lowered for keyword in ("retrying", "error", "stopped")):
            return "offline", detail or "Stream offline"
        if any(keyword in lowered for keyword in ("connecting", "starting", "waiting", "switching", "ready")):
            return "connecting", state or "Connecting"
        if stats.recognized_source_type or stats.bytes_from_source > 0:
            if stats.last_data_at > 0:
                stale_for = time.monotonic() - stats.last_data_at
                if stale_for >= FLOW_STALE_OFFLINE_SECONDS:
                    return "offline", f"No incoming data for {stale_for:.0f}s"
                if stale_for >= FLOW_STALE_WARNING_SECONDS:
                    return "connecting", f"No incoming data for {stale_for:.0f}s"
            return "online", state or "Stream active"
        return "offline", detail or "Stream offline"

    def _current_tool_overrides(self) -> ToolOverrideConfig:
        return ToolOverrideConfig(
            edi2eti_path=self.edi2eti_path_entry.get_text().strip(),
            odr_edi2edi_path=self.odr_edi2edi_path_entry.get_text().strip(),
            eti2zmq_path=self.eti2zmq_path_entry.get_text().strip(),
        )

    def _refresh_toolchain_status(self) -> None:
        self._toolchain = Toolchain.discover(self._current_tool_overrides())
        self._set_tool_status_label(self.edi2eti_status, self._toolchain.edi2eti)
        self._set_tool_status_label(
            self.odr_edi2edi_status,
            self._toolchain.odr_edi2edi,
        )
        self._set_tool_status_label(self.eti2zmq_status, self._toolchain.eti2zmq)

    def _set_tool_status_label(self, label: Gtk.Label, tool_info) -> None:
        if tool_info.available:
            markup = (
                f"{GLib.markup_escape_text(tool_info.display_status)} "
                "<span foreground='#2f9e44' weight='bold'>OK</span>"
            )
            label.set_markup(markup)
            label.set_tooltip_text(tool_info.path)
            return

        label.set_text(tool_info.display_status)
        label.set_tooltip_text(tool_info.display_status)

    def _on_connection_button_clicked(self, _button) -> None:
        if self.session is None:
            self._on_start_clicked()
            return
        self._stop_session()

    def _on_start_clicked(self) -> None:
        if self.session is not None:
            return
        self._refresh_toolchain_status()
        config = SessionConfig(
            source_mode=AUTO_SOURCE_MODE,
            output_mode=self.output_combo.get_active_id() or "tcp",
            source_uri=self.source_entry.get_text().strip(),
            listen_host=self.listen_host_entry.get_text().strip() or "0.0.0.0",
            listen_port=self.listen_port_spin.get_value_as_int(),
            output_profile=self.output_profile_combo.get_active_id() or "normal",
        )
        try:
            session = StreamSession(
                config=config,
                logger=self.append_log,
                toolchain=self._toolchain,
            )
        except ConfigurationError as exc:
            self._show_error_dialog("Invalid configuration", str(exc))
            return
        self.session = session
        try:
            self.session.start()
        except Exception as exc:
            try:
                self.session.stop()
            except Exception:
                pass
            self.session = None
            self._show_error_dialog("Unable to start the session", str(exc))
            return
        self._set_inputs_sensitive(False)
        self._update_connection_button()
        self._set_flow_status_indicator("connecting", "Connecting to stream")
        self.append_log("Session started")
        self._update_easydab_hint()

    def _stop_session(self) -> None:
        if self.session is None:
            return
        self.session.stop()
        self.session = None
        self._set_inputs_sensitive(True)
        self._update_connection_button()
        self._set_flow_status_indicator("offline", "Session stopped")
        self.append_log("Session stopped")

    def _show_error_dialog(self, title: str, secondary_text: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(secondary_text)
        dialog.run()
        dialog.destroy()

    def _refresh_status(self) -> bool:
        if self.session is None:
            self.state_value.set_text("Stopped")
            if (self.output_combo.get_active_id() or "tcp") == "zmq":
                self.clients_value.set_text("n/a")
            else:
                self.clients_value.set_text("0")
            self.bytes_value.set_text("0")
            self.recognized_type_value.set_text("-")
            self.error_value.set_text("-")
            self._update_connection_button()
            self._set_flow_status_indicator("offline", "Session stopped")
            return True

        stats = self.session.snapshot()
        self._update_connection_button()
        self.state_value.set_text(stats.state)
        if self.session.output_mode == "zmq":
            self.clients_value.set_text("n/a")
        else:
            self.clients_value.set_text(str(stats.client_count))
        self.bytes_value.set_text(str(stats.bytes_from_source))
        if stats.recognized_source_type:
            self._update_saved_stream_type(
                self.session.config.source_uri,
                stats.recognized_source_type,
            )
            self.detected_type_value.set_text(stats.recognized_source_type)
        else:
            self._update_detected_type()
        self.recognized_type_value.set_text(
            stats.recognized_source_type or "Waiting for data..."
        )
        self.error_value.set_text(stats.last_error or "-")
        indicator_state, indicator_detail = self._flow_status_from_stats(stats)
        self._set_flow_status_indicator(indicator_state, indicator_detail)
        return True

    def append_log(self, message: str) -> None:
        GLib.idle_add(self._append_log_idle, message)

    def _append_log_idle(self, message: str) -> bool:
        timestamp = time.strftime("%H:%M:%S")
        end_iter = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end_iter, f"[{timestamp}] {message}\n")
        self.log_view.scroll_to_iter(self.log_buffer.get_end_iter(), 0.0, False, 0.0, 1.0)
        return False


class DABStreamApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.openai.dabstream2easydab")

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = MainWindow(self)
        window.show_all()
        window.present()
