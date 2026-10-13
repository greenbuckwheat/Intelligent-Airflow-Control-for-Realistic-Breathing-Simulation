"""Dual-branch LungLogic plant and controller used by the desktop simulation.

The model is intentionally small enough to identify from bench data.  It is a
grey-box pneumatic model: each lung is a compliant volume with separate open-
valve inspiration and expiration time constants.  The same 100 Hz PID law and
phase logic are implemented in the STM32 firmware sketch.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np


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
class BranchParameters:
    """Identifiable parameters for one lung branch."""

    capacity_ml: float = 558.0
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
class ExperimentConfig:
    control_hz: float = 100.0
    respiratory_rate_bpm: float = 15.0
    ie_inspiration: float = 1.0
    ie_expiration: float = 2.0
    left_target_ml: float = 250.0
    right_target_ml: float = 250.0
    target_fill_time_s: float = 1.30
    dead_time_s: float = 0.080
    precondition_s: float = 1.50
    cycles: int = 5
    end_volume_ml: float = 5.0
    max_branch_volume_ml: float = 320.0
    max_abs_flow_slm: float = 220.0
    random_seed: int = 7
    left: BranchParameters = field(default_factory=BranchParameters)
    right: BranchParameters = field(
        default_factory=lambda: BranchParameters(tau_in_s=0.60, tau_out_s=0.54)
    )
    pi: PIDParameters = field(default_factory=PIDParameters)

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
        if not (0 < self.left_target_ml <= self.max_branch_volume_ml):
            raise ValueError("invalid left target")
        if not (0 < self.right_target_ml <= self.max_branch_volume_ml):
            raise ValueError("invalid right target")
        if self.target_fill_time_s <= 0 or self.target_fill_time_s > self.inspiration_s:
            raise ValueError("target_fill_time_s must fit inside inspiration time")
        if self.dead_time_s < 0 or self.dead_time_s > 0.5:
            raise ValueError("dead_time_s must be 0..0.5 s")
        for name, branch in (("left", self.left), ("right", self.right)):
            if branch.capacity_ml <= self.max_branch_volume_ml:
                raise ValueError(f"{name} capacity must exceed safety volume")
            if branch.tau_in_s <= 0 or branch.tau_out_s <= 0:
                raise ValueError(f"{name} time constants must be positive")
        if self.pi.kp < 0 or self.pi.ki < 0 or self.pi.kd < 0:
            raise ValueError("PID gains must be non-negative")
        if self.pi.integral_limit <= 0:
            raise ValueError("integral_limit must be positive")


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


def load_config(path: str | Path | None = None) -> ExperimentConfig:
    cfg = ExperimentConfig()
    if path is not None:
        with Path(path).open("r", encoding="utf-8") as handle:
            _merge_dataclass(cfg, json.load(handle))
    cfg.validate()
    return cfg


def save_config(cfg: ExperimentConfig, path: str | Path) -> None:
    with Path(path).open("w", encoding="utf-8") as handle:
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
        # Software transition only; physical PPV movement is not modeled.
        self.bias -= float(np.clip(self.bias, -50.0 * dt, 50.0 * dt))
        raw = self.bias + ff + self.p.kp * error + self.p.ki * candidate + self.p.kd * derivative
        output = float(np.clip(raw, 0.0, 1.0))
        # Conditional integration: do not wind further into saturation.
        if (0.0 < raw < 1.0) or (raw <= 0.0 and error > 0.0) or (raw >= 1.0 and error < 0.0):
            self.integral = candidate
        return output


class LungBranchPlant:
    """First-order volume model with transport delay and flow sensor dynamics."""

    def __init__(self, p: BranchParameters, rng: np.random.Generator) -> None:
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

        self.volume_ml = float(np.clip(
            self.volume_ml + dvdt * dt,
            0.0,
            self.p.capacity_ml,
        ))
        true_flow = 0.06 * dvdt

        delay_samples = max(0, int(round(self.p.transport_delay_s / dt)))
        self._delay_line.append(true_flow)
        delayed = self._delay_line.pop(0) if len(self._delay_line) > delay_samples else 0.0
        alpha = 1.0 - math.exp(-dt / max(self.p.sensor_tau_s, 1e-5))
        self.sensor_flow_slm += alpha * (delayed - self.sensor_flow_slm)
        measured = self.p.flow_sign * self.sensor_flow_slm
        measured += self.rng.normal(0.0, self.p.sensor_noise_slm)
        return true_flow, float(measured)


@dataclass
class SimulationResult:
    time_s: np.ndarray
    state: np.ndarray
    cycle: np.ndarray
    q_left_slm: np.ndarray
    q_right_slm: np.ndarray
    qref_left_slm: np.ndarray
    qref_right_slm: np.ndarray
    v_left_ml: np.ndarray
    v_right_ml: np.ndarray
    opening_left: np.ndarray
    opening_right: np.ndarray
    command_left_v: np.ndarray
    command_right_v: np.ndarray
    valve_mask: np.ndarray


def _phase_references(cfg: ExperimentConfig, state: int) -> Tuple[float, float]:
    if state == STATE_INSPIRATION:
        return (
            0.06 * cfg.left_target_ml / cfg.target_fill_time_s,
            0.06 * cfg.right_target_ml / cfg.target_fill_time_s,
        )
    if state in (STATE_PRECONDITION, STATE_EXPIRATION):
        usable_exp = max(0.25, cfg.expiration_s - cfg.dead_time_s)
        return (
            0.06 * cfg.left_target_ml / usable_exp,
            0.06 * cfg.right_target_ml / usable_exp,
        )
    return 0.0, 0.0


def run_simulation(cfg: ExperimentConfig) -> SimulationResult:
    """Run a deterministic-time dual-branch experiment."""

    cfg.validate()
    rng = np.random.default_rng(cfg.random_seed)
    branches = [LungBranchPlant(cfg.left, rng), LungBranchPlant(cfg.right, rng)]
    controllers = [PIDController(cfg.pi), PIDController(cfg.pi)]
    targets = [cfg.left_target_ml, cfg.right_target_ml]
    dt = cfg.dt
    max_duration = (
        cfg.precondition_s
        + cfg.cycles * (cfg.inspiration_s + cfg.expiration_s + 2.0 * cfg.dead_time_s)
        + 1.0
    )
    n = int(math.ceil(max_duration / dt)) + 1
    log: Dict[str, List[float]] = {key: [] for key in (
        "time", "state", "cycle", "ql", "qr", "qrl", "qrr", "vl", "vr",
        "ol", "orr", "dl", "dr", "mask",
    )}

    state = STATE_PRECONDITION
    state_start = 0.0
    cycle = 0
    measured = [0.0, 0.0]
    openings = [0.0, 0.0]
    for step in range(n):
        now = step * dt
        elapsed = now - state_start

        if state == STATE_PRECONDITION and elapsed >= cfg.precondition_s:
            state, state_start = STATE_DEAD_TO_INSP, now
            for controller, opening in zip(controllers, openings):
                controller.reset(opening)
        elif state == STATE_DEAD_TO_INSP and elapsed >= cfg.dead_time_s:
            state, state_start = STATE_INSPIRATION, now
            for controller, opening in zip(controllers, openings):
                controller.reset(opening)
        elif state == STATE_INSPIRATION and elapsed >= cfg.inspiration_s:
            state, state_start = STATE_DEAD_TO_EXP, now
            for controller, opening in zip(controllers, openings):
                controller.reset(opening)
        elif state == STATE_DEAD_TO_EXP and elapsed >= cfg.dead_time_s:
            state, state_start = STATE_EXPIRATION, now
            for controller, opening in zip(controllers, openings):
                controller.reset(opening)
        elif state == STATE_EXPIRATION and elapsed >= cfg.expiration_s:
            cycle += 1
            if cycle >= cfg.cycles:
                state = STATE_COMPLETE
            else:
                state = STATE_DEAD_TO_INSP
            state_start = now
            for controller, opening in zip(controllers, openings):
                controller.reset(opening)

        qrefs = list(_phase_references(cfg, state))
        if state == STATE_COMPLETE:
            openings = [0.0, 0.0]
        directions = [0, 0]
        for idx, (branch, controller) in enumerate(zip(branches, controllers)):
            if state == STATE_INSPIRATION and branch.volume_ml < targets[idx]:
                directions[idx] = 1
                qmax = max(branch.full_open_flow_slm(1), 0.1)
                ff = min(1.0, (qrefs[idx] / qmax) ** (1.0 / branch.p.valve_exponent))
                openings[idx] = controller.update(qrefs[idx], max(0.0, measured[idx]), dt, ff)
            elif state in (STATE_PRECONDITION, STATE_EXPIRATION) and branch.volume_ml > cfg.end_volume_ml:
                directions[idx] = -1
                qmax = max(branch.full_open_flow_slm(-1), 0.1)
                ff = min(1.0, (qrefs[idx] / qmax) ** (1.0 / branch.p.valve_exponent))
                openings[idx] = controller.update(qrefs[idx], max(0.0, -measured[idx]), dt, ff)
            elif state == STATE_COMPLETE:
                controller.reset()

        true_and_measured = [
            branch.step(opening, direction, dt)
            for branch, opening, direction in zip(branches, openings, directions)
        ]
        measured = [pair[1] for pair in true_and_measured]

        # Bit assignment: 0 L-atmosphere, 1 L-vacuum, 2 R-atmosphere, 3 R-vacuum.
        mask = 0
        if directions[0] > 0:
            mask |= 1
        elif directions[0] < 0:
            mask |= 2
        if directions[1] > 0:
            mask |= 4
        elif directions[1] < 0:
            mask |= 8

        log["time"].append(now)
        log["state"].append(state)
        log["cycle"].append(cycle)
        log["ql"].append(measured[0])
        log["qr"].append(measured[1])
        log["qrl"].append(qrefs[0] if directions[0] else 0.0)
        log["qrr"].append(qrefs[1] if directions[1] else 0.0)
        log["vl"].append(branches[0].volume_ml)
        log["vr"].append(branches[1].volume_ml)
        log["ol"].append(openings[0])
        log["orr"].append(openings[1])
        log["dl"].append(5.0 * (1.0 - openings[0]))
        log["dr"].append(5.0 * (1.0 - openings[1]))
        log["mask"].append(mask)
        if state in (STATE_COMPLETE, STATE_FAULT):
            break

    return SimulationResult(
        time_s=np.asarray(log["time"]),
        state=np.asarray(log["state"], dtype=int),
        cycle=np.asarray(log["cycle"], dtype=int),
        q_left_slm=np.asarray(log["ql"]),
        q_right_slm=np.asarray(log["qr"]),
        qref_left_slm=np.asarray(log["qrl"]),
        qref_right_slm=np.asarray(log["qrr"]),
        v_left_ml=np.asarray(log["vl"]),
        v_right_ml=np.asarray(log["vr"]),
        opening_left=np.asarray(log["ol"]),
        opening_right=np.asarray(log["orr"]),
        command_left_v=np.asarray(log["dl"]),
        command_right_v=np.asarray(log["dr"]),
        valve_mask=np.asarray(log["mask"], dtype=int),
    )
