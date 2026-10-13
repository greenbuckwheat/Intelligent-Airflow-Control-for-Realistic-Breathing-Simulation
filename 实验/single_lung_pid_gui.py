"""PyQt5 visual digital twin for a single 250 mL LungLogic lung model.

This is a one-lung version of the dual-branch GUI.  The breathing parameters
are converted into the same control actions: two routing solenoid valves
(atmosphere and vacuum) plus one proportional pinch valve command.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

import matplotlib
import numpy as np
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.patches import Circle, Ellipse, FancyBboxPatch, Rectangle
from PyQt5 import QtCore, QtGui, QtWidgets


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "single_lung_config.gui.json"

matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False


STATE_IDLE = 0
STATE_PRECONDITION = 1
STATE_DEAD_TO_INSP = 2
STATE_INSPIRATION = 3
STATE_DEAD_TO_EXP = 4
STATE_EXPIRATION = 5
STATE_COMPLETE = 6
STATE_FAULT = 7

STATE_NAMES = {
    STATE_IDLE: "IDLE",
    STATE_PRECONDITION: "PRECONDITION",
    STATE_DEAD_TO_INSP: "DEAD_TO_INSP",
    STATE_INSPIRATION: "INSPIRATION",
    STATE_DEAD_TO_EXP: "DEAD_TO_EXP",
    STATE_EXPIRATION: "EXPIRATION",
    STATE_COMPLETE: "COMPLETE",
    STATE_FAULT: "FAULT",
}


@dataclass
class LungParameters:
    """Identifiable parameters for the single lung branch."""

    capacity_ml: float = 250.0
    tau_in_s: float = 0.57
    tau_out_s: float = 0.50
    valve_exponent: float = 1.15
    transport_delay_s: float = 0.025
    sensor_tau_s: float = 0.020
    sensor_noise_slm: float = 0.10
    flow_sign: int = 1


@dataclass
class PIDParameters:
    """PID output is normalized pinch opening, where 0=closed and 1=open."""

    kp: float = 0.022
    ki: float = 0.16
    kd: float = 0.0
    integral_limit: float = 2.0
    use_feedforward: bool = True


@dataclass
class SmithPredictorParameters:
    """Delay compensation around the PID feedback path."""

    enabled: bool = False
    delay_s: float = 0.025
    correction_gain: float = 1.0


@dataclass
class InputDelayParameters:
    """Delay from controller command to pneumatic effect at the lung."""

    enabled: bool = False
    delay_s: float = 0.025


@dataclass
class SingleLungConfig:
    control_hz: float = 100.0
    respiratory_rate_bpm: float = 15.0
    ie_inspiration: float = 1.0
    ie_expiration: float = 2.0
    target_volume_ml: float = 250.0
    target_fill_time_s: float = 1.30
    dead_time_s: float = 0.080
    precondition_s: float = 1.50
    cycles: int = 5
    end_volume_ml: float = 5.0
    max_lung_volume_ml: float = 250.0
    max_abs_flow_slm: float = 220.0
    random_seed: int = 7
    lung: LungParameters = field(default_factory=LungParameters)
    pi: PIDParameters = field(default_factory=PIDParameters)
    smith: SmithPredictorParameters = field(default_factory=SmithPredictorParameters)
    input_delay: InputDelayParameters = field(default_factory=InputDelayParameters)

    @property
    def cycle_period_s(self) -> float:
        return 60.0 / self.respiratory_rate_bpm

    @property
    def inspiration_s(self) -> float:
        ratio_sum = self.ie_inspiration + self.ie_expiration
        return self.cycle_period_s * self.ie_inspiration / ratio_sum

    @property
    def expiration_s(self) -> float:
        ratio_sum = self.ie_inspiration + self.ie_expiration
        return self.cycle_period_s * self.ie_expiration / ratio_sum

    @property
    def dt(self) -> float:
        return 1.0 / self.control_hz

    def validate(self) -> None:
        if not (20.0 <= self.control_hz <= 200.0):
            raise ValueError("control_hz must be 20..200 Hz")
        if not (1.0 <= self.respiratory_rate_bpm <= 40.0):
            raise ValueError("respiratory_rate_bpm must be 1..40")
        if self.ie_inspiration <= 0 or self.ie_expiration <= 0:
            raise ValueError("I:E components must be positive")
        if not (0 < self.target_volume_ml <= self.max_lung_volume_ml):
            raise ValueError("target volume must be 0..max_lung_volume_ml")
        if self.target_fill_time_s <= 0 or self.target_fill_time_s > self.inspiration_s:
            raise ValueError("target_fill_time_s must fit inside inspiration time")
        if self.dead_time_s < 0 or self.dead_time_s > 0.5:
            raise ValueError("dead_time_s must be 0..0.5 s")
        if self.lung.capacity_ml < self.target_volume_ml:
            raise ValueError("lung capacity must be greater than or equal to target volume")
        if self.lung.capacity_ml > self.max_lung_volume_ml:
            raise ValueError("lung capacity must not exceed max_lung_volume_ml")
        if self.lung.tau_in_s <= 0 or self.lung.tau_out_s <= 0:
            raise ValueError("lung time constants must be positive")
        if self.pi.kp < 0 or self.pi.ki < 0 or self.pi.kd < 0:
            raise ValueError("PID gains must be non-negative")
        if self.pi.integral_limit <= 0:
            raise ValueError("integral_limit must be positive")
        if self.smith.delay_s < 0 or self.smith.delay_s > 1.0:
            raise ValueError("smith delay_s must be 0..1 s")
        if self.smith.correction_gain < 0 or self.smith.correction_gain > 2.0:
            raise ValueError("smith correction gain must be 0..2")
        if self.input_delay.delay_s < 0 or self.input_delay.delay_s > 1.0:
            raise ValueError("input delay_s must be 0..1 s")


@dataclass
class SingleLungResult:
    time_s: np.ndarray
    state: np.ndarray
    cycle: np.ndarray
    flow_slm: np.ndarray
    flow_reference_slm: np.ndarray
    volume_ml: np.ndarray
    opening: np.ndarray
    command_v: np.ndarray
    valve_mask: np.ndarray


def _merge_dataclass(instance: Any, values: Dict[str, Any]) -> Any:
    for key, value in values.items():
        if not hasattr(instance, key):
            raise KeyError(f"Unknown configuration key: {key}")
        current = getattr(instance, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge_dataclass(current, value)
        else:
            setattr(instance, key, value)
    return instance


def load_config(path: Path | None = None) -> SingleLungConfig:
    cfg = SingleLungConfig()
    if path is not None and path.exists():
        with path.open("r", encoding="utf-8") as handle:
            _merge_dataclass(cfg, json.load(handle))
    cfg.validate()
    return cfg


def save_config(cfg: SingleLungConfig, path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(asdict(cfg), handle, indent=2, ensure_ascii=False)
        handle.write("\n")


class PIDController:
    def __init__(self, parameters: PIDParameters) -> None:
        self.p = parameters
        self.integral = 0.0
        self.previous_measurement: float | None = None

    def reset(self) -> None:
        self.integral = 0.0
        self.previous_measurement = None

    def update(self, reference: float, measurement: float, dt: float, feedforward: float) -> float:
        error = reference - measurement
        candidate = float(np.clip(
            self.integral + error * dt,
            -self.p.integral_limit,
            self.p.integral_limit,
        ))
        ff = feedforward if self.p.use_feedforward else 0.0
        if self.previous_measurement is None or dt <= 0.0:
            derivative = 0.0
        else:
            derivative = -(measurement - self.previous_measurement) / dt
        self.previous_measurement = measurement
        raw = ff + self.p.kp * error + self.p.ki * candidate + self.p.kd * derivative
        output = float(np.clip(raw, 0.0, 1.0))
        if (0.0 < raw < 1.0) or (raw <= 0.0 and error > 0.0) or (raw >= 1.0 and error < 0.0):
            self.integral = candidate
        return output


class SingleLungPlant:
    """First-order lung volume model with transport delay and sensor dynamics."""

    def __init__(self, p: LungParameters, rng: np.random.Generator) -> None:
        self.p = p
        self.rng = rng
        self.volume_ml = 0.0
        self.sensor_flow_slm = 0.0
        self._delay_line: List[float] = []

    def full_open_flow_slm(self, direction: int) -> float:
        if direction > 0:
            derivative = (self.p.capacity_ml - self.volume_ml) / self.p.tau_in_s
        else:
            derivative = self.volume_ml / self.p.tau_out_s
        return max(0.0, 0.06 * derivative)

    def step(self, opening: float, direction: int, dt: float) -> Tuple[float, float]:
        opening = float(np.clip(opening, 0.0, 1.0))
        effective = opening ** self.p.valve_exponent
        if direction > 0:
            dvdt = effective * (self.p.capacity_ml - self.volume_ml) / self.p.tau_in_s
        elif direction < 0:
            dvdt = -effective * self.volume_ml / self.p.tau_out_s
        else:
            dvdt = 0.0

        self.volume_ml = float(np.clip(self.volume_ml + dvdt * dt, 0.0, self.p.capacity_ml))
        true_flow = 0.06 * dvdt

        delay_samples = max(0, int(round(self.p.transport_delay_s / dt)))
        self._delay_line.append(true_flow)
        delayed = self._delay_line.pop(0) if len(self._delay_line) > delay_samples else 0.0
        alpha = 1.0 - math.exp(-dt / max(self.p.sensor_tau_s, 1e-5))
        self.sensor_flow_slm += alpha * (delayed - self.sensor_flow_slm)
        measured = self.p.flow_sign * self.sensor_flow_slm
        measured += self.rng.normal(0.0, self.p.sensor_noise_slm)
        return true_flow, float(measured)


class SmithPredictor:
    """Predict current flow while delayed sensor feedback catches up."""

    def __init__(self, p: LungParameters, delay_s: float, correction_gain: float) -> None:
        self.p = p
        self.delay_s = delay_s
        self.correction_gain = correction_gain
        self.volume_ml = 0.0
        self._delay_line: List[float] = []

    def reset(self, volume_ml: float = 0.0) -> None:
        self.volume_ml = float(np.clip(volume_ml, 0.0, self.p.capacity_ml))
        self._delay_line.clear()

    def update(self, opening: float, direction: int, measured_delayed: float, dt: float) -> float:
        opening = float(np.clip(opening, 0.0, 1.0))
        effective = opening ** self.p.valve_exponent
        if direction > 0:
            dvdt = effective * (self.p.capacity_ml - self.volume_ml) / self.p.tau_in_s
        elif direction < 0:
            dvdt = -effective * self.volume_ml / self.p.tau_out_s
        else:
            dvdt = 0.0

        self.volume_ml = float(np.clip(self.volume_ml + dvdt * dt, 0.0, self.p.capacity_ml))
        model_now = self.p.flow_sign * 0.06 * dvdt
        delay_samples = max(0, int(round(self.delay_s / dt)))
        self._delay_line.append(model_now)
        model_delayed = self._delay_line.pop(0) if len(self._delay_line) > delay_samples else 0.0
        return float(model_now + self.correction_gain * (measured_delayed - model_delayed))


class InputDelayLine:
    """Hold valve commands before they reach the simulated pneumatic plant."""

    def __init__(self, p: InputDelayParameters) -> None:
        self.p = p
        self._delay_line: List[Tuple[float, int]] = []

    def reset(self) -> None:
        self._delay_line.clear()

    def update(self, opening: float, direction: int, dt: float) -> Tuple[float, int]:
        if not self.p.enabled:
            return opening, direction
        delay_samples = max(0, int(round(self.p.delay_s / dt)))
        self._delay_line.append((opening, direction))
        if len(self._delay_line) > delay_samples:
            return self._delay_line.pop(0)
        return 0.0, 0


def _phase_reference(cfg: SingleLungConfig, state: int) -> float:
    if state == STATE_INSPIRATION:
        return 0.06 * cfg.target_volume_ml / cfg.target_fill_time_s
    if state in (STATE_PRECONDITION, STATE_EXPIRATION):
        usable_exp = max(0.25, cfg.expiration_s - cfg.dead_time_s)
        return 0.06 * cfg.target_volume_ml / usable_exp
    return 0.0


def run_single_lung_simulation(cfg: SingleLungConfig) -> SingleLungResult:
    """Run deterministic-time single-lung pneumatic control simulation."""

    cfg.validate()
    rng = np.random.default_rng(cfg.random_seed)
    plant = SingleLungPlant(cfg.lung, rng)
    controller = PIDController(cfg.pi)
    smith = SmithPredictor(cfg.lung, cfg.smith.delay_s, cfg.smith.correction_gain)
    input_delay = InputDelayLine(cfg.input_delay)
    dt = cfg.dt
    max_duration = (
        cfg.precondition_s
        + cfg.cycles * (cfg.inspiration_s + cfg.expiration_s + 2.0 * cfg.dead_time_s)
        + 1.0
    )
    n = int(math.ceil(max_duration / dt)) + 1
    log: Dict[str, List[float]] = {key: [] for key in (
        "time", "state", "cycle", "flow", "qref", "volume", "opening", "dac", "mask",
    )}

    state = STATE_PRECONDITION
    state_start = 0.0
    cycle = 0
    measured = 0.0
    control_feedback = 0.0
    for step in range(n):
        now = step * dt
        elapsed = now - state_start

        if state == STATE_PRECONDITION and elapsed >= cfg.precondition_s:
            state, state_start = STATE_DEAD_TO_INSP, now
            controller.reset()
            smith.reset(plant.volume_ml)
            input_delay.reset()
        elif state == STATE_DEAD_TO_INSP and elapsed >= cfg.dead_time_s:
            state, state_start = STATE_INSPIRATION, now
            controller.reset()
            smith.reset(plant.volume_ml)
            input_delay.reset()
        elif state == STATE_INSPIRATION and elapsed >= cfg.inspiration_s:
            state, state_start = STATE_DEAD_TO_EXP, now
            controller.reset()
            smith.reset(plant.volume_ml)
            input_delay.reset()
        elif state == STATE_DEAD_TO_EXP and elapsed >= cfg.dead_time_s:
            state, state_start = STATE_EXPIRATION, now
            controller.reset()
            smith.reset(plant.volume_ml)
            input_delay.reset()
        elif state == STATE_EXPIRATION and elapsed >= cfg.expiration_s:
            cycle += 1
            state = STATE_COMPLETE if cycle >= cfg.cycles else STATE_DEAD_TO_INSP
            state_start = now
            controller.reset()
            smith.reset(plant.volume_ml)
            input_delay.reset()

        qref_mag = _phase_reference(cfg, state)
        opening = 0.0
        direction = 0
        if state == STATE_INSPIRATION and plant.volume_ml < cfg.target_volume_ml:
            direction = 1
            qmax = max(plant.full_open_flow_slm(1), 0.1)
            ff = min(1.0, (qref_mag / qmax) ** (1.0 / plant.p.valve_exponent))
            feedback = control_feedback if cfg.smith.enabled else measured
            opening = controller.update(qref_mag, max(0.0, feedback), dt, ff)
        elif state in (STATE_PRECONDITION, STATE_EXPIRATION) and plant.volume_ml > cfg.end_volume_ml:
            direction = -1
            qmax = max(plant.full_open_flow_slm(-1), 0.1)
            ff = min(1.0, (qref_mag / qmax) ** (1.0 / plant.p.valve_exponent))
            feedback = control_feedback if cfg.smith.enabled else measured
            opening = controller.update(qref_mag, max(0.0, -feedback), dt, ff)
        else:
            controller.reset()

        plant_opening, plant_direction = input_delay.update(opening, direction, dt)
        _, measured = plant.step(plant_opening, plant_direction, dt)
        control_feedback = smith.update(plant_opening, plant_direction, measured, dt) if cfg.smith.enabled else measured

        # Bit assignment: bit 0 atmosphere solenoid, bit 1 vacuum solenoid.
        mask = 0
        if direction > 0:
            mask |= 1
        elif direction < 0:
            mask |= 2

        log["time"].append(now)
        log["state"].append(state)
        log["cycle"].append(cycle)
        log["flow"].append(measured)
        log["qref"].append(direction * qref_mag if direction else 0.0)
        log["volume"].append(plant.volume_ml)
        log["opening"].append(opening)
        log["dac"].append(5.0 * (1.0 - opening))
        log["mask"].append(mask)
        if state in (STATE_COMPLETE, STATE_FAULT):
            break

    return SingleLungResult(
        time_s=np.asarray(log["time"]),
        state=np.asarray(log["state"], dtype=int),
        cycle=np.asarray(log["cycle"], dtype=int),
        flow_slm=np.asarray(log["flow"]),
        flow_reference_slm=np.asarray(log["qref"]),
        volume_ml=np.asarray(log["volume"]),
        opening=np.asarray(log["opening"]),
        command_v=np.asarray(log["dac"]),
        valve_mask=np.asarray(log["mask"], dtype=int),
    )


class LungModelCanvas(FigureCanvas):
    def __init__(self) -> None:
        self.figure = Figure(figsize=(8.5, 7.2), tight_layout=True)
        super().__init__(self.figure)
        self.ax = self.figure.add_subplot(111)
        self.figure.patch.set_facecolor("white")

    def draw_frame(self, result: SingleLungResult, index: int, cfg: SingleLungConfig) -> None:
        ax = self.ax
        ax.clear()
        ax.set_xlim(0, 10)
        ax.set_ylim(0, 10)
        ax.set_aspect("equal")
        ax.axis("off")
        ax.set_title("Single-Lung Pneumatic PID Digital Twin", fontsize=16, fontweight="bold", pad=10)

        state = int(result.state[index])
        time_s = result.time_s[index]
        cycle = int(result.cycle[index])
        mask = int(result.valve_mask[index])
        volume = float(result.volume_ml[index])
        flow = float(result.flow_slm[index])
        opening = float(result.opening[index])

        ax.text(
            0.25, 9.45,
            f"t = {time_s:6.2f} s    State = {STATE_NAMES[state]}    Cycle = {cycle + 1}/{cfg.cycles}",
            fontsize=11, family="monospace",
            bbox=dict(boxstyle="round,pad=0.35", fc="#edf4fb", ec="#7a9bbd"),
        )

        ax.add_patch(FancyBboxPatch((0.45, 8.05), 1.8, 0.7, boxstyle="round,pad=0.1",
                                    fc="#dff3ff", ec="#2878a8", lw=1.5))
        ax.text(1.35, 8.40, "Atmosphere Inlet", ha="center", va="center", fontsize=11)
        ax.add_patch(FancyBboxPatch((7.75, 8.05), 1.8, 0.7, boxstyle="round,pad=0.1",
                                    fc="#eee4fa", ec="#7550a5", lw=1.5))
        ax.text(8.65, 8.40, "Regulated Vacuum", ha="center", va="center", fontsize=11)

        x = 5.0
        ax.plot([2.25, x - 0.70], [8.40, 8.40], color="#2878a8", lw=2)
        ax.plot([7.75, x + 0.70], [8.40, 8.40], color="#7550a5", lw=2)
        ax.plot([x - 0.70, x - 0.70], [8.40, 7.25], color="#555", lw=2)
        ax.plot([x + 0.70, x + 0.70], [8.40, 7.25], color="#555", lw=2)
        self._valve(ax, x - 0.70, 7.10, bool(mask & 1), "ATM")
        self._valve(ax, x + 0.70, 7.10, bool(mask & 2), "VAC")
        ax.plot([x - 0.70, x - 0.70, x], [6.82, 6.55, 6.25], color="#555", lw=2)
        ax.plot([x + 0.70, x + 0.70, x], [6.82, 6.55, 6.25], color="#555", lw=2)

        ax.plot([x, x], [6.25, 5.75], color="#555", lw=max(1.0, 5.0 * opening))
        pinch_color = "#27ae60" if opening > 0.02 else "#9b9b9b"
        ax.add_patch(Circle((x, 5.52), 0.24, fc="white", ec=pinch_color, lw=2.2))
        gap = 0.05 + 0.15 * opening
        ax.plot([x - 0.17, x - gap], [5.67, 5.40], color=pinch_color, lw=2)
        ax.plot([x + 0.17, x + gap], [5.67, 5.40], color=pinch_color, lw=2)
        ax.text(x + 0.36, 5.54, f"PPV  {opening * 100:4.1f}%", va="center", fontsize=9)
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

        fraction = float(np.clip(volume / max(cfg.target_volume_ml, 1.0), 0.0, 1.15))
        width = 1.05 + 1.35 * fraction
        height = 1.30 + 1.70 * fraction
        lung_color = "#f49a9a" if state == STATE_INSPIRATION else "#b8d7f0"
        if state == STATE_EXPIRATION:
            lung_color = "#c7b7e5"
        ax.add_patch(Ellipse((x, 2.20), width, height, fc=lung_color, ec="#555", lw=1.8, alpha=0.92))
        ax.plot([x, x], [3.52, 2.95], color="#555", lw=2)
        ax.text(x, 2.25, "Single Lung", ha="center", va="center", fontsize=12, fontweight="bold")
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

    def set_result(self, result: SingleLungResult, cfg: SingleLungConfig) -> None:
        for ax in self.axes:
            ax.clear()
        t = result.time_s
        self.axes[0].plot(t, result.volume_ml, label="Volume")
        self.axes[0].axhline(cfg.target_volume_ml, color="k", ls="--", lw=0.8, label="Target")
        self.axes[0].set_ylabel("Volume / mL")
        self.axes[1].plot(t, result.flow_slm, label="Measured")
        self.axes[1].plot(t, result.flow_reference_slm, "k--", lw=0.9, label="Reference")
        self.axes[1].set_ylabel("Flow / SLM")
        self.axes[2].plot(t, result.opening, label="PPV opening")
        self.axes[2].plot(t, result.command_v / 5.0, label="DAC / 5 V")
        self.axes[2].set_ylabel("Opening")
        self.axes[2].set_ylim(-0.05, 1.05)
        self.axes[3].step(t, result.state, where="post", label="State")
        self.axes[3].step(t, result.valve_mask / 2.0, where="post", label="Valve mask / 2")
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

    def set_result(self, result: SingleLungResult | None, index: int, window_s: float = 8.0) -> None:
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
            flow = result.flow_slm[start:index + 1]
            self.ax.plot(t, flow, label="Measured", lw=1.8)
            self.ax.plot(t, result.flow_reference_slm[start:index + 1], "k--", label="Reference", lw=1.0)
            self.ax.axvline(now, color="#d62728", lw=1.0)
            self.ax.set_xlim(start_time, max(start_time + window_s, now + 0.25))
            self.ax.set_ylim(min(-2.0, float(np.min(flow)) - 2.0), max(2.0, float(np.max(flow)) + 2.0))
            self.ax.legend(loc="upper right", ncol=2, fontsize=8)
        self.draw_idle()


class LiveMonitorWindow(QtWidgets.QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent, QtCore.Qt.Window)
        self.setWindowTitle("Single-Lung Live Monitor")
        self.resize(560, 620)
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

        lung = QtWidgets.QGroupBox("Single Lung")
        lung_form = QtWidgets.QFormLayout(lung)
        for key, label in (
            ("flow", "Flow rate"),
            ("reference", "Reference flow"),
            ("volume", "Volume"),
            ("opening", "PPV opening"),
            ("dac", "PPV command"),
            ("valves", "Routing valves"),
        ):
            value = self._value_label()
            self.metric_labels[key] = value
            lung_form.addRow(label, value)
        layout.addWidget(lung)

        hardware = QtWidgets.QGroupBox("Control Outputs")
        hardware_form = QtWidgets.QFormLayout(hardware)
        for key, label in (
            ("atm", "ATM solenoid"),
            ("vac", "VAC solenoid"),
            ("mask", "Valve mask"),
        ):
            value = self._value_label()
            self.metric_labels[key] = value
            hardware_form.addRow(label, value)
        layout.addWidget(hardware)

        self.flow_canvas = LiveFlowCanvas()
        layout.addWidget(self.flow_canvas, stretch=1)

    @staticmethod
    def _value_label() -> QtWidgets.QLabel:
        label = QtWidgets.QLabel("--")
        label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        label.setMinimumWidth(120)
        label.setStyleSheet("font-family: 'Segoe UI', Arial, sans-serif; font-size: 12px;")
        return label

    def update_frame(self, result: SingleLungResult | None, index: int, cfg: SingleLungConfig | None) -> None:
        if result is None or cfg is None or result.time_s.size == 0:
            return
        index = int(np.clip(index, 0, result.time_s.size - 1))
        mask = int(result.valve_mask[index])
        state = int(result.state[index])
        cycle = int(result.cycle[index])
        atm_open = bool(mask & 1)
        vac_open = bool(mask & 2)

        self.metric_labels["time"].setText(f"{result.time_s[index]:.2f} s")
        self.metric_labels["state"].setText(STATE_NAMES[state])
        self.metric_labels["cycle"].setText(f"{cycle + 1} / {cfg.cycles}")
        self.metric_labels["flow"].setText(f"{result.flow_slm[index]:+.2f} SLM")
        self.metric_labels["reference"].setText(f"{result.flow_reference_slm[index]:+.2f} SLM")
        self.metric_labels["volume"].setText(f"{result.volume_ml[index]:.1f} mL")
        self.metric_labels["opening"].setText(f"{result.opening[index] * 100.0:.1f} %")
        self.metric_labels["dac"].setText(f"{result.command_v[index]:.2f} V")
        self.metric_labels["valves"].setText(
            f"{'ATM open' if atm_open else 'ATM closed'}, {'VAC open' if vac_open else 'VAC closed'}"
        )
        self.metric_labels["atm"].setText("HIGH / open" if atm_open else "LOW / closed")
        self.metric_labels["vac"].setText("HIGH / open" if vac_open else "LOW / closed")
        self.metric_labels["mask"].setText(f"{mask:02b}")
        if index == 0 or index >= result.time_s.size - 1 or abs(index - self._last_plot_index) >= 10:
            self.flow_canvas.set_result(result, index)
            self._last_plot_index = index


class FormulaDialog(QtWidgets.QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Single-Lung Modeling and PID Control Equations")
        self.resize(900, 720)
        layout = QtWidgets.QVBoxLayout(self)
        text = QtWidgets.QTextBrowser()
        text.setFont(QtGui.QFont("Consolas", 10))
        text.setPlainText(
            "Single-Lung Pneumatic Lung Model and Control\n\n"
            "1. State convention\n"
            "   inspiration: atmosphere solenoid open, vacuum solenoid closed, Q > 0\n"
            "   expiration : atmosphere solenoid closed, vacuum solenoid open, Q < 0\n"
            "   dead time  : both routing solenoids closed during direction change\n\n"
            "2. First-order grey-box model\n"
            "   Inspiration: dV/dt = a^gamma (Vmax - V) / tau_in\n"
            "   Expiration : dV/dt = -a^gamma V / tau_out\n"
            "   Flow       : Q = 0.06 dV/dt  [SLM]\n\n"
            "3. Target flow from UI parameters\n"
            "   Q*_in  = 0.06 Vtarget / Tfill\n"
            "   Q*_out = -0.06 Vtarget / (Texpiration - Tdead) for display\n"
            "   Default: 0.06 x 250 / 1.3 = 11.54 SLM\n\n"
            "4. Feedforward + PID pinch valve control\n"
            "   e = Q* - |Qmeasured|\n"
            "   d = -d|Qmeasured|/dt\n"
            "   a = clip(a_ff + Kp e + Ki integral(e dt) + Kd d, 0, 1)\n"
            "   Conditional integration prevents windup while saturated.\n\n"
            "5. Optional command-to-lung input delay\n"
            "   a_plant(t), direction_plant(t) = command(t - Tinput)\n"
            "   This models valve, tubing and pneumatic propagation before the lung responds.\n\n"
            "6. Optional Smith predictor delay compensation\n"
            "   Qfeedback = Qmodel_now + gain x (Qmeasured_delayed - Qmodel_delayed)\n"
            "   PID still controls flow; the predictor only estimates a less-delayed feedback signal.\n\n"
            "7. PPV command and solenoid outputs\n"
            "   Ucmd = 5(1-a) V\n"
            "   a=1 -> 0 V -> fully open; a=0 -> 5 V -> fully closed\n"
            "   valve_mask bit 0: atmosphere solenoid\n"
            "   valve_mask bit 1: vacuum solenoid\n\n"
            "The UI therefore turns respiratory rate, tidal volume, I:E ratio and\n"
            "flow target settings into direct atmosphere/vacuum routing plus PPV\n"
            "modulation for a single 250 mL lung model."
        )
        layout.addWidget(text)
        button = QtWidgets.QPushButton("Close")
        button.clicked.connect(self.accept)
        layout.addWidget(button, alignment=QtCore.Qt.AlignRight)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("LungLogic PID Digital Twin | Single 250 mL Lung")
        self.resize(1320, 860)
        self.cfg = load_config(CONFIG_PATH)
        self.result: SingleLungResult | None = None
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
        splitter.setSizes([390, 930])
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)
        self.statusBar().showMessage("After changing parameters, click \"Save and Apply\" before playback.")

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
        self._spin(form, "volume", "Target tidal volume / mL", self.cfg.target_volume_ml, 10, 250, 5, 1)
        self._spin(form, "fill_time", "Target fill time / s", self.cfg.target_fill_time_s, 0.1, 10, 0.05, 2)
        self._spin(form, "cycles", "Simulation cycles", self.cfg.cycles, 1, 100, 1, 0)
        self._spin(form, "play_speed", "Playback speed", 1.0, 0.1, 10, 0.1, 2)
        box.addWidget(experiment)

        controller = QtWidgets.QGroupBox("Flow PID Control")
        form = QtWidgets.QFormLayout(controller)
        self._spin(form, "kp", "Kp / opening per SLM", self.cfg.pi.kp, 0, 1, 0.001, 4)
        self._spin(form, "ki", "Ki / opening per SLM s", self.cfg.pi.ki, 0, 5, 0.01, 4)
        self._spin(form, "kd", "Kd / opening s per SLM", self.cfg.pi.kd, 0, 1, 0.001, 4)
        self._spin(form, "ilim", "Integral limit", self.cfg.pi.integral_limit, 0.01, 20, 0.1, 2)
        self.feedforward = QtWidgets.QCheckBox("Enable model feedforward")
        self.feedforward.setChecked(self.cfg.pi.use_feedforward)
        form.addRow(self.feedforward)
        box.addWidget(controller)

        smith_group = QtWidgets.QGroupBox("Smith Predictor Delay Compensation")
        form = QtWidgets.QFormLayout(smith_group)
        self.smith_enabled = QtWidgets.QCheckBox("Enable Smith predictor")
        self.smith_enabled.setChecked(self.cfg.smith.enabled)
        form.addRow(self.smith_enabled)
        self._spin(form, "smith_delay", "Predicted delay / s", self.cfg.smith.delay_s, 0, 1.0, 0.005, 3)
        self._spin(form, "smith_gain", "Correction gain", self.cfg.smith.correction_gain, 0, 2.0, 0.05, 2)
        box.addWidget(smith_group)

        input_delay_group = QtWidgets.QGroupBox("Command-to-Lung Input Delay")
        form = QtWidgets.QFormLayout(input_delay_group)
        self.input_delay_enabled = QtWidgets.QCheckBox("Enable input delay")
        self.input_delay_enabled.setChecked(self.cfg.input_delay.enabled)
        form.addRow(self.input_delay_enabled)
        self._spin(form, "input_delay", "Input delay / s", self.cfg.input_delay.delay_s, 0, 1.0, 0.005, 3)
        box.addWidget(input_delay_group)

        model = QtWidgets.QGroupBox("Lung and Valve Model")
        form = QtWidgets.QFormLayout(model)
        self._spin(form, "capacity", "Lung Vmax / mL", self.cfg.lung.capacity_ml, 10, 250, 5, 1)
        self._spin(form, "tau_in", "Tau_in / s", self.cfg.lung.tau_in_s, 0.02, 10, 0.01, 3)
        self._spin(form, "tau_out", "Tau_out / s", self.cfg.lung.tau_out_s, 0.02, 10, 0.01, 3)
        self._spin(form, "gamma", "Pinch valve exponent gamma", self.cfg.lung.valve_exponent, 0.5, 3, 0.05, 2)
        self._spin(form, "delay", "Transport delay / s", self.cfg.lung.transport_delay_s, 0, 0.5, 0.005, 3)
        self._spin(form, "noise", "Flow noise sigma / SLM", self.cfg.lung.sensor_noise_slm, 0, 10, 0.05, 2)
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
        self.summary.setMinimumHeight(155)
        self.summary.setMaximumHeight(195)
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

    def _config_from_ui(self) -> SingleLungConfig:
        c = SingleLungConfig()
        c.respiratory_rate_bpm = self.inputs["rr"].value()
        c.ie_inspiration = self.inputs["ie_in"].value()
        c.ie_expiration = self.inputs["ie_out"].value()
        c.target_volume_ml = self.inputs["volume"].value()
        c.target_fill_time_s = self.inputs["fill_time"].value()
        c.cycles = int(self.inputs["cycles"].value())
        c.dead_time_s = self.inputs["dead"].value()
        c.precondition_s = self.inputs["precondition"].value()
        c.pi.kp = self.inputs["kp"].value()
        c.pi.ki = self.inputs["ki"].value()
        c.pi.kd = self.inputs["kd"].value()
        c.pi.integral_limit = self.inputs["ilim"].value()
        c.pi.use_feedforward = self.feedforward.isChecked()
        c.smith.enabled = self.smith_enabled.isChecked()
        c.smith.delay_s = self.inputs["smith_delay"].value()
        c.smith.correction_gain = self.inputs["smith_gain"].value()
        c.input_delay.enabled = self.input_delay_enabled.isChecked()
        c.input_delay.delay_s = self.inputs["input_delay"].value()
        c.lung.capacity_ml = self.inputs["capacity"].value()
        c.lung.tau_in_s = self.inputs["tau_in"].value()
        c.lung.tau_out_s = self.inputs["tau_out"].value()
        c.lung.valve_exponent = self.inputs["gamma"].value()
        c.lung.transport_delay_s = self.inputs["delay"].value()
        c.lung.sensor_noise_slm = self.inputs["noise"].value()
        c.validate()
        return c

    def _show_live_monitor(self) -> None:
        self.live_monitor.show()
        self.live_monitor.raise_()
        self.live_monitor.activateWindow()

    def apply_and_reset(self) -> None:
        try:
            cfg = self._config_from_ui()
            result = run_single_lung_simulation(cfg)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Parameter Error", str(exc))
            return
        self.stop_playback()
        self.cfg = cfg
        self.result = result
        save_config(cfg, CONFIG_PATH)
        self.plot_canvas.set_result(result, cfg)
        self.play_index = 0.0
        self._show_index(0)
        q_in = 0.06 * cfg.target_volume_ml / cfg.target_fill_time_s
        q_out = 0.06 * cfg.target_volume_ml / max(0.25, cfg.expiration_s - cfg.dead_time_s)
        self.summary.setPlainText(
            "Parameters saved and applied for the next playback\n"
            f"Inspiration time: {cfg.inspiration_s:.3f} s\n"
            f"Expiration time: {cfg.expiration_s:.3f} s\n"
            f"Target tidal volume: {cfg.target_volume_ml:.1f} mL\n"
            f"Inspiration/expiration target flow: {q_in:.2f} / {q_out:.2f} SLM\n"
            f"Outputs: ATM solenoid, VAC solenoid, PPV command 0-5 V\n"
            f"Samples: {result.time_s.size} @ {cfg.control_hz:.0f} Hz"
        )
        self.statusBar().showMessage("Single-lung digital twin recalculated; click \"Start\" to play.")

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
        if index % 3 == 0 or index == self.result.time_s.size - 1:
            self.plot_canvas.set_time(float(self.result.time_s[index]))


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QtGui.QFont("Segoe UI", 9))
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
