"""PyQt5 visual digital twin for the dual-branch LungLogic system."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
import matplotlib
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.patches import Circle, Ellipse, FancyBboxPatch, Rectangle

from lung_model import (
    STATE_COMPLETE,
    STATE_EXPIRATION,
    STATE_INSPIRATION,
    STATE_NAMES,
    ExperimentConfig,
    SimulationResult,
    load_config,
    run_simulation,
    save_config,
)


SCRIPT_DIR = Path(__file__).resolve().parent
matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False


class LungModelCanvas(FigureCanvas):
    def __init__(self) -> None:
        self.figure = Figure(figsize=(8.5, 7.2), tight_layout=True)
        super().__init__(self.figure)
        self.ax = self.figure.add_subplot(111)
        self.figure.patch.set_facecolor("white")

    def draw_frame(self, result: SimulationResult, index: int, cfg: ExperimentConfig) -> None:
        ax = self.ax
        ax.clear()
        ax.set_xlim(0, 10)
        ax.set_ylim(0, 10)
        ax.set_aspect("equal")
        ax.axis("off")
        ax.set_title("Dual-Branch Pneumatic Lung Digital Twin", fontsize=16, fontweight="bold", pad=10)

        state = int(result.state[index])
        time_s = result.time_s[index]
        cycle = int(result.cycle[index])
        mask = int(result.valve_mask[index])
        values = [
            ("Left Lung", 3.0, result.v_left_ml[index], result.q_left_slm[index],
             result.opening_left[index], bool(mask & 1), bool(mask & 2), cfg.left.capacity_ml),
            ("Right Lung", 7.0, result.v_right_ml[index], result.q_right_slm[index],
             result.opening_right[index], bool(mask & 4), bool(mask & 8), cfg.right.capacity_ml),
        ]

        ax.text(
            0.25, 9.45,
            f"t = {time_s:6.2f} s    State = {STATE_NAMES[state]}    Cycle = {cycle + 1}/{cfg.cycles}",
            fontsize=11, family="monospace",
            bbox=dict(boxstyle="round,pad=0.35", fc="#edf4fb", ec="#7a9bbd"),
        )

        ax.add_patch(FancyBboxPatch((0.35, 7.95), 1.6, 0.7, boxstyle="round,pad=0.1",
                                    fc="#dff3ff", ec="#2878a8", lw=1.5))
        ax.text(1.15, 8.3, "Atmosphere Inlet", ha="center", va="center", fontsize=11)
        ax.add_patch(FancyBboxPatch((8.05, 7.95), 1.6, 0.7, boxstyle="round,pad=0.1",
                                    fc="#eee4fa", ec="#7550a5", lw=1.5))
        ax.text(8.85, 8.3, "Regulated Vacuum", ha="center", va="center", fontsize=11)

        for name, x, volume, flow, opening, atm_open, vac_open, capacity in values:
            # Source pipes and two normally-closed routing valves.
            ax.plot([1.95, x - 0.52], [8.30, 8.30], color="#2878a8", lw=2)
            ax.plot([8.05, x + 0.52], [8.30, 8.30], color="#7550a5", lw=2)
            ax.plot([x - 0.52, x - 0.52], [8.30, 7.25], color="#555", lw=2)
            ax.plot([x + 0.52, x + 0.52], [8.30, 7.25], color="#555", lw=2)
            self._valve(ax, x - 0.52, 7.15, atm_open, "ATM")
            self._valve(ax, x + 0.52, 7.15, vac_open, "VAC")
            ax.plot([x - 0.52, x - 0.52, x], [6.82, 6.55, 6.25], color="#555", lw=2)
            ax.plot([x + 0.52, x + 0.52, x], [6.82, 6.55, 6.25], color="#555", lw=2)

            # PPV40V proportional pinch valve and SFM3300.
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

            # Flow arrow: positive is toward the lung, negative is toward the sources.
            if abs(flow) > 0.25:
                dy = -0.45 if flow > 0 else 0.45
                y0 = 3.95 if flow > 0 else 3.55
                ax.annotate("", xy=(x, y0 + dy), xytext=(x, y0),
                            arrowprops=dict(arrowstyle="-|>", color="#e64a35", lw=2.3))

            fraction = float(np.clip(volume / max(capacity, 1.0), 0.0, 1.0))
            width = 1.05 + 1.40 * fraction
            height = 1.30 + 1.75 * fraction
            lung_color = "#f49a9a" if state == STATE_INSPIRATION else "#b8d7f0"
            if state == STATE_EXPIRATION:
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


class ResultCanvas(FigureCanvas):
    def __init__(self) -> None:
        self.figure = Figure(figsize=(9, 7.5), tight_layout=True)
        super().__init__(self.figure)
        self.axes = self.figure.subplots(4, 1, sharex=True)
        self.time_markers = []

    def set_result(self, result: SimulationResult, cfg: ExperimentConfig) -> None:
        for ax in self.axes:
            ax.clear()
        t = result.time_s
        self.axes[0].plot(t, result.v_left_ml, label="Left")
        self.axes[0].plot(t, result.v_right_ml, label="Right")
        self.axes[0].axhline(cfg.left_target_ml, color="k", ls="--", lw=0.8)
        self.axes[0].set_ylabel("Volume / mL")
        self.axes[1].plot(t, result.q_left_slm, label="Left")
        self.axes[1].plot(t, result.q_right_slm, label="Right")
        self.axes[1].plot(t, result.qref_left_slm, "k--", lw=0.9, label="Reference")
        self.axes[1].set_ylabel("Flow / SLM")
        self.axes[2].plot(t, result.opening_left, label="Left")
        self.axes[2].plot(t, result.opening_right, label="Right")
        self.axes[2].set_ylabel("Opening")
        self.axes[3].step(t, result.state, where="post", label="State")
        self.axes[3].set_yticks(sorted(STATE_NAMES))
        self.axes[3].set_yticklabels([STATE_NAMES[k] for k in sorted(STATE_NAMES)], fontsize=7)
        self.axes[3].set_xlabel("Time / s")
        self.time_markers = []
        for ax in self.axes:
            ax.grid(True, ls="--", alpha=0.3)
            ax.legend(loc="best", ncol=3, fontsize=8)
            self.time_markers.append(ax.axvline(0.0, color="#d62728", lw=1.2))
        self.draw_idle()

    def set_time(self, time_s: float) -> None:
        for marker in self.time_markers:
            marker.set_xdata([time_s, time_s])
        self.draw_idle()


class LiveFlowCanvas(FigureCanvas):
    def __init__(self) -> None:
        self.figure = Figure(figsize=(6.2, 3.4), tight_layout=True)
        super().__init__(self.figure)
        self.ax = self.figure.add_subplot(111)
        self.figure.patch.set_facecolor("white")

    def set_result(self, result: SimulationResult | None, index: int, window_s: float = 8.0) -> None:
        self.ax.clear()
        self.ax.set_title("Live Flow Rate", fontsize=11, fontweight="bold")
        self.ax.set_ylabel("Flow / SLM")
        self.ax.set_xlabel("Time / s")
        self.ax.grid(True, ls="--", alpha=0.3)
        if result is not None and result.time_s.size:
            index = int(np.clip(index, 0, result.time_s.size - 1))
            now = float(result.time_s[index])
            start_time = max(0.0, now - window_s)
            start = int(np.searchsorted(result.time_s, start_time, side="left"))
            t = result.time_s[start:index + 1]
            self.ax.plot(t, result.q_left_slm[start:index + 1], label="Left", lw=1.8)
            self.ax.plot(t, result.q_right_slm[start:index + 1], label="Right", lw=1.8)
            self.ax.plot(t, result.qref_left_slm[start:index + 1], "k--", label="Reference", lw=1.0)
            self.ax.axvline(now, color="#d62728", lw=1.0)
            self.ax.set_xlim(start_time, max(start_time + window_s, now + 0.25))
            ymin = min(-2.0, float(np.min(result.q_left_slm[start:index + 1])) - 2.0,
                       float(np.min(result.q_right_slm[start:index + 1])) - 2.0)
            ymax = max(2.0, float(np.max(result.q_left_slm[start:index + 1])) + 2.0,
                       float(np.max(result.q_right_slm[start:index + 1])) + 2.0)
            self.ax.set_ylim(ymin, ymax)
            self.ax.legend(loc="upper right", ncol=3, fontsize=8)
        self.draw_idle()


class LiveMonitorWindow(QtWidgets.QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent, QtCore.Qt.Window)
        self.setWindowTitle("LungLogic Live Monitor")
        self.resize(620, 640)
        self.metric_labels: Dict[str, QtWidgets.QLabel] = {}
        self._last_plot_index = -1
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QGroupBox("Playback Status")
        header_form = QtWidgets.QFormLayout(header)
        for key, label in (
            ("time", "Time"),
            ("state", "State"),
            ("cycle", "Cycle"),
        ):
            value = self._value_label()
            self.metric_labels[key] = value
            header_form.addRow(label, value)
        layout.addWidget(header)

        branches = QtWidgets.QHBoxLayout()
        branches.addWidget(self._branch_group("left", "Left Lung"))
        branches.addWidget(self._branch_group("right", "Right Lung"))
        layout.addLayout(branches)

        total = QtWidgets.QGroupBox("Combined Flow")
        total_form = QtWidgets.QFormLayout(total)
        for key, label in (
            ("total_flow", "Left + right"),
            ("flow_balance", "Left - right"),
        ):
            value = self._value_label()
            self.metric_labels[key] = value
            total_form.addRow(label, value)
        layout.addWidget(total)

        self.flow_canvas = LiveFlowCanvas()
        layout.addWidget(self.flow_canvas, stretch=1)

    def _branch_group(self, prefix: str, title: str) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox(title)
        form = QtWidgets.QFormLayout(group)
        for suffix, label in (
            ("flow", "Flow rate"),
            ("reference", "Reference flow"),
            ("volume", "Volume"),
            ("opening", "PPV opening"),
            ("dac", "PPV command"),
            ("valves", "Routing valves"),
        ):
            value = self._value_label()
            self.metric_labels[f"{prefix}_{suffix}"] = value
            form.addRow(label, value)
        return group

    @staticmethod
    def _value_label() -> QtWidgets.QLabel:
        label = QtWidgets.QLabel("--")
        label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        label.setMinimumWidth(120)
        label.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")
        return label

    def update_frame(self, result: SimulationResult | None, index: int, cfg: ExperimentConfig | None) -> None:
        if result is None or cfg is None or result.time_s.size == 0:
            return
        index = int(np.clip(index, 0, result.time_s.size - 1))
        mask = int(result.valve_mask[index])
        state = int(result.state[index])
        cycle = int(result.cycle[index])
        left_flow = float(result.q_left_slm[index])
        right_flow = float(result.q_right_slm[index])

        self.metric_labels["time"].setText(f"{result.time_s[index]:.2f} s")
        self.metric_labels["state"].setText(STATE_NAMES[state])
        self.metric_labels["cycle"].setText(f"{cycle + 1} / {cfg.cycles}")

        self._set_branch("left", left_flow, float(result.qref_left_slm[index]),
                         float(result.v_left_ml[index]), float(result.opening_left[index]),
                         float(result.command_left_v[index]), bool(mask & 1), bool(mask & 2))
        self._set_branch("right", right_flow, float(result.qref_right_slm[index]),
                         float(result.v_right_ml[index]), float(result.opening_right[index]),
                         float(result.command_right_v[index]), bool(mask & 4), bool(mask & 8))
        self.metric_labels["total_flow"].setText(f"{left_flow + right_flow:+.2f} SLM")
        self.metric_labels["flow_balance"].setText(f"{left_flow - right_flow:+.2f} SLM")
        if index == 0 or index >= result.time_s.size - 1 or abs(index - self._last_plot_index) >= 10:
            self.flow_canvas.set_result(result, index)
            self._last_plot_index = index

    def _set_branch(
        self,
        prefix: str,
        flow: float,
        reference: float,
        volume: float,
        opening: float,
        dac: float,
        atm_open: bool,
        vac_open: bool,
    ) -> None:
        self.metric_labels[f"{prefix}_flow"].setText(f"{flow:+.2f} SLM")
        self.metric_labels[f"{prefix}_reference"].setText(f"{reference:.2f} SLM")
        self.metric_labels[f"{prefix}_volume"].setText(f"{volume:.1f} mL")
        self.metric_labels[f"{prefix}_opening"].setText(f"{opening * 100.0:.1f} %")
        self.metric_labels[f"{prefix}_dac"].setText(f"{dac:.2f} V")
        atm = "ATM open" if atm_open else "ATM closed"
        vac = "VAC open" if vac_open else "VAC closed"
        self.metric_labels[f"{prefix}_valves"].setText(f"{atm}, {vac}")


class FormulaDialog(QtWidgets.QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("LungLogic Modeling and PID Control Equations")
        self.resize(900, 720)
        layout = QtWidgets.QVBoxLayout(self)
        text = QtWidgets.QTextBrowser()
        text.setFont(QtGui.QFont("Consolas", 10))
        text.setPlainText(
            "LungLogic Dual-Branch Lung Model and Control\n\n"
            "1. State convention\n"
            "   inspiration: atmosphere valve open, vacuum valve closed, Q > 0\n"
            "   expiration : atmosphere valve closed, vacuum valve open, Q < 0\n"
            "   dead time  : both directional valves on the same side are closed\n\n"
            "2. First-order grey-box model\n"
            "   Inspiration: dV/dt = a^gamma (Vmax - V) / tau_in\n"
            "   Expiration : dV/dt = -a^gamma V / tau_out\n"
            "   Flow       : Q = 0.06 dV/dt  [SLM]\n\n"
            "3. Target flow\n"
            "   Q*_in = 0.06 Vtarget / Tfill\n"
            "   Default per side: 0.06 x 250 / 1.3 = 11.54 SLM\n\n"
            "4. Model feedforward + PID\n"
            "   e = Q* - |Qmeasured|\n"
            "   d = -d|Qmeasured|/dt\n"
            "   a = clip(a_ff + Kp e + Ki integral(e dt) + Kd d, 0, 1)\n"
            "   Conditional integration prevents windup while the valve is saturated.\n\n"
            "5. PPV40V command\n"
            "   Ucmd = 5(1-a) V\n"
            "   a=1 -> 0 V -> fully open; a=0 -> 5 V -> fully closed\n\n"
            "6. Volume target\n"
            "   Left and right targets are 250 mL each, giving a 500 mL symmetric total.\n"
            "   If one side reaches its target first, that side closes independently\n"
            "   while the other side continues control.\n\n"
            "Note: tau_out, valve exponent, and sensor dynamics are still initial\n"
            "values and should be identified from bench CSV data."
        )
        layout.addWidget(text)
        button = QtWidgets.QPushButton("Close")
        button.clicked.connect(self.accept)
        layout.addWidget(button, alignment=QtCore.Qt.AlignRight)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("LungLogic PID Digital Twin | Dual-Branch Pneumatic Lung")
        self.resize(1380, 880)
        self.cfg = load_config(SCRIPT_DIR / "config.example.json")
        self.result: SimulationResult | None = None
        self.play_index = 0.0
        self.playing = False
        self.inputs: Dict[str, QtWidgets.QDoubleSpinBox] = {}
        self._build_ui()
        self.live_monitor = LiveMonitorWindow(self)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self._advance)
        self.apply_and_reset()
        QtCore.QTimer.singleShot(0, self._show_live_monitor)

    def _build_ui(self) -> None:
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(self._build_control_panel())
        self.tabs = QtWidgets.QTabWidget()
        self.model_canvas = LungModelCanvas()
        self.plot_canvas = ResultCanvas()
        self.tabs.addTab(self.model_canvas, "Dynamic Model")
        self.tabs.addTab(self.plot_canvas, "Live Curves")
        splitter.addWidget(self.tabs)
        splitter.setSizes([390, 990])
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)
        self.statusBar().showMessage("After changing parameters, click \"Save and Apply\" before starting playback.")

    def _build_control_panel(self) -> QtWidgets.QWidget:
        outer = QtWidgets.QWidget()
        outer.setMinimumWidth(365)
        layout = QtWidgets.QVBoxLayout(outer)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        content = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(content)

        experiment = QtWidgets.QGroupBox("Breathing Parameters")
        form = QtWidgets.QFormLayout(experiment)
        self._spin(form, "rr", "Respiratory rate / bpm", self.cfg.respiratory_rate_bpm, 1, 40, 1, 2)
        self._spin(form, "ie_in", "I:E inspiration", self.cfg.ie_inspiration, 0.1, 10, 0.1, 2)
        self._spin(form, "ie_out", "I:E expiration", self.cfg.ie_expiration, 0.1, 10, 0.1, 2)
        self._spin(form, "v_left", "Left target / mL", self.cfg.left_target_ml, 10, 320, 5, 1)
        self._spin(form, "v_right", "Right target / mL", self.cfg.right_target_ml, 10, 320, 5, 1)
        self._spin(form, "fill_time", "Target fill time / s", self.cfg.target_fill_time_s, 0.1, 10, 0.05, 2)
        self._spin(form, "cycles", "Simulation cycles", self.cfg.cycles, 1, 100, 1, 0)
        self._spin(form, "play_speed", "Playback speed", 1.0, 0.1, 10, 0.1, 2)
        box.addWidget(experiment)

        controller = QtWidgets.QGroupBox("Flow PID Control")
        form = QtWidgets.QFormLayout(controller)
        self._spin(form, "kp", "Kp / opening·SLM⁻¹", self.cfg.pi.kp, 0, 1, 0.001, 4)
        self._spin(form, "ki", "Ki / opening·(SLM·s)⁻¹", self.cfg.pi.ki, 0, 5, 0.01, 4)
        self._spin(form, "kd", "Kd / opening·s·SLM⁻¹", self.cfg.pi.kd, 0, 1, 0.001, 4)
        self._spin(form, "ilim", "Integral limit", self.cfg.pi.integral_limit, 0.01, 20, 0.1, 2)
        self.feedforward = QtWidgets.QCheckBox("Enable model feedforward")
        self.feedforward.setChecked(self.cfg.pi.use_feedforward)
        form.addRow(self.feedforward)
        box.addWidget(controller)

        model = QtWidgets.QGroupBox("Lung and Valve Model")
        form = QtWidgets.QFormLayout(model)
        self._spin(form, "capacity_l", "Left Vmax / mL", self.cfg.left.capacity_ml, 321, 1500, 5, 1)
        self._spin(form, "capacity_r", "Right Vmax / mL", self.cfg.right.capacity_ml, 321, 1500, 5, 1)
        self._spin(form, "tau_in_l", "Left tau_in / s", self.cfg.left.tau_in_s, 0.02, 10, 0.01, 3)
        self._spin(form, "tau_in_r", "Right tau_in / s", self.cfg.right.tau_in_s, 0.02, 10, 0.01, 3)
        self._spin(form, "tau_out_l", "Left tau_out / s", self.cfg.left.tau_out_s, 0.02, 10, 0.01, 3)
        self._spin(form, "tau_out_r", "Right tau_out / s", self.cfg.right.tau_out_s, 0.02, 10, 0.01, 3)
        self._spin(form, "gamma", "Pinch valve exponent gamma", self.cfg.left.valve_exponent, 0.5, 3, 0.05, 2)
        self._spin(form, "delay", "Transport delay / s", self.cfg.left.transport_delay_s, 0, 0.5, 0.005, 3)
        self._spin(form, "noise", "Flow noise sigma / SLM", self.cfg.left.sensor_noise_slm, 0, 10, 0.05, 2)
        self._spin(form, "dead", "Direction-change dead time / s", self.cfg.dead_time_s, 0, 0.5, 0.01, 3)
        self._spin(form, "precondition", "Precondition time / s", self.cfg.precondition_s, 0, 10, 0.1, 2)
        box.addWidget(model)

        help_button = QtWidgets.QPushButton("Model and PID Equations")
        help_button.clicked.connect(lambda: FormulaDialog(self).exec_())
        box.addWidget(help_button)
        monitor_button = QtWidgets.QPushButton("Show Live Monitor")
        monitor_button.clicked.connect(self._show_live_monitor)
        box.addWidget(monitor_button)
        apply_button = QtWidgets.QPushButton("Save and Apply Parameters")
        apply_button.setMinimumHeight(38)
        apply_button.setStyleSheet("font-weight:bold; background:#dceeff;")
        apply_button.clicked.connect(self.apply_and_reset)
        box.addWidget(apply_button)

        row = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("Start")
        self.start_button.clicked.connect(self.toggle_play)
        reset_button = QtWidgets.QPushButton("Reset")
        reset_button.clicked.connect(self.reset_playback)
        stop_button = QtWidgets.QPushButton("Stop")
        stop_button.clicked.connect(self.stop_playback)
        row.addWidget(self.start_button)
        row.addWidget(reset_button)
        row.addWidget(stop_button)
        box.addLayout(row)

        self.summary = QtWidgets.QTextBrowser()
        self.summary.setMinimumHeight(150)
        self.summary.setMaximumHeight(190)
        box.addWidget(self.summary)
        box.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll)
        return outer

    def _spin(self, form, key, label, value, low, high, step, decimals) -> None:
        widget = QtWidgets.QDoubleSpinBox()
        widget.setRange(low, high)
        widget.setSingleStep(step)
        widget.setDecimals(decimals)
        widget.setValue(value)
        widget.setKeyboardTracking(False)
        self.inputs[key] = widget
        form.addRow(label, widget)

    def _config_from_ui(self) -> ExperimentConfig:
        c = load_config(SCRIPT_DIR / "config.example.json")
        c.respiratory_rate_bpm = self.inputs["rr"].value()
        c.ie_inspiration = self.inputs["ie_in"].value()
        c.ie_expiration = self.inputs["ie_out"].value()
        c.left_target_ml = self.inputs["v_left"].value()
        c.right_target_ml = self.inputs["v_right"].value()
        c.target_fill_time_s = self.inputs["fill_time"].value()
        c.cycles = int(self.inputs["cycles"].value())
        c.dead_time_s = self.inputs["dead"].value()
        c.precondition_s = self.inputs["precondition"].value()
        c.pi.kp = self.inputs["kp"].value()
        c.pi.ki = self.inputs["ki"].value()
        c.pi.kd = self.inputs["kd"].value()
        c.pi.integral_limit = self.inputs["ilim"].value()
        c.pi.use_feedforward = self.feedforward.isChecked()
        c.left.capacity_ml = self.inputs["capacity_l"].value()
        c.right.capacity_ml = self.inputs["capacity_r"].value()
        c.left.tau_in_s = self.inputs["tau_in_l"].value()
        c.right.tau_in_s = self.inputs["tau_in_r"].value()
        c.left.tau_out_s = self.inputs["tau_out_l"].value()
        c.right.tau_out_s = self.inputs["tau_out_r"].value()
        gamma = self.inputs["gamma"].value()
        delay = self.inputs["delay"].value()
        noise = self.inputs["noise"].value()
        c.left.valve_exponent = c.right.valve_exponent = gamma
        c.left.transport_delay_s = c.right.transport_delay_s = delay
        c.left.sensor_noise_slm = c.right.sensor_noise_slm = noise
        c.validate()
        return c

    def _show_live_monitor(self) -> None:
        self.live_monitor.show()
        self.live_monitor.raise_()
        self.live_monitor.activateWindow()

    def apply_and_reset(self) -> None:
        try:
            cfg = self._config_from_ui()
            result = run_simulation(cfg)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Parameter Error", str(exc))
            return
        self.stop_playback()
        self.cfg = cfg
        self.result = result
        save_config(cfg, SCRIPT_DIR / "config.gui.json")
        self.plot_canvas.set_result(result, cfg)
        self.play_index = 0.0
        self._show_index(0)
        q_left = 0.06 * cfg.left_target_ml / cfg.target_fill_time_s
        q_right = 0.06 * cfg.right_target_ml / cfg.target_fill_time_s
        self.summary.setPlainText(
            "Parameters saved and applied for the next playback\n"
            f"Inspiration time: {cfg.inspiration_s:.3f} s\n"
            f"Expiration time: {cfg.expiration_s:.3f} s\n"
            f"Left/right target flow: {q_left:.2f} / {q_right:.2f} SLM\n"
            f"Left/right target volume: {cfg.left_target_ml:.1f} / {cfg.right_target_ml:.1f} mL\n"
            f"Symmetric total target: {cfg.left_target_ml + cfg.right_target_ml:.1f} mL\n"
            f"Samples: {result.time_s.size} @ {cfg.control_hz:.0f} Hz"
        )
        self.statusBar().showMessage("Digital twin recalculated; click \"Start\" to play.")

    def toggle_play(self) -> None:
        if self.result is None:
            return
        self.playing = not self.playing
        if self.playing:
            if self.play_index >= self.result.time_s.size - 1:
                self.play_index = 0.0
            self.timer.start()
            self.start_button.setText("Pause")
            self.statusBar().showMessage("Simulation playback running")
        else:
            self.timer.stop()
            self.start_button.setText("Resume")
            self.statusBar().showMessage("Paused")

    def stop_playback(self) -> None:
        self.playing = False
        if hasattr(self, "timer"):
            self.timer.stop()
        if hasattr(self, "start_button"):
            self.start_button.setText("Start")

    def reset_playback(self) -> None:
        self.stop_playback()
        self.play_index = 0.0
        self._show_index(0)
        self.statusBar().showMessage("Playback position reset")

    def _advance(self) -> None:
        if self.result is None:
            return
        speed = self.inputs["play_speed"].value()
        self.play_index += speed * (self.timer.interval() / 1000.0) / self.cfg.dt
        index = min(int(self.play_index), self.result.time_s.size - 1)
        self._show_index(index)
        if index >= self.result.time_s.size - 1:
            self.stop_playback()
            self.statusBar().showMessage("Simulation playback complete")

    def _show_index(self, index: int) -> None:
        if self.result is None:
            return
        index = int(np.clip(index, 0, self.result.time_s.size - 1))
        self.model_canvas.draw_frame(self.result, index, self.cfg)
        if hasattr(self, "live_monitor"):
            self.live_monitor.update_frame(self.result, index, self.cfg)
        # Updating the red cursor less frequently keeps playback smooth.
        if index % 3 == 0 or index == self.result.time_s.size - 1:
            self.plot_canvas.set_time(float(self.result.time_s[index]))


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
