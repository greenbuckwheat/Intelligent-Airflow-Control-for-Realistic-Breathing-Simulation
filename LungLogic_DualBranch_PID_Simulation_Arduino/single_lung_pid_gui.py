"""PyQt5 visual digital twin for a single 250 mL LungLogic lung model.

This is a one-lung version of the dual-branch GUI.  The breathing parameters
are converted into the same control actions: two routing solenoid valves
(atmosphere and vacuum) plus one proportional pinch valve command.
"""

from __future__ import annotations

import copy
import json
import math
import sys
import time
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
    """Single-lung response assumptions; the assembled path is unmeasured."""

    capacity_ml: float = 250.0
    tau_in_s: float = 0.57
    full_open_fill_time_s: float = 1.30
    tau_out_s: float = 0.50
    valve_exponent: float = 1.15
    transport_delay_s: float = 0.025
    sensor_tau_s: float = 0.020
    sensor_noise_slm: float = 0.10
    sensor_bias_slm: float = 0.0
    flow_sign: int = 1
    # Hardware-only empirical model; simulation keeps its existing response.
    hardware_flow_offset_slm: float = 0.0034
    hardware_flow_gain: float = 1.0471
    # Calibrated 2026-10-12 VAC bypass plateau; it rolls off only near closure.
    hardware_vac_leak_slm: float = 0.814
    hardware_model_volume_ml: float = 323.9
    hardware_gamma_in: float = 1.496
    hardware_gamma_out: float = 2.0621
    hardware_dead_zone_in_v: float = 4.0986
    hardware_dead_zone_out_v: float = 4.1779
    hardware_closure_v: float = 4.35

    def inspiration_equilibrium_ml(self) -> float:
        """Virtual open-path equilibrium; the physical stop remains capacity_ml.

        A fully open path starting at 0 mL reaches the capacity at
        full_open_fill_time_s. Tau controls the unknown curve shape.
        This is an explicit hypothetical fit to one endpoint, not a measured
        equilibrium volume or a fitted flow waveform.
        """
        fraction_remaining = -math.expm1(-self.full_open_fill_time_s / self.tau_in_s)
        return self.capacity_ml / fraction_remaining


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
    """Delay compensation around the PID feedback path.

    This is the estimated total feedback delay used by the predictor. It is
    normally close to the pneumatic transport delay plus the sensor response
    time, and is independent from the command/input delay.
    """

    enabled: bool = False
    delay_s: float = 0.045
    correction_gain: float = 1.0
    # Hardware-only calibration; zero means not measured. Simulation is unchanged.
    ppv_open_time_s: float = 2.436  # 4.2 -> 0 V; measured 5 -> 0 V in 2.9 s
    ppv_close_time_s: float = 2.436  # 0 -> 4.2 V; measured 0 -> 5 V in 2.9 s


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
    target_expiration_flow_slm: float | None = None
    dead_time_s: float = 0.080
    dead_time_within_cycle: bool = False
    volume_deadline_guard: bool = False
    volume_feedback_source: str = "oracle"
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
    def active_inspiration_s(self) -> float:
        return self.inspiration_s - (self.dead_time_s if self.dead_time_within_cycle else 0.0)

    @property
    def active_expiration_s(self) -> float:
        return self.expiration_s - (self.dead_time_s if self.dead_time_within_cycle else 0.0)

    @property
    def dt(self) -> float:
        return 1.0 / self.control_hz

    def validate(self) -> None:
        if not (20.0 <= self.control_hz <= 200.0):
            raise ValueError("control_hz must be 20..200 Hz")
        if not (1.0 <= self.respiratory_rate_bpm <= 40.0):
            raise ValueError("respiratory_rate_bpm must be 1..40")
        if not (1 <= self.cycles <= 100):
            raise ValueError("simulation cycles must be 1..100")
        if self.ie_inspiration <= 0 or self.ie_expiration <= 0:
            raise ValueError("I:E components must be positive")
        if not (0 < self.target_volume_ml <= self.max_lung_volume_ml):
            raise ValueError("target volume must be 0..max_lung_volume_ml")
        if self.target_fill_time_s <= 0 or self.target_fill_time_s > self.inspiration_s:
            raise ValueError("target_fill_time_s must fit inside inspiration time")
        if self.target_expiration_flow_slm is not None:
            minimum = 0.06 * max(0.0, self.target_volume_ml - self.end_volume_ml) / max(
                0.25, self.expiration_s - self.dead_time_s
            )
            if not minimum <= self.target_expiration_flow_slm <= self.max_abs_flow_slm:
                raise ValueError(
                    f"expiration target flow must be {minimum:.2f}..{self.max_abs_flow_slm:.0f} SLM "
                    "to empty the lung within the expiration phase"
                )
        if self.dead_time_s < 0 or self.dead_time_s > 0.5:
            raise ValueError("dead_time_s must be 0..0.5 s")
        if self.active_inspiration_s <= 0 or self.active_expiration_s <= 0:
            raise ValueError("direction dead time must fit within both phase slots")
        if self.lung.capacity_ml < self.target_volume_ml:
            raise ValueError("lung capacity must be greater than or equal to target volume")
        if self.lung.capacity_ml > self.max_lung_volume_ml:
            raise ValueError("lung capacity must not exceed max_lung_volume_ml")
        if self.lung.tau_in_s <= 0 or self.lung.tau_out_s <= 0:
            raise ValueError("lung time constants must be positive")
        if not math.isfinite(self.lung.full_open_fill_time_s) or self.lung.full_open_fill_time_s <= 0:
            raise ValueError("full_open_fill_time_s must be positive and finite")
        if self.lung.transport_delay_s < 0 or self.lung.transport_delay_s > 1.0:
            raise ValueError("transport_delay_s must be 0..1 s")
        if self.lung.sensor_tau_s <= 0 or self.lung.sensor_tau_s > 1.0:
            raise ValueError("sensor_tau_s must be >0..1 s")
        if not math.isfinite(self.lung.sensor_bias_slm):
            raise ValueError("sensor bias must be finite")
        if self.lung.flow_sign not in (-1, 1):
            raise ValueError("flow_sign must be -1 or 1")
        if self.volume_feedback_source not in ("oracle", "sensor_integral"):
            raise ValueError("volume_feedback_source must be oracle or sensor_integral")
        if self.volume_feedback_source == "sensor_integral" and self.precondition_s < max(0.30, 20.0 / self.control_hz):
            raise ValueError("sensor-integrated volume needs at least 20 precondition zero samples")
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
    estimated_volume_ml: np.ndarray
    estimated_sensor_bias_slm: float
    opening: np.ndarray
    command_v: np.ndarray
    valve_mask: np.ndarray


def simulated_inspiratory_tidal_ml(result: SingleLungResult, cycle: int) -> float:
    """Volume gained from the start of this inspiration to its peak."""
    indices = np.flatnonzero((result.cycle == cycle) & (result.state == STATE_INSPIRATION))
    if not indices.size:
        raise ValueError(f"cycle {cycle} has no inspiration samples")
    start_index = int(indices[0])
    start_volume = float(result.volume_ml[start_index - 1]) if start_index else 0.0
    return max(0.0, float(np.max(result.volume_ml[indices])) - start_volume)


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
            values = json.load(handle)
        # Older files selected between two inspiratory models. Both now load
        # into the sole, explicitly assumed finite-fill response.
        lung_values = values.get("lung", {})
        if isinstance(lung_values, dict) and "inspiration_model" in lung_values:
            legacy_model = lung_values.pop("inspiration_model")
            if legacy_model not in ("asymptotic", "finite_fill"):
                raise ValueError(f"Unknown legacy inspiration_model: {legacy_model}")
        _merge_dataclass(cfg, values)
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
        self.bias = 0.0
        self.held_opening: float | None = None

    def reset(self, held_opening: float | None = None) -> None:
        self.integral = 0.0
        self.previous_measurement = None
        self.bias = 0.0
        self.held_opening = held_opening

    def update(self, reference: float, measurement: float, dt: float, feedforward: float) -> float:
        error = reference - measurement
        candidate = float(np.clip(
            self.integral + error * dt,
            -self.p.integral_limit,
            self.p.integral_limit,
        ))
        ff = feedforward if self.p.use_feedforward else 0.0
        if self.held_opening is not None:
            opening = self.held_opening
            self.bias = opening - ff - self.p.kp * error
            self.held_opening = None
            self.previous_measurement = measurement
            return opening
        if self.previous_measurement is None or dt <= 0.0:
            derivative = 0.0
        else:
            derivative = -(measurement - self.previous_measurement) / dt
        self.previous_measurement = measurement
        # Release the command-continuity bias; this is not a measured PPV slew rate.
        self.bias -= float(np.clip(self.bias, -50.0 * dt, 50.0 * dt))
        raw = self.bias + ff + self.p.kp * error + self.p.ki * candidate + self.p.kd * derivative
        output = float(np.clip(raw, 0.0, 1.0))
        if (0.0 < raw < 1.0) or (raw <= 0.0 and error > 0.0) or (raw >= 1.0 and error < 0.0):
            self.integral = candidate
        return output


class SingleLungPlant:
    """First-order lung model with separate transport and sensor dynamics.

    ``transport_delay_s`` delays true plant flow on its way to the sensor
    model. The sensor then applies a first-order lag with ``sensor_tau_s``.
    Command-to-plant delay is modeled separately by ``InputDelayLine``.
    """

    def __init__(self, p: LungParameters, rng: np.random.Generator) -> None:
        self.p = p
        self.rng = rng
        self.volume_ml = 0.0
        self.sensor_flow_slm = 0.0
        self._delay_line: List[float] = []

    def full_open_flow_slm(self, direction: int) -> float:
        if direction > 0:
            if self.volume_ml >= self.p.capacity_ml:
                return 0.0
            derivative = (self.p.inspiration_equilibrium_ml() - self.volume_ml) / self.p.tau_in_s
        else:
            derivative = self.volume_ml / self.p.tau_out_s
        return max(0.0, 0.06 * derivative)

    def step(self, opening: float, direction: int, dt: float) -> Tuple[float, float]:
        opening = float(np.clip(opening, 0.0, 1.0))
        effective = opening ** self.p.valve_exponent
        previous_volume = self.volume_ml
        if direction > 0:
            equilibrium = self.p.inspiration_equilibrium_ml()
            next_volume = equilibrium + (previous_volume - equilibrium) * math.exp(
                -effective * dt / self.p.tau_in_s
            )
            if next_volume >= self.p.capacity_ml - 1e-9:
                next_volume = self.p.capacity_ml
            self.volume_ml = float(np.clip(next_volume, 0.0, self.p.capacity_ml))
        elif direction < 0:
            dvdt = -effective * previous_volume / self.p.tau_out_s
            self.volume_ml = float(np.clip(previous_volume + dvdt * dt, 0.0, self.p.capacity_ml))
        else:
            self.volume_ml = previous_volume

        # Flow must equal the actual bounded volume change, including at the stop.
        true_flow = 0.06 * (self.volume_ml - previous_volume) / dt

        delay_samples = max(0, int(round(self.p.transport_delay_s / dt)))
        self._delay_line.append(true_flow)
        delayed = self._delay_line.pop(0) if len(self._delay_line) > delay_samples else 0.0
        alpha = 1.0 - math.exp(-dt / max(self.p.sensor_tau_s, 1e-5))
        self.sensor_flow_slm += alpha * (delayed - self.sensor_flow_slm)
        measured = self.p.flow_sign * self.sensor_flow_slm
        measured += self.p.sensor_bias_slm
        measured += self.rng.normal(0.0, self.p.sensor_noise_slm)
        return true_flow, float(measured)


class SmithPredictor:
    """Predict current flow while delayed sensor feedback catches up.

    ``delay_s`` is the feedback delay being compensated, not the command/input
    delay. It is usually approximately transport delay plus sensor lag.
    """

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
        previous_volume = self.volume_ml
        if direction > 0:
            equilibrium = self.p.inspiration_equilibrium_ml()
            next_volume = equilibrium + (previous_volume - equilibrium) * math.exp(
                -effective * dt / self.p.tau_in_s
            )
            if next_volume >= self.p.capacity_ml - 1e-9:
                next_volume = self.p.capacity_ml
            self.volume_ml = float(np.clip(next_volume, 0.0, self.p.capacity_ml))
        elif direction < 0:
            dvdt = -effective * previous_volume / self.p.tau_out_s
            self.volume_ml = float(np.clip(previous_volume + dvdt * dt, 0.0, self.p.capacity_ml))
        else:
            self.volume_ml = previous_volume

        model_now = self.p.flow_sign * 0.06 * (self.volume_ml - previous_volume) / dt
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


class FlowVolumeEstimator:
    """Integrate measured signed flow after an initial closed-valve zero sample.

    The simulator starts with a fully emptied lung. During precondition its
    valves stay closed, so the average reading estimates the sensor's fixed
    zero offset. This does not correct later drift, standard-to-actual volume
    conversion, or transport/sensor lag.
    """

    def __init__(self, capacity_ml: float, flow_sign: int) -> None:
        self.capacity_ml = capacity_ml
        self.flow_sign = flow_sign
        self.volume_ml = 0.0
        self.zero_bias_slm = 0.0
        self._zero_sum = 0.0
        self._zero_count = 0
        self.calibrated = False

    def add_zero_sample(self, measured_slm: float) -> None:
        if self.calibrated:
            raise RuntimeError("zero-flow calibration has already ended")
        self._zero_sum += measured_slm
        self._zero_count += 1

    def finish_zero_calibration(self, minimum_samples: int = 20) -> None:
        if self._zero_count < minimum_samples:
            raise ValueError(f"at least {minimum_samples} closed-valve flow samples are needed for zero calibration")
        self.zero_bias_slm = self._zero_sum / self._zero_count if self._zero_count else 0.0
        self.calibrated = True

    def update(self, measured_slm: float, dt: float) -> float:
        if not self.calibrated:
            raise RuntimeError("zero-flow calibration must finish before flow integration")
        corrected = self.flow_sign * (measured_slm - self.zero_bias_slm)
        self.volume_ml = float(np.clip(
            self.volume_ml + corrected * dt / 0.06, 0.0, self.capacity_ml
        ))
        return self.volume_ml


def _phase_reference(cfg: SingleLungConfig, state: int) -> float:
    if state == STATE_INSPIRATION:
        return 0.06 * cfg.target_volume_ml / cfg.target_fill_time_s
    if state in (STATE_PRECONDITION, STATE_EXPIRATION):
        if cfg.target_expiration_flow_slm is not None:
            return cfg.target_expiration_flow_slm
        usable_exp = max(0.25, cfg.expiration_s - cfg.dead_time_s)
        return 0.06 * cfg.target_volume_ml / usable_exp
    return 0.0


def _deadline_opening(p: LungParameters, volume_ml: float, target_ml: float,
                      remaining_s: float, direction: int) -> float:
    """Minimum model opening needed to reach an end-volume by a phase deadline.

    The caller supplies either simulated true volume or a sensor-derived
    estimate. Physical use requires a validated volume estimator and plant.
    """
    remaining_s = max(remaining_s, 1e-6)
    if direction > 0:
        if volume_ml >= target_ml:
            return 0.0
        drive = p.inspiration_equilibrium_ml()
        if drive <= target_ml:
            return 1.0
        effort = p.tau_in_s / remaining_s * math.log(
            (drive - volume_ml) / (drive - target_ml)
        )
    elif direction < 0:
        if volume_ml <= target_ml:
            return 0.0
        if target_ml <= 0:
            return 1.0
        effort = p.tau_out_s / remaining_s * math.log(volume_ml / target_ml)
    else:
        return 0.0
    return float(np.clip(effort, 0.0, 1.0) ** (1.0 / p.valve_exponent))


def _model_full_open_flow_slm(p: LungParameters, volume_ml: float, direction: int) -> float:
    """Open-valve flow predicted by a controller's lung model."""
    if direction > 0:
        if volume_ml >= p.capacity_ml:
            return 0.0
        derivative = (p.inspiration_equilibrium_ml() - volume_ml) / p.tau_in_s
    else:
        derivative = volume_ml / p.tau_out_s
    return max(0.0, 0.06 * derivative)


def run_single_lung_simulation(
    cfg: SingleLungConfig,
    controller_lung: LungParameters | None = None,
    controller_input_delay_s: float | None = None,
) -> SingleLungResult:
    """Run a timed simulation; optional fixed controller assumptions test mismatch.

    Overrides change only the controller's feedforward, Smith and deadline
    calculations. The plant and actual input delay still come from ``cfg``.
    In sensor-integral mode the controller uses a lagged flow-derived volume;
    the simulated plant state remains available only for audit and physics.
    """

    cfg.validate()
    if controller_lung is not None and (
        controller_lung.tau_in_s <= 0 or controller_lung.tau_out_s <= 0
        or controller_lung.valve_exponent <= 0
    ):
        raise ValueError("controller lung parameters must be positive")
    if controller_input_delay_s is not None and controller_input_delay_s < 0:
        raise ValueError("controller input delay must be non-negative")
    model = controller_lung if controller_lung is not None else cfg.lung
    assumed_input_delay_enabled = (
        cfg.input_delay.enabled if controller_input_delay_s is None
        else controller_input_delay_s > 0.0
    )
    assumed_input_delay_s = (
        cfg.input_delay.delay_s if controller_input_delay_s is None
        else controller_input_delay_s
    ) if assumed_input_delay_enabled else 0.0
    rng = np.random.default_rng(cfg.random_seed)
    plant = SingleLungPlant(cfg.lung, rng)
    controller = PIDController(cfg.pi)
    smith = SmithPredictor(model, cfg.smith.delay_s, cfg.smith.correction_gain)
    input_delay = InputDelayLine(cfg.input_delay)
    controller_delay = InputDelayLine(InputDelayParameters(
        enabled=assumed_input_delay_enabled, delay_s=assumed_input_delay_s
    ))
    estimator = FlowVolumeEstimator(cfg.lung.capacity_ml, cfg.lung.flow_sign)
    dt = cfg.dt
    max_duration = (
        cfg.precondition_s
        + cfg.cycles * (cfg.inspiration_s + cfg.expiration_s + 2.0 * cfg.dead_time_s)
        + 1.0
    )
    n = int(math.ceil(max_duration / dt)) + 1
    log: Dict[str, List[float]] = {key: [] for key in (
        "time", "state", "cycle", "flow", "qref", "volume", "estimated_volume",
        "opening", "dac", "mask",
    )}

    state = STATE_PRECONDITION
    state_start = 0.0
    cycle_epoch = 0.0
    cycle = 0
    breath_start_volume = 0.0
    measured = 0.0
    control_measured = 0.0
    control_feedback = 0.0
    opening = 0.0
    for step in range(n):
        now = step * dt
        elapsed = now - state_start
        control_volume = (
            estimator.volume_ml if cfg.volume_feedback_source == "sensor_integral"
            else plant.volume_ml
        )

        if state == STATE_PRECONDITION and elapsed >= cfg.precondition_s:
            estimator.finish_zero_calibration(
                20 if cfg.volume_feedback_source == "sensor_integral" else 0
            )
            state, state_start = STATE_DEAD_TO_INSP, now
            cycle_epoch = now
            controller.reset(opening)
            smith.reset(control_volume)
            input_delay.reset()
            controller_delay.reset()
        elif state == STATE_DEAD_TO_INSP and (
            now >= cycle_epoch + cycle * cfg.cycle_period_s + cfg.dead_time_s
            if cfg.dead_time_within_cycle else elapsed >= cfg.dead_time_s
        ):
            state, state_start = STATE_INSPIRATION, now
            breath_start_volume = control_volume
            controller.reset(opening)
            smith.reset(control_volume)
            input_delay.reset()
            controller_delay.reset()
        elif state == STATE_INSPIRATION and (
            now >= cycle_epoch + cycle * cfg.cycle_period_s + cfg.inspiration_s
            if cfg.dead_time_within_cycle else elapsed >= cfg.inspiration_s
        ):
            state, state_start = STATE_DEAD_TO_EXP, now
            controller.reset(opening)
            smith.reset(control_volume)
            input_delay.reset()
            controller_delay.reset()
        elif state == STATE_DEAD_TO_EXP and (
            now >= cycle_epoch + cycle * cfg.cycle_period_s + cfg.inspiration_s + cfg.dead_time_s
            if cfg.dead_time_within_cycle else elapsed >= cfg.dead_time_s
        ):
            state, state_start = STATE_EXPIRATION, now
            controller.reset(opening)
            smith.reset(control_volume)
            input_delay.reset()
            controller_delay.reset()
        elif state == STATE_EXPIRATION and (
            now >= cycle_epoch + (cycle + 1) * cfg.cycle_period_s
            if cfg.dead_time_within_cycle else elapsed >= cfg.expiration_s
        ):
            cycle += 1
            state = STATE_COMPLETE if cycle >= cfg.cycles else STATE_DEAD_TO_INSP
            state_start = now
            controller.reset(opening)
            smith.reset(control_volume)
            input_delay.reset()
            controller_delay.reset()

        qref_mag = _phase_reference(cfg, state)
        if state in (STATE_COMPLETE, STATE_FAULT):
            opening = 0.0
        direction = 0
        inspiration_limit = (
            min(model.capacity_ml, breath_start_volume + cfg.target_volume_ml)
            if cfg.volume_deadline_guard else cfg.target_volume_ml
        )
        if state == STATE_INSPIRATION and control_volume < inspiration_limit:
            direction = 1
            qmax = max(_model_full_open_flow_slm(model, control_volume, 1), 0.1)
            ff = min(1.0, (qref_mag / qmax) ** (1.0 / model.valve_exponent))
            feedback = control_feedback if cfg.smith.enabled else control_measured
            resuming_phase = controller.held_opening is not None
            opening = controller.update(qref_mag, max(0.0, feedback), dt, ff)
            if cfg.volume_deadline_guard and not resuming_phase:
                remaining = (
                    cycle_epoch + cycle * cfg.cycle_period_s + cfg.inspiration_s - now
                    if cfg.dead_time_within_cycle else cfg.inspiration_s - elapsed
                )
                if assumed_input_delay_enabled:
                    remaining -= assumed_input_delay_s
                opening = max(opening, _deadline_opening(
                    model, control_volume, inspiration_limit, remaining, direction
                ))
        elif state in (STATE_PRECONDITION, STATE_EXPIRATION) and control_volume > cfg.end_volume_ml:
            direction = -1
            qmax = max(_model_full_open_flow_slm(model, control_volume, -1), 0.1)
            ff = min(1.0, (qref_mag / qmax) ** (1.0 / model.valve_exponent))
            feedback = control_feedback if cfg.smith.enabled else control_measured
            resuming_phase = controller.held_opening is not None
            opening = controller.update(qref_mag, max(0.0, -feedback), dt, ff)
            if cfg.volume_deadline_guard and not resuming_phase:
                remaining = (
                    cycle_epoch + (cycle + 1) * cfg.cycle_period_s - now
                    if cfg.dead_time_within_cycle else cfg.expiration_s - elapsed
                )
                if assumed_input_delay_enabled:
                    remaining -= assumed_input_delay_s
                opening = max(opening, _deadline_opening(
                    model, control_volume, cfg.end_volume_ml, remaining, direction
                ))
        elif state in (STATE_COMPLETE, STATE_FAULT):
            controller.reset()

        plant_opening, plant_direction = input_delay.update(opening, direction, dt)
        _, measured = plant.step(plant_opening, plant_direction, dt)
        if estimator.calibrated:
            estimator.update(measured, dt)
        elif state == STATE_PRECONDITION and direction == 0:
            estimator.add_zero_sample(measured)
        control_measured = (
            cfg.lung.flow_sign * (measured - estimator.zero_bias_slm)
            if cfg.volume_feedback_source == "sensor_integral" else measured
        )
        if cfg.volume_feedback_source == "sensor_integral":
            model_opening, model_direction = controller_delay.update(opening, direction, dt)
        else:
            model_opening, model_direction = plant_opening, plant_direction
        control_feedback = (
            smith.update(model_opening, model_direction, control_measured, dt)
            if cfg.smith.enabled else control_measured
        )

        # Bit assignment: bit 0 atmosphere, bit 1 vacuum.
        # Positive simulated flow means increasing model volume, not sensor polarity.
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
        log["estimated_volume"].append(estimator.volume_ml)
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
        estimated_volume_ml=np.asarray(log["estimated_volume"]),
        estimated_sensor_bias_slm=estimator.zero_bias_slm,
        opening=np.asarray(log["opening"]),
        command_v=np.asarray(log["dac"]),
        valve_mask=np.asarray(log["mask"], dtype=int),
    )


class LungModelCanvas(FigureCanvas):
    def __init__(self) -> None:
        self.figure = Figure(figsize=(8.5, 7.2), tight_layout=True)
        super().__init__(self.figure)
        # Keep the diagram at a readable size; its viewport can scroll instead
        # of squeezing text and valves together when a splitter gets narrow.
        self.setFixedSize(580, 680)
        self.ax = self.figure.add_subplot(111)
        self.figure.patch.set_facecolor("white")

    def draw_frame(
        self,
        result: SingleLungResult,
        index: int,
        cfg: SingleLungConfig,
        state_names=None,
    ) -> None:
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
        names = STATE_NAMES if state_names is None else state_names
        state_name = names.get(state, f"UNKNOWN ({state})")

        ax.text(
            0.25, 9.45,
            f"t = {time_s:6.2f} s    State = {state_name}    Cycle = {cycle + 1}/{cfg.cycles}",
            fontsize=11, family="monospace",
            bbox=dict(boxstyle="round,pad=0.35", fc="#edf4fb", ec="#7a9bbd"),
        )

        ax.add_patch(FancyBboxPatch((0.45, 8.05), 1.8, 0.7, boxstyle="round,pad=0.1",
                                    fc="#dff3ff", ec="#2878a8", lw=1.5))
        ax.text(1.35, 8.40, "Atmosphere Vent", ha="center", va="center", fontsize=11)
        ax.add_patch(FancyBboxPatch((7.75, 8.05), 1.8, 0.7, boxstyle="round,pad=0.1",
                                    fc="#eee4fa", ec="#7550a5", lw=1.5))
        ax.text(8.65, 8.40, "Vacuum Exhaust", ha="center", va="center", fontsize=11)

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
        self.overview = False
        self.window_s = 12.0
        self.end_s = 0.0

    def set_overview(self, enabled: bool) -> None:
        self.overview = enabled
        if self.time_markers:
            self.set_time(float(self.time_markers[0].get_xdata()[0]))

    def set_result(self, result: SingleLungResult, cfg: SingleLungConfig) -> None:
        for ax in self.axes:
            ax.clear()
        t = result.time_s
        self.end_s = float(t[-1])
        self.window_s = max(8.0, 3.0 * cfg.cycle_period_s)
        self.axes[0].plot(t, result.volume_ml, label="Model true volume")
        if cfg.volume_feedback_source == "sensor_integral":
            self.axes[0].plot(t, result.estimated_volume_ml, color="#2a8a69", ls="--",
                              lw=1.1, label="Sensor-integrated estimate")
        self.axes[0].axhline(cfg.target_volume_ml, color="k", ls="--", lw=0.8, label="Target")
        self.axes[0].set_ylabel("Volume / mL")
        self.axes[1].plot(t, result.flow_slm, label="Measured")
        self.axes[1].plot(t, result.flow_reference_slm, "k--", lw=0.9, label="Flat controller setpoint")
        self.axes[1].set_ylabel("Flow / SLM")
        if cfg.volume_deadline_guard:
            self.axes[1].set_title("Volume guard may override requested flow", fontsize=9)
        self.axes[2].plot(t, result.opening, label="PPV command opening")
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
        self.set_time(0.0)

    def set_time(self, time_s: float) -> None:
        for marker in self.time_markers:
            marker.set_xdata([time_s, time_s])
        if self.overview or self.end_s <= self.window_s:
            self.axes[-1].set_xlim(0.0, max(self.end_s, self.window_s))
        else:
            left = float(np.clip(time_s - 0.7 * self.window_s,
                                 0.0, self.end_s - self.window_s))
            self.axes[-1].set_xlim(left, left + self.window_s)
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
            self.ax.plot(t, result.flow_reference_slm[start:index + 1], "k--",
                         label="Flat setpoint", lw=1.0)
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
            ("reference", "Flat flow setpoint"),
            ("volume", "Volume"),
            ("estimated_volume", "Sensor-integrated volume"),
            ("opening", "PPV command opening"),
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
        self.metric_labels["estimated_volume"].setText(
            f"{result.estimated_volume_ml[index]:.1f} mL"
        )
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
        self.setWindowTitle("Single-Lung Modeling and Control Guide")
        self.resize(1050, 760)
        self.setMinimumSize(800, 580)
        layout = QtWidgets.QVBoxLayout(self)
        tabs = QtWidgets.QTabWidget()
        applied = parent.cfg if parent is not None and hasattr(parent, "cfg") else SingleLungConfig()
        for title, body in self._pages(applied):
            page = QtWidgets.QTextBrowser()
            page.setFont(QtGui.QFont("Segoe UI", 10))
            page.setHtml(self._styled_html(body))
            tabs.addTab(page, title)
        layout.addWidget(tabs, 1)
        button = QtWidgets.QPushButton("Close")
        button.clicked.connect(self.accept)
        layout.addWidget(button, alignment=QtCore.Qt.AlignRight)

    @staticmethod
    def _styled_html(body: str) -> str:
        return (
            "<html><head><style>"
            "body { color:#24313d; font-family:'Segoe UI',Arial,sans-serif; font-size:10pt; }"
            "h2 { color:#17466d; margin-bottom:7px; } h3 { color:#245d80; margin-bottom:5px; }"
            "p, li { line-height:1.35; } table { border-collapse:collapse; width:100%; }"
            "th, td { border:1px solid #cbd7e1; padding:6px; vertical-align:top; }"
            "th { background:#eaf2f8; } code, pre { font-family:Consolas,monospace; }"
            "pre { background:#f2f6fa; padding:9px; }"
            ".note { background:#fff4d9; padding:8px; }"
            "</style></head><body>" + body + "</body></html>"
        )

    @staticmethod
    def _equation_table(rows: List[Tuple[str, str]]) -> str:
        """Lay out rich-text equations without relying on narrow monospace lines."""
        cells = "".join(
            '<tr><td width="180" style="background:#eaf2f8"><b>' + label + '</b></td>'
            '<td style="background:#f5f9fc; padding:6px">'
            '<span style="font-family:Segoe UI; font-size:11pt; color:#173d59">'
            + expression + '</span></td></tr>'
            for label, expression in rows
        )
        return '<table width="100%" cellspacing="0" cellpadding="4">' + cells + '</table>'

    @staticmethod
    def _pages(cfg: SingleLungConfig) -> List[Tuple[str, str]]:
        model_note = (
            f"Assumed finite fill from 0 to {cfg.lung.capacity_ml:g} mL in "
            f"{cfg.lung.full_open_fill_time_s:.3f} s at full opening."
        )
        timing_equations = FormulaDialog._equation_table([
            ("Cycle period", "T<sub>cycle</sub> = 60 / respiratory rate [s]"),
            ("Inspiration time", "T<sub>in</sub> = T<sub>cycle</sub> × I / (I + E)"),
            ("Expiration time", "T<sub>out</sub> = T<sub>cycle</sub> × E / (I + E)"),
            ("Inspiration target", "Q<sub>ref,in</sub> = 0.06 × V<sub>target</sub> / T<sub>fill</sub> [SLM]"),
            ("Expiration target", "Q<sub>ref,out</sub> = manual setting, or "
             "0.06 × V<sub>target</sub> / max(0.25 s, T<sub>out</sub> − T<sub>dead</sub>) [SLM]"),
        ])
        plant_equations = FormulaDialog._equation_table([
            ("Inspiration", "dV/dt = a<sup>γ</sup> × (V<sub>drive</sub> − V) / τ<sub>in</sub>, until V<sub>max</sub>"),
            ("Assumed finite fill", "V<sub>drive</sub> = V<sub>max</sub> / "
             "[1 − exp(−T<sub>open</sub> / τ<sub>in</sub>)]"),
            ("Expiration", "dV/dt = −a<sup>γ</sup> × V / τ<sub>out</sub>"),
            ("True flow", "Q<sub>true</sub> = 0.06 × ΔV / Δt [SLM], including at the stop"),
            ("Limits", "0 ≤ a ≤ 1; &nbsp; 0 ≤ V ≤ V<sub>max</sub>"),
        ])
        control_equations = FormulaDialog._equation_table([
            ("Full-open flow, in", "Q<sub>open,in</sub> = 0.06 × (V<sub>drive</sub> − V) / τ<sub>in</sub> "
             "while V &lt; V<sub>max</sub>"),
            ("Full-open flow, out", "Q<sub>open,out</sub> = 0.06 × V / τ<sub>out</sub>"),
            ("Feedforward", "a<sub>ff</sub> = min(1, [Q<sub>ref</sub> / "
             "max(Q<sub>open</sub>, 0.1 SLM)]<sup>1/γ</sup>)"),
            ("Flow error", "e = Q<sub>ref</sub> − |Q<sub>feedback</sub>|"),
            ("Derivative", "D = −Δ|Q<sub>feedback</sub>| / Δt"),
            ("Integral candidate", "I<sub>next</sub> = clip(I + e × Δt, −I<sub>limit</sub>, I<sub>limit</sub>)"),
            ("PPV opening", "a = clip(B + F + K<sub>p</sub> × e + K<sub>i</sub> × I<sub>next</sub> "
             "+ K<sub>d</sub> × D, 0, 1)<br>F = a<sub>ff</sub> when enabled; otherwise F = 0"),
            ("PPV voltage", "U<sub>cmd</sub> = 5(1 − a) V"),
        ])
        smith_equations = FormulaDialog._equation_table([
            ("Flow estimate", "Q<sub>estimate</sub> = Q<sub>model,now</sub> + g "
             "(Q<sub>measured</sub> − Q<sub>model,delayed</sub>)"),
            ("PID feedback", "Q<sub>estimate</sub> with Smith enabled; "
             "Q<sub>measured</sub> with Smith disabled"),
        ])
        overview = f"""
        <h2>System and breathing sequence</h2>
        <p>This is a sampled, first-order grey-box simulation of one lung bag, two
        routing solenoids and one proportional pinch valve (PPV). The state machine
        selects the flow direction; the flow controller sets PPV opening.</p>
        <table><tr><th>Phase</th><th>Physical interpretation</th><th>Model action</th></tr>
        <tr><td>Precondition</td><td>Allow initial evacuation before the first breath.</td>
        <td>Vacuum route is requested while volume exceeds the 5 mL end threshold.
        The simulated lung starts at 0 mL, so it normally waits in this state
        without opening the vacuum route.</td></tr>
        <tr><td>Inspiration</td><td>Atmospheric filling and lung expansion.</td>
        <td>Atmosphere solenoid open, vacuum closed; positive modeled volume-change flow.</td></tr>
        <tr><td>Expiration</td><td>Vacuum-driven lung contraction.</td>
        <td>Vacuum solenoid open, atmosphere closed; negative modeled volume-change flow.</td></tr>
        <tr><td>Direction change</td><td>Prevent simultaneous routing.</td>
        <td>Both solenoids requested closed for the configured dead time.</td></tr></table>
        <h3>Timing and setpoints</h3>
        {timing_equations}
        <p>The expiration expression is an automatic target-flow heuristic. The
        displayed references are positive magnitudes; the expiration curve uses
        a negative sign. With the timed-cycle option enabled, the two direction
        changes occupy part of the I:E phase slots and the simulated period is
        60/bpm. In legacy timing they are added outside the slots, making the
        period about <code>60/bpm + 2 × Tdead</code>. Playback
        speed changes display speed only; the model is computed at {cfg.control_hz:.0f} Hz.
        Simulation cycles sets the number of repetitions; precondition time sets
        the duration of the initial evacuation state.</p>
        <p class="note"><b>Currently applied:</b> {cfg.respiratory_rate_bpm:g} bpm,
        I:E = {cfg.ie_inspiration:g}:{cfg.ie_expiration:g}, target {cfg.target_volume_ml:g} mL,
        Tin slot = {cfg.inspiration_s:.3f} s, Tout slot = {cfg.expiration_s:.3f} s;
        active flow times = {cfg.active_inspiration_s:.3f} / {cfg.active_expiration_s:.3f} s.
        Unsaved edits in the developer panel are not included here.</p>
        """
        physical = f"""
        <h2>Lung and valve model</h2>
        {plant_equations}
        <p>The factor 0.06 converts mL/s to standard litres per minute.
        These are empirical response equations; the GUI does not solve pressure,
        compliance, tubing resistance and vacuum dynamics separately.</p>
        <table><tr><th>Developer parameter</th><th>Physical meaning and effect</th></tr>
        <tr><td>Lung Vmax / mL</td><td>Maximum expansion represented by this model;
        it is different from requested tidal volume. It is the capacity stop in
        the assumed finite-fill response.</td></tr>
        <tr><td>Full-open reach time</td><td>Assumed full-open atmosphere-path inflation endpoint,
        currently 1.30 s. It does not describe a measured response of the final
        solenoid, PPV and sensor path.</td></tr>
        <tr><td>τin / s</td><td>Open-path curve-shape parameter. In this model,
        changing τin also changes the virtual drive equilibrium so that the assumed
        full-open endpoint stays fixed. The intermediate flow is unmeasured.</td></tr>
        <tr><td>τout / s</td><td>Contraction time constant during vacuum exhaust.
        It lumps vacuum-path restriction and lung response;
        smaller values produce faster expiration.</td></tr>
        <tr><td>PPV exponent γ</td><td>Empirical nonlinearity from normalized valve
        opening to effective flow: <code>a^γ</code>. Fit it from several fixed-opening
        tests; it is not a manufacturer voltage calibration.</td></tr>
        <tr><td>Target tidal volume</td><td>Controller stopping threshold during
        inspiration. Achieved volume may be lower when the phase ends or the valve
        is fully open.</td></tr>
        <tr><td>Target fill time</td><td>Time used to calculate inspiratory average
        target flow. It must fit inside Tin.</td></tr>
        <tr><td>End volume / 5 mL</td><td>Fixed model threshold that closes the vacuum
        route during precondition and expiration.</td></tr></table>
        <p>The displayed model volume is the internal simulated state. The displayed
        measured flow comes from the simulated sensor path, with lag and noise.</p>
        <p class="note"><b>Applied assumption:</b> {model_note}
        τin = {cfg.lung.tau_in_s:.3f} s.
        The 250 mL capacity and 1.30 s visual endpoint are reported separately.
        Orifice.mlx instead uses 1.30 s as a requirement and estimates 558 mL from
        external dimensions; its generated curves are not measured volume traces.</p>
        """
        delays = """
        <h2>Delays and sensor dynamics</h2>
        <pre>PID opening + route
    → optional input delay of (opening, direction)
    → lung model: V and Qtrue
    → transport delay of Qtrue to sensor path
    → sensor first-order lag + Gaussian noise → Qmeasured
    → optional Smith feedback estimate → PID</pre>
        <table><tr><th>Setting</th><th>What is represented</th><th>How to identify it</th></tr>
        <tr><td>Input delay / s</td><td>Optional pure hold between controller command
        and pneumatic effect at the lung. It approximates actuation or command-path
        latency; it delays both opening and direction in the simulated plant.</td>
        <td>Compare command time with the earliest physical flow response.</td></tr>
        <tr><td>Transport delay / s</td><td>Pure delay between true lung flow and
        flow arriving at the sensor model. It delays feedback, not lung motion.</td>
        <td>Compare a lung-side flow reference with sensor response, if available.</td></tr>
        <tr><td>Sensor τ / s</td><td>First-order smoothing after transport delay.
        It rounds fast changes rather than shifting a waveform by a fixed time.</td>
        <td>Fit the rise and fall of measured sensor flow after the delay.</td></tr>
        <tr><td>Flow noise σ / SLM</td><td>Standard deviation of added Gaussian
        sample noise. It does not model calibration scale error or packet loss.</td>
        <td>Estimate from steady-flow or zero-flow measurements.</td></tr>
        <tr><td>Flow sensor zero bias / SLM</td><td>Fixed offset added to each
        simulated flow sample. Sensor-integral feedback estimates this offset
        from the initial closed-valve precondition samples.</td>
        <td>Measure with both valves closed and verified zero physical flow.</td></tr>
        <tr><td>Direction-change dead time / s</td><td>Intentional state-machine
        interval with both solenoids closed. It is separate from either transport
        or sensor delay.</td><td>Choose from valve switching requirements and verify
        route timing.</td></tr></table>
        <p class="note">The Smith setting represents feedback delay. A first-order
        sensor lag is only approximated by one effective Smith delay; it is not an
        exact replacement for the lag equation. Input delay is handled by its own
        simulated command queue and is not removed by the Smith predictor.</p>
        """
        control = f"""
        <h2>Flow control, feedforward and Smith prediction</h2>
        <h3>PPV and PID</h3>
        {control_equations}
        <p><code>a=0</code> means closed (5 V); <code>a=1</code> means fully open
        (0 V). PID controls flow magnitude; the routing solenoids determine its
        sign. Kp reacts to present flow error, Ki removes persistent error, and
        Kd damps changes in measured flow. The integral is limited and updated
        conditionally at output saturation. PID state is reset at phase changes.</p>
        <p><b>Optional volume deadline guard:</b> uses the selected volume feedback
        and assumed lung model to calculate the minimum opening needed to reach
        the phase-end volume. The final command is the greater of this opening
        and the ordinary flow PID command. It may exceed the flat flow reference.
        With <code>sensor_integral</code>, the volume feedback comes only from
        zero-calibrated integration of delayed, noisy simulated sensor flow;
        the Smith model uses the assumed input delay. The simulator still uses
        a known empty starting volume and clips the estimate to the assumed
        capacity. Standard-to-actual volume conversion and physical sensor
        validation are still required before hardware use.</p>
        <table><tr><th>Controller setting</th><th>Meaning</th></tr>
        <tr><td>Kp</td><td>Proportional change in normalized PPV opening per SLM
        of flow error.</td></tr>
        <tr><td>Ki</td><td>Opening change per accumulated SLM·s of error.</td></tr>
        <tr><td>Kd</td><td>Response to measured-flow slope; derivative is taken
        on feedback to avoid a target-step kick.</td></tr>
        <tr><td>Integral limit</td><td>Maximum magnitude of accumulated flow error
        in SLM·s, before multiplication by Ki.</td></tr>
        <tr><td>Smith delay / s</td><td>Effective delay of the feedback flow
        used in the predictor's model history.</td></tr>
        <tr><td>Smith correction gain</td><td>Weight applied to the difference
        between measured flow and delayed model flow.</td></tr></table>
        <h3>Model feedforward: when to enable</h3>
        <p>Enable after Vmax, τin, τout and γ have been fitted to the pneumatic
        setup, and the requested flow is within the observed full-open range.
        On normal phase changes, the PPV retains its last opening while both
        routing solenoids are closed. The controller sets bias B so its first
        command in the next phase equals that retained opening, then adjusts
        for flow error. B returns to zero by at most 50 normalized opening
        units per second. This transition rule acts on the commanded opening;
        actual PPV motion is not measured. Stop and completion close the PPV.
        Feedforward supplies a model opening estimate; PID corrects model error.
        Disable while identifying the open-loop model or when hardware changes make
        its opening estimate unreliable. Saturation at <code>a=1</code> means the
        requested flow cannot be produced by this model at that volume.</p>
        <h3>Smith predictor: what it changes</h3>
        {smith_equations}
        <p>The predictor runs a copy of the lung model using the command after
        any enabled input delay. It delays its own predicted flow by the configured
        Smith delay and corrects the current model estimate with the sensor residual.
        A gain of 1 applies the full residual; 0 uses only the model prediction.
        A gain above 1 amplifies residual errors and noise.</p>
        <p>Enable after the lung model and feedback delay are measured, a baseline
        PID is stable, and delayed feedback causes visible tracking error or
        oscillation. Start the delay estimate near transport delay plus an
        empirically chosen contribution from sensor lag; compare Smith on/off
        with the same test waveform. Disable if the model is inaccurate, sensor
        noise dominates, or tracking becomes less stable. Smith does not estimate
        the command/input delay.</p>
        """
        workflow = """
        <h2>Identification and interpretation</h2>
        <ol>
        <li>Confirm valve routing, flow sign and PPV voltage-to-opening direction.
        The model uses atmosphere for inspiration and vacuum for expiration.</li>
        <li>Record full-open lung volume/flow responses in each direction. Fit
        Vmax, τin and τout, then repeat at several fixed PPV openings to fit γ.</li>
        <li>Record command and flow timestamps. Separate command-to-lung latency,
        sensor-path delay, sensor lag and intentional direction-change dead time.</li>
        <li>Test feasible target volumes and flow references. Enable feedforward
        once the model roughly predicts the opening; then tune Kp, Ki and Kd.</li>
        <li>If feedback timing limits control, test Smith with a measured delay
        estimate. Compare target tracking, peak error and oscillation with Smith
        on and off under identical settings.</li>
        </ol>
        <h3>What this demonstration omits</h3>
        <p>The plant omits explicit pressure, changing vacuum supply, leakage,
        valve hysteresis, mechanical fatigue and sensor calibration drift. The
        simulation is calculated before playback. In the STM32 demonstration,
        measured SFM3300 flow is displayed separately; it does not enter this
        simulated PID, and that firmware does not drive the PPV. Feedforward and
        Smith currently use the internal simulated volume; physical deployment
        would require a validated volume estimate.</p>
        <p class="note">A target calculated from average flow is a timing
        requirement, not a guarantee of tidal volume. Check the achieved volume
        and flow curves after each parameter change.</p>
        """
        return [
            ("System & Setpoints", overview),
            ("Physical Parameters", physical),
            ("Delay Model", delays),
            ("PID, FF & Smith", control),
            ("Calibration", workflow),
        ]


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("LungLogic PID Digital Twin | Single 250 mL Lung")
        self.resize(1320, 860)
        self.cfg = load_config(CONFIG_PATH)
        self.result: SingleLungResult | None = None
        self.play_index = 0.0
        self.playing = False
        self._play_last_wall_time: float | None = None
        self._play_accumulator = 0.0
        self.inputs: Dict[str, QtWidgets.QDoubleSpinBox] = {}
        self.user_inputs: Dict[str, QtWidgets.QDoubleSpinBox] = {}
        self._user_flow_last_index = -1
        self._build_ui()
        self.live_monitor = LiveMonitorWindow(self)
        self.timer = QtCore.QTimer(self)
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self._advance)
        self.apply_and_reset()

    def _build_ui(self) -> None:
        root = QtWidgets.QWidget()
        root_layout = QtWidgets.QVBoxLayout(root)
        root_layout.setContentsMargins(8, 8, 8, 8)
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.addWidget(QtWidgets.QLabel("Mode"))
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems(["User Mode", "Developer Mode"])
        self.mode_combo.setMinimumWidth(190)
        mode_row.addWidget(self.mode_combo)
        mode_row.addStretch(1)
        root_layout.addLayout(mode_row)

        self.mode_stack = QtWidgets.QStackedWidget()
        self.mode_stack.addWidget(self._build_user_page())
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.plot_canvas = ResultCanvas()
        splitter.addWidget(self._build_control_panel())
        self.tabs = QtWidgets.QTabWidget()
        self.model_canvas = LungModelCanvas()
        self.model_view = self._model_view(self.model_canvas)
        self.tabs.addTab(self.model_view, "Dynamic Model")
        self.tabs.addTab(self.plot_canvas, "Live Curves")
        splitter.addWidget(self.tabs)
        splitter.setSizes([390, 930])
        splitter.setStretchFactor(1, 1)
        self.mode_stack.addWidget(splitter)
        root_layout.addWidget(self.mode_stack, 1)
        self.setCentralWidget(root)
        self.mode_combo.currentIndexChanged.connect(self._switch_mode)
        self.statusBar().showMessage("Set breathing parameters, click Apply, then Start.")

    @staticmethod
    def _model_view(canvas: LungModelCanvas) -> QtWidgets.QScrollArea:
        view = QtWidgets.QScrollArea()
        view.setWidget(canvas)
        view.setWidgetResizable(False)
        view.setAlignment(QtCore.Qt.AlignCenter)
        view.setFrameShape(QtWidgets.QFrame.NoFrame)
        view.setMinimumSize(0, 0)
        view.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Ignored)
        return view

    def _build_user_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        layout.addWidget(splitter)

        settings = QtWidgets.QWidget()
        settings.setMinimumWidth(270)
        settings.setMaximumWidth(340)
        side = QtWidgets.QVBoxLayout(settings)
        title = QtWidgets.QLabel("Single-Lung Breathing")
        title.setStyleSheet("font-size:18px; font-weight:bold; color:#17466d")
        title.setWordWrap(True)
        side.addWidget(title)
        form_group = QtWidgets.QGroupBox("Breathing Settings")
        form = QtWidgets.QFormLayout(form_group)
        self._user_spin(form, "rr", "Rate / bpm", self.cfg.respiratory_rate_bpm, 1, 40, 1, 1)
        self._user_spin(form, "volume", "Tidal volume / mL", self.cfg.target_volume_ml, 10, 250, 5, 1)
        self._user_spin(form, "ie_in", "I:E inspiration", self.cfg.ie_inspiration, 0.1, 10, 0.1, 1)
        self._user_spin(form, "ie_out", "I:E expiration", self.cfg.ie_expiration, 0.1, 10, 0.1, 1)
        in_flow = 0.06 * self.cfg.target_volume_ml / self.cfg.target_fill_time_s
        out_flow = self.cfg.target_expiration_flow_slm or self._automatic_expiration_flow(self.cfg)
        self._user_spin(form, "in_flow", "Insp. flow / SLM", in_flow, 0.1, 220, 0.5, 3)
        self._user_spin(form, "out_flow", "Exp. flow / SLM", out_flow, 0.1, 220, 0.5, 3)
        self._user_spin(form, "cycles", "Simulation cycles", self.cfg.cycles, 1, 100, 1, 0)
        side.addWidget(form_group)
        self.user_timing = QtWidgets.QLabel()
        self.user_timing.setWordWrap(True)
        self.user_timing.setStyleSheet("color:#36556c")
        side.addWidget(self.user_timing)
        for field in self.user_inputs.values():
            field.valueChanged.connect(self._update_user_timing)
        self._update_user_timing()

        apply_button = QtWidgets.QPushButton("Apply Settings")
        apply_button.setMinimumHeight(38)
        apply_button.setStyleSheet("font-weight:bold; background:#dceeff")
        apply_button.clicked.connect(self._apply_user_settings)
        side.addWidget(apply_button)
        row = QtWidgets.QHBoxLayout()
        self.user_start_button = QtWidgets.QPushButton("Start")
        self.user_start_button.clicked.connect(self.toggle_play)
        reset = QtWidgets.QPushButton("Reset")
        reset.clicked.connect(self.reset_playback)
        stop = QtWidgets.QPushButton("Stop")
        stop.clicked.connect(self.stop_playback)
        row.addWidget(self.user_start_button)
        row.addWidget(reset)
        row.addWidget(stop)
        side.addLayout(row)

        readout = QtWidgets.QGroupBox("Simulation: current frame and run result")
        values = QtWidgets.QFormLayout(readout)
        self.user_readouts: Dict[str, QtWidgets.QLabel] = {}
        for key, label in (("phase", "Phase now"), ("flow", "Flow now"),
                           ("volume", "Volume now"), ("tidal", "Last breath / target")):
            value = QtWidgets.QLabel("—")
            self.user_readouts[key] = value
            values.addRow(label, value)
        side.addWidget(readout)
        self.user_accuracy = QtWidgets.QLabel()
        self.user_accuracy.setWordWrap(True)
        side.addWidget(self.user_accuracy)
        self.user_flow_note = QtWidgets.QLabel()
        self.user_flow_note.setWordWrap(True)
        side.addWidget(self.user_flow_note)
        note = QtWidgets.QLabel("All values are simulated. Phase, flow and volume follow the current playback frame; tidal volume summarizes the last modeled breath.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#666")
        side.addWidget(note)
        self.user_model_assumption = QtWidgets.QLabel()
        self.user_model_assumption.setWordWrap(True)
        self.user_model_assumption.setStyleSheet("color:#684d19")
        side.addWidget(self.user_model_assumption)
        side.addStretch(1)
        splitter.addWidget(settings)

        charts = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.user_model_canvas = LungModelCanvas()
        self.user_flow_canvas = LiveFlowCanvas()
        self.user_model_view = self._model_view(self.user_model_canvas)
        charts.addWidget(self.user_model_view)
        flow_panel = QtWidgets.QWidget()
        self.user_flow_layout = QtWidgets.QVBoxLayout(flow_panel)
        self.user_flow_layout.setContentsMargins(0, 0, 0, 0)
        self.user_flow_layout.addWidget(self.user_flow_canvas, 1)
        charts.addWidget(flow_panel)
        charts.setSizes([590, 410])
        splitter.addWidget(charts)
        splitter.setSizes([310, 1000])
        splitter.setStretchFactor(1, 1)
        return page

    def _user_spin(self, form, key, label, value, low, high, step, decimals) -> None:
        widget = QtWidgets.QDoubleSpinBox()
        widget.setRange(low, high)
        widget.setSingleStep(step)
        widget.setDecimals(decimals)
        widget.setValue(value)
        widget.setKeyboardTracking(False)
        self.user_inputs[key] = widget
        form.addRow(label, widget)

    @staticmethod
    def _automatic_expiration_flow(cfg: SingleLungConfig) -> float:
        return 0.06 * cfg.target_volume_ml / max(0.25, cfg.expiration_s - cfg.dead_time_s)

    def _update_user_timing(self) -> None:
        rr = self.user_inputs["rr"].value()
        ratio_in = self.user_inputs["ie_in"].value()
        ratio_out = self.user_inputs["ie_out"].value()
        duration = 60.0 / rr
        inspiration = duration * ratio_in / (ratio_in + ratio_out)
        expiration = duration - inspiration
        active_inspiration = inspiration - (self.cfg.dead_time_s if self.cfg.dead_time_within_cycle else 0.0)
        volume = self.user_inputs["volume"].value()
        minimum_in = 0.06 * volume / max(active_inspiration, 0.01)
        minimum_out = 0.06 * max(0.0, volume - self.cfg.end_volume_ml) / max(
            0.25, expiration - self.cfg.dead_time_s
        )
        warnings = []
        if self.user_inputs["in_flow"].value() < minimum_in:
            if self.cfg.volume_deadline_guard:
                warnings.append("The simulated volume guard may exceed the inspiration flow target.")
            else:
                warnings.append("Increase inspiration flow or reduce rate/volume.")
        if self.user_inputs["out_flow"].value() < minimum_out:
            warnings.append("Increase expiration flow or allow more expiration time.")
        warning = "\n" + " ".join(warnings) if warnings else ""
        self.user_timing.setText(
            f"Inspiration slot {inspiration:.2f} s  |  Expiration slot {expiration:.2f} s\n"
            f"Active inspiration {active_inspiration:.2f} s\n"
            f"Minimum flow for timing: {minimum_in:.2f} / {minimum_out:.2f} SLM (in / out){warning}"
        )

    def _apply_user_settings(self) -> None:
        rr = self.user_inputs["rr"].value()
        ratio_in = self.user_inputs["ie_in"].value()
        ratio_out = self.user_inputs["ie_out"].value()
        inspiration = 60.0 / rr * ratio_in / (ratio_in + ratio_out)
        volume = self.user_inputs["volume"].value()
        flow = self.user_inputs["in_flow"].value()
        fill_time = 0.06 * volume / flow
        active_inspiration = inspiration - (self.cfg.dead_time_s if self.cfg.dead_time_within_cycle else 0.0)
        allowed_fill_time = inspiration if self.cfg.volume_deadline_guard else active_inspiration
        if fill_time > allowed_fill_time + 1e-9:
            QtWidgets.QMessageBox.warning(
                self, "Flow target", f"Inspiration flow must be at least {0.06 * volume / allowed_fill_time:.2f} SLM for this breathing rate and I:E ratio."
            )
            return
        cfg = copy.deepcopy(self.cfg)
        cfg.respiratory_rate_bpm = rr
        cfg.target_volume_ml = volume
        cfg.ie_inspiration = ratio_in
        cfg.ie_expiration = ratio_out
        cfg.target_fill_time_s = fill_time
        cfg.target_expiration_flow_slm = self.user_inputs["out_flow"].value()
        cfg.cycles = int(self.user_inputs["cycles"].value())
        self.stop_playback()
        self._apply_config(cfg)

    def _sync_user_from_config(self) -> None:
        cfg = self.cfg
        values = {
            "rr": cfg.respiratory_rate_bpm, "volume": cfg.target_volume_ml,
            "ie_in": cfg.ie_inspiration, "ie_out": cfg.ie_expiration,
            "in_flow": 0.06 * cfg.target_volume_ml / cfg.target_fill_time_s,
            "out_flow": cfg.target_expiration_flow_slm or self._automatic_expiration_flow(cfg),
            "cycles": cfg.cycles,
        }
        for key, value in values.items():
            with QtCore.QSignalBlocker(self.user_inputs[key]):
                self.user_inputs[key].setValue(value)
        self._update_user_timing()

    def _sync_developer_from_config(self) -> None:
        cfg = self.cfg
        values = {
            "rr": cfg.respiratory_rate_bpm, "ie_in": cfg.ie_inspiration,
            "ie_out": cfg.ie_expiration, "volume": cfg.target_volume_ml,
            "fill_time": cfg.target_fill_time_s,
            "exp_flow": cfg.target_expiration_flow_slm or self._automatic_expiration_flow(cfg),
            "cycles": cfg.cycles, "kp": cfg.pi.kp, "ki": cfg.pi.ki,
            "kd": cfg.pi.kd, "ilim": cfg.pi.integral_limit,
            "smith_delay": cfg.smith.delay_s, "smith_gain": cfg.smith.correction_gain,
            "input_delay": cfg.input_delay.delay_s, "capacity": cfg.lung.capacity_ml,
            "tau_in": cfg.lung.tau_in_s, "full_open_fill_time": cfg.lung.full_open_fill_time_s,
            "tau_out": cfg.lung.tau_out_s,
            "gamma": cfg.lung.valve_exponent, "delay": cfg.lung.transport_delay_s,
            "sensor_tau": cfg.lung.sensor_tau_s, "noise": cfg.lung.sensor_noise_slm,
            "sensor_bias": cfg.lung.sensor_bias_slm,
            "dead": cfg.dead_time_s, "precondition": cfg.precondition_s,
        }
        for key, value in values.items():
            with QtCore.QSignalBlocker(self.inputs[key]):
                self.inputs[key].setValue(value)
        for checkbox, checked in (
            (self.exp_flow_auto, cfg.target_expiration_flow_slm is None),
            (self.feedforward, cfg.pi.use_feedforward),
            (self.volume_deadline_guard, cfg.volume_deadline_guard),
            (self.dead_time_within_cycle, cfg.dead_time_within_cycle),
            (self.smith_enabled, cfg.smith.enabled),
            (self.input_delay_enabled, cfg.input_delay.enabled),
        ):
            with QtCore.QSignalBlocker(checkbox):
                checkbox.setChecked(checked)
        self.inputs["exp_flow"].setEnabled(not self.exp_flow_auto.isChecked())
        with QtCore.QSignalBlocker(self.volume_feedback_combo):
            self.volume_feedback_combo.setCurrentIndex(
                self.volume_feedback_combo.findData(cfg.volume_feedback_source)
            )

    def _switch_mode(self, index: int) -> None:
        self.mode_stack.setCurrentIndex(index)
        if index == 1:
            self._show_live_monitor()
        else:
            self.live_monitor.hide()
        if self.result is not None:
            self._user_flow_last_index = -1
            self._show_index(min(int(self.play_index), self.result.time_s.size - 1))

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
        self._spin(form, "fill_time", "Target fill time / s", self.cfg.target_fill_time_s, 0.001, 60, 0.05, 4)
        self.exp_flow_auto = QtWidgets.QCheckBox("Derive expiration flow from I:E")
        self.exp_flow_auto.setChecked(self.cfg.target_expiration_flow_slm is None)
        form.addRow(self.exp_flow_auto)
        self._spin(form, "exp_flow", "Expiration target flow / SLM",
                   self.cfg.target_expiration_flow_slm or self._automatic_expiration_flow(self.cfg),
                   0.1, 220, 0.5, 3)
        self.inputs["exp_flow"].setEnabled(not self.exp_flow_auto.isChecked())
        self.exp_flow_auto.toggled.connect(lambda checked: self.inputs["exp_flow"].setEnabled(not checked))
        self._spin(form, "cycles", "Simulation cycles", self.cfg.cycles, 1, 100, 1, 0)
        self.dead_time_within_cycle = QtWidgets.QCheckBox("Include direction dead time within bpm period")
        self.dead_time_within_cycle.setChecked(self.cfg.dead_time_within_cycle)
        form.addRow(self.dead_time_within_cycle)
        self._spin(form, "play_speed", "Playback speed", 1.0, 0.1, 10, 0.1, 2)
        overview = QtWidgets.QCheckBox("Show full run in Live Curves")
        overview.setChecked(False)
        overview.toggled.connect(self.plot_canvas.set_overview)
        form.addRow(overview)
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
        self.volume_deadline_guard = QtWidgets.QCheckBox("Enable volume deadline guard (simulation)")
        self.volume_deadline_guard.setChecked(self.cfg.volume_deadline_guard)
        form.addRow(self.volume_deadline_guard)
        self.volume_feedback_combo = QtWidgets.QComboBox()
        self.volume_feedback_combo.addItem("Internal true volume (legacy simulation)", "oracle")
        self.volume_feedback_combo.addItem("Integrate simulated sensor flow", "sensor_integral")
        self.volume_feedback_combo.setCurrentIndex(
            self.volume_feedback_combo.findData(self.cfg.volume_feedback_source)
        )
        form.addRow("Volume feedback source", self.volume_feedback_combo)
        box.addWidget(controller)

        smith_group = QtWidgets.QGroupBox("Smith Predictor Delay Compensation")
        form = QtWidgets.QFormLayout(smith_group)
        self.smith_enabled = QtWidgets.QCheckBox("Enable Smith predictor")
        self.smith_enabled.setChecked(self.cfg.smith.enabled)
        form.addRow(self.smith_enabled)
        self._spin(form, "smith_delay", "Feedback delay estimate / s", self.cfg.smith.delay_s, 0, 1.0, 0.005, 3)
        self._spin(form, "smith_gain", "Correction gain", self.cfg.smith.correction_gain, 0, 2.0, 0.05, 2)
        smith_hint = QtWidgets.QLabel("Usually ≈ transport delay + sensor time constant")
        smith_hint.setStyleSheet("color:#666; font-size:11px;")
        form.addRow(smith_hint)
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
        self._spin(form, "full_open_fill_time", "Full-open reach time / s",
                   self.cfg.lung.full_open_fill_time_s, 0.05, 10, 0.05, 3)
        assumption_note = QtWidgets.QLabel(
            "Finite fill assumes a 0 mL, fully open atmosphere path reaches Vmax "
            "at this time. Intermediate flow and final hardware response are unknown."
        )
        assumption_note.setWordWrap(True)
        assumption_note.setStyleSheet("color:#684d19; font-size:11px")
        form.addRow(assumption_note)
        self._spin(form, "tau_in", "Tau_in / s", self.cfg.lung.tau_in_s, 0.02, 10, 0.01, 3)
        self._spin(form, "tau_out", "Tau_out / s", self.cfg.lung.tau_out_s, 0.02, 10, 0.01, 3)
        self._spin(form, "gamma", "Pinch valve exponent gamma", self.cfg.lung.valve_exponent, 0.5, 3, 0.05, 2)
        self._spin(form, "delay", "Pneumatic transport delay / s", self.cfg.lung.transport_delay_s, 0, 1.0, 0.005, 3)
        self._spin(form, "sensor_tau", "Flow sensor time constant / s", self.cfg.lung.sensor_tau_s, 0.001, 1.0, 0.005, 3)
        self._spin(form, "noise", "Flow noise sigma / SLM", self.cfg.lung.sensor_noise_slm, 0, 10, 0.05, 2)
        self._spin(form, "sensor_bias", "Flow sensor zero bias / SLM", self.cfg.lung.sensor_bias_slm, -2, 2, 0.01, 3)
        self._spin(form, "dead", "Direction-change dead time / s", self.cfg.dead_time_s, 0, 0.5, 0.01, 3)
        self._spin(form, "precondition", "Precondition time / s", self.cfg.precondition_s, 0, 10, 0.1, 2)
        box.addWidget(model)

        help_button = QtWidgets.QPushButton("Modeling & Control Guide")
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
        c = copy.deepcopy(self.cfg)
        c.respiratory_rate_bpm = self.inputs["rr"].value()
        c.ie_inspiration = self.inputs["ie_in"].value()
        c.ie_expiration = self.inputs["ie_out"].value()
        c.target_volume_ml = self.inputs["volume"].value()
        c.target_fill_time_s = self.inputs["fill_time"].value()
        c.target_expiration_flow_slm = None if self.exp_flow_auto.isChecked() else self.inputs["exp_flow"].value()
        c.cycles = int(self.inputs["cycles"].value())
        c.dead_time_s = self.inputs["dead"].value()
        c.dead_time_within_cycle = self.dead_time_within_cycle.isChecked()
        c.volume_deadline_guard = self.volume_deadline_guard.isChecked()
        c.volume_feedback_source = self.volume_feedback_combo.currentData()
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
        c.lung.full_open_fill_time_s = self.inputs["full_open_fill_time"].value()
        c.lung.tau_in_s = self.inputs["tau_in"].value()
        c.lung.tau_out_s = self.inputs["tau_out"].value()
        c.lung.valve_exponent = self.inputs["gamma"].value()
        c.lung.transport_delay_s = self.inputs["delay"].value()
        c.lung.sensor_tau_s = self.inputs["sensor_tau"].value()
        c.lung.sensor_noise_slm = self.inputs["noise"].value()
        c.lung.sensor_bias_slm = self.inputs["sensor_bias"].value()
        c.validate()
        return c

    def _show_live_monitor(self) -> None:
        self.live_monitor.show()
        if self.result is not None:
            self.live_monitor.update_frame(self.result, min(int(self.play_index), self.result.time_s.size - 1), self.cfg)
        self.live_monitor.raise_()
        self.live_monitor.activateWindow()

    def apply_and_reset(self) -> None:
        try:
            cfg = self._config_from_ui()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Parameter Error", str(exc))
            return
        self._apply_config(cfg)

    def _apply_config(self, cfg: SingleLungConfig) -> None:
        try:
            result = run_single_lung_simulation(cfg)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Parameter Error", str(exc))
            return
        self.stop_playback()
        self.cfg = cfg
        self.result = result
        save_config(cfg, CONFIG_PATH)
        self._sync_developer_from_config()
        self._sync_user_from_config()
        self.plot_canvas.set_result(result, cfg)
        self.play_index = 0.0
        self._user_flow_last_index = -1
        self._show_index(0)
        self.user_model_assumption.setText(
            f"Model assumption: with the atmosphere path and PPV fully open, "
            f"volume reaches {cfg.lung.capacity_ml:g} mL from empty in "
            f"{cfg.lung.full_open_fill_time_s:.2f} s. This is not an installed-path measurement. "
            "The dashed line is a flat flow setpoint calculated from target volume and fill time."
        )
        if cfg.volume_deadline_guard:
            self.user_model_assumption.setText(
                self.user_model_assumption.text()
                + " The simulated volume guard may request more flow to meet the deadline."
            )
        if cfg.volume_feedback_source == "sensor_integral":
            self.user_model_assumption.setText(
                self.user_model_assumption.text()
                + " Controller volume uses zero-calibrated simulated sensor-flow integration."
                + " Predicted tidal volume is still audited from the simulated lung state."
            )
        q_in = 0.06 * cfg.target_volume_ml / cfg.target_fill_time_s
        q_out = cfg.target_expiration_flow_slm or self._automatic_expiration_flow(cfg)
        last_cycle = result.volume_ml[result.cycle == cfg.cycles - 1]
        if last_cycle.size:
            achieved = simulated_inspiratory_tidal_ml(result, cfg.cycles - 1)
            self.user_readouts["tidal"].setText(f"{achieved:.1f} / {cfg.target_volume_ml:.1f} mL")
            error = abs(achieved - cfg.target_volume_ml)
            if error > max(5.0, 0.05 * cfg.target_volume_ml):
                self.user_accuracy.setText("Last simulated breath misses the target by more than 5%.")
                self.user_accuracy.setStyleSheet("color:#a7332a; font-weight:bold")
            else:
                self.user_accuracy.setText("Last simulated breath is within 5% of the target.")
                self.user_accuracy.setStyleSheet("color:#167b61")
        inspiration = result.state == STATE_INSPIRATION
        saturation = float(np.mean(result.opening[inspiration] >= 0.999)) if np.any(inspiration) else 0.0
        peak_flow = float(np.max(result.flow_slm[inspiration])) if np.any(inspiration) else 0.0
        if cfg.volume_deadline_guard and saturation >= 0.95:
            self.user_flow_note.setText(
                f"PPV command is fully open for {saturation:.0%} of inspiration; "
                f"peak modeled flow is {peak_flow:.1f} SLM. The volume guard requests "
                "more flow than the flat setpoint to meet the deadline. Once the PPV "
                "command is fully open, PID tuning alone cannot increase flow."
            )
            self.user_flow_note.setStyleSheet("color:#a05c16")
        else:
            self.user_flow_note.setText(
                f"Peak modeled inspiration flow: {peak_flow:.1f} SLM; "
                f"PPV command fully open for {saturation:.0%} of inspiration."
            )
            self.user_flow_note.setStyleSheet("color:#36556c")
        model_summary = f"Inspiratory model: assumed finite fill at {cfg.lung.full_open_fill_time_s:.2f} s\n"
        timing_summary = (
            "Direction dead time is included in the requested bpm period.\n"
            if cfg.dead_time_within_cycle else
            "Legacy timing adds direction dead time outside the requested bpm period.\n"
        )
        self.summary.setPlainText(
            "Parameters saved and applied for the next playback\n"
            f"Inspiration time: {cfg.inspiration_s:.3f} s\n"
            f"Expiration time: {cfg.expiration_s:.3f} s\n"
            f"Target tidal volume: {cfg.target_volume_ml:.1f} mL\n"
            + model_summary
            + timing_summary
            + f"Volume deadline guard: {'on (simulation only)' if cfg.volume_deadline_guard else 'off'}\n"
            + f"Volume feedback: {cfg.volume_feedback_source}; estimated sensor zero "
              f"{result.estimated_sensor_bias_slm:+.3f} SLM\n"
            +
            f"Inspiration/expiration target flow: {q_in:.2f} / {q_out:.2f} SLM\n"
            f"Delays: input {cfg.input_delay.delay_s:.3f} s ({'on' if cfg.input_delay.enabled else 'off'}), "
            f"transport {cfg.lung.transport_delay_s:.3f} s, sensor τ {cfg.lung.sensor_tau_s:.3f} s, "
            f"Smith feedback estimate {cfg.smith.delay_s:.3f} s ({'on' if cfg.smith.enabled else 'off'})\n"
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
            # The wall clock feeds an accumulator; playback advances only in
            # exact simulation steps (cfg.dt), like the real controller loop.
            # This keeps timer jitter and canvas rendering out of simulation
            # time while still allowing delayed callbacks to catch up.
            self._play_last_wall_time = time.perf_counter()
            self.timer.start()
            self.start_button.setText("Pause")
            self.user_start_button.setText("Pause")
            self.statusBar().showMessage("Simulation playback running")
        else:
            self.timer.stop()
            self.start_button.setText("Resume")
            self.user_start_button.setText("Resume")
            self.statusBar().showMessage("Paused")

    def stop_playback(self) -> None:
        self.playing = False
        self._play_last_wall_time = None
        self._play_accumulator = 0.0
        if hasattr(self, "timer"):
            self.timer.stop()
        if hasattr(self, "start_button"):
            self.start_button.setText("Start")
        if hasattr(self, "user_start_button"):
            self.user_start_button.setText("Start")

    def reset_playback(self) -> None:
        self.stop_playback()
        self.play_index = 0.0
        self._show_index(0)
        self.statusBar().showMessage("Playback position reset")

    def _advance(self) -> None:
        if self.result is None:
            return
        if self.play_index >= self.result.time_s.size - 1:
            self._show_index(self.result.time_s.size - 1)
            self.stop_playback()
            self.statusBar().showMessage("Simulation playback complete")
            return
        speed = self.inputs["play_speed"].value()
        now = time.perf_counter()
        if self._play_last_wall_time is None:
            self._play_last_wall_time = now
            return
        wall_dt = max(0.0, now - self._play_last_wall_time)
        self._play_last_wall_time = now
        self._play_accumulator += wall_dt * speed

        # Advance by fixed simulation samples.  The result is precomputed, so
        # this is the playback equivalent of the reference controller's
        # fixed-step simulation loop.
        step_count = 0
        max_steps = self.result.time_s.size
        while self._play_accumulator + 1e-12 >= self.cfg.dt and step_count < max_steps:
            if self.play_index >= self.result.time_s.size - 1:
                break
            self.play_index += 1.0
            self._play_accumulator -= self.cfg.dt
            step_count += 1
        index = min(int(self.play_index), self.result.time_s.size - 1)
        self._show_index(index)
        if index >= self.result.time_s.size - 1:
            self.stop_playback()
            self.statusBar().showMessage("Simulation playback complete")

    def _show_index(self, index: int) -> None:
        if self.result is None:
            return
        index = int(np.clip(index, 0, self.result.time_s.size - 1))
        self.user_readouts["phase"].setText(STATE_NAMES[int(self.result.state[index])])
        self.user_readouts["flow"].setText(f"{self.result.flow_slm[index]:+.2f} SLM")
        self.user_readouts["volume"].setText(f"{self.result.volume_ml[index]:.1f} mL")
        if self.mode_stack.currentIndex() == 0:
            self.user_model_canvas.draw_frame(self.result, index, self.cfg)
            if index == 0 or index >= self.result.time_s.size - 1 or abs(index - self._user_flow_last_index) >= 10:
                self.user_flow_canvas.set_result(self.result, index)
                self._user_flow_last_index = index
        else:
            self.model_canvas.draw_frame(self.result, index, self.cfg)
        if hasattr(self, "live_monitor") and self.live_monitor.isVisible():
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
