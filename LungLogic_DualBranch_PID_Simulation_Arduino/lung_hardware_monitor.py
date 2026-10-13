"""Configure, run, monitor and log the NUCLEO-G474RE LungLogic controller."""

from __future__ import annotations

import argparse
import csv
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import matplotlib.pyplot as plt
try:
    import serial
    from serial.tools import list_ports
except ModuleNotFoundError:  # Allows --dry-run before requirements are installed.
    serial = None
    list_ports = None

from lung_model import STATE_NAMES, ExperimentConfig, load_config


BAUD = 115200
PROTOCOL_VERSION = 2
READY = "READY,LUNGLOGIC_G474,2"


@dataclass
class Telemetry:
    run_id: int
    time_ms: int
    state: int
    cycle: int
    q_left: float
    q_right: float
    v_left: float
    v_right: float
    qref_left: float
    qref_right: float
    opening_left: float
    opening_right: float
    command_left: float
    command_right: float
    valve_mask: int

    @classmethod
    def parse(cls, line: str) -> "Telemetry":
        fields = line.strip().split(",")
        if len(fields) != 16 or fields[0] != "TEL":
            raise ValueError("not telemetry")
        return cls(
            int(fields[1]), int(fields[2]), int(fields[3]), int(fields[4]),
            *[float(v) for v in fields[5:15]], int(fields[15]),
        )


def choose_port(explicit: Optional[str]) -> str:
    if list_ports is None:
        raise RuntimeError("pyserial is not installed; run: python -m pip install -r requirements.txt")
    if explicit:
        return explicit
    ports = list(list_ports.comports())
    likely = [p for p in ports if any(tag in (p.description or "").lower()
              for tag in ("stlink", "st-link", "stmicroelectronics", "virtual com"))]
    if len(likely) == 1:
        return likely[0].device
    if len(ports) == 1:
        return ports[0].device
    choices = ", ".join(f"{p.device} ({p.description})" for p in ports) or "none"
    raise RuntimeError(f"Specify --port. Detected ports: {choices}")


def config_command(cfg: ExperimentConfig, token: int) -> str:
    return ",".join(map(str, [
        "CFG", PROTOCOL_VERSION, token, cfg.respiratory_rate_bpm,
        cfg.ie_inspiration, cfg.ie_expiration, cfg.left_target_ml, cfg.right_target_ml,
        cfg.target_fill_time_s, int(round(cfg.dead_time_s * 1000.0)), cfg.pi.kp,
        cfg.pi.ki, cfg.pi.kd, cfg.pi.integral_limit, cfg.precondition_s, cfg.max_abs_flow_slm,
        cfg.max_branch_volume_ml, cfg.left.flow_sign, cfg.right.flow_sign,
        cfg.left.capacity_ml, cfg.right.capacity_ml, cfg.left.tau_in_s, cfg.right.tau_in_s,
        cfg.left.tau_out_s, cfg.right.tau_out_s,
        0.5 * (cfg.left.valve_exponent + cfg.right.valve_exponent),
    ]))


class SerialWorker(threading.Thread):
    def __init__(self, connection: serial.Serial, messages: queue.Queue[str]) -> None:
        super().__init__(daemon=True)
        self.connection = connection
        self.messages = messages
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            raw = self.connection.readline()
            if raw:
                self.messages.put(raw.decode("ascii", errors="replace").strip())


def wait_for(messages: queue.Queue[str], prefix: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = messages.get(timeout=0.1)
        except queue.Empty:
            continue
        print(f"< {line}")
        if line.startswith(prefix):
            return line
        if line.startswith("ERR,"):
            raise RuntimeError(line)
    raise TimeoutError(f"Timed out waiting for {prefix}")


def save_log(rows: List[Telemetry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "run_id", "time_s", "state", "state_name", "cycle", "left_flow_slm",
            "right_flow_slm", "left_volume_ml", "right_volume_ml", "left_flow_reference_slm",
            "right_flow_reference_slm", "left_opening", "right_opening", "left_command_v",
            "right_command_v", "valve_mask",
        ])
        for r in rows:
            writer.writerow([
                r.run_id, r.time_ms / 1000.0, r.state, STATE_NAMES.get(r.state, "UNKNOWN"), r.cycle,
                r.q_left, r.q_right, r.v_left, r.v_right, r.qref_left, r.qref_right,
                r.opening_left, r.opening_right, r.command_left, r.command_right, r.valve_mask,
            ])


def plot_log(rows: List[Telemetry], path: Path, show: bool) -> None:
    if not rows:
        return
    t = [r.time_ms / 1000.0 for r in rows]
    fig, axes = plt.subplots(3, 1, figsize=(11, 8), sharex=True, constrained_layout=True)
    axes[0].plot(t, [r.v_left for r in rows], label="Left")
    axes[0].plot(t, [r.v_right for r in rows], label="Right")
    axes[0].set_ylabel("Volume / mL")
    axes[1].plot(t, [r.q_left for r in rows], label="Left")
    axes[1].plot(t, [r.q_right for r in rows], label="Right")
    axes[1].plot(t, [r.qref_left for r in rows], "k--", label="Reference")
    axes[1].set_ylabel("Flow / SLM")
    axes[2].plot(t, [r.opening_left for r in rows], label="Left")
    axes[2].plot(t, [r.opening_right for r in rows], label="Right")
    axes[2].set_ylabel("Opening")
    axes[2].set_xlabel("Time / s")
    for ax in axes:
        ax.grid(True, ls="--", alpha=0.35)
        ax.legend(ncol=3)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=180)
    if show:
        plt.show()


def launch_gui(config_path: str = "config.example.json") -> int:
    try:
        from PyQt5 import QtCore, QtWidgets
        from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
        from matplotlib.figure import Figure
        from matplotlib.patches import Circle, Ellipse, FancyBboxPatch, Rectangle
    except ModuleNotFoundError as exc:
        raise RuntimeError("PyQt5 is required for the GUI; install it or run with --cli") from exc

    class HardwareModelCanvas(FigureCanvas):
        def __init__(self) -> None:
            self.figure = Figure(figsize=(8.4, 6.8), tight_layout=True)
            super().__init__(self.figure)
            self.ax = self.figure.add_subplot(111)
            self.figure.patch.set_facecolor("white")

        def draw_frame(self, row: Optional[Telemetry], cfg: ExperimentConfig) -> None:
            ax = self.ax
            ax.clear()
            ax.set_xlim(0, 10)
            ax.set_ylim(0, 10)
            ax.set_aspect("equal")
            ax.axis("off")
            ax.set_title("Hardware Dual-Branch Pneumatic Lung Monitor", fontsize=15, fontweight="bold", pad=10)

            if row is None:
                time_s = 0.0
                state = 1
                cycle = 0
                mask = 0
                q_left = q_right = 0.0
                v_left = v_right = 0.0
                open_left = open_right = 0.0
            else:
                time_s = row.time_ms / 1000.0
                state = row.state
                cycle = row.cycle
                mask = row.valve_mask
                q_left, q_right = row.q_left, row.q_right
                v_left, v_right = row.v_left, row.v_right
                open_left, open_right = row.opening_left, row.opening_right

            ax.text(
                0.25, 9.45,
                f"t = {time_s:6.2f} s    State = {STATE_NAMES.get(state, 'UNKNOWN')}    Cycle = {cycle + 1}/{cfg.cycles}",
                fontsize=11, family="monospace",
                bbox=dict(boxstyle="round,pad=0.35", fc="#edf4fb", ec="#7a9bbd"),
            )

            ax.add_patch(FancyBboxPatch((0.35, 7.95), 1.6, 0.7, boxstyle="round,pad=0.1",
                                        fc="#dff3ff", ec="#2878a8", lw=1.5))
            ax.text(1.15, 8.3, "Atmosphere Inlet", ha="center", va="center", fontsize=11)
            ax.add_patch(FancyBboxPatch((8.05, 7.95), 1.6, 0.7, boxstyle="round,pad=0.1",
                                        fc="#eee4fa", ec="#7550a5", lw=1.5))
            ax.text(8.85, 8.3, "Regulated Vacuum", ha="center", va="center", fontsize=11)

            values = [
                ("Left Lung", 3.0, v_left, q_left, open_left, bool(mask & 1), bool(mask & 2), cfg.left.capacity_ml),
                ("Right Lung", 7.0, v_right, q_right, open_right, bool(mask & 4), bool(mask & 8), cfg.right.capacity_ml),
            ]
            for name, x, volume, flow, opening, atm_open, vac_open, capacity in values:
                ax.plot([1.95, x - 0.52], [8.30, 8.30], color="#2878a8", lw=2)
                ax.plot([8.05, x + 0.52], [8.30, 8.30], color="#7550a5", lw=2)
                ax.plot([x - 0.52, x - 0.52], [8.30, 7.25], color="#555", lw=2)
                ax.plot([x + 0.52, x + 0.52], [8.30, 7.25], color="#555", lw=2)
                self._valve(ax, x - 0.52, 7.15, atm_open, "ATM")
                self._valve(ax, x + 0.52, 7.15, vac_open, "VAC")
                ax.plot([x - 0.52, x - 0.52, x], [6.82, 6.55, 6.25], color="#555", lw=2)
                ax.plot([x + 0.52, x + 0.52, x], [6.82, 6.55, 6.25], color="#555", lw=2)

                ax.plot([x, x], [6.25, 5.75], color="#555", lw=max(1.0, 5.0 * opening))
                pinch_color = "#27ae60" if opening > 0.02 else "#9b9b9b"
                ax.add_patch(Circle((x, 5.52), 0.23, fc="white", ec=pinch_color, lw=2.2))
                gap = 0.05 + 0.15 * opening
                ax.plot([x - 0.17, x - gap], [5.67, 5.40], color=pinch_color, lw=2)
                ax.plot([x + 0.17, x + gap], [5.67, 5.40], color=pinch_color, lw=2)
                ax.text(x + 0.32, 5.54, f"PPV  {opening*100:4.1f}%", va="center", fontsize=9)
                ax.plot([x, x], [5.29, 4.78], color="#555", lw=2)
                ax.add_patch(FancyBboxPatch((x - 0.52, 4.18), 1.04, 0.60, boxstyle="round,pad=0.06",
                                            fc="#fff4d8", ec="#bc8520", lw=1.5))
                ax.text(x, 4.48, "SFM3300", ha="center", va="center", fontsize=9)
                ax.plot([x, x], [4.18, 3.52], color="#555", lw=2)

                if abs(flow) > 0.25:
                    dy = -0.45 if flow > 0 else 0.45
                    y0 = 3.95 if flow > 0 else 3.55
                    ax.annotate("", xy=(x, y0 + dy), xytext=(x, y0),
                                arrowprops=dict(arrowstyle="-|>", color="#e64a35", lw=2.3))

                fraction = max(0.0, min(volume / max(capacity, 1.0), 1.0))
                width = 1.05 + 1.40 * fraction
                height = 1.30 + 1.75 * fraction
                lung_color = "#f49a9a" if state == 3 else "#b8d7f0"
                if state == 5:
                    lung_color = "#c7b7e5"
                ax.add_patch(Ellipse((x, 2.20), width, height, fc=lung_color, ec="#555", lw=1.8, alpha=0.92))
                ax.plot([x, x], [3.52, 2.95], color="#555", lw=2)
                ax.text(x, 2.25, name, ha="center", va="center", fontsize=12, fontweight="bold")
                ax.text(x, 1.45, f"V = {volume:6.1f} mL\nQ = {flow:+6.2f} SLM",
                        ha="center", va="top", fontsize=10, family="monospace")

            ax.text(
                5.0, 0.25,
                "Blue: atmosphere line    Purple: vacuum line    Green: open/modulating valve",
                ha="center", fontsize=9, color="#444",
            )
            self.draw_idle()

        @staticmethod
        def _valve(ax, x: float, y: float, opened: bool, label: str) -> None:
            color = "#27ae60" if opened else "#b7b7b7"
            ax.add_patch(Rectangle((x - 0.25, y - 0.28), 0.50, 0.56, fc=color, ec="#444", lw=1.3))
            ax.plot([x - 0.15, x + 0.15], [y - 0.12, y + 0.12], color="white", lw=2)
            ax.text(x, y + 0.42, label, ha="center", fontsize=8)

    class HardwareFlowCanvas(FigureCanvas):
        def __init__(self) -> None:
            self.figure = Figure(figsize=(6.4, 3.2), tight_layout=True)
            super().__init__(self.figure)
            self.ax = self.figure.add_subplot(111)
            self.draw_rows([])

        def draw_rows(self, rows: List[Telemetry], window_s: float = 10.0) -> None:
            self.ax.clear()
            self.ax.set_title("Hardware Flow Rate: measured vs expected")
            self.ax.set_xlabel("Time / s")
            self.ax.set_ylabel("Flow / SLM")
            self.ax.grid(True, ls="--", alpha=0.3)
            if rows:
                now = rows[-1].time_ms / 1000.0
                start = max(0.0, now - window_s)
                view = [row for row in rows if row.time_ms / 1000.0 >= start]
                t = [row.time_ms / 1000.0 for row in view]
                self.ax.plot(t, [row.q_left for row in view], label="Left measured", lw=1.6)
                self.ax.plot(t, [row.q_right for row in view], label="Right measured", lw=1.6)
                self.ax.plot(t, [row.qref_left for row in view], "k--", label="Left expected", lw=1.0)
                self.ax.plot(t, [row.qref_right for row in view], color="#555", ls=":", label="Right expected", lw=1.0)
                self.ax.axvline(now, color="#d62728", lw=1.0)
                self.ax.set_xlim(start, max(start + window_s, now + 0.2))
                values = []
                for row in view:
                    values.extend([row.q_left, row.q_right, row.qref_left, row.qref_right])
                ymin = min(-2.0, min(values) - 2.0)
                ymax = max(2.0, max(values) + 2.0)
                self.ax.set_ylim(ymin, ymax)
                self.ax.legend(loc="upper right", ncol=2, fontsize=8)
            self.draw_idle()

    class HardwareControlPanel(QtWidgets.QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("LungLogic Hardware Dual PID Control Panel")
            self.resize(1320, 820)
            self.cfg = load_config(config_path)
            self.connection = None
            self.firmware_ready = False
            self.worker: Optional[SerialWorker] = None
            self.messages: queue.Queue[str] = queue.Queue()
            self.rows: List[Telemetry] = []
            self.run_id = int(time.time()) & 0x7FFFFFFF
            self.last_ping = 0.0
            self.last_plot_count = -1
            self.inputs = {}
            self.metrics = {}
            self._build_ui()
            self.model_canvas.draw_frame(None, self.cfg)
            self.refresh_ports()
            self.timer = QtCore.QTimer(self)
            self.timer.setInterval(100)
            self.timer.timeout.connect(self._tick)
            self.timer.start()

        def _build_ui(self) -> None:
            layout = QtWidgets.QVBoxLayout(self)
            splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
            layout.addWidget(splitter)

            left_outer = QtWidgets.QWidget()
            left_outer.setMinimumWidth(380)
            left_layout = QtWidgets.QVBoxLayout(left_outer)
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            left_content = QtWidgets.QWidget()
            panel = QtWidgets.QVBoxLayout(left_content)

            serial_group = QtWidgets.QGroupBox("Serial Connection")
            serial_layout = QtWidgets.QGridLayout(serial_group)
            self.port_combo = QtWidgets.QComboBox()
            self.refresh_button = QtWidgets.QPushButton("Refresh")
            self.connect_button = QtWidgets.QPushButton("Connect")
            self.disconnect_button = QtWidgets.QPushButton("Disconnect")
            self.status_label = QtWidgets.QLabel("Disconnected")
            self.refresh_button.clicked.connect(self.refresh_ports)
            self.connect_button.clicked.connect(self.connect_serial)
            self.disconnect_button.clicked.connect(self.disconnect_serial)
            serial_layout.addWidget(self.port_combo, 0, 0, 1, 3)
            serial_layout.addWidget(self.refresh_button, 1, 0)
            serial_layout.addWidget(self.connect_button, 1, 1)
            serial_layout.addWidget(self.disconnect_button, 1, 2)
            serial_layout.addWidget(self.status_label, 2, 0, 1, 3)
            panel.addWidget(serial_group)

            control_group = QtWidgets.QGroupBox("Controller")
            form = QtWidgets.QFormLayout(control_group)
            self._spin(form, "rr", "Respiratory rate / bpm", self.cfg.respiratory_rate_bpm, 1, 40, 1, 2)
            self._spin(form, "ie_in", "I:E inspiration", self.cfg.ie_inspiration, 0.1, 10, 0.1, 2)
            self._spin(form, "ie_out", "I:E expiration", self.cfg.ie_expiration, 0.1, 10, 0.1, 2)
            self._spin(form, "v_left", "Left target / mL", self.cfg.left_target_ml, 10, 320, 5, 1)
            self._spin(form, "v_right", "Right target / mL", self.cfg.right_target_ml, 10, 320, 5, 1)
            self._spin(form, "fill_time", "Target fill time / s", self.cfg.target_fill_time_s, 0.1, 10, 0.05, 2)
            self._spin(form, "dead", "Direction-change dead time / s", self.cfg.dead_time_s, 0, 0.5, 0.01, 3)
            self._spin(form, "precondition", "Precondition time / s", self.cfg.precondition_s, 0, 10, 0.1, 2)
            self._spin(form, "cycles", "Cycles", self.cfg.cycles, 1, 1000, 1, 0)
            self._spin(form, "kp", "Kp / opening per SLM", self.cfg.pi.kp, 0, 1, 0.001, 4)
            self._spin(form, "ki", "Ki / opening per SLM*s", self.cfg.pi.ki, 0, 5, 0.01, 4)
            self._spin(form, "kd", "Kd / opening*s per SLM", self.cfg.pi.kd, 0, 1, 0.001, 4)
            self._spin(form, "ilim", "Integral limit", self.cfg.pi.integral_limit, 0.01, 20, 0.1, 2)
            panel.addWidget(control_group)

            buttons = QtWidgets.QHBoxLayout()
            self.configure_button = QtWidgets.QPushButton("Send Config")
            self.start_button = QtWidgets.QPushButton("Start")
            self.stop_button = QtWidgets.QPushButton("Stop")
            self.save_button = QtWidgets.QPushButton("Save Log")
            self.configure_button.clicked.connect(self.send_config)
            self.start_button.clicked.connect(self.start_run)
            self.stop_button.clicked.connect(self.stop_run)
            self.save_button.clicked.connect(self.save_current_log)
            buttons.addWidget(self.configure_button)
            buttons.addWidget(self.start_button)
            buttons.addWidget(self.stop_button)
            buttons.addWidget(self.save_button)
            panel.addLayout(buttons)

            monitor_group = QtWidgets.QGroupBox("Live Telemetry")
            monitor_form = QtWidgets.QFormLayout(monitor_group)
            for key, label in (
                ("state", "State"),
                ("cycle", "Cycle"),
                ("flow", "Flow L/R"),
                ("volume", "Volume L/R"),
                ("opening", "PPV opening L/R"),
                ("command", "PPV command L/R"),
                ("valves", "Valve mask"),
            ):
                value = QtWidgets.QLabel("--")
                value.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                value.setStyleSheet("font-family: Consolas, monospace;")
                self.metrics[key] = value
                monitor_form.addRow(label, value)
            panel.addWidget(monitor_group)

            self.log = QtWidgets.QTextBrowser()
            self.log.setMinimumHeight(150)
            panel.addWidget(self.log)
            panel.addStretch(1)
            scroll.setWidget(left_content)
            left_layout.addWidget(scroll)
            splitter.addWidget(left_outer)

            self.tabs = QtWidgets.QTabWidget()
            self.model_canvas = HardwareModelCanvas()
            self.flow_canvas = HardwareFlowCanvas()
            self.tabs.addTab(self.model_canvas, "Dynamic Model")
            self.tabs.addTab(self.flow_canvas, "Measured Flow")
            splitter.addWidget(self.tabs)
            splitter.setSizes([420, 900])
            splitter.setStretchFactor(1, 1)

        def _spin(self, form, key, label, value, low, high, step, decimals) -> None:
            widget = QtWidgets.QDoubleSpinBox()
            widget.setRange(low, high)
            widget.setSingleStep(step)
            widget.setDecimals(decimals)
            widget.setValue(value)
            widget.setKeyboardTracking(False)
            self.inputs[key] = widget
            form.addRow(label, widget)

        def refresh_ports(self) -> None:
            self.port_combo.clear()
            if list_ports is None:
                self.port_combo.addItem("pyserial is not installed", "")
                self._log("pyserial is not installed; the panel can open but cannot connect.")
                return
            ports = list(list_ports.comports())
            if not ports:
                self.port_combo.addItem("No serial ports detected", "")
                self._log("No serial ports detected. Connect the NUCLEO-G474RE and click Refresh.")
                return
            for port in ports:
                label = f"{port.device} | {port.description}"
                self.port_combo.addItem(label, port.device)

        def connect_serial(self) -> None:
            if serial is None:
                self._log("Cannot connect: pyserial is not installed.")
                return
            port = self.port_combo.currentData()
            if not port:
                self._log("Select a valid serial port first.")
                return
            self.disconnect_serial(save=False)
            try:
                self.connection = serial.Serial(port, BAUD, timeout=0.2)
                time.sleep(0.2)
                self.connection.reset_input_buffer()
                self.messages = queue.Queue()
                self.worker = SerialWorker(self.connection, self.messages)
                self.worker.start()
                self.connection.write(b"HELLO\n")
                self.status_label.setText(f"Connecting on {port}...")
                self._log(f"> HELLO ({port})")
            except Exception as exc:
                self.connection = None
                self.status_label.setText("Disconnected")
                self._log(f"Connection failed: {exc}")

        def disconnect_serial(self, save: bool = True) -> None:
            self.firmware_ready = False
            if self.connection is not None:
                try:
                    self.connection.write(b"STOP\n")
                except Exception:
                    pass
            if self.worker is not None:
                self.worker.stop_event.set()
                self.worker.join(timeout=1.0)
                self.worker = None
            if self.connection is not None:
                try:
                    self.connection.close()
                except Exception:
                    pass
                self.connection = None
            if save and self.rows:
                self.save_current_log()
            self.status_label.setText("Disconnected")

        def cfg_from_ui(self) -> ExperimentConfig:
            cfg = load_config(config_path)
            cfg.respiratory_rate_bpm = self.inputs["rr"].value()
            cfg.ie_inspiration = self.inputs["ie_in"].value()
            cfg.ie_expiration = self.inputs["ie_out"].value()
            cfg.left_target_ml = self.inputs["v_left"].value()
            cfg.right_target_ml = self.inputs["v_right"].value()
            cfg.target_fill_time_s = self.inputs["fill_time"].value()
            cfg.dead_time_s = self.inputs["dead"].value()
            cfg.precondition_s = self.inputs["precondition"].value()
            cfg.cycles = int(self.inputs["cycles"].value())
            cfg.pi.kp = self.inputs["kp"].value()
            cfg.pi.ki = self.inputs["ki"].value()
            cfg.pi.kd = self.inputs["kd"].value()
            cfg.pi.integral_limit = self.inputs["ilim"].value()
            cfg.validate()
            return cfg

        def send_config(self) -> None:
            if self.connection is None:
                self._log("Not connected. The UI stays available; connect before sending config.")
                return
            if not self.firmware_ready:
                self._log(f"Waiting for compatible firmware: {READY}")
                return
            try:
                self.cfg = self.cfg_from_ui()
                token = int(time.time()) & 0x7FFFFFFF
                command = config_command(self.cfg, token)
                self.connection.write((command + "\n").encode("ascii"))
                self._log(f"> {command}")
            except Exception as exc:
                self._log(f"Config error: {exc}")

        def start_run(self) -> None:
            if self.connection is None:
                self._log("Not connected. Connect the NUCLEO-G474RE before starting.")
                return
            if not self.firmware_ready:
                self._log(f"Cannot start: expected {READY}")
                return
            self.rows = []
            self.last_plot_count = -1
            self.flow_canvas.draw_rows([])
            self.model_canvas.draw_frame(None, self.cfg)
            self.run_id = int(time.time()) & 0x7FFFFFFF
            self.send_config()
            command = f"START,{self.run_id},{int(self.inputs['cycles'].value())}"
            self.connection.write((command + "\n").encode("ascii"))
            self._log(f"> {command}")

        def stop_run(self) -> None:
            if self.connection is None:
                self._log("Not connected.")
                return
            self.connection.write(b"STOP\n")
            self._log("> STOP")

        def save_current_log(self) -> None:
            csv_path = Path("output/lung_hardware_gui.csv")
            png_path = Path("output/lung_hardware_gui.png")
            save_log(self.rows, csv_path)
            plot_log(self.rows, png_path, False)
            self._log(f"Saved {csv_path} ({len(self.rows)} telemetry rows)")

        def _tick(self) -> None:
            if self.connection is not None and time.monotonic() - self.last_ping >= 1.0:
                try:
                    self.connection.write(b"PING\n")
                    self.last_ping = time.monotonic()
                except Exception as exc:
                    self._log(f"PING failed: {exc}")
                    self.disconnect_serial(save=True)
            while True:
                try:
                    line = self.messages.get_nowait()
                except queue.Empty:
                    break
                self._handle_line(line)

        def _handle_line(self, line: str) -> None:
            self._log(f"< {line}")
            if line == READY:
                self.firmware_ready = True
                self.status_label.setText("Connected")
            elif line.startswith("READY,"):
                self.firmware_ready = False
                self.status_label.setText("Wrong STM32 firmware")
                self._log(f"Expected {READY}; refusing {line}")
            elif line.startswith("TEL,"):
                try:
                    row = Telemetry.parse(line)
                except ValueError:
                    return
                self.rows.append(row)
                self.metrics["state"].setText(STATE_NAMES.get(row.state, "UNKNOWN"))
                self.metrics["cycle"].setText(str(row.cycle + 1))
                self.metrics["flow"].setText(f"{row.q_left:+.2f} / {row.q_right:+.2f} SLM")
                self.metrics["volume"].setText(f"{row.v_left:.1f} / {row.v_right:.1f} mL")
                self.metrics["opening"].setText(f"{row.opening_left*100:.1f} / {row.opening_right*100:.1f} %")
                self.metrics["command"].setText(f"{row.command_left:.2f} / {row.command_right:.2f} V")
                self.metrics["valves"].setText(f"0x{row.valve_mask:02X}")
                self.model_canvas.draw_frame(row, self.cfg)
                if len(self.rows) - self.last_plot_count >= 5:
                    self.flow_canvas.draw_rows(self.rows)
                    self.last_plot_count = len(self.rows)
            elif line.startswith("DONE,"):
                self.status_label.setText("Run complete")
                self.save_current_log()
            elif line.startswith("FAULT,") or line.startswith("ERR,"):
                self.status_label.setText("Fault/Error")

        def _log(self, text: str) -> None:
            self.log.append(text)

        def closeEvent(self, event) -> None:
            self.disconnect_serial(save=True)
            event.accept()

    app = QtWidgets.QApplication(sys.argv)
    window = HardwareControlPanel()
    window.show()
    return app.exec_()


def run_hardware(args) -> int:
    if serial is None:
        raise RuntimeError("pyserial is not installed; run: python -m pip install -r requirements.txt")
    cfg = load_config(args.config)
    port = choose_port(args.port)
    print(f"Opening {port} at {BAUD} baud")
    connection = serial.Serial(port, BAUD, timeout=0.2)
    time.sleep(0.2)  # ST-LINK VCP does not normally reset the G474RE on open.
    connection.reset_input_buffer()
    messages: queue.Queue[str] = queue.Queue()
    worker = SerialWorker(connection, messages)
    worker.start()
    rows: List[Telemetry] = []
    run_id = int(time.time()) & 0x7FFFFFFF
    token = run_id
    try:
        connection.write(b"HELLO\n")
        wait_for(messages, READY, 3.0)
        command = config_command(cfg, token)
        print(f"> {command}")
        connection.write((command + "\n").encode("ascii"))
        wait_for(messages, f"ACK,CFG,{token}", 3.0)
        if args.configure_only:
            return 0
        start = f"START,{run_id},{cfg.cycles}"
        print(f"> {start}")
        connection.write((start + "\n").encode("ascii"))
        wait_for(messages, f"STARTED,{run_id}", 3.0)

        last_ping = time.monotonic()
        while True:
            if time.monotonic() - last_ping >= 1.0:
                connection.write(b"PING\n")
                last_ping = time.monotonic()
            try:
                line = messages.get(timeout=0.2)
            except queue.Empty:
                continue
            if line.startswith("TEL,"):
                try:
                    row = Telemetry.parse(line)
                except ValueError:
                    print(f"Malformed: {line}")
                else:
                    rows.append(row)
                    if len(rows) % 25 == 0:
                        print(
                            f"t={row.time_ms/1000:6.2f}s {STATE_NAMES.get(row.state)} "
                            f"V=({row.v_left:6.1f},{row.v_right:6.1f})mL "
                            f"Q=({row.q_left:6.1f},{row.q_right:6.1f})SLM"
                        )
            elif line.startswith(f"DONE,{run_id}"):
                print(line)
                break
            elif line.startswith("FAULT,") or line.startswith("ERR,"):
                raise RuntimeError(line)
    except KeyboardInterrupt:
        print("Stopping...")
        connection.write(b"STOP\n")
    finally:
        worker.stop_event.set()
        connection.write(b"STOP\n")
        worker.join(timeout=1.0)
        connection.close()
        save_log(rows, Path(args.output_csv))
        plot_log(rows, Path(args.output_png), args.show)
        print(f"Saved {args.output_csv} ({len(rows)} telemetry rows)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui", action="store_true", help="open the hardware control panel")
    parser.add_argument("--cli", action="store_true", help="run the original command-line hardware monitor")
    parser.add_argument("--port")
    parser.add_argument("--config", default="config.example.json")
    parser.add_argument("--output-csv", default="output/lung_hardware.csv")
    parser.add_argument("--output-png", default="output/lung_hardware.png")
    parser.add_argument("--configure-only", action="store_true")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.gui or not args.cli and not args.dry_run and args.port is None and not args.configure_only and not args.show:
        return launch_gui(args.config)
    if args.dry_run:
        cfg = load_config(args.config)
        print(config_command(cfg, 12345))
        print(f"START,12345,{cfg.cycles}")
        return 0
    return run_hardware(args)


if __name__ == "__main__":
    raise SystemExit(main())
