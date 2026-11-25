"""Bench experiment UI for the separate flow_control_exp STM32 firmware.

Run:
    python flow_control_test_ui.py

This tool is intentionally separate from the original LungLogic hardware GUI.
It is for open-loop bench experiments that verify hardware behavior and collect
data for model parameters such as flow sign, command delay, PPV voltage-flow
mapping, tau_in/tau_out and gamma.
"""

from __future__ import annotations

import csv
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


BAUD = 115200
READY = "READY,FLOW_EXP_G474,2"
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
PPV_SWEEP_START_V = 3.8
PPV_SWEEP_END_V = 4.5
PPV_SWEEP_STEP_V = 0.05
PPV_SETTLE_S = 3.0
PPV_SWEEP_VOLTAGES = tuple(
    round(PPV_SWEEP_START_V + index * PPV_SWEEP_STEP_V, 2)
    for index in range(round((PPV_SWEEP_END_V - PPV_SWEEP_START_V) / PPV_SWEEP_STEP_V) + 1)
)


@dataclass
class Telemetry:
    mcu_ms: int
    flow_slm: float
    atm: int
    vac: int
    ppv_v: float
    ppv_pwm_code: int
    pressure_model_kpa: float
    pressure_syringe_kpa: float
    sensor_status: str
    fault: str
    raw_line: str
    sample_ms: Optional[int] = None
    sensor_raw: Optional[int] = None
    uncorrected_flow_slm: float = math.nan
    zero_offset_slm: float = math.nan
    sampled_atm: Optional[int] = None
    sampled_vac: Optional[int] = None
    sampled_ppv_v: float = math.nan

    @classmethod
    def parse(cls, line: str) -> "Telemetry":
        fields = line.split(",")
        if len(fields) not in (11, 18) or fields[0] != "TEL":
            raise ValueError("not FLOW_EXP telemetry")
        sensor_status = fields[9]
        if sensor_status not in {"OK", "STARTING", "I2C", "CRC"}:
            raise ValueError("invalid SFM3300 status")
        flow_slm = float(fields[2])
        if sensor_status == "OK" and not math.isfinite(flow_slm):
            raise ValueError("valid SFM3300 status without a finite flow value")
        if sensor_status != "OK":
            flow_slm = math.nan
        return cls(
            mcu_ms=int(fields[1]),
            flow_slm=flow_slm,
            atm=int(fields[3]),
            vac=int(fields[4]),
            ppv_v=float(fields[5]),
            ppv_pwm_code=int(fields[6]),
            pressure_model_kpa=float(fields[7]),
            pressure_syringe_kpa=float(fields[8]),
            sensor_status=sensor_status,
            fault=fields[10],
            raw_line=line,
            sample_ms=int(fields[11]) if len(fields) == 18 and sensor_status == "OK" else None,
            sensor_raw=int(fields[12]) if len(fields) == 18 and sensor_status == "OK" else None,
            uncorrected_flow_slm=float(fields[13]) if len(fields) == 18 and sensor_status == "OK" else math.nan,
            zero_offset_slm=float(fields[14]) if len(fields) == 18 else math.nan,
            sampled_atm=int(fields[15]) if len(fields) == 18 and sensor_status == "OK" else None,
            sampled_vac=int(fields[16]) if len(fields) == 18 and sensor_status == "OK" else None,
            sampled_ppv_v=float(fields[17]) if len(fields) == 18 and sensor_status == "OK" else math.nan,
        )


def fmt_float(value: float, digits: int = 3) -> str:
    return "nan" if math.isnan(value) else f"{value:.{digits}f}"


def main() -> int:
    try:
        import serial
        from serial.tools import list_ports
        from PyQt5 import QtCore, QtGui, QtWidgets
        from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
        from matplotlib.figure import Figure
    except ModuleNotFoundError as exc:
        missing = exc.name or "required package"
        print(
            f"Missing {missing}. Install dependencies with:\n"
            f"python -m pip install pyserial PyQt5 matplotlib"
        )
        return 2

    class FlowCanvas(FigureCanvas):
        def __init__(self) -> None:
            self.figure = Figure(figsize=(7.0, 3.5), tight_layout=True)
            super().__init__(self.figure)
            self.ax = self.figure.add_subplot(111)
            self.draw_rows([])

        def draw_rows(self, rows: list[Telemetry], window_s: float = 20.0) -> None:
            self.ax.clear()
            self.ax.set_title("Live Flow Monitor - SFM3300", fontsize=13, fontweight="bold")
            self.ax.set_xlabel("MCU time (s)")
            self.ax.set_ylabel("Flow (SLM)")
            self.ax.grid(True, ls="--", alpha=0.35)
            if rows:
                now_s = rows[-1].mcu_ms / 1000.0
                start_s = max(0.0, now_s - window_s)
                view = [row for row in rows
                        if row.mcu_ms / 1000.0 >= start_s
                        and row.sensor_status == "OK" and math.isfinite(row.flow_slm)]
                if not view:
                    self.ax.text(0.5, 0.5, "Waiting for valid SFM3300 data",
                                 ha="center", va="center", transform=self.ax.transAxes)
                    self.draw_idle()
                    return
                t = [row.mcu_ms / 1000.0 for row in view]
                q = [row.flow_slm for row in view]
                self.ax.plot(t, q, lw=1.8, label="SFM3300")
                self.ax.axhline(0.0, color="#555", lw=0.8)
                self.ax.set_xlim(start_s, max(start_s + window_s, now_s + 0.2))
                y_min = min(-1.0, min(q) - 1.0)
                y_max = max(1.0, max(q) + 1.0)
                self.ax.set_ylim(y_min, y_max)
                self.ax.legend(loc="upper right")
            self.draw_idle()

    class Window(QtWidgets.QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("Flow Control Experiment UI - STM32 / PPV / SFM3300")
            self.resize(1180, 760)
            self.conn: Optional[serial.Serial] = None
            self.ready = False
            self.rx = bytearray()
            self.last_ping = 0.0
            self.last_rx = 0.0
            self.rows: list[Telemetry] = []
            self.latest: Optional[Telemetry] = None
            self.csv_file = None
            self.writer: Optional[csv.DictWriter] = None
            self.record_path: Optional[Path] = None
            self.telemetry_count = 0
            self.plot_count = 0
            self._build_ui(QtCore, QtGui, QtWidgets, list_ports)
            self.timer = QtCore.QTimer(self)
            self.timer.setInterval(50)
            self.timer.timeout.connect(self.poll)
            self.timer.start()

        def _build_ui(self, QtCore, QtGui, QtWidgets, list_ports) -> None:
            app_font = QtGui.QFont("Microsoft YaHei", 9)
            QtWidgets.QApplication.instance().setFont(app_font)

            central = QtWidgets.QWidget()
            self.setCentralWidget(central)
            layout = QtWidgets.QVBoxLayout(central)

            top = QtWidgets.QGroupBox("Connect")
            top_layout = QtWidgets.QGridLayout(top)
            self.port_combo = QtWidgets.QComboBox()
            self.refresh_button = QtWidgets.QPushButton("Refresh ports")
            self.connect_button = QtWidgets.QPushButton("Connect")
            self.disconnect_button = QtWidgets.QPushButton("Disconnect")
            self.status = QtWidgets.QLabel("Disconnected")
            self.refresh_button.clicked.connect(self.refresh_ports)
            self.connect_button.clicked.connect(self.connect_serial)
            self.disconnect_button.clicked.connect(self.disconnect_serial)
            top_layout.addWidget(self.port_combo, 0, 0, 1, 3)
            top_layout.addWidget(self.refresh_button, 1, 0)
            top_layout.addWidget(self.connect_button, 1, 1)
            top_layout.addWidget(self.disconnect_button, 1, 2)
            top_layout.addWidget(self.status, 2, 0, 1, 3)
            layout.addWidget(top)

            splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
            layout.addWidget(splitter, 1)

            left = QtWidgets.QWidget()
            left_layout = QtWidgets.QVBoxLayout(left)
            splitter.addWidget(left)

            valves = QtWidgets.QGroupBox("Directional valves")
            valve_layout = QtWidgets.QGridLayout(valves)
            self.atm_on = QtWidgets.QPushButton("ATM ON")
            self.atm_off = QtWidgets.QPushButton("ATM OFF")
            self.vac_on = QtWidgets.QPushButton("VAC ON")
            self.vac_off = QtWidgets.QPushButton("VAC OFF")
            self.stop_all = QtWidgets.QPushButton("Close all valves / STOP")
            self.stop_all.setStyleSheet("color: #b91c1c; font-weight: bold;")
            self.atm_on.clicked.connect(lambda: self.send("ATM,1", "ATM_ON"))
            self.atm_off.clicked.connect(lambda: self.send("ATM,0", "ATM_OFF"))
            self.vac_on.clicked.connect(lambda: self.send("VAC,1", "VAC_ON"))
            self.vac_off.clicked.connect(lambda: self.send("VAC,0", "VAC_OFF"))
            self.stop_all.clicked.connect(lambda: self.send("STOP", "STOP_ALL"))
            valve_layout.addWidget(self.atm_on, 0, 0)
            valve_layout.addWidget(self.atm_off, 0, 1)
            valve_layout.addWidget(self.vac_on, 1, 0)
            valve_layout.addWidget(self.vac_off, 1, 1)
            valve_layout.addWidget(self.stop_all, 2, 0, 1, 2)
            left_layout.addWidget(valves)

            ppv = QtWidgets.QGroupBox("PPV target voltage")
            ppv_layout = QtWidgets.QVBoxLayout(ppv)
            buttons = QtWidgets.QGridLayout()
            for i, voltage in enumerate(PPV_SWEEP_VOLTAGES):
                b = QtWidgets.QPushButton(f"{voltage:.2f} V")
                b.clicked.connect(lambda checked=False, v=voltage: self.set_ppv(v))
                buttons.addWidget(b, i // 5, i % 5)
            ppv_layout.addLayout(buttons)
            custom = QtWidgets.QHBoxLayout()
            self.ppv_spin = QtWidgets.QDoubleSpinBox()
            self.ppv_spin.setRange(0.0, 5.0)
            self.ppv_spin.setDecimals(3)
            self.ppv_spin.setSingleStep(PPV_SWEEP_STEP_V)
            self.ppv_spin.setValue(PPV_SWEEP_START_V)
            custom.addWidget(QtWidgets.QLabel("Custom V"))
            custom.addWidget(self.ppv_spin)
            custom_button = QtWidgets.QPushButton("Send")
            custom_button.clicked.connect(lambda: self.set_ppv(self.ppv_spin.value()))
            custom.addWidget(custom_button)
            ppv_layout.addLayout(custom)
            hint = QtWidgets.QLabel(
                "Suggested sweep: 3.800-4.500 V in 0.050 V steps; after each voltage change, wait at least "
                f"{PPV_SETTLE_S:.0f} seconds before collecting steady-state data. 0 V is fully open; higher voltage increases clamping. "
                "Calibrate the actual output using a multimeter."
            )
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #475569;")
            ppv_layout.addWidget(hint)
            left_layout.addWidget(ppv)

            record = QtWidgets.QGroupBox("Data recording")
            record_layout = QtWidgets.QGridLayout(record)
            self.start_record_button = QtWidgets.QPushButton("Start recording")
            self.stop_record_button = QtWidgets.QPushButton("Stop recording")
            self.zero_flow_button = QtWidgets.QPushButton("Zero flow")
            self.filename_edit = QtWidgets.QLineEdit()
            self.filename_edit.setPlaceholderText("e.g. function_check_atm.csv or ppv_sweep_vac.csv")
            self.filename_edit.setText(
                f"ppv_sweep_3p8_to_4p5_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            )
            self.choose_csv_button = QtWidgets.QPushButton("Browse")
            self.record_label = QtWidgets.QLabel("Not recording")
            self.start_record_button.clicked.connect(self.start_recording)
            self.stop_record_button.clicked.connect(self.stop_recording)
            self.zero_flow_button.clicked.connect(lambda: self.send("ZERO_FLOW", "ZERO_FLOW"))
            self.zero_flow_button.setToolTip(
                "Confirm zero physical flow with ATM and VAC closed. Firmware waits 0.5 s and averages 50 fresh samples. "
                "Excessive or unstable flow rejects zeroing and retains the previous offset. Do not change valves or PPV during zeroing."
            )
            self.choose_csv_button.clicked.connect(self.choose_csv_path)
            record_layout.addWidget(QtWidgets.QLabel("CSV filename / path"), 0, 0, 1, 2)
            record_layout.addWidget(self.filename_edit, 1, 0)
            record_layout.addWidget(self.choose_csv_button, 1, 1)
            record_layout.addWidget(self.start_record_button, 2, 0)
            record_layout.addWidget(self.stop_record_button, 2, 1)
            record_layout.addWidget(self.zero_flow_button, 3, 0, 1, 2)
            record_layout.addWidget(self.record_label, 4, 0, 1, 2)
            left_layout.addWidget(record)

            monitor = QtWidgets.QGroupBox("Live monitor")
            monitor_layout = QtWidgets.QFormLayout(monitor)
            self.metric_flow = self.metric_label()
            self.metric_pressure = self.metric_label()
            self.metric_valves = self.metric_label()
            self.metric_ppv = self.metric_label()
            self.metric_sensor = self.metric_label()
            monitor_layout.addRow("Current flow", self.metric_flow)
            monitor_layout.addRow("Pressure", self.metric_pressure)
            monitor_layout.addRow("Valve status", self.metric_valves)
            monitor_layout.addRow("PPV", self.metric_ppv)
            monitor_layout.addRow("Sensor / fault", self.metric_sensor)
            left_layout.addWidget(monitor)

            docs = QtWidgets.QTextBrowser()
            docs.setHtml(
                "<h3>Two preliminary experiments</h3>"
                "<p>This tool performs open-loop checks of the physical behavior assumed by the hardware controller. "
                "It does not modify the original GUI or controller firmware.</p>"
                "<table border='1' cellspacing='0' cellpadding='5'>"
                "<tr><th>Experiment</th><th>Purpose</th><th>Procedure</th></tr>"
                "<tr>"
                "<td>Functional verification</td>"
                "<td>Verify ATM/VAC solenoid commands and PPV flow adjustment.</td>"
                "<td>Operate ATM, VAC and PPV individually; observe physical motion, flow response and valve status.</td>"
                "</tr>"
                "<tr>"
                "<td>PPV nonlinear response</td>"
                "<td>Measure flow at different PPV target voltages to fit the gamma exponent.</td>"
                "<td>Sweep from 3.800 to 4.500 V in 0.050 V steps. Wait at least 3 s after each change, "
                "then record the steady-state segment. Test ATM and VAC separately, restoring comparable bag volume and source pressure.</td>"
                "</tr>"
                "</table>"
                "<p>CSV records MCU timestamps, flow, model-side and syringe-side pressure, ATM/VAC status, "
                "PPV target voltage, sensor status and operator events. Unconnected or disabled pressure channels are left blank.</p>"
                "<p><b>Stop recording</b> closes the CSV file only; valves remain in their current state. "
                "<b>STOP</b> closes ATM/VAC and sets PPV to 5 V.</p>"
            )
            left_layout.addWidget(docs, 1)

            right = QtWidgets.QWidget()
            right_layout = QtWidgets.QVBoxLayout(right)
            splitter.addWidget(right)
            splitter.setSizes([410, 770])

            self.canvas = FlowCanvas()
            right_layout.addWidget(self.canvas, 3)
            log_box = QtWidgets.QGroupBox("Serial log")
            log_layout = QtWidgets.QVBoxLayout(log_box)
            self.log = QtWidgets.QPlainTextEdit()
            self.log.setReadOnly(True)
            log_layout.addWidget(self.log)
            right_layout.addWidget(log_box, 2)

            self.refresh_ports()

        def metric_label(self):
            from PyQt5 import QtCore, QtWidgets

            label = QtWidgets.QLabel("--")
            label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            label.setStyleSheet("font-family: Consolas, 'Microsoft YaHei';")
            return label

        def refresh_ports(self) -> None:
            from serial.tools import list_ports

            self.port_combo.clear()
            ports = list(list_ports.comports())
            if not ports:
                self.port_combo.addItem("No serial ports found", "")
                return
            for port in ports:
                self.port_combo.addItem(f"{port.device} | {port.description}", port.device)

        def connect_serial(self) -> None:
            port = self.port_combo.currentData()
            if not port:
                self.log.appendPlainText("Select a valid serial port.")
                return
            self.disconnect_serial(send_stop=False)
            try:
                self.conn = serial.Serial(port, BAUD, timeout=0, write_timeout=0.5)
                time.sleep(0.2)
                self.conn.reset_input_buffer()
                self.rx.clear()
                self.ready = False
                self.rows.clear()
                self.latest = None
                self.telemetry_count = 0
                self.plot_count = 0
                self.canvas.draw_rows([])
                self.last_rx = time.monotonic()
                self.conn.write(b"HELLO\n")
                self.status.setText(f"Connecting to {port} ...")
                self.log.appendPlainText(f"> HELLO ({port})")
            except Exception as exc:
                self.conn = None
                self.status.setText("Connection failed")
                self.log.appendPlainText(f"Connection failed: {exc}")

        def disconnect_serial(self, send_stop: bool = True) -> None:
            if self.conn is not None:
                try:
                    if send_stop:
                        self.conn.write(b"STOP\n")
                except Exception:
                    pass
                try:
                    self.conn.close()
                except Exception:
                    pass
            self.conn = None
            self.ready = False
            self.status.setText("Disconnected")

        def set_ppv(self, volts: float) -> None:
            volts = max(0.0, min(5.0, float(volts)))
            self.ppv_spin.setValue(volts)
            self.send(f"PPV,{round(volts * 1000)}", f"PPV_{volts:.3f}V")

        def send(self, command: str, event: str) -> None:
            if self.conn is None:
                self.log.appendPlainText("Disconnected。")
                return
            if not self.ready and command not in ("HELLO", "PING", "STOP"):
                self.log.appendPlainText(f"Handshake not received: {READY}; refusing to send: {command}")
                return
            try:
                self.conn.write((command + "\n").encode("ascii"))
                self.log.appendPlainText(f"> {command}")
                self.write_csv_event(event, f"> {command}")
            except Exception as exc:
                self.log.appendPlainText(f"Send failed: {exc}")
                self.disconnect_serial()

        def poll(self) -> None:
            if self.conn is None:
                return
            now = time.monotonic()
            try:
                if now - self.last_ping >= 0.8:
                    self.conn.write(b"PING\n" if self.ready else b"HELLO\n")
                    self.last_ping = now
                waiting = self.conn.in_waiting
                if waiting:
                    self.rx.extend(self.conn.read(waiting))
                if len(self.rx) > 16384:
                    raise RuntimeError("Serial receive buffer exceeded its limit.")
                while b"\n" in self.rx:
                    raw, _, rest = self.rx.partition(b"\n")
                    self.rx = bytearray(rest)
                    line = raw.decode("ascii", errors="replace").strip()
                    if line:
                        self.last_rx = now
                        self.handle_line(line)
                if now - self.last_rx > 5.0:
                    raise RuntimeError("Serial timeout; disconnecting.")
            except Exception as exc:
                self.log.appendPlainText(str(exc))
                self.disconnect_serial()

        def handle_line(self, line: str) -> None:
            if not line.startswith("TEL,") and line != "PONG":
                self.write_csv_event("RX", line)
            if line == "PONG":
                return
            if line in (READY, "READY,FLOW_EXP_G474,1"):
                if not self.ready:
                    self.ready = True
                    self.status.setText("Connected to FLOW_EXP_G474 experiment firmware")
                self.log.appendPlainText(f"< {line}")
                if line.endswith(",1"):
                    self.log.appendPlainText("Legacy firmware lacks raw flow, offset and sample timestamps. Upload v2 for complete calibration recordings.")
                return
            if line.startswith("READY,") and line != READY:
                self.ready = False
                self.status.setText("Firmware mismatch")
                self.log.appendPlainText(f"< {line}")
                self.log.appendPlainText("Upload flow_control_exp/flow_control_exp.ino before using this experiment UI.")
                return
            if line.startswith("TEL,"):
                try:
                    row = Telemetry.parse(line)
                except ValueError:
                    self.log.appendPlainText(f"< malformed: {line}")
                    self.write_csv_event("MALFORMED_RX", line)
                    return
                self.latest = row
                self.update_metrics(row)
                self.write_csv_telemetry(row)
                # Keep the numeric monitor live at all times, but only build the
                # experiment data series and graph while CSV recording is active.
                if self.writer is not None:
                    self.rows.append(row)
                    self.telemetry_count += 1
                    if len(self.rows) > 5000:
                        self.rows = self.rows[-5000:]
                    if self.telemetry_count - self.plot_count >= 5:
                        self.canvas.draw_rows(self.rows)
                        self.plot_count = self.telemetry_count
                return
            self.log.appendPlainText(f"< {line}")
            if line.startswith(("FAULT,", "ERR,")):
                self.status.setText(line)

        def update_metrics(self, row: Telemetry) -> None:
            if row.sensor_status == "OK" and math.isfinite(row.flow_slm):
                self.metric_flow.setText(f"{row.flow_slm:+.3f} SLM")
            else:
                self.metric_flow.setText("-- SLM")
            self.metric_pressure.setText(
                f"Model {fmt_float(row.pressure_model_kpa)} kPa / "
                f"Syringe {fmt_float(row.pressure_syringe_kpa)} kPa"
            )
            self.metric_valves.setText(
                f"ATM {'ON' if row.atm else 'OFF'} / VAC {'ON' if row.vac else 'OFF'}"
            )
            self.metric_ppv.setText(f"{row.ppv_v:.3f} V, PWM {row.ppv_pwm_code}")
            self.metric_sensor.setText(f"{row.sensor_status} / {row.fault}")

        def csv_fields(self) -> list[str]:
            return [
                "host_time",
                "mcu_ms",
                "flow_slm",
                "atm",
                "vac",
                "ppv_target_v",
                "ppv_pwm_code",
                "pressure_model_kpa",
                "pressure_syringe_kpa",
                "sensor_status",
                "fault",
                "event",
                "raw_line",
                "record_type",
                "sample_ms",
                "sensor_raw",
                "uncorrected_flow_slm",
                "zero_offset_slm",
                "sampled_atm",
                "sampled_vac",
                "sampled_ppv_target_v",
            ]

        def start_recording(self) -> None:
            if self.writer is not None:
                self.log.appendPlainText("Recording is already active.")
                return
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            raw_name = self.filename_edit.text().strip()
            if not raw_name:
                raw_name = f"flow_exp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                self.filename_edit.setText(raw_name)
            path = Path(raw_name).expanduser()
            if not path.is_absolute() and path.parent == Path("."):
                path = DATA_DIR / path
            if path.suffix.lower() != ".csv":
                path = path.with_suffix(".csv")
            path.parent.mkdir(parents=True, exist_ok=True)
            # A new recording must not truncate an earlier PPV experiment.
            base_path = path
            suffix = 1
            while True:
                try:
                    self.csv_file = path.open("x", newline="", encoding="utf-8-sig")
                    break
                except FileExistsError:
                    path = base_path.with_name(f"{base_path.stem}_{suffix}{base_path.suffix}")
                    suffix += 1
            self.record_path = path
            self.writer = csv.DictWriter(self.csv_file, fieldnames=self.csv_fields())
            self.writer.writeheader()
            self.rows.clear()
            self.telemetry_count = 0
            self.plot_count = 0
            self.canvas.draw_rows([])
            self.filename_edit.setText(str(path))
            self.record_label.setText(f"Recording: {path.name}")
            self.log.appendPlainText(f"Start recording: {path}")
            self.write_csv_event("RECORD_START")

        def choose_csv_path(self) -> None:
            from PyQt5 import QtWidgets

            DATA_DIR.mkdir(parents=True, exist_ok=True)
            current = self.filename_edit.text().strip()
            if current:
                default = Path(current).expanduser()
                if not default.is_absolute() and default.parent == Path("."):
                    default = DATA_DIR / default
            else:
                default = DATA_DIR / f"flow_exp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            filename, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Choose CSV save location", str(default.with_suffix(".csv")), "CSV files (*.csv)"
            )
            if filename:
                self.filename_edit.setText(str(Path(filename).with_suffix(".csv")))

        def stop_recording(self) -> None:
            if self.writer is None:
                return
            self.write_csv_event("RECORD_STOP")
            self.csv_file.close()
            self.csv_file = None
            self.writer = None
            saved_path = self.record_path
            self.record_label.setText(f"Saved automatically: {saved_path}")
            self.log.appendPlainText(f"Recording stopped; CSV saved: {saved_path}; no valve commands were sent.")

        def csv_base_row(self, event: str = "") -> dict[str, object]:
            # Command/ACK rows are events, not copies of the previous sample.
            # This prevents duplicate timestamps and double-counted volume.
            row = None if event else self.latest
            now = datetime.now().astimezone().isoformat(timespec="milliseconds")
            if row is None:
                return {
                    "host_time": now,
                    "mcu_ms": "",
                    "flow_slm": "",
                    "atm": "",
                    "vac": "",
                    "ppv_target_v": "",
                    "ppv_pwm_code": "",
                    "pressure_model_kpa": "",
                    "pressure_syringe_kpa": "",
                    "sensor_status": "",
                    "fault": "",
                    "event": event,
                    "raw_line": "",
                }
            return {
                "host_time": now,
                "mcu_ms": row.mcu_ms,
                "flow_slm": row.flow_slm if math.isfinite(row.flow_slm) else "",
                "atm": row.atm,
                "vac": row.vac,
                "ppv_target_v": row.ppv_v,
                "ppv_pwm_code": row.ppv_pwm_code,
                "pressure_model_kpa": "" if math.isnan(row.pressure_model_kpa) else row.pressure_model_kpa,
                "pressure_syringe_kpa": "" if math.isnan(row.pressure_syringe_kpa) else row.pressure_syringe_kpa,
                "sensor_status": row.sensor_status,
                "fault": row.fault,
                "event": event,
                "raw_line": row.raw_line,
                "record_type": "telemetry",
                "sample_ms": row.sample_ms if row.sample_ms is not None else "",
                "sensor_raw": row.sensor_raw if row.sensor_raw is not None else "",
                "uncorrected_flow_slm": row.uncorrected_flow_slm if math.isfinite(row.uncorrected_flow_slm) else "",
                "zero_offset_slm": row.zero_offset_slm if math.isfinite(row.zero_offset_slm) else "",
                "sampled_atm": row.sampled_atm if row.sampled_atm is not None else "",
                "sampled_vac": row.sampled_vac if row.sampled_vac is not None else "",
                "sampled_ppv_target_v": row.sampled_ppv_v if math.isfinite(row.sampled_ppv_v) else "",
            }

        def write_csv_telemetry(self, row: Telemetry) -> None:
            if self.writer is None:
                return
            self.writer.writerow(self.csv_base_row(""))
            self.csv_file.flush()

        def write_csv_event(self, event: str, raw_line: str = "") -> None:
            if self.writer is None:
                return
            record = self.csv_base_row(event)
            record["record_type"] = "event"
            record["raw_line"] = raw_line
            self.writer.writerow(record)
            self.csv_file.flush()

        def closeEvent(self, event) -> None:
            self.stop_recording()
            self.disconnect_serial(send_stop=True)
            event.accept()

    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    window = Window()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
