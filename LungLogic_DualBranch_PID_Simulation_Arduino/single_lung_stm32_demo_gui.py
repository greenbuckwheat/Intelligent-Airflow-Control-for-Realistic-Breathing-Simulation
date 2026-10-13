"""Simulation UI plus an independent STM32/SFM3300 demonstration interface.

Run alongside single_lung_pid_gui.py. Real measurements never enter the PID.
Protocol and wiring: README_STM32_DEMO_CN.md.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import sys
import time

from PyQt5 import QtCore, QtGui, QtWidgets
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg

import single_lung_pid_gui as sim

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

BAUD = 115200
READY = "READY,LUNGLOGIC_DEMO_G474,1"


def output_mask(result, index: int, playing: bool, swap: bool = False) -> int:
    """Follow the displayed directional valve command, including early closure."""
    mask = int(result.valve_mask[index]) if playing and result is not None else 0
    if mask not in (0, 1, 2):
        raise ValueError("Invalid simulation valve mask")
    return ((mask & 1) << 1) | ((mask & 2) >> 1) if swap else mask


@dataclass(frozen=True)
class Telemetry:
    time_ms: int
    flow_slm: float
    sensor_status: str
    applied_mask: int
    watchdog: bool
    raw: int

    @classmethod
    def parse(cls, line: str) -> "Telemetry":
        f = line.split(",")
        if len(f) != 7 or f[0] != "FLOW":
            raise ValueError("Invalid telemetry format")
        stamp, flow, status, mask, watchdog, raw = int(f[1]), float(f[2]), f[3], int(f[4]), int(f[5]), int(f[6])
        if not 0 <= stamp <= 0xFFFFFFFF or mask not in (0, 1, 2) or watchdog not in (0, 1):
            raise ValueError("Invalid telemetry values")
        if status not in ("OK", "STARTING", "I2C", "CRC") or not 0 <= raw <= 65535:
            raise ValueError("Invalid sensor status")
        if status == "OK" and (not math.isfinite(flow) or abs(flow - (raw - 32768) / 120) > 0.002):
            raise ValueError("Inconsistent SFM3300 reading")
        return cls(stamp, flow if status == "OK" else math.nan, status, mask, bool(watchdog), raw)


class MainWindow(sim.MainWindow):
    def __init__(self):
        self.connection = None
        self.ready = False
        self.rx = bytearray()
        self.last_rx = self.last_tx = self.connected_at = 0.0
        self.last_mask = None
        self.samples = deque(maxlen=1500)
        self.display_index = 0
        super().__init__()
        self.setWindowTitle("LungLogic | Simulation + STM32 LED / Live SFM3300")
        self.io_timer = QtCore.QTimer(self)
        # Serial polling and chart refresh are presentation tasks; the
        # simulation playback clock is handled by the parent accumulator.
        self.io_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.io_timer.setInterval(20)
        self.io_timer.timeout.connect(self._poll_serial)
        self.io_timer.start()
        self.chart_timer = QtCore.QTimer(self)
        self.chart_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.chart_timer.setInterval(200)
        self.chart_timer.timeout.connect(self._draw_real_flow)
        self.chart_timer.start()
        self.refresh_ports()

    def _build_control_panel(self):
        panel = super()._build_control_panel()
        group = QtWidgets.QGroupBox("STM32 interface | 115200 baud")
        form = QtWidgets.QFormLayout(group)
        row = QtWidgets.QHBoxLayout()
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setEditable(True)
        row.addWidget(self.port_combo, 1)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_ports)
        row.addWidget(refresh)
        form.addRow("Serial port", row)
        self.connect_button = QtWidgets.QPushButton("Connect")
        self.connect_button.clicked.connect(self._toggle_connection)
        form.addRow(self.connect_button)
        self.swap = QtWidgets.QCheckBox("Swap PCB ATM / VAC (wiring check only)")
        self.swap.setToolTip("Default uses ATM for inspiration and VAC for expiration. Check only when verifying reversed PCB wiring.")
        self.swap.toggled.connect(lambda _: self._send_outputs(force=True))
        form.addRow(self.swap)
        self.link_label = QtWidgets.QLabel("Disconnected | simulation available")
        self.link_label.setWordWrap(True)
        form.addRow(self.link_label)
        self.command_label = QtWidgets.QLabel("Requested PCB: ATM OFF | VAC OFF")
        form.addRow(self.command_label)
        panel.layout().insertWidget(0, group)
        return panel

    def _build_ui(self):
        super()._build_ui()
        user_hardware = QtWidgets.QGroupBox("SFM3300 Hardware Flow")
        user_hardware_layout = QtWidgets.QVBoxLayout(user_hardware)
        connect_row = QtWidgets.QHBoxLayout()
        self.user_port_combo = QtWidgets.QComboBox()
        self.user_port_combo.setEditable(True)
        self.user_port_combo.currentTextChanged.connect(self.port_combo.setCurrentText)
        self.port_combo.currentTextChanged.connect(self.user_port_combo.setCurrentText)
        connect_row.addWidget(self.user_port_combo, 1)
        self.user_connect_button = QtWidgets.QPushButton("Connect")
        self.user_connect_button.clicked.connect(self._toggle_connection)
        connect_row.addWidget(self.user_connect_button)
        refresh_button = QtWidgets.QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh_ports)
        connect_row.addWidget(refresh_button)
        user_hardware_layout.addLayout(connect_row)
        self.user_hardware_status = QtWidgets.QLabel("Disconnected | simulation available")
        self.user_hardware_status.setWordWrap(True)
        user_hardware_layout.addWidget(self.user_hardware_status)
        self.user_sensor_flow = QtWidgets.QLabel("— SLM")
        self.user_sensor_flow.setStyleSheet("font-size:22px; color:#167b61; font-weight:bold")
        user_hardware_layout.addWidget(self.user_sensor_flow)
        hardware_note = QtWidgets.QLabel("Real SFM3300 readings are displayed separately; the lung model and PID remain simulated.")
        hardware_note.setWordWrap(True)
        user_hardware_layout.addWidget(hardware_note)
        user_figure = Figure(figsize=(6, 3), tight_layout=True)
        self.user_real_canvas = FigureCanvasQTAgg(user_figure)
        self.user_real_ax = user_figure.add_subplot(111)
        user_hardware_layout.addWidget(self.user_real_canvas, 1)
        self.user_flow_layout.addWidget(user_hardware, 1)

        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        title = QtWidgets.QLabel("LIVE HARDWARE — SFM3300-AW")
        title.setStyleSheet("font-size:20px; font-weight:bold")
        layout.addWidget(title)
        self.flow_label = QtWidgets.QLabel("— SLM")
        self.flow_label.setStyleSheet("font-size:36px; color:#167b61")
        layout.addWidget(self.flow_label)
        self.sensor_label = QtWidgets.QLabel("No hardware data")
        layout.addWidget(self.sensor_label)
        self.applied_label = QtWidgets.QLabel("MCU output feedback: unknown")
        layout.addWidget(self.applied_label)
        note = QtWidgets.QLabel("Real sensor data is displayed independently. PID, lung motion and demo parameters remain simulated.")
        note.setWordWrap(True)
        layout.addWidget(note)
        figure = Figure(figsize=(7, 4), tight_layout=True)
        self.real_canvas = FigureCanvasQTAgg(figure)
        self.real_ax = figure.add_subplot(111)
        layout.addWidget(self.real_canvas, 1)
        self.tabs.addTab(page, "Live Flow Meter (Hardware)")

    def refresh_ports(self):
        previous = self.port_combo.currentText()
        self.port_combo.clear()
        if list_ports:
            self.port_combo.addItems([p.device for p in list_ports.comports()])
        if previous:
            self.port_combo.setCurrentText(previous)
        self.user_port_combo.setCurrentText(self.port_combo.currentText())

    def _toggle_connection(self):
        if self.connection:
            self.disconnect_serial()
            return
        if serial is None:
            self.link_label.setText("Install pyserial: python -m pip install pyserial")
            self.user_hardware_status.setText(self.link_label.text())
            return
        try:
            self.connection = serial.Serial(self.port_combo.currentText().strip(), BAUD, timeout=0, write_timeout=0.05)
            self.connection.reset_input_buffer()
        except (OSError, serial.SerialException, ValueError) as exc:
            self.disconnect_serial(f"Connection failed: {exc}")
            return
        self.ready = False
        self.samples.clear()
        self.rx.clear()
        self.last_mask = None
        self.connected_at = self.last_rx = time.monotonic()
        self.last_tx = 0
        self.port_combo.setEnabled(False)
        self.user_port_combo.setEnabled(False)
        self.connect_button.setText("Disconnect")
        self.user_connect_button.setText("Disconnect")
        self.link_label.setText("Waiting for LungLogic STM32 handshake…")
        self.user_hardware_status.setText(self.link_label.text())
        self._write("HELLO")

    def _write(self, command):
        if not self.connection:
            return False
        try:
            data = (command + "\n").encode("ascii")
            if self.connection.write(data) != len(data):
                raise OSError("Incomplete serial write")
            self.last_tx = time.monotonic()
            return True
        except (OSError, serial.SerialException) as exc:
            self.disconnect_serial(f"Serial error: {exc}", send_stop=False)
            return False

    def disconnect_serial(self, reason="Disconnected | simulation available", send_stop=True):
        connection, self.connection = self.connection, None
        if connection:
            try:
                if send_stop:
                    connection.write(b"STOP\n")
            except (OSError, serial.SerialException):
                pass
            finally:
                connection.close()
        self.ready = False
        self.last_mask = None
        self.rx.clear()
        self.port_combo.setEnabled(True)
        self.user_port_combo.setEnabled(True)
        self.connect_button.setText("Connect")
        self.user_connect_button.setText("Connect")
        self.link_label.setText(reason)
        self.user_hardware_status.setText(reason)
        self.flow_label.setText("— SLM")
        self.user_sensor_flow.setText("— SLM")
        self.sensor_label.setText("No live data")
        self.applied_label.setText("MCU output feedback: unknown / disconnected")

    def _send_outputs(self, force=False):
        mask = output_mask(self.result, self.display_index, self.playing, self.swap.isChecked())
        self.command_label.setText(f"Requested PCB: ATM {'ON' if mask & 1 else 'OFF'} | VAC {'ON' if mask & 2 else 'OFF'}")
        if self.ready and (force or mask != self.last_mask or time.monotonic() - self.last_tx >= 0.1):
            if self._write(f"LED,{mask}"):
                self.last_mask = mask

    def _poll_serial(self):
        if not self.connection:
            return
        try:
            self.rx.extend(self.connection.read(min(self.connection.in_waiting, 4096)))
            if len(self.rx) > 8192:
                raise ValueError("Serial input overflow")
            while b"\n" in self.rx:
                line, _, remainder = self.rx.partition(b"\n")
                self.rx = bytearray(remainder)
                self._handle_line(line.decode("ascii", errors="replace").strip())
        except (OSError, serial.SerialException, ValueError) as exc:
            self.disconnect_serial(f"Serial data error: {exc}")
            return
        now = time.monotonic()
        if not self.ready:
            if now - self.connected_at > 5:
                self.disconnect_serial("Handshake timed out: upload STM32 demo firmware")
            elif now - self.last_tx > 0.5:
                self._write("HELLO")
        elif now - self.last_rx > 1.0:
            self.disconnect_serial("Telemetry timed out; reconnect STM32")
        else:
            self._send_outputs()

    def _handle_line(self, line):
        if line == READY:
            self.ready = True
            self.last_rx = time.monotonic()
            self.link_label.setText("Connected | STM32 demo protocol v1")
            self.user_hardware_status.setText(self.link_label.text())
            self._send_outputs(force=True)
        elif line.startswith("FLOW,") and self.ready:
            try:
                row = Telemetry.parse(line)
            except ValueError:
                return  # Corrupt lines do not refresh the connection timeout.
            self.last_rx = time.monotonic()
            self.samples.append((self.last_rx, row.flow_slm))
            self.flow_label.setText(f"{row.flow_slm:+.3f} SLM" if row.sensor_status == "OK" else "— SLM")
            self.user_sensor_flow.setText(self.flow_label.text())
            self.sensor_label.setText(f"Sensor: {row.sensor_status} | raw: {row.raw} | MCU: {row.time_ms / 1000:.2f} s")
            self.user_hardware_status.setText(self.sensor_label.text())
            self.applied_label.setText(
                f"MCU GPIO: ATM {'ON' if row.applied_mask & 1 else 'OFF'} | VAC {'ON' if row.applied_mask & 2 else 'OFF'}"
                + (" | HOST TIMEOUT" if row.watchdog else ""))
        elif line.startswith("INFO,"):
            self.link_label.setText(line[5:])
            self.user_hardware_status.setText(self.link_label.text())

    def _draw_real_flow(self):
        now = time.monotonic()
        rows = [(t - now, q) for t, q in self.samples if now - t <= 15 and math.isfinite(q)]
        for ax, canvas in ((self.real_ax, self.real_canvas), (self.user_real_ax, self.user_real_canvas)):
            ax.clear()
            if rows:
                ax.plot(*zip(*rows), color="#167b61", label="Measured SFM3300")
            ax.set(xlim=(-15, 0), xlabel="Time relative to now / s", ylabel="Measured flow / SLM",
                   title="Hardware flow — last 15 seconds")
            ax.grid(alpha=0.25)
            canvas.draw_idle()

    def _show_index(self, index):
        super()._show_index(index)
        self.display_index = int(max(0, min(index, self.result.time_s.size - 1))) if self.result is not None else 0
        self._send_outputs()

    def toggle_play(self):
        super().toggle_play()
        self.display_index = int(self.play_index)
        self._send_outputs(force=True)

    def stop_playback(self):
        super().stop_playback()
        self._send_outputs(force=True)

    def apply_and_reset(self):
        # Stop before a potentially slow simulation recalculation.
        self.stop_playback()
        super().apply_and_reset()

    def closeEvent(self, event):
        self.stop_playback()
        self.io_timer.stop()
        self.chart_timer.stop()
        self.disconnect_serial()
        self.live_monitor.close()
        event.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QtGui.QFont("Segoe UI", 9))
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
