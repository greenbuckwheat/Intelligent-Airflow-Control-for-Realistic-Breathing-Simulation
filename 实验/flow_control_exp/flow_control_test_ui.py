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
READY = "READY,FLOW_EXP_G474,1"
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

    @classmethod
    def parse(cls, line: str) -> "Telemetry":
        fields = line.split(",")
        if len(fields) != 11 or fields[0] != "TEL":
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

            top = QtWidgets.QGroupBox("连接")
            top_layout = QtWidgets.QGridLayout(top)
            self.port_combo = QtWidgets.QComboBox()
            self.refresh_button = QtWidgets.QPushButton("刷新串口")
            self.connect_button = QtWidgets.QPushButton("连接")
            self.disconnect_button = QtWidgets.QPushButton("断开")
            self.status = QtWidgets.QLabel("未连接")
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

            valves = QtWidgets.QGroupBox("方向阀")
            valve_layout = QtWidgets.QGridLayout(valves)
            self.atm_on = QtWidgets.QPushButton("ATM 开启")
            self.atm_off = QtWidgets.QPushButton("ATM 关闭")
            self.vac_on = QtWidgets.QPushButton("VAC 开启")
            self.vac_off = QtWidgets.QPushButton("VAC 关闭")
            self.stop_all = QtWidgets.QPushButton("全部电磁阀关闭 / STOP")
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

            ppv = QtWidgets.QGroupBox("PPV 目标电压")
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
            custom.addWidget(QtWidgets.QLabel("自定义 V"))
            custom.addWidget(self.ppv_spin)
            custom_button = QtWidgets.QPushButton("发送")
            custom_button.clicked.connect(lambda: self.set_ppv(self.ppv_spin.value()))
            custom.addWidget(custom_button)
            ppv_layout.addLayout(custom)
            hint = QtWidgets.QLabel(
                "本轮建议：3.800–4.500 V，每档 0.050 V；改变电压后至少等待 "
                f"{PPV_SETTLE_S:.0f} 秒再取稳态数据。0 V 全开，电压升高逐渐夹紧；"
                "实际输出需用万用表校准。"
            )
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #475569;")
            ppv_layout.addWidget(hint)
            left_layout.addWidget(ppv)

            record = QtWidgets.QGroupBox("数据记录")
            record_layout = QtWidgets.QGridLayout(record)
            self.start_record_button = QtWidgets.QPushButton("开始记录")
            self.stop_record_button = QtWidgets.QPushButton("停止记录")
            self.zero_flow_button = QtWidgets.QPushButton("流量归零")
            self.filename_edit = QtWidgets.QLineEdit()
            self.filename_edit.setPlaceholderText("例如 function_check_atm.csv 或 ppv_sweep_vac.csv")
            self.filename_edit.setText(
                f"ppv_sweep_3p8_to_4p5_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            )
            self.choose_csv_button = QtWidgets.QPushButton("选择位置")
            self.record_label = QtWidgets.QLabel("未记录")
            self.start_record_button.clicked.connect(self.start_recording)
            self.stop_record_button.clicked.connect(self.stop_recording)
            self.zero_flow_button.clicked.connect(lambda: self.send("ZERO_FLOW", "ZERO_FLOW"))
            self.choose_csv_button.clicked.connect(self.choose_csv_path)
            record_layout.addWidget(QtWidgets.QLabel("CSV 文件名 / 路径"), 0, 0, 1, 2)
            record_layout.addWidget(self.filename_edit, 1, 0)
            record_layout.addWidget(self.choose_csv_button, 1, 1)
            record_layout.addWidget(self.start_record_button, 2, 0)
            record_layout.addWidget(self.stop_record_button, 2, 1)
            record_layout.addWidget(self.zero_flow_button, 3, 0, 1, 2)
            record_layout.addWidget(self.record_label, 4, 0, 1, 2)
            left_layout.addWidget(record)

            monitor = QtWidgets.QGroupBox("实时监测")
            monitor_layout = QtWidgets.QFormLayout(monitor)
            self.metric_flow = self.metric_label()
            self.metric_pressure = self.metric_label()
            self.metric_valves = self.metric_label()
            self.metric_ppv = self.metric_label()
            self.metric_sensor = self.metric_label()
            monitor_layout.addRow("当前流量", self.metric_flow)
            monitor_layout.addRow("压力", self.metric_pressure)
            monitor_layout.addRow("阀门状态", self.metric_valves)
            monitor_layout.addRow("PPV", self.metric_ppv)
            monitor_layout.addRow("传感器/故障", self.metric_sensor)
            left_layout.addWidget(monitor)

            docs = QtWidgets.QTextBrowser()
            docs.setHtml(
                "<h3>两个初步实验</h3>"
                "<p>本工具用于开放环验证原 hardware 控制代码所依赖的实际硬件行为，"
                "不修改原 GUI 或原固件文件。</p>"
                "<table border='1' cellspacing='0' cellpadding='5'>"
                "<tr><th>实验</th><th>目的</th><th>如何使用程序</th></tr>"
                "<tr>"
                "<td>功能验证实验</td>"
                "<td>确认指令能正确控制 ATM/VAC 两个电磁阀，并确认 PPV 能调节气流。</td>"
                "<td>分别操作 ATM、VAC 和 PPV，观察实际动作、流量响应和阀门状态。</td>"
                "</tr>"
                "<tr>"
                "<td>PPV 非线性特性实验</td>"
                "<td>测量不同 PPV 目标电压下的流量，用于拟合指数 gamma。</td>"
                "<td>从 3.800 V 开始，以 0.050 V 增加到 4.500 V。每次改变电压后等待至少 3 秒，"
                "再记录稳态段；ATM 与 VAC 分开测试，并尽量恢复相同袋体体积和源压力。</td>"
                "</tr>"
                "</table>"
                "<p>CSV 会保存 MCU 时间戳、流量、模体侧压力、针筒侧压力、ATM/VAC 状态、"
                "PPV 目标电压、传感器状态和操作事件。压力通道未接入或未启用时记录为空。</p>"
                "<p><b>停止记录</b>只关闭 CSV 文件；不会关闭阀门。"
                "<b>STOP</b>才会关闭 ATM/VAC 并把 PPV 设为 5 V。</p>"
            )
            left_layout.addWidget(docs, 1)

            right = QtWidgets.QWidget()
            right_layout = QtWidgets.QVBoxLayout(right)
            splitter.addWidget(right)
            splitter.setSizes([410, 770])

            self.canvas = FlowCanvas()
            right_layout.addWidget(self.canvas, 3)
            log_box = QtWidgets.QGroupBox("串口日志")
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
                self.port_combo.addItem("未检测到串口", "")
                return
            for port in ports:
                self.port_combo.addItem(f"{port.device} | {port.description}", port.device)

        def connect_serial(self) -> None:
            port = self.port_combo.currentData()
            if not port:
                self.log.appendPlainText("请选择有效串口。")
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
                self.status.setText(f"正在连接 {port} ...")
                self.log.appendPlainText(f"> HELLO ({port})")
            except Exception as exc:
                self.conn = None
                self.status.setText("连接失败")
                self.log.appendPlainText(f"连接失败: {exc}")

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
            self.status.setText("未连接")

        def set_ppv(self, volts: float) -> None:
            volts = max(0.0, min(5.0, float(volts)))
            self.ppv_spin.setValue(volts)
            self.send(f"PPV,{round(volts * 1000)}", f"PPV_{volts:.3f}V")

        def send(self, command: str, event: str) -> None:
            if self.conn is None:
                self.log.appendPlainText("未连接。")
                return
            if not self.ready and command not in ("HELLO", "PING", "STOP"):
                self.log.appendPlainText(f"尚未握手到 {READY}，拒绝发送: {command}")
                return
            try:
                self.conn.write((command + "\n").encode("ascii"))
                self.log.appendPlainText(f"> {command}")
                self.write_csv_event(event)
            except Exception as exc:
                self.log.appendPlainText(f"发送失败: {exc}")
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
                    raise RuntimeError("串口缓存异常增长。")
                while b"\n" in self.rx:
                    raw, _, rest = self.rx.partition(b"\n")
                    self.rx = bytearray(rest)
                    line = raw.decode("ascii", errors="replace").strip()
                    if line:
                        self.last_rx = now
                        self.handle_line(line)
                if now - self.last_rx > 5.0:
                    raise RuntimeError("串口超时；将断开连接。")
            except Exception as exc:
                self.log.appendPlainText(str(exc))
                self.disconnect_serial()

        def handle_line(self, line: str) -> None:
            if line == "PONG":
                return
            if line == READY:
                if not self.ready:
                    self.ready = True
                    self.status.setText("已连接实验固件 FLOW_EXP_G474")
                self.log.appendPlainText(f"< {line}")
                return
            if line.startswith("READY,") and line != READY:
                self.ready = False
                self.status.setText("固件不匹配")
                self.log.appendPlainText(f"< {line}")
                self.log.appendPlainText("请上传 实验/flow_control_exp/flow_control_exp.ino 后再使用此实验 UI。")
                return
            if line.startswith("TEL,"):
                try:
                    row = Telemetry.parse(line)
                except ValueError:
                    self.log.appendPlainText(f"< malformed: {line}")
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
                f"模体 {fmt_float(row.pressure_model_kpa)} kPa / "
                f"针筒 {fmt_float(row.pressure_syringe_kpa)} kPa"
            )
            self.metric_valves.setText(
                f"ATM {'开' if row.atm else '关'} / VAC {'开' if row.vac else '关'}"
            )
            self.metric_ppv.setText(f"{row.ppv_v:.3f} V, PWM {row.ppv_pwm_code}")
            self.metric_sensor.setText(f"{row.sensor_status} / {row.fault}")

        def csv_fields(self) -> list[str]:
            return [
                "主机时间",
                "MCU时间戳ms",
                "流量SLM",
                "ATM状态",
                "VAC状态",
                "PPV目标电压V",
                "PPV_PWM码",
                "模体侧压力kPa",
                "针筒侧压力kPa",
                "传感器状态",
                "故障",
                "操作事件",
                "原始串口行",
            ]

        def start_recording(self) -> None:
            if self.writer is not None:
                self.log.appendPlainText("已经在记录。")
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
            self.csv_file = path.open("w", newline="", encoding="utf-8-sig")
            self.record_path = path
            self.writer = csv.DictWriter(self.csv_file, fieldnames=self.csv_fields())
            self.writer.writeheader()
            self.rows.clear()
            self.telemetry_count = 0
            self.plot_count = 0
            self.canvas.draw_rows([])
            self.filename_edit.setText(str(path))
            self.record_label.setText(f"记录中: {path.name}")
            self.log.appendPlainText(f"开始记录: {path}")
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
                self, "选择 CSV 保存位置", str(default.with_suffix(".csv")), "CSV files (*.csv)"
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
            self.record_label.setText(f"已自动保存: {saved_path}")
            self.log.appendPlainText(f"停止记录并已保存 CSV: {saved_path}；未发送阀门命令。")

        def csv_base_row(self, event: str = "") -> dict[str, object]:
            row = self.latest
            now = datetime.now().astimezone().isoformat(timespec="milliseconds")
            if row is None:
                return {
                    "主机时间": now,
                    "MCU时间戳ms": "",
                    "流量SLM": "",
                    "ATM状态": "",
                    "VAC状态": "",
                    "PPV目标电压V": "",
                    "PPV_PWM码": "",
                    "模体侧压力kPa": "",
                    "针筒侧压力kPa": "",
                    "传感器状态": "",
                    "故障": "",
                    "操作事件": event,
                    "原始串口行": "",
                }
            return {
                "主机时间": now,
                "MCU时间戳ms": row.mcu_ms,
                "流量SLM": row.flow_slm if math.isfinite(row.flow_slm) else "",
                "ATM状态": row.atm,
                "VAC状态": row.vac,
                "PPV目标电压V": row.ppv_v,
                "PPV_PWM码": row.ppv_pwm_code,
                "模体侧压力kPa": "" if math.isnan(row.pressure_model_kpa) else row.pressure_model_kpa,
                "针筒侧压力kPa": "" if math.isnan(row.pressure_syringe_kpa) else row.pressure_syringe_kpa,
                "传感器状态": row.sensor_status,
                "故障": row.fault,
                "操作事件": event,
                "原始串口行": row.raw_line,
            }

        def write_csv_telemetry(self, row: Telemetry) -> None:
            if self.writer is None:
                return
            self.writer.writerow(self.csv_base_row(""))
            self.csv_file.flush()

        def write_csv_event(self, event: str) -> None:
            if self.writer is None:
                return
            self.writer.writerow(self.csv_base_row(event))
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
