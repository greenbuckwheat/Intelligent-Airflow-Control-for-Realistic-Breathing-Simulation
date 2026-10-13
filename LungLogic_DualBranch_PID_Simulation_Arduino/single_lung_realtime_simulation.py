"""Standalone real-time single-lung PID simulation with a PyQt5 UI.

This file is intentionally independent from ``single_lung_pid_gui.py``.  The
plant, controller, delay models, real-time runner and user interface all live
here.  Run it directly with::

    python single_lung_realtime_simulation.py
"""

from __future__ import annotations

import csv
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

import matplotlib
import numpy as np
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from PyQt5 import QtCore, QtGui, QtWidgets


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "single_lung_config.gui.json"
matplotlib.rcParams["axes.unicode_minus"] = False

STATE_PRECONDITION = 1
STATE_DEAD_TO_INSP = 2
STATE_INSPIRATION = 3
STATE_DEAD_TO_EXP = 4
STATE_EXPIRATION = 5
STATE_COMPLETE = 6
STATE_NAMES = {
    STATE_PRECONDITION: "PRECONDITION",
    STATE_DEAD_TO_INSP: "DEAD TO INSP",
    STATE_INSPIRATION: "INSPIRATION",
    STATE_DEAD_TO_EXP: "DEAD TO EXP",
    STATE_EXPIRATION: "EXPIRATION",
    STATE_COMPLETE: "COMPLETE",
}


@dataclass
class LungParameters:
    capacity_ml: float = 250.0
    tau_in_s: float = 0.57
    tau_out_s: float = 0.50
    valve_exponent: float = 1.15
    transport_delay_s: float = 0.025
    sensor_tau_s: float = 0.020
    sensor_noise_slm: float = 0.10
    sensor_bias_slm: float = 0.0
    full_open_fill_time_s: float = 1.30
    flow_sign: int = 1


@dataclass
class PIDParameters:
    kp: float = 0.022
    ki: float = 0.16
    kd: float = 0.0
    integral_limit: float = 2.0
    use_feedforward: bool = True


@dataclass
class SmithParameters:
    enabled: bool = False
    delay_s: float = 0.045
    correction_gain: float = 0.85


@dataclass
class InputDelayParameters:
    enabled: bool = True
    delay_s: float = 0.025


@dataclass
class Config:
    control_hz: float = 100.0
    respiratory_rate_bpm: float = 15.0
    ie_inspiration: float = 1.0
    ie_expiration: float = 2.0
    target_volume_ml: float = 250.0
    target_fill_time_s: float = 1.30
    target_expiration_flow_slm: float | None = None
    dead_time_s: float = 0.080
    dead_time_within_cycle: bool = False
    volume_deadline_guard: bool = True
    volume_feedback_source: str = "oracle"
    precondition_s: float = 1.50
    cycles: int = 5
    end_volume_ml: float = 5.0
    max_duration_extra_s: float = 1.0
    random_seed: int = 7
    lung: LungParameters = field(default_factory=LungParameters)
    # Keep the JSON key compatible with the existing project configuration.
    pi: PIDParameters = field(default_factory=PIDParameters)
    smith: SmithParameters = field(default_factory=SmithParameters)
    input_delay: InputDelayParameters = field(default_factory=InputDelayParameters)
    max_lung_volume_ml: float = 250.0
    max_abs_flow_slm: float = 220.0

    @property
    def dt(self) -> float:
        return 1.0 / self.control_hz

    @property
    def cycle_period_s(self) -> float:
        return 60.0 / self.respiratory_rate_bpm

    @property
    def inspiration_s(self) -> float:
        return self.cycle_period_s * self.ie_inspiration / (self.ie_inspiration + self.ie_expiration)

    @property
    def expiration_s(self) -> float:
        return self.cycle_period_s * self.ie_expiration / (self.ie_inspiration + self.ie_expiration)

    @property
    def max_duration_s(self) -> float:
        return self.precondition_s + self.cycles * (
            self.inspiration_s + self.expiration_s + 2.0 * self.dead_time_s
        ) + self.max_duration_extra_s

    def validate(self) -> None:
        if not 20 <= self.control_hz <= 200:
            raise ValueError("Control frequency must be 20..200 Hz")
        if not 1 <= self.respiratory_rate_bpm <= 40:
            raise ValueError("Respiratory rate must be 1..40 bpm")
        if self.ie_inspiration <= 0 or self.ie_expiration <= 0:
            raise ValueError("I:E values must be positive")
        if not 0 < self.target_volume_ml <= self.lung.capacity_ml:
            raise ValueError("Target volume must be within lung capacity")
        if self.target_fill_time_s <= 0 or self.target_fill_time_s > self.inspiration_s:
            raise ValueError("Target fill time must fit inside inspiration")
        if not 0 <= self.dead_time_s <= 0.5:
            raise ValueError("Dead time must be 0..0.5 s")
        if self.lung.tau_in_s <= 0 or self.lung.tau_out_s <= 0:
            raise ValueError("Lung time constants must be positive")
        if self.lung.transport_delay_s < 0 or self.lung.sensor_tau_s <= 0:
            raise ValueError("Delay and sensor parameters are invalid")
        if min(self.pi.kp, self.pi.ki, self.pi.kd) < 0:
            raise ValueError("PID gains cannot be negative")
        if self.pi.integral_limit <= 0:
            raise ValueError("Integral limit must be positive")
        if not 0 <= self.smith.delay_s <= 1 or not 0 <= self.smith.correction_gain <= 2:
            raise ValueError("Smith parameters are invalid")
        if not 0 <= self.input_delay.delay_s <= 1:
            raise ValueError("Input delay must be 0..1 s")


def _merge(instance, values: dict):
    for key, value in values.items():
        if not hasattr(instance, key):
            raise KeyError(f"Unknown configuration key: {key}")
        current = getattr(instance, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge(current, value)
        else:
            setattr(instance, key, value)
    return instance


def load_config() -> Config:
    cfg = Config()
    if CONFIG_PATH.exists():
        _merge(cfg, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    cfg.validate()
    return cfg


class PID:
    def __init__(self, p: PIDParameters):
        self.p = p
        self.reset()

    def reset(self, held_opening=None):
        self.integral = 0.0
        self.previous_measurement = None
        self.bias = 0.0
        self.held_opening = held_opening

    def update(self, reference: float, measurement: float, dt: float, feedforward: float) -> float:
        error = reference - measurement
        ff = feedforward if self.p.use_feedforward else 0.0
        if self.held_opening is not None:
            opening = self.held_opening
            self.bias = opening - ff - self.p.kp * error
            self.held_opening = None
            self.previous_measurement = measurement
            return opening
        candidate = float(np.clip(self.integral + error * dt, -self.p.integral_limit, self.p.integral_limit))
        derivative = 0.0 if self.previous_measurement is None else -(measurement - self.previous_measurement) / dt
        self.previous_measurement = measurement
        # Software transition only; physical PPV movement is not modeled.
        self.bias -= float(np.clip(self.bias, -50.0 * dt, 50.0 * dt))
        raw = self.bias + ff + self.p.kp * error + self.p.ki * candidate + self.p.kd * derivative
        output = float(np.clip(raw, 0.0, 1.0))
        if 0 < raw < 1 or (raw <= 0 and error > 0) or (raw >= 1 and error < 0):
            self.integral = candidate
        return output


class LungPlant:
    def __init__(self, p: LungParameters, rng: np.random.Generator):
        self.p = p
        self.rng = rng
        self.volume_ml = 0.0
        self.sensor_flow_slm = 0.0
        self.delay_line: List[float] = []

    def full_open_flow(self, direction: int) -> float:
        derivative = ((self.p.capacity_ml - self.volume_ml) / self.p.tau_in_s
                      if direction > 0 else self.volume_ml / self.p.tau_out_s)
        return max(0.0, 0.06 * derivative)

    def step(self, opening: float, direction: int, dt: float) -> Tuple[float, float]:
        effective = float(np.clip(opening, 0, 1)) ** self.p.valve_exponent
        if direction > 0:
            dvdt = effective * (self.p.capacity_ml - self.volume_ml) / self.p.tau_in_s
        elif direction < 0:
            dvdt = -effective * self.volume_ml / self.p.tau_out_s
        else:
            dvdt = 0.0
        self.volume_ml = float(np.clip(self.volume_ml + dvdt * dt, 0, self.p.capacity_ml))
        true_flow = 0.06 * dvdt
        delay_samples = max(0, int(round(self.p.transport_delay_s / dt)))
        self.delay_line.append(true_flow)
        delayed = self.delay_line.pop(0) if len(self.delay_line) > delay_samples else 0.0
        alpha = 1.0 - math.exp(-dt / max(self.p.sensor_tau_s, 1e-5))
        self.sensor_flow_slm += alpha * (delayed - self.sensor_flow_slm)
        measured = (self.p.flow_sign * self.sensor_flow_slm + self.p.sensor_bias_slm
                    + self.rng.normal(0, self.p.sensor_noise_slm))
        return true_flow, float(measured)


class DelayLine:
    def __init__(self, p: InputDelayParameters):
        self.p = p
        self.line: List[Tuple[float, int]] = []

    def reset(self):
        self.line.clear()

    def update(self, opening: float, direction: int, dt: float) -> Tuple[float, int]:
        if not self.p.enabled:
            return opening, direction
        samples = max(0, int(round(self.p.delay_s / dt)))
        self.line.append((opening, direction))
        return self.line.pop(0) if len(self.line) > samples else (0.0, 0)


class Smith:
    def __init__(self, p: LungParameters, delay_s: float, gain: float):
        self.p, self.delay_s, self.gain = p, delay_s, gain
        self.reset()

    def reset(self, volume_ml=0.0):
        self.volume_ml = float(np.clip(volume_ml, 0, self.p.capacity_ml))
        self.line: List[float] = []

    def update(self, opening, direction, measured, dt):
        effective = float(np.clip(opening, 0, 1)) ** self.p.valve_exponent
        if direction > 0:
            dvdt = effective * (self.p.capacity_ml - self.volume_ml) / self.p.tau_in_s
        elif direction < 0:
            dvdt = -effective * self.volume_ml / self.p.tau_out_s
        else:
            dvdt = 0.0
        self.volume_ml = float(np.clip(self.volume_ml + dvdt * dt, 0, self.p.capacity_ml))
        model_now = self.p.flow_sign * 0.06 * dvdt
        samples = max(0, int(round(self.delay_s / dt)))
        self.line.append(model_now)
        model_delayed = self.line.pop(0) if len(self.line) > samples else 0.0
        return float(model_now + self.gain * (measured - model_delayed))


class RealtimeRunner:
    """The control loop runs in fixed dt steps; the GUI only supplies wall time."""
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.reset()

    def reset(self):
        self.rng = np.random.default_rng(self.cfg.random_seed)
        self.plant = LungPlant(self.cfg.lung, self.rng)
        self.pid = PID(self.cfg.pi)
        self.smith = Smith(self.cfg.lung, self.cfg.smith.delay_s, self.cfg.smith.correction_gain)
        self.input_delay = DelayLine(self.cfg.input_delay)
        self.time_s = 0.0
        self.state = STATE_PRECONDITION
        self.state_start = 0.0
        self.cycle = 0
        self.measured = 0.0
        self.feedback = 0.0
        self.opening = 0.0
        self.running = False
        self.complete = False
        self.accumulator = 0.0
        self.last_wall = None
        self.samples = [[0.0, self.state, 0, 0.0, 0.0, 0.0, 0.0, 5.0, 0]]

    def start(self):
        if self.complete:
            self.reset()
        self.running = True
        self.last_wall = time.perf_counter()

    def pause(self):
        self.running = False
        self.last_wall = None

    def tick(self, speed=1.0) -> int:
        now = time.perf_counter()
        if self.last_wall is None:
            self.last_wall = now
            return 0
        elapsed = max(0.0, now - self.last_wall)
        self.last_wall = now
        return self.advance(elapsed, speed)

    def advance(self, wall_dt: float, speed=1.0) -> int:
        if not self.running or self.complete:
            return 0
        self.accumulator += max(0.0, wall_dt) * max(0.0, speed)
        count = 0
        while self.accumulator + 1e-12 >= self.cfg.dt and count < 10000:
            self.accumulator -= self.cfg.dt
            self._step()
            count += 1
        return count

    def _change_state(self, state):
        self.state = state
        self.state_start = self.time_s
        self.pid.reset(self.opening)
        self.smith.reset(self.plant.volume_ml)
        self.input_delay.reset()

    def _step(self):
        elapsed = self.time_s - self.state_start
        if self.state == STATE_PRECONDITION and elapsed >= self.cfg.precondition_s:
            self._change_state(STATE_DEAD_TO_INSP)
        elif self.state == STATE_DEAD_TO_INSP and elapsed >= self.cfg.dead_time_s:
            self._change_state(STATE_INSPIRATION)
        elif self.state == STATE_INSPIRATION and elapsed >= self.cfg.inspiration_s:
            self._change_state(STATE_DEAD_TO_EXP)
        elif self.state == STATE_DEAD_TO_EXP and elapsed >= self.cfg.dead_time_s:
            self._change_state(STATE_EXPIRATION)
        elif self.state == STATE_EXPIRATION and elapsed >= self.cfg.expiration_s:
            self.cycle += 1
            if self.cycle >= self.cfg.cycles:
                self._change_state(STATE_COMPLETE)
                self.complete = True
                self.running = False
            else:
                self._change_state(STATE_DEAD_TO_INSP)

        qref = 0.06 * self.cfg.target_volume_ml / self.cfg.target_fill_time_s if self.state == STATE_INSPIRATION else (
            0.06 * self.cfg.target_volume_ml / max(0.25, self.cfg.expiration_s - self.cfg.dead_time_s)
            if self.state in (STATE_PRECONDITION, STATE_EXPIRATION) else 0.0
        )
        opening, direction = self.opening, 0
        if self.state == STATE_COMPLETE:
            opening = 0.0
        if self.state == STATE_INSPIRATION and self.plant.volume_ml < self.cfg.target_volume_ml:
            direction = 1
            qmax = max(self.plant.full_open_flow(1), 0.1)
            ff = min(1.0, (qref / qmax) ** (1.0 / self.cfg.lung.valve_exponent))
            opening = self.pid.update(qref, max(0.0, self.feedback if self.cfg.smith.enabled else self.measured), self.cfg.dt, ff)
        elif self.state == STATE_EXPIRATION and self.plant.volume_ml > self.cfg.end_volume_ml:
            direction = -1
            qmax = max(self.plant.full_open_flow(-1), 0.1)
            ff = min(1.0, (qref / qmax) ** (1.0 / self.cfg.lung.valve_exponent))
            feedback = self.feedback if self.cfg.smith.enabled else self.measured
            opening = self.pid.update(qref, max(0.0, -feedback), self.cfg.dt, ff)
        elif self.state == STATE_COMPLETE:
            self.pid.reset()

        plant_opening, plant_direction = self.input_delay.update(opening, direction, self.cfg.dt)
        _, self.measured = self.plant.step(plant_opening, plant_direction, self.cfg.dt)
        self.feedback = self.smith.update(plant_opening, plant_direction, self.measured, self.cfg.dt) if self.cfg.smith.enabled else self.measured
        self.time_s += self.cfg.dt
        self.opening = opening
        mask = 1 if direction > 0 else 2 if direction < 0 else 0
        self.samples.append([self.time_s, self.state, self.cycle, self.measured, direction * qref, self.plant.volume_ml, opening, 5.0 * (1.0 - opening), mask])

    def current(self):
        return self.samples[-1]

    def arrays(self):
        a = np.asarray(self.samples, dtype=float)
        return a[:, 0], a


class PlotCanvas(FigureCanvas):
    def __init__(self):
        self.figure = Figure(figsize=(8, 6), tight_layout=True)
        super().__init__(self.figure)
        self.axes = self.figure.subplots(3, 1, sharex=True)

    def redraw(self, runner: RealtimeRunner):
        t, a = runner.arrays()
        for ax in self.axes:
            ax.clear()
            ax.grid(True, ls="--", alpha=0.3)
        self.axes[0].plot(t, a[:, 5], label="Volume")
        self.axes[0].axhline(runner.cfg.target_volume_ml, ls="--", color="k", label="Target")
        self.axes[0].set_ylabel("Volume / mL")
        self.axes[1].plot(t, a[:, 3], label="Measured flow")
        self.axes[1].plot(t, a[:, 4], "k--", label="Reference")
        self.axes[1].set_ylabel("Flow / SLM")
        self.axes[2].plot(t, a[:, 6] * 100, label="Opening / %")
        self.axes[2].plot(t, a[:, 7], label="Command / V")
        self.axes[2].set_ylabel("Valve")
        self.axes[2].set_xlabel("Simulation time / s")
        for ax in self.axes:
            ax.legend(loc="upper right", fontsize=8)
        self.draw_idle()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.runner = RealtimeRunner(self.cfg)
        self.inputs = {}
        self.setWindowTitle("LungLogic Standalone Real-Time Single-Lung Simulation")
        self.resize(1250, 820)
        self._build_ui()
        self.timer = QtCore.QTimer(self)
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self._on_tick)
        self.timer.start()
        self._refresh()

    def _spin(self, form, key, label, value, lo, hi, step, decimals):
        w = QtWidgets.QDoubleSpinBox()
        w.setRange(lo, hi); w.setSingleStep(step); w.setDecimals(decimals); w.setValue(value)
        w.setKeyboardTracking(False)
        self.inputs[key] = w
        form.addRow(label, w)

    def _build_ui(self):
        root = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        left = QtWidgets.QWidget(); layout = QtWidgets.QVBoxLayout(left)
        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(left)
        root.addWidget(scroll)
        right = QtWidgets.QWidget(); right_layout = QtWidgets.QVBoxLayout(right)
        self.plot = PlotCanvas(); right_layout.addWidget(self.plot, 1)
        self.status = QtWidgets.QLabel(); self.status.setWordWrap(True); right_layout.addWidget(self.status)
        root.addWidget(right); root.setSizes([410, 840])
        self.setCentralWidget(root)

        form = QtWidgets.QFormLayout()
        group = QtWidgets.QGroupBox("Breathing")
        for key, label, value, lo, hi, step, dec in [
            ("rr", "Respiratory rate / bpm", self.cfg.respiratory_rate_bpm, 1, 40, 1, 1),
            ("ie_in", "I:E inspiration", self.cfg.ie_inspiration, .1, 10, .1, 2),
            ("ie_out", "I:E expiration", self.cfg.ie_expiration, .1, 10, .1, 2),
            ("volume", "Target volume / mL", self.cfg.target_volume_ml, 10, 250, 5, 1),
            ("fill", "Target fill time / s", self.cfg.target_fill_time_s, .1, 10, .05, 2),
            ("cycles", "Cycles", self.cfg.cycles, 1, 100, 1, 0),
            ("speed", "Playback speed", 1.0, .1, 10, .1, 2),
        ]: self._spin(form, key, label, value, lo, hi, step, dec)
        group.setLayout(form); layout.addWidget(group)

        form = QtWidgets.QFormLayout(); group = QtWidgets.QGroupBox("PID")
        for key, label, value, hi, step, dec in [("kp", "Kp", self.cfg.pi.kp, 1, .001, 4), ("ki", "Ki", self.cfg.pi.ki, 5, .01, 4), ("kd", "Kd", self.cfg.pi.kd, 1, .001, 4), ("ilim", "Integral limit", self.cfg.pi.integral_limit, 20, .1, 2)]: self._spin(form, key, label, value, 0, hi, step, dec)
        self.ff = QtWidgets.QCheckBox("Enable feedforward"); self.ff.setChecked(self.cfg.pi.use_feedforward); form.addRow(self.ff)
        group.setLayout(form); layout.addWidget(group)

        form = QtWidgets.QFormLayout(); group = QtWidgets.QGroupBox("Delays and Smith Predictor")
        self.smith_on = QtWidgets.QCheckBox("Enable Smith predictor"); self.smith_on.setChecked(self.cfg.smith.enabled); form.addRow(self.smith_on)
        self._spin(form, "smith_delay", "Smith feedback delay / s", self.cfg.smith.delay_s, 0, 1, .005, 3)
        self._spin(form, "smith_gain", "Smith correction gain", self.cfg.smith.correction_gain, 0, 2, .05, 2)
        self.input_on = QtWidgets.QCheckBox("Enable input delay"); self.input_on.setChecked(self.cfg.input_delay.enabled); form.addRow(self.input_on)
        self._spin(form, "input_delay", "Input delay / s", self.cfg.input_delay.delay_s, 0, 1, .005, 3)
        self._spin(form, "transport", "Transport delay / s", self.cfg.lung.transport_delay_s, 0, 1, .005, 3)
        self._spin(form, "sensor_tau", "Sensor time constant / s", self.cfg.lung.sensor_tau_s, .001, 1, .005, 3)
        group.setLayout(form); layout.addWidget(group)

        form = QtWidgets.QFormLayout(); group = QtWidgets.QGroupBox("Lung model")
        for key, label, value, lo, hi, step, dec in [("tau_in", "Tau in / s", self.cfg.lung.tau_in_s, .02, 10, .01, 3), ("tau_out", "Tau out / s", self.cfg.lung.tau_out_s, .02, 10, .01, 3), ("gamma", "Valve exponent", self.cfg.lung.valve_exponent, .5, 3, .05, 2), ("dead", "Direction dead time / s", self.cfg.dead_time_s, 0, .5, .01, 3), ("pre", "Precondition / s", self.cfg.precondition_s, 0, 10, .1, 2)]: self._spin(form, key, label, value, lo, hi, step, dec)
        group.setLayout(form); layout.addWidget(group)

        buttons = QtWidgets.QHBoxLayout()
        for text, slot in [("Apply / Reset", self.apply), ("Start", self.start), ("Pause", self.pause), ("Save CSV", self.save_csv)]:
            b = QtWidgets.QPushButton(text); b.clicked.connect(slot); buttons.addWidget(b)
        layout.addLayout(buttons); layout.addStretch(1)

    def config_from_ui(self):
        c = Config()
        c.respiratory_rate_bpm = self.inputs["rr"].value(); c.ie_inspiration = self.inputs["ie_in"].value(); c.ie_expiration = self.inputs["ie_out"].value()
        c.target_volume_ml = self.inputs["volume"].value(); c.target_fill_time_s = self.inputs["fill"].value(); c.cycles = int(self.inputs["cycles"].value())
        c.dead_time_s = self.inputs["dead"].value(); c.precondition_s = self.inputs["pre"].value()
        c.pi.kp = self.inputs["kp"].value(); c.pi.ki = self.inputs["ki"].value(); c.pi.kd = self.inputs["kd"].value(); c.pi.integral_limit = self.inputs["ilim"].value(); c.pi.use_feedforward = self.ff.isChecked()
        c.smith.enabled = self.smith_on.isChecked(); c.smith.delay_s = self.inputs["smith_delay"].value(); c.smith.correction_gain = self.inputs["smith_gain"].value()
        c.input_delay.enabled = self.input_on.isChecked(); c.input_delay.delay_s = self.inputs["input_delay"].value()
        c.lung.transport_delay_s = self.inputs["transport"].value(); c.lung.sensor_tau_s = self.inputs["sensor_tau"].value(); c.lung.tau_in_s = self.inputs["tau_in"].value(); c.lung.tau_out_s = self.inputs["tau_out"].value(); c.lung.valve_exponent = self.inputs["gamma"].value()
        c.validate(); return c

    def apply(self):
        try:
            self.cfg = self.config_from_ui(); self.runner = RealtimeRunner(self.cfg); self.plot.redraw(self.runner); self._refresh()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Parameter error", str(exc))

    def start(self): self.runner.start()
    def pause(self): self.runner.pause()

    def _on_tick(self):
        self.runner.tick(self.inputs["speed"].value())
        self._refresh()

    def _refresh(self):
        row = self.runner.current(); state = STATE_NAMES.get(int(row[1]), "UNKNOWN")
        self.status.setText(f"Status: {'RUNNING' if self.runner.running else 'PAUSED/READY'} | State: {state} | Cycle: {int(row[2]) + 1}/{self.cfg.cycles}\n"
                            f"Simulation time: {row[0]:.3f} / {self.cfg.max_duration_s:.3f} s | Volume: {row[5]:.1f} mL | Flow: {row[3]:+.2f} SLM | Opening: {row[6] * 100:.1f}% | Command: {row[7]:.2f} V")
        if self.runner.running and len(self.runner.samples) % 5 == 0 or len(self.runner.samples) == 1:
            self.plot.redraw(self.runner)

    def save_csv(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save simulation CSV", str(SCRIPT_DIR / "output" / "single_lung_realtime.csv"), "CSV files (*.csv)")
        if not path: return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f); writer.writerow(["time_s", "state", "cycle", "flow_slm", "reference_slm", "volume_ml", "opening", "command_v", "valve_mask"]); writer.writerows(self.runner.samples)
        self.statusBar().showMessage(f"Saved {path}")

    def closeEvent(self, event):
        self.runner.pause(); self.timer.stop(); event.accept()


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QtGui.QFont("Segoe UI", 9))
    window = MainWindow(); window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
