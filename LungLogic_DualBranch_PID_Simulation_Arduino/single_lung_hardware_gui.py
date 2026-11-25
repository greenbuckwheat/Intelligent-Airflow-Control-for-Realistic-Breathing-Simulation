"""PyQt5 hardware control panel for the NUCLEO-G474RE single-lung controller.

This host program is the hardware counterpart of ``single_lung_pid_gui.py``.
It provides user/developer views and optional bounded run-to-run PI tuning. It
does not simulate the lung response: it sends parameters to the STM32,
receives real telemetry, and logs measured data.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import os
import queue
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from PyQt5 import QtCore, QtGui, QtWidgets

try:
    import serial
    from serial.tools import list_ports
except ModuleNotFoundError:
    serial = None
    list_ports = None

from single_lung_pid_gui import (
    _merge_dataclass,
    CONFIG_PATH,
    STATE_EXPIRATION,
    STATE_DEAD_TO_INSP,
    STATE_IDLE,
    STATE_INSPIRATION,
    STATE_NAMES,
    LungModelCanvas,
    SingleLungConfig,
    load_config,
)


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "single_lung_hardware_reference.json"
BAUD = 115200
HARDWARE_CONTROL_HZ = 100.0
HARDWARE_TELEMETRY_HZ = 50.0
PROTOCOL_VERSION = 21
READY = "READY,LUNGLOGIC_SINGLE_G474,21"
# Firmware ATM map: fitted near-closure law down to this depth below closure.
ATM_JOIN_DEPTH_V = 0.10
# Upper bound of "unbounded" spin boxes (Qt needs a finite range).
UNBOUNDED = 1e9
# Firmware Smith delay line holds 1 s of 100 Hz samples.
SMITH_DELAY_MAX_S = 1.0
OBJECTIVE = {
    "version": "signed_flow_tidal_mse_v4_atm_cut_zero_ref",
    "formula": "J = 0.65 * J_Q + 0.35 * J_V",
    "weight_flow": 0.65, "weight_volume": 0.35,
    "flow_term": "0.5 * (mean(((Q-Qref_in)/max(abs(Qref_in)))^2) + mean(((Q-Qref_out)/max(abs(Qref_out)))^2))",
    "volume_term": "mean(((V_inspired_ml-V_target_ml)/V_target_ml)^2)",
    "atm_cut_reference": ("v4 (2026-10-22): once the ATM is cut during inspiration (valve mask "
                          "1 -> 0 within the phase: per-cycle volume target reached), the "
                          "remaining inspiration samples are scored against 0 SLM instead of "
                          "the still-transmitted inspiration reference; normalization unchanged"),
    "retention": "Complete cycles only; inspiration/expiration states; no additional transient trimming",
    "volume_method": "Trapezoidal integral of positive calibrated net flow over intervals starting in inspiration; telemetry-resolution estimate, not clipped firmware volume",
    "reference": "Signed per-sample MCU reference; v14 expiration reference is constant. Zero expiration reference is accepted only for legacy v11-v13 PPV 5 V exhausted-hold samples. Phase errors normalized by maximum absolute phase reference.",
    "paper": "N. J. Killingsworth and M. Krstic, PID Tuning Using Extremum Seeking: Online, Model-Free Performance Optimization, IEEE Control Systems Magazine, February 2006, pp. 70-79; discrete ES equations (5)-(7) (equations (4)-(6) of the ACC 2005 version)",
    "phase_costs": ("J_ins = w_flow*0.5*J_Q_in + w_vol*J_V, J_out = w_flow*0.5*J_Q_out, "
                    "J_ins + J_out = score (weights from the metrics); a split for Auto PID, "
                    "score itself unchanged"),
    "algorithm": ("Default: discrete extremum seeking, Killingsworth & Krstic 2006, equations (5)-(7), "
                  "one gain pair per round: inspiration pair minimises J_ins, expiration pair "
                  "minimises J_out (each pair only affects its own phase, so the expected optimum "
                  "equals that of the full J without the other phase's noise). One iteration = one "
                  "hardware run (warm-up cycle excluded as t0). The tuned pair is a quadrature pair "
                  "at omega = a*pi (Kp cos, Ki sin), one filter state; a=0.8, h=0.5 as in the paper. "
                  "Each round starts with N_base identical baseline runs (default 3): their mean "
                  "is J_base and their relative spread (|a-b|/mean for 2, (max-min)/mean for 3+) "
                  "is the noise floor of J_ins, J_out and J. Final result = theta_hat(N), accepted only "
                  "if a confirmation run improves the round's phase cost by > 3x its noise floor and "
                  "the full J is not worse than the baseline mean by more than its noise floor. "
                  "Differences from the paper: parameters scaled to their initial value "
                  "(alpha_i = alpha_rel*theta0_i, gamma_i = gamma_rel, i.e. the update runs on "
                  "theta/theta0); theta and theta_hat bounded to [0.25, 4] x theta0; the baseline "
                  "mean initialises the filter (zeta(-1) = J_base/(1+h), J(-1) = J_base); J clipped to "
                  "3*J_base for every run and abnormal runs set to 3*J_base; optional "
                  "demodulation phase from the paper's Fig. 1, a deviation "
                  "from equation (6) (off by default; 44 deg at 0.8*pi converges without it). "
                  "Alternatives: a sensitivity pre-experiment (each gain x0.6 / x1.4, suggests "
                  "alpha_rel and gamma_rel, never changes gains) and a bounded run-to-run "
                  "coordinate search (not the paper's equations)."),
}
STATE_RECOVERY = 8
# The simulation module does not have the hardware-only post-run recovery state.
STATE_NAMES = dict(STATE_NAMES)
STATE_NAMES[STATE_RECOVERY] = "RECOVERY"
matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False


@dataclass
class HardwareTelemetry:
    run_id: int
    time_ms: int
    state: int
    cycle: int
    flow_slm: float
    volume_ml: float
    flow_reference_slm: float
    opening: float
    command_v: float
    valve_mask: int
    estimated_ppv_v: float = math.nan
    ppv_remaining_s: float = math.nan
    raw_flow_slm: float = math.nan
    net_flow_slm: float = math.nan
    sampled_valve_mask: int = -1
    pid_feedback_slm: float = math.nan
    smith_ideal_flow_slm: float = math.nan
    smith_actuator_flow_slm: float = math.nan
    cycle_inspired_ml: float = math.nan
    cycle_expired_ml: float = math.nan
    expiration_budget_ml: float = math.nan
    expiration_exhausted: bool = False

    @property
    def control_flow_slm(self) -> float:
        return self.net_flow_slm if math.isfinite(self.net_flow_slm) else self.flow_slm

    @classmethod
    def parse(cls, line: str) -> "HardwareTelemetry":
        fields = line.strip().split(",")
        if len(fields) not in (11, 13, 16, 19, 23) or fields[0] != "TEL":
            raise ValueError("not single-lung telemetry")
        return cls(
            int(fields[1]),
            int(fields[2]),
            int(fields[3]),
            int(fields[4]),
            float(fields[5]),
            float(fields[6]),
            float(fields[7]),
            float(fields[8]),
            float(fields[9]),
            int(fields[10]),
            float(fields[11]) if len(fields) >= 13 else math.nan,
            float(fields[12]) if len(fields) >= 13 else math.nan,
            float(fields[13]) if len(fields) >= 16 else math.nan,
            float(fields[14]) if len(fields) >= 16 else math.nan,
            int(fields[15]) if len(fields) >= 16 else -1,
            float(fields[16]) if len(fields) >= 19 else math.nan,
            float(fields[17]) if len(fields) >= 19 else math.nan,
            float(fields[18]) if len(fields) >= 19 else math.nan,
            float(fields[19]) if len(fields) == 23 else math.nan,
            float(fields[20]) if len(fields) == 23 else math.nan,
            float(fields[21]) if len(fields) == 23 else math.nan,
            bool(int(fields[22])) if len(fields) == 23 else False,
        )


STATE_DEAD_TO_EXP = 4
# Firmware v8 expiration opening cap (ppv_motion.h ppvExpirationOpeningLimit):
# 0.1883 for 250 ms, linear to 0.3451 at 750 ms, linear to 0.4707 at phase end.
EXPIRATION_OPENING_CAP = (0.1883, 0.3451, 0.4707)
EXPIRATION_CAP_BREAKPOINTS_MS = (250.0, 750.0)
# A sample is "pinned" when its opening is within this of the cap.
PINNED_OPENING_TOLERANCE = 0.005
# ... and the net flow magnitude is below this fraction of the reference.
PINNED_FLOW_FRACTION = 0.5
# Expiration-onset surge: firmware volume drop in the first 0.2 s after VAC opens.
TRANSITION_DROP_LIMIT_ML = 10.0
# Proposed residual term; diagnostic only, score and Auto PID objective unchanged.
RESIDUAL_SCORE_WEIGHT = 0.35
# Expiration onset: first expiration sample with net flow at or below this.
EXPIRATION_ONSET_SLM = -1.0
# "Lung empty": firmware volume estimate below this (biased early, see _phase_diagnostics).
LUNG_EMPTY_ML = 8.0
# Inspiration sample counts as capped at command <= hardware_inspiration_min_v + margin.
INSPIRATION_CAP_MARGIN_V = 0.02


def expiration_opening_cap(elapsed_ms: float, duration_ms: float) -> float:
    """Python copy of the firmware v8 cap (model-opening coordinate)."""
    initial, mid, final = EXPIRATION_OPENING_CAP
    hold, ramp = EXPIRATION_CAP_BREAKPOINTS_MS
    if elapsed_ms <= hold:
        return initial
    if elapsed_ms <= ramp:
        return initial + (mid - initial) * (elapsed_ms - hold) / (ramp - hold)
    late = (elapsed_ms - ramp) / max(1.0, max(250.0, duration_ms) - ramp)
    return mid + min(1.0, max(0.0, late)) * (final - mid)


def _trapezoid_ml(rows: List[HardwareTelemetry], indices: List[int], attr: str, sign: int) -> float:
    """Integral of the positive part of sign*attr over intervals starting at indices."""
    total = 0.0
    for i in indices:
        if i + 1 >= len(rows):
            continue
        a, b = rows[i], rows[i + 1]
        total += ((max(0.0, sign * getattr(a, attr)) + max(0.0, sign * getattr(b, attr)))
                  / 2 * (b.time_ms - a.time_ms) / 60.0)
    return total


def _run_diagnostics(rows: List[HardwareTelemetry], cfg: SingleLungConfig,
                     complete: List[int], tidal_values: List[float],
                     score: float) -> Dict[str, float]:
    """Diagnostics appended after the existing metrics; never affect ``score``.

    Only states 3/4/5 (inspiration, transition, expiration) are used, so the
    cycle-0 PRECONDITION (VAC open, state 1) never counts as a VAC opening or
    in the volume balance.
    """
    nan = math.nan
    target = cfg.target_volume_ml
    out: Dict[str, float] = {}
    per_cycle: Dict[str, List[float]] = {}

    def put(name: str, cycle: int, value: float) -> None:
        out[f"cycle_{cycle}_{name}"] = value
        per_cycle.setdefault(name, []).append(value)

    pinned = exp_samples = 0
    leak_comp_samples: List[float] = []
    mb_cum = {"raw": 0.0, "net": 0.0}
    mb: Dict[str, float] = {}
    net_diag: Dict[str, float] = {}
    for cycle, tidal in zip(complete, tidal_values):
        idx = [i for i, r in enumerate(rows) if r.cycle == cycle]
        active = [i for i in idx if rows[i].state in (STATE_INSPIRATION, STATE_DEAD_TO_EXP, STATE_EXPIRATION)]
        insp = [i for i in idx if rows[i].state == STATE_INSPIRATION]
        exp = [i for i in idx if rows[i].state == STATE_EXPIRATION]
        after_exp = exp[-1] + 1 if exp[-1] + 1 < len(rows) else None

        # Expiration emptying from the firmware volume estimate (uncompensated
        # expired flow, clipped at 0 by the MCU).
        v_start, v_end = rows[exp[0]].volume_ml, rows[exp[-1]].volume_ml
        put("expiration_residual_ml", cycle, v_end)
        put("expiration_emptied_fraction", cycle,
            (v_start - v_end) / v_start if math.isfinite(v_start) and v_start > 0 else nan)

        # Expiration opening pinned at the v8 cap.
        t_exp = rows[exp[0]].time_ms
        duration = (rows[after_exp].time_ms if after_exp is not None
                    else rows[exp[-1]].time_ms + 1000.0 / HARDWARE_TELEMETRY_HZ) - t_exp
        for i in exp:
            r = rows[i]
            exp_samples += 1
            cap = expiration_opening_cap(r.time_ms - t_exp, duration)
            if (r.opening >= cap - PINNED_OPENING_TOLERANCE
                    and abs(r.net_flow_slm) < PINNED_FLOW_FRACTION * abs(r.flow_reference_slm)):
                pinned += 1
            if math.isfinite(r.net_flow_slm) and math.isfinite(r.flow_slm):
                leak_comp_samples.append(r.net_flow_slm - r.flow_slm)

        # Transition / expiration-onset surge.  VAC opening = first sample in
        # states 3/4/5 whose valve mask becomes 2.
        vac = next((i for i in active if rows[i].valve_mask == 2
                    and i > 0 and rows[i - 1].valve_mask != 2), None)
        if vac is None:
            for name in ("vac_drop_0p2s_ml", "vac_peak_0p2s_slm", "vac_peak_0p5s_slm"):
                put(name, cycle, nan)
            put("transition_ok", cycle, nan)
        else:
            t0 = rows[vac].time_ms
            w02 = [r for r in rows[vac:] if r.time_ms <= t0 + 200]
            w05 = [r for r in rows[vac:] if r.time_ms <= t0 + 500]
            drop = rows[vac].volume_ml - w02[-1].volume_ml
            put("vac_drop_0p2s_ml", cycle, drop)
            put("vac_peak_0p2s_slm", cycle, max(abs(r.net_flow_slm) for r in w02))
            put("vac_peak_0p5s_slm", cycle, max(abs(r.net_flow_slm) for r in w05))
            put("transition_ok", cycle, float(drop <= TRANSITION_DROP_LIMIT_ML) if math.isfinite(drop) else nan)
        t_last = rows[insp[-1]].time_ms
        late = [rows[i].net_flow_slm for i in insp if rows[i].time_ms >= t_last - 300]
        put("late_insp_mean_slm", cycle, sum(late) / len(late))

        # Cycle time: DEAD_TO_INSP entry (cycle 0: INSPIRATION entry, its
        # positioning is the initial stroke) to the first sample after expiration.
        positioning = [i for i in idx if rows[i].state == STATE_DEAD_TO_INSP]
        start = rows[positioning[0]].time_ms if cycle > 0 and positioning else rows[insp[0]].time_ms
        put("cycle_time_s", cycle,
            (rows[after_exp].time_ms - start) / 1000.0 if after_exp is not None else nan)
        put("tidal_error_pct", cycle, 100.0 * (tidal - target) / target)

        # Volume balance before (flow_slm) and after (net_flow_slm) the VAC
        # leak compensation; outflow is signed negative.
        for key, attr in (("raw", "flow_slm"), ("net", "net_flow_slm")):
            inflow = _trapezoid_ml(rows, active, attr, 1)
            outflow = -_trapezoid_ml(rows, active, attr, -1)
            mb_cum[key] += inflow + outflow
            mb[f"mb_{key}_in_ml_c{cycle}"] = inflow
            mb[f"mb_{key}_out_ml_c{cycle}"] = outflow
            mb[f"mb_{key}_net_ml_c{cycle}"] = inflow + outflow
            mb[f"mb_{key}_cum_ml_c{cycle}"] = mb_cum[key]
            # Phase-only view (inspiration inflow, expiration outflow); the
            # whole-cycle values above also contain the 3-5 mL of negative
            # flow seen during inspiration.
            mb[f"mb_{key}_insp_in_ml_c{cycle}"] = _trapezoid_ml(rows, insp, attr, 1)
            mb[f"mb_{key}_exp_out_ml_c{cycle}"] = -_trapezoid_ml(rows, exp, attr, -1)
        mb[f"leak_comp_ml_c{cycle}"] = mb[f"mb_net_out_ml_c{cycle}"] - mb[f"mb_raw_out_ml_c{cycle}"]
        per_cycle.setdefault("leak_comp", []).append(mb[f"leak_comp_ml_c{cycle}"])

        # Emptying from the leak-compensated phase ledger.  The firmware volume
        # above integrates uncompensated flow and is clipped at 0, so with the
        # VAC leak it reports full emptying even when the bag is not empty.
        # Signed: negative residual / fraction > 1 means more out than in.
        net_in = mb[f"mb_net_insp_in_ml_c{cycle}"]
        net_out = -mb[f"mb_net_exp_out_ml_c{cycle}"]
        net_diag[f"cycle_{cycle}_net_expiration_residual_ml"] = net_in - net_out
        net_diag[f"cycle_{cycle}_net_expiration_emptied_fraction"] = (
            net_out / net_in if net_in > 0 else nan)
        per_cycle.setdefault("net_expiration_residual_ml", []).append(net_in - net_out)
        per_cycle.setdefault("net_expiration_emptied_fraction", []).append(
            net_out / net_in if net_in > 0 else nan)

    def mean(name: str) -> float:
        values = [v for v in per_cycle.get(name, []) if math.isfinite(v)]
        return sum(values) / len(values) if values else nan

    def extreme(name: str, fn) -> float:
        values = [v for v in per_cycle.get(name, []) if math.isfinite(v)]
        return fn(values) if values else nan

    residuals = per_cycle["expiration_residual_ml"]
    j_r = (sum((v / target) ** 2 for v in residuals) / len(residuals)
           if all(math.isfinite(v) for v in residuals) else nan)
    mean_cycle = mean("cycle_time_s")
    summary = {
        "J_R": j_r,
        "score_with_residual": score + RESIDUAL_SCORE_WEIGHT * j_r,
        "weight_residual": RESIDUAL_SCORE_WEIGHT,
        "mean_expiration_residual_ml": mean("expiration_residual_ml"),
        "mean_expiration_emptied_fraction": mean("expiration_emptied_fraction"),
        "min_expiration_emptied_fraction": extreme("expiration_emptied_fraction", min),
        "pinned_fraction_exp": pinned / exp_samples if exp_samples else nan,
        "max_vac_drop_0p2s_ml": extreme("vac_drop_0p2s_ml", max),
        "max_vac_peak_0p2s_slm": extreme("vac_peak_0p2s_slm", max),
        "max_vac_peak_0p5s_slm": extreme("vac_peak_0p5s_slm", max),
        "transition_fail_cycles": sum(v == 0.0 for v in per_cycle["transition_ok"]),
        "mean_late_insp_slm": mean("late_insp_mean_slm"),
        "mean_cycle_time_s": mean_cycle,
        "effective_bpm": 60.0 / mean_cycle if math.isfinite(mean_cycle) and mean_cycle > 0 else nan,
        "mb_raw_net_mean_ml": sum(mb[f"mb_raw_net_ml_c{c}"] for c in complete) / len(complete),
        "mb_net_net_mean_ml": sum(mb[f"mb_net_net_ml_c{c}"] for c in complete) / len(complete),
        "leak_comp_ml_per_cycle": mean("leak_comp"),
        "leak_comp_mean_slm": (sum(leak_comp_samples) / len(leak_comp_samples)
                               if leak_comp_samples else nan),
    }
    net_summary = {
        "mean_net_expiration_residual_ml": mean("net_expiration_residual_ml"),
        "mean_net_expiration_emptied_fraction": mean("net_expiration_emptied_fraction"),
        "min_net_expiration_emptied_fraction": extreme("net_expiration_emptied_fraction", min),
    }
    return {**summary, **out, **mb, **net_summary, **net_diag}


def _phase_diagnostics(rows: List[HardwareTelemetry], cfg: SingleLungConfig,
                       complete: List[int], scales: List[float], counts: List[int],
                       terms: List[float]) -> Dict[str, float]:
    """Where J_Q_in / J_Q_out come from; appended last, never affect ``score``.

    Same per-sample error and normalisation as the score.  Switch samples are
    the leading inspiration samples whose net flow is still negative (the
    meter still shows expiration flow for ~60 ms after the ATM opens).  The
    expiration clock starts at the cycle's first EXPIRATION sample (VAC open).
    lung_empty_time_s uses the firmware volume estimate, which integrates
    UNcompensated flow and therefore reaches 0 early.
    """
    nan = math.nan
    (scale_in, scale_out), (n_in, n_out), (j_q_in, _) = scales, counts, terms
    cap_v = max(0.0, cfg.hardware_inspiration_min_v) + INSPIRATION_CAP_MARGIN_V
    out: Dict[str, float] = {}
    switch_e2 = out_e2 = out_e2_0p5 = out_e2_1p0 = 0.0
    switch_samples = cap_samples = insp_samples = cap_cycles = 0
    onset_v: List[float] = []
    onset_t: List[float] = []
    empty_t: List[float] = []
    for cycle in complete:
        ins = [r for r in rows if r.cycle == cycle and r.state == STATE_INSPIRATION]
        exp = [r for r in rows if r.cycle == cycle and r.state == STATE_EXPIRATION]
        lead = 0
        while lead < len(ins) and ins[lead].net_flow_slm < 0:
            switch_e2 += ((ins[lead].net_flow_slm - ins[lead].flow_reference_slm) / scale_in) ** 2
            lead += 1
        switch_samples += lead
        out[f"cycle_{cycle}_switch_samples"] = lead
        capped = sum(r.command_v <= cap_v for r in ins)
        cap_samples += capped
        insp_samples += len(ins)
        cap_cycles += capped > 0
        t0 = exp[0].time_ms
        onset = empty = None
        for r in exp:
            e2 = ((r.net_flow_slm - r.flow_reference_slm) / scale_out) ** 2
            out_e2 += e2
            if r.time_ms - t0 < 500:
                out_e2_0p5 += e2
            if r.time_ms - t0 < 1000:
                out_e2_1p0 += e2
            if onset is None and r.net_flow_slm <= EXPIRATION_ONSET_SLM:
                onset = r
            if empty is None and r.volume_ml < LUNG_EMPTY_ML:
                empty = r
        out[f"cycle_{cycle}_onset_cmd_v"] = onset.command_v if onset else nan
        out[f"cycle_{cycle}_onset_time_s"] = (onset.time_ms - t0) / 1000.0 if onset else nan
        out[f"cycle_{cycle}_lung_empty_time_s"] = (empty.time_ms - t0) / 1000.0 if empty else nan
        if onset:
            onset_v.append(onset.command_v)
            onset_t.append((onset.time_ms - t0) / 1000.0)
        if empty:
            empty_t.append((empty.time_ms - t0) / 1000.0)

    def mean(values: List[float]) -> float:
        return sum(values) / len(values) if values else nan

    j_switch = switch_e2 / n_in
    summary = {
        "J_Q_in_switch": j_switch,
        "J_Q_in_rest": j_q_in - j_switch,
        "switch_samples": switch_samples,
        "J_Q_out_first0p5s_share": out_e2_0p5 / out_e2 if out_e2 > 0 else nan,
        "J_Q_out_first1s_share": out_e2_1p0 / out_e2 if out_e2 > 0 else nan,
        "onset_cmd_v_mean": mean(onset_v),
        "onset_cmd_v_min": min(onset_v) if onset_v else nan,
        "onset_cmd_v_max": max(onset_v) if onset_v else nan,
        "onset_time_s": mean(onset_t),
        "onset_cycles": len(onset_v),
        "lung_empty_time_s": mean(empty_t),
        # Not saturation_fraction (opening >= 0.999): inspiration samples at
        # the configured lowest voltage.  The cycle share is a separate,
        # diagnostic-only view (15 bpm: the cap binds only late in each cycle).
        "inspiration_cap_fraction": cap_samples / insp_samples if insp_samples else nan,
        "inspiration_cap_cycle_fraction": cap_cycles / len(complete),
    }
    return {**summary, **out}


def atm_cut_rows(rows: List[HardwareTelemetry], cycles: List[int]) -> set:
    """ids of inspiration rows after the ATM was cut within the phase.

    The firmware cuts the ATM when the per-cycle inspired volume reaches the
    target but keeps transmitting the inspiration reference; scoring those
    samples against it penalised reaching the volume early (2026-10-22).
    """
    wanted = set(cycles)
    cut: set = set()
    opened = closed = False
    previous = None
    for r in rows:
        if r.state != STATE_INSPIRATION or r.cycle not in wanted:
            opened = closed = False
            previous = r
            continue
        if previous is None or previous.state != STATE_INSPIRATION or previous.cycle != r.cycle:
            opened = closed = False
        if r.valve_mask & 1:
            opened = True
        elif opened:
            closed = True
        if closed:
            cut.add(id(r))
        previous = r
    return cut


def hardware_run_metrics(
    rows: List[HardwareTelemetry], cfg: SingleLungConfig, *, run_completed: bool = False,
    warmup_cycles: int = 0,
) -> Dict[str, float]:
    """Evaluate the selected dimensionless objective; reject unusable data.

    A cycle needs both phases and evidence that expiration ended (cycle
    increment or DONE). Partial runs may report previously completed cycles.
    Volume is integrated from measured net flow, since MCU volume is clipped.
    The first ``warmup_cycles`` cycles (Auto PID warm-up) are not scored.
    Diagnostics (see ``_run_diagnostics``) are appended after the existing keys.
    """
    if not math.isfinite(cfg.target_volume_ml) or cfg.target_volume_ml <= 0:
        raise ValueError("target volume must be finite and positive")
    if not rows or len({r.run_id for r in rows}) != 1:
        raise ValueError("evaluation requires telemetry from one run")
    if any(b.time_ms <= a.time_ms or b.cycle < a.cycle for a, b in zip(rows, rows[1:])):
        raise ValueError("telemetry must have increasing timestamps and cycle numbers")
    complete = []
    tidal_values = []
    expiration_volume_errors = []
    for cycle in sorted({r.cycle for r in rows if r.state == STATE_INSPIRATION}):
        if cycle < warmup_cycles:
            continue
        indices = [i for i, r in enumerate(rows) if r.cycle == cycle]
        insp = [i for i in indices if rows[i].state == STATE_INSPIRATION]
        exp = [i for i in indices if rows[i].state == STATE_EXPIRATION]
        ended = exp and (any(r.cycle > cycle for r in rows[exp[-1] + 1:])
                         or (run_completed and cycle == cfg.cycles - 1))
        if not ended or insp[-1] >= exp[0]:
            continue
        # An observed start is required to avoid silently scoring a log that
        # started partway through inspiration.  DEAD_TO_INSP can last a single
        # 10 ms control tick and fall between two 20 ms telemetry samples; the
        # previous cycle's expiration sample one telemetry period earlier
        # equally proves the start was observed.
        if insp[0] == 0:
            raise ValueError("missing inspiration start boundary")
        before = rows[insp[0] - 1]
        if not (before.state == STATE_DEAD_TO_INSP
                or (before.state == STATE_EXPIRATION and before.cycle == cycle - 1
                    and rows[insp[0]].time_ms - before.time_ms
                    <= 1.5 * 1000.0 / HARDWARE_TELEMETRY_HZ)):
            raise ValueError("missing inspiration start boundary")
        volume = 0.0
        # rows[i + 1] exists because the check above guarantees an expiration
        # row after the last inspiration row; keep an explicit error anyway.
        for i in insp:
            if i + 1 >= len(rows):
                raise ValueError("inspiration must be followed by an expiration row")
            a, b = rows[i], rows[i + 1]
            if not all(math.isfinite(r.net_flow_slm) for r in (a, b)):
                raise ValueError("calibrated net flow is required for volume scoring")
            volume += (max(0.0, a.net_flow_slm) + max(0.0, b.net_flow_slm)) / 2 * (b.time_ms - a.time_ms) / 60.0
        complete.append(cycle)
        tidal_values.append(volume)
        expired = sum((max(0.0, -rows[i].net_flow_slm) + max(0.0, -rows[i+1].net_flow_slm))
                      / 2 * (rows[i+1].time_ms - rows[i].time_ms) / 60.0
                      for i in exp if i+1 < len(rows))
        budget = rows[exp[0]].expiration_budget_ml
        expected = budget if math.isfinite(budget) else cfg.target_volume_ml
        expiration_volume_errors.append(((expired-expected)/cfg.target_volume_ml)**2)
    if not complete:
        raise ValueError("run contains no complete evaluable cycles")
    active = [r for r in rows if r.cycle in complete and r.state in (STATE_INSPIRATION, STATE_EXPIRATION)]
    after_cut = atm_cut_rows(rows, complete)

    def scored_ref(r: HardwareTelemetry) -> float:
        return 0.0 if id(r) in after_cut else r.flow_reference_slm

    terms = []
    counts = []
    scales = []
    for state, sign in ((STATE_INSPIRATION, 1), (STATE_EXPIRATION, -1)):
        phase = [r for r in active if r.state == state]
        if any(not math.isfinite(r.net_flow_slm) or not math.isfinite(r.flow_reference_slm)
               or sign * r.flow_reference_slm < 0
               or (r.flow_reference_slm == 0 and not (
                   state == STATE_EXPIRATION and r.expiration_exhausted
                   and r.command_v >= 4.999 and r.opening == 0)) for r in phase):
            raise ValueError("finite calibrated net flow and nonzero signed references required")
        scale = max(abs(r.flow_reference_slm) for r in phase)
        if scale <= 0:
            raise ValueError("phase requires a positive reference normalization scale")
        terms.append(sum(((r.net_flow_slm - scored_ref(r)) / scale) ** 2 for r in phase) / len(phase))
        counts.append(len(phase))
        scales.append(scale)
    jq = sum(terms) / 2
    jv = sum(((v - cfg.target_volume_ml) / cfg.target_volume_ml) ** 2 for v in tidal_values) / len(tidal_values)
    errors = [r.net_flow_slm - scored_ref(r) for r in active]
    volume_error = sum(abs(v - cfg.target_volume_ml) / cfg.target_volume_ml for v in tidal_values) / len(tidal_values)
    score = 0.65 * jq + 0.35 * jv
    if not math.isfinite(score):
        raise ValueError("nonfinite objective")
    return {
        "score": score,
        "J_Q": jq, "J_V": jv,
        "J_V_out": sum(expiration_volume_errors)/len(expiration_volume_errors),
        "J_Q_in": terms[0], "J_Q_out": terms[1],
        "N_in": counts[0], "N_out": counts[1], "N_c": len(complete),
        "weight_flow": 0.65, "weight_volume": 0.35,
        "flow_rmse_slm": math.sqrt(sum(e * e for e in errors) / len(errors)),
        "flow_mae_slm": sum(abs(e) for e in errors) / len(errors),
        "mean_tidal_ml": sum(tidal_values) / len(tidal_values),
        "mean_tidal_error_pct": 100.0 * volume_error,
        # Only detects a fully open PPV (opening >= 0.999); it does NOT detect
        # the expiration opening pinned at the v8 cap (see pinned_fraction_exp).
        "saturation_fraction": sum(r.opening >= 0.999 for r in active) / len(active),
        **{f"cycle_{c}_inspired_ml": v for c, v in zip(complete, tidal_values)},
        **_run_diagnostics(rows, cfg, complete, tidal_values, score),
        **_phase_diagnostics(rows, cfg, complete, scales, counts, terms),
    }


def config_from_dict(values: dict) -> SingleLungConfig:
    """Rebuild a configuration saved as ``asdict(cfg)`` in a run JSON."""
    cfg = SingleLungConfig()
    _merge_dataclass(cfg, copy.deepcopy(values))
    return cfg


def load_logged_run(csv_path: Path) -> Tuple[List[HardwareTelemetry], dict]:
    """Read telemetry rows from a saved CSV and metadata from its JSON pair."""
    kinds = {name: f.type for name, f in HardwareTelemetry.__dataclass_fields__.items()}
    rows = []
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as handle:
        for record in csv.DictReader(handle):
            values = {}
            for name, kind in kinds.items():
                text = record[name]
                if kind in ("int", int):
                    values[name] = int(text)
                elif kind in ("bool", bool):
                    values[name] = text in ("True", "1", "true")
                else:
                    values[name] = float(text)
            rows.append(HardwareTelemetry(**values))
    metadata = json.loads(Path(csv_path).with_suffix(".json").read_text(encoding="utf-8"))
    return rows, metadata


# Auto PID start-up health limits (refuse unless the operator overrides).
HEALTH_MAX_PINNED_FRACTION = 0.5
# Expiration pair: with more of expiration pinned at the v8 opening cap, the
# expiration gains have little gradient, so the round is refused (override-able).
EXPIRATION_PAIR_MAX_PINNED_FRACTION = 0.2
HEALTH_MAX_TIDAL_ERROR_PCT = 10.0
HEALTH_MIN_EMPTIED_FRACTION = 0.8
# Inspiration commanded at the lowest allowed voltage for more than this share
# of its samples: the operating point is beyond what the hardware can follow.
HEALTH_MAX_INSPIRATION_CAP_FRACTION = 0.3


def auto_pid_health_issues(metrics: Dict[str, float], *, tune_inspiration: bool = True,
                           tune_expiration: bool = True) -> List[str]:
    """Reasons a baseline is unsuitable for Auto PID; empty when healthy.

    Surge, tidal error and inspiration capping are always checked.  Expiration pinning and
    emptying only matter when the expiration pair is tuned.  Non-finite
    diagnostics (e.g. telemetry without volume) are not judged.
    """
    issues = []
    checks = [
        ("mean_tidal_error_pct", lambda v: v > HEALTH_MAX_TIDAL_ERROR_PCT,
         "mean tidal error {:.1f}% (> {:.0f}%)", HEALTH_MAX_TIDAL_ERROR_PCT),
        ("max_vac_drop_0p2s_ml", lambda v: v > TRANSITION_DROP_LIMIT_ML,
         "expiration-onset surge: {:.1f} mL extracted in the first 0.2 s (> {:.0f} mL)",
         TRANSITION_DROP_LIMIT_ML),
        ("inspiration_cap_fraction", lambda v: v > HEALTH_MAX_INSPIRATION_CAP_FRACTION,
         "inspiration capped at the minimum voltage (min_v) for {:.0%} of its samples "
         "(> {:.0%}): operating point beyond hardware capability",
         HEALTH_MAX_INSPIRATION_CAP_FRACTION),
    ]
    if tune_expiration:
        checks[:0] = [("pinned_fraction_exp", lambda v: v > EXPIRATION_PAIR_MAX_PINNED_FRACTION,
                       "expiration opening pinned at the v8 cap for {:.0%} of samples (> {:.0%}); "
                       "PI gains cannot fix a cap limit", EXPIRATION_PAIR_MAX_PINNED_FRACTION)]
        # Leak-compensated emptying; the firmware-volume value is clipped at 0
        # and only used for metrics saved before the net value existed.
        emptied_key = ("mean_net_expiration_emptied_fraction"
                       if "mean_net_expiration_emptied_fraction" in metrics
                       else "mean_expiration_emptied_fraction")
        checks.append((emptied_key, lambda v: v < HEALTH_MIN_EMPTIED_FRACTION,
                       "expiration empties only {:.0%} of the bag (< {:.0%})",
                       HEALTH_MIN_EMPTIED_FRACTION))
    for key, bad, text, limit in checks:
        value = metrics.get(key, math.nan)
        if isinstance(value, (int, float)) and math.isfinite(value) and bad(value):
            issues.append(text.format(value, limit))
    return issues


# Fields that must match for an earlier run to stand in for the Auto PID baseline.
COMPARABLE_FIELDS = (
    "respiratory_rate_bpm", "ie_inspiration", "ie_expiration", "target_volume_ml",
    "target_fill_time_s", "hardware_transition_dwell_min_s", "hardware_transition_overclose_v",
    "hardware_inspiration_dwell_min_s", "hardware_transition_settle_s",
    "hardware_expiration_closure_v",
    "dead_time_within_cycle", "hardware_inspiration_min_v", "hardware_expiration_kp",
    "hardware_expiration_ki", "hardware_expiration_integral_limit",
)


def comparable_runs(a: SingleLungConfig, b: SingleLungConfig) -> bool:
    return (all(getattr(a, f) == getattr(b, f) for f in COMPARABLE_FIELDS)
            and asdict(a.pi) == asdict(b.pi) and asdict(a.lung) == asdict(b.lung)
            and asdict(a.smith) == asdict(b.smith))


# Repeat-run noise of J: runs 1114002333 vs 1114081801 (identical settings),
# |0.328757 - 0.330573| / 0.330573.  Coordinate search only, when no repeat is
# available; ES and the sensitivity probe measure noise from their own baselines.
DEFAULT_SCORE_NOISE_FRACTION = 0.0055
AUTO_PID_WARMUP_CYCLES = 1
# Stage name of the optional unscored run before the Auto PID baselines.
AUTO_PID_STABILIZATION_STAGE = "stabilize"
# Live display: the schematic redraw costs ~0.3 s, so it runs at <= 4 Hz
# (and on every state change); the on-screen log keeps the newest lines.
MODEL_REFRESH_S = 0.25
LOG_MAX_LINES = 2000


def auto_pid_repeat_metrics(rows: List[HardwareTelemetry], cfg: SingleLungConfig,
                            metrics: Dict[str, float],
                            warmup_cycles: int) -> Optional[Dict[str, float]]:
    """Metrics of a completed run scored like an Auto PID candidate.

    Candidates skip AUTO_PID_WARMUP_CYCLES; a manual run scored with fewer
    keeps its first cycle (filled from the preconditioned, empty bag), which
    biases the repeat-noise estimate.  Re-score it with the same warm-up.
    """
    if warmup_cycles >= AUTO_PID_WARMUP_CYCLES:
        return metrics
    try:
        return hardware_run_metrics(rows, cfg, run_completed=True,
                                    warmup_cycles=AUTO_PID_WARMUP_CYCLES)
    except ValueError:
        return None

AUTO_PID_GAINS = ("kp_in", "ki_in", "kp_out", "ki_out")
# Gains are not capped; the search box is only 0.25x..4x the start values.
AUTO_PID_GAIN_CAPS = (math.inf,) * 4
Gains = Tuple[float, float, float, float]


def auto_pid_run_estimate_s(cfg: SingleLungConfig, warmup_cycles: int = 0) -> float:
    """Rough wall time of one candidate: precondition, initial positioning
    (~2.5 s from 0 V), cycles (plus unscored warm-up) incl. transition dwell
    unless deducted, and the 5 s recovery timeout as an upper bound."""
    per_cycle = cfg.cycle_period_s + (
        0.0 if cfg.dead_time_within_cycle
        else cfg_expiration_transition_s(cfg) + cfg.hardware_inspiration_dwell_min_s)
    return cfg.precondition_s + 2.5 + (cfg.cycles + warmup_cycles) * per_cycle + 5.0


def apply_auto_pid_gains(cfg: SingleLungConfig, gains: Gains) -> None:
    cfg.pi.kp, cfg.pi.ki, cfg.hardware_expiration_kp, cfg.hardware_expiration_ki = gains


class CoordinateSearchTuner:
    """Bounded coordinate search over both phases' Kp and Ki using hardware runs.

    Inspiration and expiration Kp/Ki are searched together and every candidate
    is ranked by the full objective J = 0.65 J_Q + 0.35 J_V.  This is
    deliberately run-to-run and leaves Kd unchanged.  Each candidate is
    evaluated using the same breathing settings and number of cycles.  It is a
    conservative engineering search, not a claim that the STM32 performs an
    online self-tuning algorithm.
    """

    def __init__(self, gains: Gains, rounds: int = 2,
                 perturbation_log: float = 0.12) -> None:
        gains = tuple(float(g) for g in gains)
        if len(gains) != len(AUTO_PID_GAINS):
            raise ValueError("Auto PID requires inspiration and expiration Kp/Ki")
        if not all(math.isfinite(v) and v > 0 for v in (*gains, perturbation_log)):
            raise ValueError("Auto PID requires positive starting Kp and Ki in both phases")
        if rounds < 1:
            raise ValueError("Auto PID rounds must be at least 1")
        self.original: Gains = gains
        self.rounds = rounds
        self.perturbation_log = perturbation_log
        self.lower = tuple(0.25 * g for g in gains)
        self.upper = tuple(min(4.0 * g, cap) for g, cap in zip(gains, AUTO_PID_GAIN_CAPS))
        self.best_gains: Gains = self.original
        self.best_score = math.inf
        self.history: List[Dict[str, float | str]] = []
        self._round = 0
        self._queue: List[Tuple[str, Gains]] = [("baseline", self.original)]
        self.current_stage = ""
        self.current_gains: Optional[Gains] = None
        self._seen: set[Gains] = set()
        # Relative repeat-run noise of J; replaced by a measured repeat when
        # one is available (see set_noise_from_repeat).
        self.noise_fraction = DEFAULT_SCORE_NOISE_FRACTION
        self.noise_source = "default: runs 1114002333 vs 1114081801"
        self.baseline_score = math.nan
        # Score of an earlier run with identical settings and gains; when set,
        # it and the baseline candidate form the repeat used as noise floor.
        self.repeat_candidate_score = math.nan
        self.confirmation: Optional[Dict[str, float | str]] = None
        self.confirmation_started = False

    def set_noise_from_repeat(self, score_a: float, score_b: float, source: str) -> None:
        if all(math.isfinite(s) for s in (score_a, score_b)) and max(score_a, score_b) > 0:
            self.noise_fraction = abs(score_a - score_b) / max(score_a, score_b)
            self.noise_source = source

    method = "coordinate_search"
    # Tunes all four gains, so every health check applies.
    tune_inspiration = tune_expiration = True

    def begin_confirmation(self) -> Gains:
        """Queue one extra run of the best gains; it does not change best_*."""
        if self.current_gains is not None:
            raise RuntimeError("record the current Auto PID result first")
        self.confirmation_started = True
        self.current_stage = "confirm_best"
        self.current_gains = self.best_gains
        return self.best_gains

    def final_gains(self) -> Tuple[Gains, str]:
        return self.best_gains, "best tested gains"

    def summary_extra(self) -> dict:
        return {"method": self.method, "rounds": self.rounds,
                "perturbation_log": self.perturbation_log}

    @property
    def total_candidates(self) -> int:
        """Upper bound; duplicates created by the bounds are skipped."""
        return 1 + 2 * len(AUTO_PID_GAINS) * self.rounds

    def _bounded(self, gains: Gains) -> Gains:
        return tuple(min(u, max(l, g)) for g, l, u in zip(gains, self.lower, self.upper))

    def _prepare_round(self) -> None:
        self._round += 1
        step = self.perturbation_log / (2 ** (self._round - 1))
        factor = math.exp(step)
        self._queue = []
        for index, name in enumerate(AUTO_PID_GAINS):
            for label, scale in (("plus", factor), ("minus", 1.0 / factor)):
                gains = list(self.best_gains)
                gains[index] *= scale
                self._queue.append((f"round_{self._round}_{name}_{label}",
                                    self._bounded(tuple(gains))))

    def next_candidate(self) -> Optional[Gains]:
        if self.current_gains is not None:
            raise RuntimeError("record the current Auto PID result first")
        while True:
            if not self._queue:
                if self._round >= self.rounds:
                    return None
                self._prepare_round()
            stage, gains = self._queue.pop(0)
            key = tuple(round(g, 10) for g in gains)
            if key in self._seen:
                continue
            self._seen.add(key)
            self.current_stage = stage
            self.current_gains = gains
            return gains

    def record(self, metrics: Dict[str, float]) -> None:
        if self.current_gains is None:
            raise RuntimeError("no Auto PID candidate is awaiting a result")
        if not math.isfinite(metrics["score"]) or metrics["score"] < 0:
            raise ValueError("Auto PID requires a finite nonnegative score")
        score = metrics["score"]
        if self.current_stage == "baseline":
            self.baseline_score = score
            self.set_noise_from_repeat(score, self.repeat_candidate_score,
                                       "earlier identical run vs baseline candidate")
        improvement = ((self.baseline_score - score) / self.baseline_score
                       if math.isfinite(self.baseline_score) and self.baseline_score > 0 else math.nan)
        row: Dict[str, float | str] = {
            "stage": self.current_stage,
            **dict(zip(AUTO_PID_GAINS, self.current_gains)),
            **metrics,
            "improvement_vs_baseline": improvement,
            "noise_fraction": self.noise_fraction,
            "exceeds_3x_noise": bool(improvement > 3 * self.noise_fraction),
        }
        self.history.append(row)
        if self.current_stage == "confirm_best":
            self.confirmation = row
        elif score < self.best_score:
            self.best_score = score
            self.best_gains = self.current_gains
        self.current_gains = None


# Discrete extremum seeking (Killingsworth & Krstic): defaults from the paper's
# simulations (a = 0.8, h = 0.5).  alpha_rel / gamma_rel are NOT calibrated.
ES_DEFAULT_A = 0.8
ES_DEFAULT_H = 0.5
ES_DEFAULT_ALPHA_REL = 0.07
ES_DEFAULT_GAMMA_REL = 15.0
ES_DEFAULT_ITERATIONS = 30
ES_FAILURE_FACTOR = 3.0          # abnormal run: J = 3 J_base; every J clipped to it
ES_MAX_CONSECUTIVE_FAILURES = 3
ES_CORRELATION_WARN = 0.2
ES_BOUNDS = (0.25, 4.0)          # x initial value, for both theta_hat and theta

# One Auto PID round tunes one gain pair against that pair's phase cost.
AUTO_PID_PAIRS = {"inspiration": (0, 1), "expiration": (2, 3)}
PHASE_COST_KEYS = {"inspiration": "J_ins", "expiration": "J_out"}
# Identical baseline runs at the start of every round; their spread is the noise floor.
AUTO_PID_BASELINE_RUNS = 3
AUTO_PID_BASELINE_RANGE = (2, 5)
# Sensitivity pre-experiment: each gain of the pair at these multiples, others unchanged.
SENSITIVITY_SCALES = (0.6, 1.4)
SENSITIVITY_TARGET_SNR = 3.0     # dither effect on J_phase >= 3 x noise floor
SENSITIVITY_ALPHA_MAX = 0.25
SENSITIVITY_TARGET_STEP = 0.03   # step_statistics target for the gamma_rel suggestion


def phase_costs(metrics: Dict[str, float]) -> Dict[str, float]:
    """Split ``score`` into inspiration and expiration parts (score unchanged).

        J_ins = w_flow * 0.5 * J_Q_in + w_vol * J_V
        J_out = w_flow * 0.5 * J_Q_out
        J_ins + J_out = w_flow * J_Q + w_vol * J_V = score

    Inspiration gains act only during inspiration (flow and delivered volume),
    expiration gains only during expiration.  Tuning a pair on its own phase
    cost therefore has the same expected optimum as tuning it on the full J;
    it only removes the other phase's run-to-run noise from the measurement.
    """
    wf, wv = metrics["weight_flow"], metrics["weight_volume"]
    return {"J_ins": wf * 0.5 * metrics["J_Q_in"] + wv * metrics["J_V"],
            "J_out": wf * 0.5 * metrics["J_Q_out"]}


def relative_spread(values: List[float]) -> float:
    """Relative noise of repeated runs: |a - b| / mean for two values,
    (max - min) / mean for three or more."""
    values = [v for v in values if math.isfinite(v)]
    if len(values) < 2:
        return math.nan
    mean = sum(values) / len(values)
    return (max(values) - min(values)) / mean if mean > 0 else math.nan


class AutoPidBaselines:
    """Identical runs at the start of a round: means and measured noise floor
    of J_ins, J_out and the full score."""

    QUANTITIES = ("J_ins", "J_out", "score")

    def __init__(self, runs: int) -> None:
        low, high = AUTO_PID_BASELINE_RANGE
        if not low <= int(runs) <= high:
            raise ValueError(f"Auto PID needs {low}-{high} baseline runs to measure the noise floor")
        self.runs = int(runs)
        self.values: Dict[str, List[float]] = {q: [] for q in self.QUANTITIES}

    @property
    def count(self) -> int:
        return len(self.values["score"])

    @property
    def done(self) -> bool:
        return self.count >= self.runs

    def add(self, metrics: Dict[str, float]) -> None:
        costs = phase_costs(metrics)
        for q in self.QUANTITIES:
            self.values[q].append(metrics["score"] if q == "score" else costs[q])

    def mean(self, quantity: str) -> float:
        values = self.values[quantity]
        return sum(values) / len(values) if values else math.nan

    def noise(self, quantity: str) -> float:
        return relative_spread(self.values[quantity])

    def fields(self) -> Dict[str, float]:
        """Report columns: baseline mean and noise floor of each quantity."""
        out: Dict[str, float] = {}
        for q in self.QUANTITIES:
            out[f"baseline_mean_{q}"] = self.mean(q)
            out[f"noise_{q}"] = self.noise(q)
        return out

    def summary(self) -> dict:
        return {"runs": self.runs, "values": self.values,
                "noise_rule": "|a-b|/mean for 2 runs, (max-min)/mean for 3 or more",
                **self.fields()}


def es_channels(pair: str, a: float) -> List[Tuple[int, float, str]]:
    """(gain index, omega, 'cos'|'sin') for the tuned pair.

    Whichever pair is tuned, it is a quadrature pair at omega = a*pi:
    Kp with cos, Ki with sin (one round = one pair, one filter state).
    """
    if pair not in AUTO_PID_PAIRS:
        raise ValueError(f"Auto PID pair must be one of {sorted(AUTO_PID_PAIRS)}")
    kp, ki = AUTO_PID_PAIRS[pair]
    return [(kp, a * math.pi, "cos"), (ki, a * math.pi, "sin")]


def es_dither(omega: float, kind: str, k: int, phase: float = 0.0) -> float:
    return math.cos(omega * k + phase) if kind == "cos" else math.sin(omega * k + phase)


def es_highpass_phase(omega: float, h: float) -> float:
    """Phase of the high-pass filter (z - 1)/(z + h) at z = exp(j omega)."""
    z = complex(math.cos(omega), math.sin(omega))
    return math.atan2(((z - 1) / (z + h)).imag, ((z - 1) / (z + h)).real)


def es_dither_correlation(channels: List[Tuple[int, float, str]], iterations: int
                          ) -> Tuple[List[List[float]], List[str]]:
    """Pearson correlation of the dither sequences over k = 0..iterations-1."""
    seqs = [[es_dither(w, kind, k) for k in range(iterations)] for _, w, kind in channels]

    def corr(x, y):
        mx, my = sum(x) / len(x), sum(y) / len(y)
        sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
        sxx = sum((a - mx) ** 2 for a in x)
        syy = sum((b - my) ** 2 for b in y)
        return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else math.nan

    matrix = [[corr(x, y) for y in seqs] for x in seqs]
    warnings = []
    names = [AUTO_PID_GAINS[i] for i, _, _ in channels]
    for i in range(len(seqs)):
        for j in range(i + 1, len(seqs)):
            if not math.isfinite(matrix[i][j]) or abs(matrix[i][j]) > ES_CORRELATION_WARN:
                warnings.append(f"dither correlation {names[i]}/{names[j]} = {matrix[i][j]:+.2f} "
                                f"(|rho| > {ES_CORRELATION_WARN}) over {iterations} iterations")
    return matrix, warnings


class ExtremumSeekingTuner:
    """Discrete extremum seeking, Killingsworth & Krstic, equations (5)-(7),
    on one gain pair per round.

    Iteration k is one complete hardware run.  J(k) is the phase cost of the
    tuned pair (J_ins for the inspiration pair, J_out for the expiration pair;
    see ``phase_costs``), warm-up cycle excluded, i.e. the paper's t0:

        zeta(k)      = -h zeta(k-1) + J(k-1)                                 (5)
        theta_hat(k+1) = theta_hat(k) - gamma alpha d(k) [J(k) - (1+h) zeta(k)]  (6)
        theta(k+1)   = theta_hat(k+1) + alpha d(k+1)                         (7)

    Implementation choices (not in the paper):
    - a round starts with N_base identical baseline runs; their mean is J_base
      and their spread is the measured noise floor of J_ins, J_out and J;
    - the pair is a quadrature pair at omega = a*pi (Kp cos, Ki sin), one
      filter state zeta;
    - the update runs on u = theta / theta0; this equals (6)-(7) with
      alpha_i = alpha_rel * theta0_i and gamma_i = gamma_rel;
    - theta_hat and theta are clamped to [0.25, 4] x theta0;
    - zeta(-1) = J_base/(1+h), J(-1) = J_base so the high-pass output starts at zero;
    - J(k) is clipped to 3 J_base; an abnormal run uses J = 3 J_base;
    - optional demodulation phase = high-pass phase at omega (the paper's
      Fig. 1 "cos(omega k - phi)"; equations (5)-(7) use phi = 0, the default).
    The result is theta_hat(N), accepted only after a confirmation run (see
    ``final_gains``); the other pair is never changed.
    """

    method = "extremum_seeking"
    needs_confirmation = True

    def __init__(self, gains: Gains, *, pair: str = "inspiration",
                 iterations: int = ES_DEFAULT_ITERATIONS,
                 alpha_rel: float = ES_DEFAULT_ALPHA_REL, gamma_rel: float = ES_DEFAULT_GAMMA_REL,
                 a: float = ES_DEFAULT_A, h: float = ES_DEFAULT_H,
                 phase_compensation: bool = False,
                 baseline_runs: int = AUTO_PID_BASELINE_RUNS) -> None:
        gains = tuple(float(g) for g in gains)
        if len(gains) != len(AUTO_PID_GAINS):
            raise ValueError("Auto PID requires inspiration and expiration Kp/Ki")
        if not (0 < a < 1 and 0 < h < 1):
            raise ValueError("Extremum seeking requires 0 < a < 1 and 0 < h < 1")
        if not (iterations >= 1 and alpha_rel > 0 and gamma_rel > 0):
            raise ValueError("Extremum seeking requires iterations >= 1, alpha_rel > 0, gamma_rel > 0")
        self.channels = es_channels(pair, a)
        if not all(math.isfinite(gains[i]) and gains[i] > 0 for i, _, _ in self.channels):
            raise ValueError("Extremum seeking requires positive starting Kp and Ki for the tuned pair")
        self.baselines = AutoPidBaselines(baseline_runs)
        self.pair = pair
        self.cost_key = PHASE_COST_KEYS[pair]
        self.tune_inspiration, self.tune_expiration = pair == "inspiration", pair == "expiration"
        self.original: Gains = gains
        self.iterations = int(iterations)
        self.alpha_rel, self.gamma_rel, self.a, self.h = alpha_rel, gamma_rel, a, h
        self.phase_compensation = phase_compensation
        self.demod_phase = [es_highpass_phase(w, h) if phase_compensation else 0.0
                            for _, w, _ in self.channels]
        self.lower = tuple(ES_BOUNDS[0] * g for g in gains)
        self.upper = tuple(ES_BOUNDS[1] * g for g in gains)
        self.u_hat = [1.0] * len(self.channels)     # theta_hat / theta0 per channel
        self.k = 0
        self.zeta_prev = self.j_prev = math.nan
        self.consecutive_failures = 0
        self.correlation, self.correlation_warnings = es_dither_correlation(self.channels, self.iterations)
        self.history: List[Dict[str, float | str]] = []
        self.theta_hat_trajectory: List[Gains] = [gains]
        self.j_curve: List[float] = []
        self.current_stage = ""
        self.current_gains: Optional[Gains] = None
        self.confirmation: Optional[Dict[str, float | str]] = None
        self.confirmation_started = False
        self.best_measured_score = math.inf
        self.best_measured_gains: Gains = gains

    @property
    def significance_rule(self) -> str:
        return (f"accept theta_hat(N) only if the confirmation run improves {self.cost_key} vs the "
                f"baseline mean by > 3 x noise_{self.cost_key} and the full J is not worse than "
                "the baseline mean by more than noise_score")

    @property
    def baseline_score(self) -> float:
        """Baseline mean of this round's phase cost (J_base)."""
        return self.baselines.mean(self.cost_key)

    @property
    def noise_fraction(self) -> float:
        """Measured relative noise floor of this round's phase cost."""
        return self.baselines.noise(self.cost_key)

    @property
    def noise_source(self) -> str:
        return f"measured: {self.baselines.count} identical baseline runs of this round"

    @property
    def total_candidates(self) -> int:
        """Baseline runs + iterations (the confirmation run is extra)."""
        return self.baselines.runs + self.iterations

    @property
    def theta_hat(self) -> Gains:
        gains = list(self.original)
        for (i, _, _), u in zip(self.channels, self.u_hat):
            gains[i] = self.original[i] * u
        return tuple(gains)

    # Compatibility with the GUI's coordinate-search interface.
    @property
    def best_gains(self) -> Gains:
        return self.theta_hat

    @property
    def best_score(self) -> float:
        return self.best_measured_score

    def health_scope(self) -> dict:
        return {"tune_inspiration": self.tune_inspiration, "tune_expiration": self.tune_expiration}

    def _clamp(self, u: float) -> float:
        return min(ES_BOUNDS[1], max(ES_BOUNDS[0], u))

    def _dithered(self, k: int) -> Gains:
        gains = list(self.original)
        for (i, w, kind), u in zip(self.channels, self.u_hat):
            gains[i] = self.original[i] * self._clamp(u + self.alpha_rel * es_dither(w, kind, k))
        return tuple(gains)

    def next_candidate(self) -> Optional[Gains]:
        if self.current_gains is not None:
            raise RuntimeError("record the current Auto PID result first")
        if not self.baselines.done:
            self.current_stage = f"baseline_{self.baselines.count + 1}"
            self.current_gains = self.original
        elif self.k < self.iterations:
            self.current_stage, self.current_gains = f"es_{self.k}", self._dithered(self.k)
        else:
            return None
        return self.current_gains

    def begin_confirmation(self) -> Gains:
        if self.current_gains is not None:
            raise RuntimeError("record the current Auto PID result first")
        self.confirmation_started = True
        self.current_stage, self.current_gains = "confirm_best", self.theta_hat
        return self.current_gains

    def _row(self, role: str, gains: Gains, metrics: Dict, j_used: float, j_raw: float,
             **extra) -> Dict:
        costs = phase_costs(metrics) if "J_Q_in" in metrics else {}
        row: Dict[str, float | str] = {
            "stage": self.current_stage, "role": role, "method": self.method,
            "pair": self.pair, "phase_cost": self.cost_key,
            **dict(zip(AUTO_PID_GAINS, gains)),
            **metrics,
            "J_ins": costs.get("J_ins", math.nan), "J_out": costs.get("J_out", math.nan),
            "J_phase_raw": j_raw, "J_phase": j_used,
            "a": self.a, "h": self.h, "alpha_rel": self.alpha_rel, "gamma_rel": self.gamma_rel,
            "phase_compensation": self.phase_compensation,
        }
        for (i, w, kind), phase in zip(self.channels, self.demod_phase):
            name = AUTO_PID_GAINS[i]
            row.update({f"alpha_{name}": self.alpha_rel * self.original[i],
                        f"gamma_{name}": self.gamma_rel,
                        f"omega_{name}_pi": w / math.pi, f"dither_{name}": kind,
                        f"demod_phase_{name}_deg": math.degrees(phase)})
        row.update(self.baselines.fields())
        row.update(extra)
        return row

    def _improvement(self, value: float, quantity: Optional[str] = None) -> float:
        base = self.baselines.mean(quantity or self.cost_key)
        return (base - value) / base if math.isfinite(base) and base > 0 else math.nan

    def record(self, metrics: Dict[str, float]) -> None:
        if self.current_gains is None:
            raise RuntimeError("no Auto PID candidate is awaiting a result")
        score = metrics["score"]
        if not math.isfinite(score) or score < 0:
            raise ValueError("Auto PID requires a finite nonnegative score")
        j_raw = phase_costs(metrics)[self.cost_key]
        if self.current_stage.startswith("baseline"):
            self.baselines.add(metrics)
            if self.baselines.done:
                # Steady-state initialisation: high-pass output 0 at k = 0 if J(0) = J_base.
                j_base = self.baseline_score
                self.zeta_prev, self.j_prev = j_base / (1 + self.h), j_base
            self.history.append(self._row("baseline", self.current_gains, metrics, j_raw, j_raw,
                                          iteration=-1, zeta=self.zeta_prev))
            self.current_gains = None
        elif self.current_stage == "confirm_best":
            self._record_confirmation(j_raw, metrics, None)
        else:
            self.consecutive_failures = 0
            self._update(j_raw, metrics, None)

    def record_failure(self, reason: str) -> None:
        """Abnormal run (aborted, unscorable): J = 3 J_base; ES continues."""
        if self.current_gains is None:
            raise RuntimeError("no Auto PID candidate is awaiting a result")
        if not self.baselines.done:
            raise ValueError(f"baseline run failed: {reason}")
        if self.current_stage == "confirm_best":
            self._record_confirmation(math.nan, {}, reason)
            return
        self.consecutive_failures += 1
        self._update(ES_FAILURE_FACTOR * self.baseline_score, {}, reason)

    def acceptance(self, conf: Optional[Dict]) -> Dict[str, float | bool | str]:
        """Acceptance of theta_hat(N) from the confirmation run."""
        j_phase = float(conf["J_phase_raw"]) if conf else math.nan
        j_full = float(conf.get("score", math.nan)) if conf else math.nan
        improvement = self._improvement(j_phase)
        threshold = 3 * self.noise_fraction
        base_full = self.baselines.mean("score")
        full_change = ((j_full - base_full) / base_full
                       if math.isfinite(base_full) and base_full > 0 else math.nan)
        full_tolerance = self.baselines.noise("score")
        phase_ok = (math.isfinite(improvement) and math.isfinite(threshold)
                    and improvement > threshold)
        full_ok = (math.isfinite(full_change) and math.isfinite(full_tolerance)
                   and full_change <= full_tolerance)
        if conf is None:
            decision = "no confirmation run; original gains kept"
        elif phase_ok and full_ok:
            decision = "theta_hat(N) improved significantly; accepted"
        elif not phase_ok:
            decision = f"no significant improvement in {self.cost_key}; original gains kept"
        else:
            decision = "no significant improvement: full J worse than noise allows; original gains kept"
        return {"phase_cost": self.cost_key,
                "phase_improvement_pct": 100 * improvement,
                "phase_threshold_pct": 100 * threshold,
                "full_J_change_pct": 100 * full_change,
                "full_J_tolerance_pct": 100 * full_tolerance,
                "accepted": bool(conf is not None and phase_ok and full_ok),
                "decision": decision}

    def _record_confirmation(self, j_raw: float, metrics: Dict, failure: Optional[str]) -> None:
        self.confirmation = self._row("confirmation", self.current_gains, metrics, j_raw, j_raw,
                                      iteration=self.k, failure=failure or "")
        self.confirmation.update({f"acceptance_{k}": v
                                  for k, v in self.acceptance(self.confirmation).items()})
        self.history.append(self.confirmation)
        self.current_gains = None

    def _update(self, j_raw: float, metrics: Dict, failure: Optional[str]) -> None:
        k, gains = self.k, self.current_gains
        j = min(j_raw, ES_FAILURE_FACTOR * self.baseline_score)
        zeta = -self.h * self.zeta_prev + self.j_prev                       # (5)
        highpass = j - (1 + self.h) * zeta
        theta_hat_k = self.theta_hat
        for c, ((i, w, kind), phase) in enumerate(zip(self.channels, self.demod_phase)):
            # (6) in u = theta/theta0:  u_hat -= gamma_rel alpha_rel d(k) [J - (1+h) zeta]
            self.u_hat[c] = self._clamp(
                self.u_hat[c] - self.gamma_rel * self.alpha_rel * es_dither(w, kind, k, phase) * highpass)
        improvement = self._improvement(j)
        row = {**self._row("dither", gains, metrics, j, j_raw, iteration=k, zeta=zeta,
                           highpass=highpass, failure=failure or ""),
               **{f"theta_hat_k_{name}": v for name, v in zip(AUTO_PID_GAINS, theta_hat_k)},
               **{f"theta_hat_next_{name}": v for name, v in zip(AUTO_PID_GAINS, self.theta_hat)},
               "improvement_vs_baseline": improvement, "noise_fraction": self.noise_fraction,
               "exceeds_3x_noise": bool(improvement > 3 * self.noise_fraction)}
        self.history.append(row)
        self.j_curve.append(j)
        self.theta_hat_trajectory.append(self.theta_hat)
        if failure is None and j_raw < self.best_measured_score:
            self.best_measured_score, self.best_measured_gains = j_raw, gains
        self.zeta_prev, self.j_prev = zeta, j
        self.k += 1
        self.current_gains = None

    def final_gains(self) -> Tuple[Gains, str]:
        """theta_hat(N) when the confirmation run passes ``acceptance``."""
        result = self.acceptance(self.confirmation)
        return (self.theta_hat if result["accepted"] else self.original), str(result["decision"])

    def step_statistics(self, target_step: float = SENSITIVITY_TARGET_STEP) -> Dict[str, dict]:
        """Measured mean |delta theta_hat| / theta0 per iteration and the gamma_rel
        that would scale it to ``target_step`` (the step is linear in gamma_rel;
        noise contributes too, so this is a calibration aid, not a rule)."""
        stats = {}
        # Dithered iterations without abnormal runs: (k, J_phase_raw).
        samples = [(row["iteration"], row["J_phase_raw"]) for row in self.history
                   if row.get("role") == "dither" and not row.get("failure")]
        for i, w, kind in self.channels:
            steps = [abs(b[i] - a[i]) / self.original[i]
                     for a, b in zip(self.theta_hat_trajectory, self.theta_hat_trajectory[1:])]
            mean = sum(steps) / len(steps) if steps else math.nan
            # g = |dJ/du| from the covariance of J(k) with this channel's dither,
            # J(k) ~ c + g * alpha_rel * d(k).  Per-channel regression (the
            # dithers are nearly uncorrelated); theta_hat drift is ignored.
            g = math.nan
            if len(samples) >= 3:
                d = [es_dither(w, kind, k) for k, _ in samples]
                j = [value for _, value in samples]
                md, mj = sum(d) / len(d), sum(j) / len(j)
                var = sum((x - md) ** 2 for x in d)
                if var > 0:
                    g = abs(sum((x - md) * (y - mj) for x, y in zip(d, j)) / (self.alpha_rel * var))
            stats[AUTO_PID_GAINS[i]] = {
                "mean_rel_step": mean,
                "gamma_rel_for_target_step": (self.gamma_rel * target_step / mean
                                              if steps and mean > 0 else math.nan),
                "g_abs_dJ_du_est": g,
                "gamma_rel_from_g": es_gamma_from_g(target_step, self.alpha_rel, g),
                "target_step": target_step,
                "note": ("Rough estimates. g = |dJ/du| (u = theta/theta0) from the correlation of "
                         "J(k) with the dither; gamma_rel_from_g = 2*step/(alpha_rel^2*g) ignores the "
                         "high-pass gain/phase at omega (about 2.86 and 44 deg at 0.8*pi), so the "
                         "realised step may be about 2x the target. gamma_rel_for_target_step scales "
                         "the measured mean step (includes noise)."),
            }
        return stats

    def summary_extra(self) -> dict:
        names = [AUTO_PID_GAINS[i] for i, _, _ in self.channels]
        return {
            "step_statistics": self.step_statistics(),
            "method": self.method,
            "pair": self.pair,
            "phase_cost": self.cost_key,
            "tuned_gains": names,
            "iterations": self.iterations, "a": self.a, "h": self.h,
            "alpha_rel": self.alpha_rel, "gamma_rel": self.gamma_rel,
            "phase_compensation": self.phase_compensation,
            "channels": [{"gain": AUTO_PID_GAINS[i], "omega_pi": w / math.pi, "dither": kind,
                          "demod_phase_deg": math.degrees(p)}
                         for (i, w, kind), p in zip(self.channels, self.demod_phase)],
            "baselines": self.baselines.summary(),
            "theta_hat_trajectory": [dict(zip(AUTO_PID_GAINS, g)) for g in self.theta_hat_trajectory],
            "J_curve": self.j_curve,
            "best_measured": {"J_phase": self.best_measured_score,
                              "gains": dict(zip(AUTO_PID_GAINS, self.best_measured_gains))},
            "dither_correlation": {"gains": names, "matrix": self.correlation},
            "correlation_warnings": self.correlation_warnings,
            "acceptance": self.acceptance(self.confirmation) if self.confirmation else None,
            "final_decision": self.final_gains()[1] if self.confirmation else None,
        }


def es_gamma_from_g(target_step: float, alpha_rel: float, g: float) -> float:
    """step_statistics rule: gamma_rel giving a step of ~target_step per
    iteration for gradient g, 2*step/(alpha_rel^2*g)."""
    return (2 * target_step / (alpha_rel ** 2 * g)
            if math.isfinite(g) and g > 0 and alpha_rel > 0 else math.nan)


class SensitivityProbe:
    """Sensitivity pre-experiment for one gain pair; never changes the gains.

    N_base identical baselines, then each gain of the pair at
    SENSITIVITY_SCALES x its start value (the other gains unchanged), one run
    each.  For every gain, from the round's phase cost J_phase:

        dJ(s)     = J_phase(s) - J_base
        g         = |J_phase(1.4) - J_phase(0.6)| / (1.4 - 0.6)   (per unit u = theta/theta0)
        SNR(s)    = |dJ(s)| / (noise * J_base)
        alpha_rel = 3 * noise * J_base / g, capped at 0.25
                    (dither alpha_rel * g >= 3 x noise floor)
        gamma_rel = 2 * 0.03 / (alpha_rel^2 * g)                  (step_statistics rule)

    The suggestion for the round uses the least sensitive gain for alpha_rel
    (both dithers visible) and the most sensitive gain for gamma_rel (no
    overshoot).  Suggestions are reported only, never applied.

    ``probes`` limits the run to some probe stages (e.g. the ones missing
    from an earlier report, see ``probe_resume_plan``); ``carried`` supplies
    the others as relative changes measured against that earlier report's own
    baseline, J = J_base(this session) * (1 + delta_rel).  A probe run stopped
    part-way is recorded as incomplete and never enters the analysis.
    """

    method = "sensitivity_probe"
    needs_confirmation = False

    def __init__(self, gains: Gains, *, pair: str = "inspiration",
                 baseline_runs: int = AUTO_PID_BASELINE_RUNS,
                 current_alpha_rel: float = ES_DEFAULT_ALPHA_REL,
                 probes: Optional[List[str]] = None,
                 carried: Optional[Dict[str, dict]] = None) -> None:
        gains = tuple(float(g) for g in gains)
        if len(gains) != len(AUTO_PID_GAINS):
            raise ValueError("Auto PID requires inspiration and expiration Kp/Ki")
        if pair not in AUTO_PID_PAIRS:
            raise ValueError(f"Auto PID pair must be one of {sorted(AUTO_PID_PAIRS)}")
        if not all(math.isfinite(gains[i]) and gains[i] > 0 for i in AUTO_PID_PAIRS[pair]):
            raise ValueError("The sensitivity probe requires positive starting Kp and Ki for the pair")
        self.baselines = AutoPidBaselines(baseline_runs)
        self.pair, self.cost_key = pair, PHASE_COST_KEYS[pair]
        self.tune_inspiration, self.tune_expiration = pair == "inspiration", pair == "expiration"
        self.original: Gains = gains
        self.current_alpha_rel = current_alpha_rel
        self.lower = tuple(min(SENSITIVITY_SCALES) * g for g in gains)
        self.upper = tuple(max(SENSITIVITY_SCALES) * g for g in gains)
        self._all_probes: List[Tuple[str, int, float, Gains]] = []
        for index in AUTO_PID_PAIRS[pair]:
            for scale in SENSITIVITY_SCALES:
                probe = list(gains)
                probe[index] *= scale
                self._all_probes.append((f"probe_{AUTO_PID_GAINS[index]}_x{scale:g}", index, scale,
                                         tuple(probe)))
        names = [p[0] for p in self._all_probes]
        self.carried: Dict[str, dict] = dict(carried or {})
        unknown = (set(probes or ()) | set(self.carried)) - set(names)
        if unknown:
            raise ValueError(f"unknown probe stages for the {pair} pair: {sorted(unknown)}")
        if probes is not None and set(probes) & set(self.carried):
            raise ValueError("a probe stage cannot be both run and carried over")
        self._probes = [p for p in self._all_probes if probes is None or p[0] in probes]
        if not self._probes:
            raise ValueError("the sensitivity probe needs at least one probe stage to run")
        self.incomplete: Dict[str, dict] = {}
        self.results: Dict[Tuple[int, float], float] = {}
        self.history: List[Dict[str, float | str]] = []
        self.current_stage = ""
        self.current_gains: Optional[Gains] = None
        self._current_probe: Optional[Tuple[int, float]] = None
        self.confirmation = None
        self.confirmation_started = False

    significance_rule = "sensitivity probe: no gains are changed"

    @property
    def total_candidates(self) -> int:
        return self.baselines.runs + len(self._probes)

    @property
    def baseline_score(self) -> float:
        return self.baselines.mean(self.cost_key)

    @property
    def noise_fraction(self) -> float:
        return self.baselines.noise(self.cost_key)

    @property
    def noise_source(self) -> str:
        return f"measured: {self.baselines.count} identical baseline runs of this probe"

    @property
    def best_gains(self) -> Gains:
        return self.original

    best_score = math.nan

    def health_scope(self) -> dict:
        return {"tune_inspiration": self.tune_inspiration, "tune_expiration": self.tune_expiration}

    def next_candidate(self) -> Optional[Gains]:
        if self.current_gains is not None:
            raise RuntimeError("record the current Auto PID result first")
        if not self.baselines.done:
            self.current_stage = f"baseline_{self.baselines.count + 1}"
            self.current_gains, self._current_probe = self.original, None
        else:
            done = sum(row["role"] == "probe" for row in self.history)
            if done >= len(self._probes):
                return None
            stage, index, scale, gains = self._probes[done]
            self.current_stage, self.current_gains = stage, gains
            self._current_probe = (index, scale)
        return self.current_gains

    def record(self, metrics: Dict[str, float]) -> None:
        if self.current_gains is None:
            raise RuntimeError("no Auto PID candidate is awaiting a result")
        if not math.isfinite(metrics["score"]) or metrics["score"] < 0:
            raise ValueError("Auto PID requires a finite nonnegative score")
        costs = phase_costs(metrics)
        if self._current_probe is None:
            self.baselines.add(metrics)
        else:
            self.results[self._current_probe] = costs[self.cost_key]
        self.history.append({
            "stage": self.current_stage, "role": "baseline" if self._current_probe is None else "probe",
            "method": self.method, "pair": self.pair, "phase_cost": self.cost_key,
            **dict(zip(AUTO_PID_GAINS, self.current_gains)), **metrics,
            "J_ins": costs["J_ins"], "J_out": costs["J_out"],
            "J_phase_raw": costs[self.cost_key], "J_phase": costs[self.cost_key],
            **self.baselines.fields()})
        self.current_gains = self._current_probe = None

    def record_incomplete(self, metrics: Optional[Dict[str, float]], requested_cycles: int,
                          run_id: Optional[int] = None) -> None:
        """A probe run stopped before all its cycles: kept in the report as
        incomplete (row role 'probe_incomplete'), excluded from the analysis.
        A partial baseline is simply not counted."""
        if self.current_gains is None or self._current_probe is None:
            return
        complete = int(metrics["N_c"]) if metrics else 0
        partial = phase_costs(metrics)[self.cost_key] if metrics else math.nan
        self.incomplete[self.current_stage] = {
            "complete_cycles": complete, "requested_cycles": int(requested_cycles),
            "J_phase_partial": partial, "run_id": run_id}
        self.history.append({
            "stage": self.current_stage, "role": "probe_incomplete",
            "status": f"incomplete ({complete}/{int(requested_cycles)} cycles)",
            "method": self.method, "pair": self.pair, "phase_cost": self.cost_key,
            **dict(zip(AUTO_PID_GAINS, self.current_gains)), **(metrics or {}),
            "J_phase_partial": partial, "run_id": run_id})
        self.current_gains = self._current_probe = None

    @property
    def finished(self) -> bool:
        return (self.baselines.done
                and sum(row["role"] == "probe" for row in self.history) >= len(self._probes))

    def probe_status(self) -> Dict[str, str]:
        """complete / incomplete (n/N cycles) / carried from ... / missing /
        not selected, for every probe stage of the pair."""
        done = {row["stage"] for row in self.history if row["role"] == "probe"}
        selected = {p[0] for p in self._probes}
        status = {}
        for stage, *_ in self._all_probes:
            if stage in done:
                status[stage] = "complete"
            elif stage in self.incomplete:
                info = self.incomplete[stage]
                status[stage] = (f"incomplete ({info['complete_cycles']}/"
                                 f"{info['requested_cycles']} cycles)")
            elif stage in self.carried:
                status[stage] = f"carried from {self.carried[stage].get('source', 'earlier report')}"
            elif stage in selected:
                status[stage] = "missing"
            else:
                status[stage] = "not selected"
        return status

    def _phase_value(self, index: int, scale: float) -> Tuple[float, str]:
        """This session's probe result, else a carried relative change."""
        if (index, scale) in self.results:
            return self.results[(index, scale)], "this session"
        stage = f"probe_{AUTO_PID_GAINS[index]}_x{scale:g}"
        if stage in self.carried and math.isfinite(self.baseline_score):
            return (self.baseline_score * (1.0 + float(self.carried[stage]["delta_rel"])),
                    f"carried from {self.carried[stage].get('source', 'earlier report')}")
        return math.nan, self.probe_status().get(stage, "missing")

    def record_failure(self, reason: str) -> None:
        raise ValueError(f"sensitivity probe run failed: {reason}")

    def final_gains(self) -> Tuple[Gains, str]:
        return self.original, "sensitivity probe only; gains unchanged"

    def analysis(self) -> dict:
        j_base, noise = self.baseline_score, self.noise_fraction
        noise_abs = noise * j_base if math.isfinite(noise) and math.isfinite(j_base) else math.nan
        low, high = SENSITIVITY_SCALES
        per_gain = {}
        for index in AUTO_PID_PAIRS[self.pair]:
            values = {s: self._phase_value(index, s) for s in SENSITIVITY_SCALES}
            j_low, j_high = values[low][0], values[high][0]
            g = abs(j_high - j_low) / (high - low)
            dj = {f"x{s:g}": values[s][0] - j_base for s in SENSITIVITY_SCALES}
            snr = {k: (abs(v) / noise_abs if noise_abs > 0 else math.nan) for k, v in dj.items()}
            alpha = (min(SENSITIVITY_ALPHA_MAX, SENSITIVITY_TARGET_SNR * noise_abs / g)
                     if noise_abs > 0 and math.isfinite(g) and g > 0 else
                     (SENSITIVITY_ALPHA_MAX if math.isfinite(g) and noise_abs > 0 else math.nan))
            per_gain[AUTO_PID_GAINS[index]] = {
                "J_phase": {f"x{s:g}": values[s][0] for s in SENSITIVITY_SCALES},
                "J_phase_source": {f"x{s:g}": values[s][1] for s in SENSITIVITY_SCALES},
                "delta_J_phase": dj,
                "g_abs_dJ_du": g,
                "snr": snr,
                "dither_snr_at_current_alpha": (g * self.current_alpha_rel / noise_abs
                                                if noise_abs > 0 else math.nan),
                "alpha_rel_for_3x_noise": alpha,
                "alpha_capped": bool(math.isfinite(g) and noise_abs > 0
                                     and (g <= 0 or SENSITIVITY_TARGET_SNR * noise_abs / g
                                          > SENSITIVITY_ALPHA_MAX)),
            }
        gs = [v["g_abs_dJ_du"] for v in per_gain.values() if math.isfinite(v["g_abs_dJ_du"])]
        alphas = [v["alpha_rel_for_3x_noise"] for v in per_gain.values()
                  if math.isfinite(v["alpha_rel_for_3x_noise"])]
        alpha = min(SENSITIVITY_ALPHA_MAX, max(alphas)) if alphas else math.nan
        gamma = es_gamma_from_g(SENSITIVITY_TARGET_STEP, alpha, max(gs)) if gs else math.nan
        for name, values in per_gain.items():
            values["gamma_rel_at_suggested_alpha"] = es_gamma_from_g(
                SENSITIVITY_TARGET_STEP, alpha, values["g_abs_dJ_du"])
        return {
            "pair": self.pair, "phase_cost": self.cost_key,
            "J_base": j_base, "noise_fraction": noise, "noise_abs": noise_abs,
            "scales": list(SENSITIVITY_SCALES), "per_gain": per_gain,
            "suggested_alpha_rel": alpha, "suggested_gamma_rel": gamma,
            "rules": {"g": "|J(1.4) - J(0.6)| / 0.8 per unit theta/theta0",
                      "snr": "|dJ| / (noise_fraction * J_base)",
                      "alpha_rel": "3 * noise_fraction * J_base / g, capped at 0.25; round value = "
                                   "largest per-gain value (least sensitive gain)",
                      "gamma_rel": "2 * 0.03 / (alpha_rel^2 * g) with the largest g "
                                   "(step_statistics rule)",
                      "applied": "never; suggestions only"},
        }

    def summary_extra(self) -> dict:
        status = self.probe_status()
        return {"method": self.method, "pair": self.pair, "phase_cost": self.cost_key,
                "tuned_gains": [AUTO_PID_GAINS[i] for i in AUTO_PID_PAIRS[self.pair]],
                "baselines": self.baselines.summary(),
                "sensitivity": self.analysis(),
                "probe_status": status,
                "probes_selected": [p[0] for p in self._probes],
                "missing_probes": [s for s, v in status.items() if v == "missing"
                                   or v.startswith("incomplete")],
                "carried_probes": self.carried,
                "incomplete_probes": self.incomplete}

    def unfinished_text(self) -> str:
        """'not finished: probe_kp_out_x1.4 incomplete (2/10 cycles); ...'."""
        parts = [f"{self.baselines.count}/{self.baselines.runs} baselines"] if not self.baselines.done else []
        parts += [f"{stage} {value}" for stage, value in self.probe_status().items()
                  if value == "missing" or value.startswith("incomplete")]
        return "not finished: " + "; ".join(parts) if parts else "not finished"


def probe_resume_plan(summary: dict, source: str = "earlier report") -> dict:
    """Which probe stages of an earlier sensitivity-probe report must be run
    again (missing or incomplete) and which can be carried over.

    Carried stages keep their change relative to that report's own baseline
    mean (delta_rel = J_phase / J_base - 1); the new session's baselines are
    always re-measured.  Reports written before probe_status existed only list
    finished probes, so a stopped probe there shows up as missing.
    """
    if summary.get("method") != SensitivityProbe.method:
        raise ValueError("not a sensitivity-probe report")
    pair = summary.get("pair")
    if pair not in AUTO_PID_PAIRS:
        raise ValueError(f"unknown pair {pair!r}")
    original = tuple(float(summary["original_gains"][g]) for g in AUTO_PID_GAINS)
    sens = summary.get("sensitivity") or {}
    j_base = float(sens.get("J_base", math.nan))
    status = summary.get("probe_status") or {}
    previous_carried = summary.get("carried_probes") or {}
    carried: Dict[str, dict] = {}
    rerun: List[str] = []
    for index in AUTO_PID_PAIRS[pair]:
        name = AUTO_PID_GAINS[index]
        for scale in SENSITIVITY_SCALES:
            stage = f"probe_{name}_x{scale:g}"
            value = float(((sens.get("per_gain") or {}).get(name, {}).get("J_phase") or {})
                          .get(f"x{scale:g}", math.nan))
            state = status.get(stage)
            if state is not None and state.startswith("carried") and stage in previous_carried:
                carried[stage] = dict(previous_carried[stage])
            elif ((state == "complete" or (state is None and math.isfinite(value)))
                  and math.isfinite(j_base) and j_base > 0):
                carried[stage] = {"delta_rel": value / j_base - 1.0, "J_phase": value,
                                  "J_base": j_base, "source": source}
            else:
                rerun.append(stage)
    return {"pair": pair, "original": original, "rerun": rerun, "carried": carried,
            "source": source}


SERIAL_LOST = "HOST,SERIAL_LOST"  # Host-generated; never sent by the firmware.


class SerialReader(threading.Thread):
    def __init__(self, connection: serial.Serial, messages: queue.Queue[str]) -> None:
        super().__init__(daemon=True)
        self.connection = connection
        self.messages = messages
        self.stop_event = threading.Event()

    def run(self) -> None:
        errors = (OSError,) + ((serial.SerialException,) if serial is not None else ())
        while not self.stop_event.is_set():
            try:
                raw = self.connection.readline()
            except errors as exc:
                # USB unplugged or port closed: let the GUI thread disconnect.
                if not self.stop_event.is_set():
                    self.messages.put(f"{SERIAL_LOST},{exc}")
                return
            if raw:
                self.messages.put(raw.decode("ascii", errors="replace").strip())


def config_command(cfg: SingleLungConfig, token: int) -> str:
    # Only physical/structural limits; tunable values have no upper cap.
    if not all(math.isfinite(v) and v >= 0 for v in (
            cfg.hardware_expiration_kp, cfg.hardware_expiration_ki, cfg.hardware_expiration_kd)):
        raise ValueError("Invalid expiration PI/PID gains")
    if not all(math.isfinite(v) and v > 0 for v in (
            cfg.pi.integral_limit, cfg.hardware_expiration_integral_limit)):
        raise ValueError("Both phase integral limits must be positive")
    if not (math.isfinite(cfg.hardware_inspiration_min_v)
            and 0 <= cfg.hardware_inspiration_min_v < cfg.lung.hardware_closure_v):
        raise ValueError("Inspiration PPV minimum voltage must be within [0, closure) V")
    p = cfg.lung
    values = (p.hardware_model_volume_ml, p.hardware_gamma_in, p.hardware_gamma_out,
              p.hardware_dead_zone_in_v, p.hardware_dead_zone_out_v, p.hardware_closure_v)
    if not all(math.isfinite(v) for v in values) or not (
        cfg.target_volume_ml <= p.hardware_model_volume_ml
        and p.hardware_gamma_in > 0 and p.hardware_gamma_out > 0
        and 0 <= p.hardware_dead_zone_in_v < p.hardware_closure_v <= 5
        and 0 <= p.hardware_dead_zone_out_v < p.hardware_closure_v
        and p.hardware_closure_v - p.hardware_dead_zone_in_v > ATM_JOIN_DEPTH_V
        and p.hardware_closure_v > 3.5
    ):
        raise ValueError("Invalid empirical directional PPV model")
    calibration = (p.hardware_flow_offset_slm, p.hardware_flow_gain, p.hardware_vac_leak_slm)
    if not all(math.isfinite(v) for v in calibration) or not (
        calibration[1] > 0 and calibration[2] >= 0
    ):
        raise ValueError("Invalid hardware flow calibration")
    if cfg.dead_time_s != 0:
        raise ValueError("Fixed dead time is disabled; use PPV positioning")
    times = (cfg.smith.ppv_open_time_s, cfg.smith.ppv_close_time_s)
    if not all(math.isfinite(t) and t > 0 for t in times):
        raise ValueError("PPV positioning requires positive measured stroke times")
    if not (math.isfinite(cfg.smith.delay_s) and 0 <= cfg.smith.delay_s <= SMITH_DELAY_MAX_S):
        raise ValueError("Smith residual delay must be within [0, 1] s (firmware buffer)")
    if cfg.smith.enabled and min(times) <= 0:
        raise ValueError("Measure both PPV stroke times before enabling hardware Smith")
    dwell, overclose = cfg.hardware_transition_dwell_min_s, cfg.hardware_transition_overclose_v
    if not (math.isfinite(dwell) and dwell >= 0):
        raise ValueError("Transition minimum dwell must be non-negative")
    exp_closure = cfg.hardware_expiration_closure_v
    if not (math.isfinite(exp_closure) and p.hardware_dead_zone_out_v < exp_closure <= 5):
        raise ValueError("Expiration closure must be above the VAC dead zone and <= 5 V")
    if not (math.isfinite(overclose) and overclose >= 0
            and exp_closure + overclose <= 5):
        raise ValueError("Transition over-close must be non-negative and keep target <= 5 V")
    insp_dwell = cfg.hardware_inspiration_dwell_min_s
    if not (math.isfinite(insp_dwell) and insp_dwell >= 0):
        raise ValueError("Inspiration minimum dwell must be non-negative")
    settle = cfg.hardware_transition_settle_s
    if not (math.isfinite(settle) and settle >= 0):
        raise ValueError("Transition settle after PPV arrival must be non-negative")
    return ",".join(map(str, [
        "CFG",
        PROTOCOL_VERSION,
        token,
        cfg.respiratory_rate_bpm,
        cfg.ie_inspiration,
        cfg.ie_expiration,
        cfg.target_volume_ml,
        cfg.target_fill_time_s,
        int(round(cfg.dead_time_s * 1000.0)),
        cfg.pi.kp,
        cfg.pi.ki,
        cfg.pi.kd,
        cfg.pi.integral_limit,
        cfg.precondition_s,
        cfg.max_abs_flow_slm,
        cfg.max_lung_volume_ml,
        cfg.lung.flow_sign,
        cfg.lung.hardware_model_volume_ml,
        cfg.lung.tau_in_s,
        cfg.lung.tau_out_s,
        cfg.lung.hardware_gamma_in,
        int(cfg.smith.enabled),
        cfg.smith.delay_s,
        cfg.smith.correction_gain,
        cfg.lung.hardware_gamma_out,
        cfg.lung.hardware_dead_zone_in_v,
        cfg.lung.hardware_dead_zone_out_v,
        cfg.lung.hardware_closure_v,
        cfg.smith.ppv_open_time_s,
        cfg.smith.ppv_close_time_s,
        p.hardware_flow_offset_slm,
        p.hardware_flow_gain,
        p.hardware_vac_leak_slm,
        int(cfg.hardware_ff_inspiration_enabled),
        0,  # Expiration always uses PI without feedforward.
        cfg.hardware_expiration_integral_limit,
        cfg.hardware_expiration_kp,
        cfg.hardware_expiration_ki,
        0.0,  # Expiration PI: derivative disabled.
        cfg.hardware_inspiration_min_v,
        dwell,
        overclose,
        int(cfg.dead_time_within_cycle),
        insp_dwell,
        settle,
        exp_closure,
    ]))


TIMING_DEVIATION_WARN = 0.03
WITHIN_CYCLE_MAX_EXP_FLOW_SLM = 6.0
WITHIN_CYCLE_MIN_EXP_S = 1.5
DEEP_INSPIRATION_MIN_V = 4.0
SURGE_DWELL_HINT_S = 1.2
# Protocol 19 DEAD_TO_EXP = close stroke from the inspiration end + settle.
# Typical inspiration end for the host estimate (10 bpm 1:2 runs, 2026-10-22);
# deep ends (3.0-3.2 V) wait longer in the firmware.
TYPICAL_INSPIRATION_END_V = 3.7
# Measured PPV lag beyond the stroke model; a shorter settle risks VAC opening
# on a still-open PPV (34-50 SLM surges with a fixed 0.6 s dwell).
SURGE_SETTLE_HINT_S = 0.2


def expiration_transition_estimate_s(dwell_min_s: float, settle_s: float, target_v: float,
                                     close_stroke_s: float) -> float:
    """Host estimate of DEAD_TO_EXP: modelled stroke from a typical inspiration
    end to the close target plus the settle, at least the optional floor."""
    travel = max(0.0, target_v - TYPICAL_INSPIRATION_END_V) * close_stroke_s / 4.2
    return max(dwell_min_s, travel + settle_s)


def cfg_expiration_transition_s(cfg: SingleLungConfig) -> float:
    return expiration_transition_estimate_s(
        cfg.hardware_transition_dwell_min_s, cfg.hardware_transition_settle_s,
        cfg.hardware_expiration_closure_v + cfg.hardware_transition_overclose_v,
        cfg.smith.ppv_close_time_s)


def cycle_timing_summary(rr: float, ie_in: float, ie_out: float, volume_ml: float,
                         dwell_s: float, within_cycle: bool, insp_min_v: float,
                         insp_dwell_s: float = 0.0,
                         settle_s: Optional[float] = None) -> dict:
    """Host estimate of the real cycle and the operator warnings (warn only).

    dwell_s is the (estimated) DEAD_TO_EXP duration.  Without deduction every
    cycle is lengthened by both transitions; with deduction expiration shrinks.
    With settle_s (protocol 19) the surge hint checks the settle, not dwell_s.
    """
    period = 60.0 / rr
    inspiration = period * ie_in / (ie_in + ie_out)
    nominal_expiration = period - inspiration
    added = dwell_s + insp_dwell_s
    if within_cycle:
        # Protocol 21: the time left after both transitions is shared by I:E.
        actual = period
        available = max(0.5, period - added)
        inspiration = min(inspiration, max(0.25, available * ie_in / (ie_in + ie_out)))
        expiration = max(0.25, period - added - inspiration)
    else:
        actual = period + added
        expiration = nominal_expiration
    expiration_flow = 0.06 * volume_ml / max(0.25, expiration)
    deviation = actual / period - 1.0
    warnings, short = [], []
    if abs(deviation) > TIMING_DEVIATION_WARN:
        short.append(f"Cycle {deviation:+.0%} longer than set (dwell added)")
        warnings.append(
            f"Actual cycle is {deviation:+.0%} vs the set {period:.2f} s: the transition "
            "dwell is added to every cycle (enable 'Deduct transition from expiration' "
            "to keep 60/bpm).")
    if within_cycle and expiration_flow > WITHIN_CYCLE_MAX_EXP_FLOW_SLM:
        short.append(f"Expiration reference {expiration_flow:.1f} SLM is high")
        warnings.append(
            f"Expiration reference {expiration_flow:.1f} SLM > {WITHIN_CYCLE_MAX_EXP_FLOW_SLM:g} SLM "
            "after deducting the dwell; the v8 opening cap may not reach it.")
    if within_cycle and expiration < WITHIN_CYCLE_MIN_EXP_S:
        short.append(f"Expiration only {expiration:.2f} s")
        warnings.append(
            f"Expiration after deducting the dwell is {expiration:.2f} s "
            f"(< {WITHIN_CYCLE_MIN_EXP_S:g} s).")
    if insp_min_v < DEEP_INSPIRATION_MIN_V:
        short.append(f"PPV min {insp_min_v:.1f} V: onset-surge risk")
        warnings.append(
            f"Inspiration PPV minimum {insp_min_v:.2f} V < {DEEP_INSPIRATION_MIN_V:g} V: the "
            "end of inspiration may be pushed deep, increasing expiration-onset surge risk.")
        if settle_s:  # Protocol 19 settle in use (0 = fixed dwell only).
            if settle_s < SURGE_SETTLE_HINT_S:
                short.append(f"Settle {settle_s:.2f} s < {SURGE_SETTLE_HINT_S:g} s PPV lag")
                warnings.append(
                    f"Transition settle {settle_s:.2f} s is shorter than the measured PPV lag "
                    f"({SURGE_SETTLE_HINT_S:g}-0.3 s): VAC may open on a still-open PPV after a "
                    "deep inspiration end (34-50 SLM surges with a fixed 0.6 s dwell).")
        elif dwell_s < SURGE_DWELL_HINT_S:
            short.append(f"Dwell {dwell_s:.2f} s < {SURGE_DWELL_HINT_S:g} s for a deep end")
            warnings.append(
                f"With a deep inspiration end, a dwell of {dwell_s:.2f} s (< {SURGE_DWELL_HINT_S:g} s) "
                "still showed a 17.8 mL onset surge (run 1114002333, cycle 2, from 3.000 V).")
    return {
        "period_s": period, "actual_period_s": actual, "actual_bpm": 60.0 / actual,
        "deviation": deviation, "inspiration_s": inspiration, "expiration_s": expiration,
        "expiration_flow_slm": expiration_flow, "warnings": warnings,
        "short_warnings": short,
        "period_alert": abs(deviation) > TIMING_DEVIATION_WARN,
    }


def flatten_parameters(values: dict, prefix: str = "config") -> dict:
    result = {}
    for key, value in values.items():
        name = f"{prefix}.{key}"
        if isinstance(value, dict):
            result.update(flatten_parameters(value, name))
        else:
            result[name] = value
    return result


def save_log(rows: List[HardwareTelemetry], path: Path, metadata: Optional[dict] = None,
             *, include_context: bool = True) -> None:
    """Self-contained CSV: all transmitted runtime fields and frozen parameters.

    ``include_context=False`` writes telemetry columns only (run_id is one of
    them); configuration, objective and evaluation then live only in the JSON
    pair.  No analysis script reads the repeated CSV context columns.
    """
    metadata = metadata or {}
    if not include_context:
        fields = list(HardwareTelemetry.__dataclass_fields__) + ["time_s", "state_name", "control_flow_slm"]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({**asdict(row), "time_s": row.time_ms / 1000.0,
                                 "state_name": STATE_NAMES.get(row.state, "UNKNOWN"),
                                 "control_flow_slm": row.control_flow_slm})
        return
    context = flatten_parameters(metadata.get("configuration", {}))
    context.update({
        "objective_version": OBJECTIVE["version"],
        "objective_formula": OBJECTIVE["formula"],
        "objective_json": json.dumps(OBJECTIVE, ensure_ascii=False),
        "auto_pid_stage": metadata.get("auto_pid_stage"),
        "auto_pid_settings_json": json.dumps(metadata.get("auto_pid_settings")),
        "run_status": metadata.get("run_status", "unknown"),
        "score_status": metadata.get("score_status", "not_evaluated"),
        "score_error": metadata.get("score_error"),
        "configuration_source": metadata.get("configuration_source", "unavailable"),
        "protocol_version": PROTOCOL_VERSION,
    })
    for name in ("score", "J_Q", "J_V", "J_Q_in", "J_Q_out", "N_in", "N_out", "N_c",
                 "weight_flow", "weight_volume", "mean_tidal_ml", "mean_tidal_error_pct",
                 "flow_rmse_slm", "flow_mae_slm", "saturation_fraction"):
        context[f"evaluation.{name}"] = (metadata.get("metrics") or {}).get(name)
    for name, value in (metadata.get("metrics") or {}).items():
        context[f"evaluation.{name}"] = value
    fields = list(HardwareTelemetry.__dataclass_fields__) + ["time_s", "state_name", "control_flow_slm"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields + list(context))
        writer.writeheader()
        for row in rows:
            writer.writerow({**asdict(row), "time_s": row.time_ms / 1000.0,
                             "state_name": STATE_NAMES.get(row.state, "UNKNOWN"),
                             "control_flow_slm": row.control_flow_slm, **context})


def output_dir() -> Path:
    """Default folder for run logs and Auto PID reports.  LUNGLOGIC_OUTPUT_DIR
    overrides it (the test suite points it at a temporary directory)."""
    override = os.environ.get("LUNGLOGIC_OUTPUT_DIR")
    return Path(override) if override else SCRIPT_DIR / "output"


def displayable_minimum(low: float, decimals: int) -> float:
    """Qt rounds a spin-box range to its decimals, so a positive minimum such
    as 0.01 shown with 1 decimal became 0 and allowed 0 bpm / 0 I:E (division
    by zero).  Keep positive minima at the smallest displayable step."""
    return max(low, 10.0 ** -decimals) if low > 0 else low


def resolve_log_path(raw_name: str, run_id: int) -> Path:
    """Resolve a user-entered CSV name relative to the application directory."""
    raw_name = raw_name.strip()
    if not raw_name:
        return output_dir() / f"single_lung_hardware_run_{run_id}.csv"
    path = Path(raw_name).expanduser()
    if not path.is_absolute():
        path = (output_dir() / path) if path.parent == Path(".") else (SCRIPT_DIR / path)
    if path.suffix.lower() != ".csv":
        path = path.with_suffix(".csv")
    return path


def with_run_id(path: Path, run_id: int) -> Path:
    """Append _<run_id> to a user-chosen name unless it already ends with it."""
    suffix = f"_{run_id}"
    return path if path.stem.endswith(suffix) else path.with_name(f"{path.stem}{suffix}{path.suffix}")


def auto_pid_log_path(path: Path, stage: str, run_id: int) -> Path:
    """Give every Auto PID candidate its own CSV/JSON pair."""
    safe_stage = "".join(char if char.isalnum() else "_" for char in stage).strip("_")
    safe_stage = safe_stage or "candidate"
    return path.with_name(f"{path.stem}_{safe_stage}_{run_id}{path.suffix}")


class FlowCanvas(FigureCanvas):
    """Live flow plot.  Artists are created once and only their data change:
    clearing the axes and re-running tight_layout on every refresh cost about
    0.3 s per draw, slower than telemetry arrives, so the display fell behind."""

    def __init__(self) -> None:
        self.figure = Figure(figsize=(7.5, 4.0))
        super().__init__(self.figure)
        ax = self.ax = self.figure.add_subplot(111)
        ax.set_title("Hardware Flow Rate")
        ax.set_xlabel("Time / s")
        ax.set_ylabel("Flow / SLM")
        ax.grid(True, ls="--", alpha=0.3)
        self.measured_line, = ax.plot([], [], label="Calibrated sensor", lw=1.8)
        self.net_line, = ax.plot([], [], label="Estimated lung net", lw=1.3)
        self.reference_line, = ax.plot([], [], "k--", label="Reference", lw=1.0)
        self.now_line = ax.axvline(0.0, color="#d62728", lw=1.0, visible=False)
        self.legend = ax.legend(loc="upper right", ncol=2, fontsize=8)
        self.legend.set_visible(False)
        self._needs_layout = True
        self.draw_rows([])

    def resizeEvent(self, event) -> None:
        self._needs_layout = True
        super().resizeEvent(event)

    def draw_rows(self, rows: List[HardwareTelemetry], window_s: float = 10.0) -> None:
        ax = self.ax
        if rows:
            now = rows[-1].time_ms / 1000.0
            start_time = max(0.0, now - window_s)
            first = len(rows)
            while first > 0 and rows[first - 1].time_ms / 1000.0 >= start_time:
                first -= 1
            view = rows[first:]
            t = [row.time_ms / 1000.0 for row in view]
            measured = [row.flow_slm for row in view]
            net = [row.control_flow_slm for row in view]
            reference = [row.flow_reference_slm for row in view]
            self.measured_line.set_data(t, measured)
            self.net_line.set_data(t, net)
            self.reference_line.set_data(t, reference)
            self.now_line.set_xdata([now, now])
            ax.set_xlim(start_time, max(start_time + window_s, now + 0.2))
            values = [v for v in measured + net + reference if math.isfinite(v)] or [0.0]
            ax.set_ylim(min(-2.0, min(values) - 2.0), max(2.0, max(values) + 2.0))
        else:
            for line in (self.measured_line, self.net_line, self.reference_line):
                line.set_data([], [])
            ax.set_xlim(0.0, window_s)
            ax.set_ylim(-2.0, 2.0)
        self.now_line.set_visible(bool(rows))
        self.legend.set_visible(bool(rows))
        if self._needs_layout and self.width() > 1 and self.height() > 1:
            # Layout only after a resize; tick labels change little while running.
            self.figure.tight_layout()
            self._needs_layout = False
        self.draw_idle()


class HardwareLungCanvas(LungModelCanvas):
    """Render the existing dynamic model using one real telemetry frame."""

    def draw_telemetry(self, row: HardwareTelemetry, cfg: SingleLungConfig) -> None:
        class Frame:
            pass

        frame = Frame()
        frame.state = [row.state]
        frame.time_s = [row.time_ms / 1000.0]
        frame.cycle = [row.cycle]
        frame.valve_mask = [row.valve_mask]
        frame.volume_ml = [row.volume_ml]
        frame.flow_slm = [row.flow_slm]
        frame.opening = [row.opening]
        # Supply the hardware state map because RECOVERY is a firmware-only
        # state and is intentionally absent from the simulation state table.
        self.draw_frame(frame, 0, cfg, state_names=STATE_NAMES)
        self.ax.set_title(
            "Single-Lung Live Hardware Dynamic Model",
            fontsize=15,
            fontweight="bold",
            pad=10,
        )
        self.draw_idle()


class HardwareGuideDialog(QtWidgets.QDialog):
    """Operator and developer guidance kept inside the hardware application."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Single-Lung Hardware Modeling and Control Guide")
        self.resize(1050, 760)
        layout = QtWidgets.QVBoxLayout(self)
        tabs = QtWidgets.QTabWidget()
        pages = [
            ("System & Setpoints", """
                <h2>Physical hardware sequence and setpoints</h2>
                <p>This application controls one physical lung bag, two routing
                solenoids and one proportional pinch valve (PPV). Unlike the digital
                twin, it does <b>not</b> generate a simulated lung response: the
                Dynamic Model is animated from STM32 telemetry.</p>
                <table><tr><th>Phase</th><th>Physical action</th></tr>
                <tr><td>Precondition</td><td>Vacuum route prepares a repeatable start.</td></tr>
                <tr><td>Inspiration</td><td>Atmosphere open, vacuum closed; measured flow should be positive.</td></tr>
                <tr><td>Expiration</td><td>Vacuum open, atmosphere closed; measured flow should be negative.</td></tr>
                <tr><td>Direction change</td><td>Both routing valves close until PPV reaches its estimated next-phase position.</td></tr></table>
                <h3>Timing and flow references</h3>
                <p>Sensor polling, PI and Smith run at 100 Hz (10 ms).
                Telemetry is sent at 50 Hz (20 ms), over 115200 baud.
                Screen refresh runs independently at 10 Hz.</p>
                <pre>Tcycle = 60 / respiratory rate
Tin = Tcycle × I / (I + E)       Tout = Tcycle × E / (I + E)
Qref,in = 0.06 × Vtarget / Tfill
Qref,out = 0.06 × Vtarget / actual expiration duration (minimum 0.25 s)</pre>
                <p><b>Protocol 21 timing:</b> with transition deduction enabled,
                subtract the measured inspiration transition and the previous
                cycle's expiration-transition estimate from the requested period,
                then share the available active time by I:E. Inspiration flow uses
                min(Tfill, active inspiration time). At VAC opening, expiration
                takes the remaining cycle time, floored at 0.25 s. Cycle 0 excludes
                startup positioning. Without deduction the nominal active I:E
                phases are retained and positioning adds time to the period.</p>
                <p>User Mode contains routine setpoints. Developer Mode contains
                controller and identified plant parameters. User Mode derives
                inspiration flow from tidal volume and the inspiration phase;
                expiration flow is derived in firmware. Neither SLM reference
                is an independent User Mode setting.</p>
                <p class='warn'><b>Bench prototype only:</b> verify drivers, flyback
                protection, shared ground, tubing and emergency stop. This is not a
                medical device and must not be connected to a person.</p>
            """),
            ("Physical Parameters", """
                <h2>Measured lung and valve parameters</h2>
                <p>These values describe the assembled pneumatic path used by model
                feedforward and the Smith predictor. They are not convenient tuning
                knobs and should come from repeatable bench identification.</p>
                <table><tr><th>Parameter</th><th>Meaning and identification source</th></tr>
                <tr><td>Volume guard / mL</td><td>Configured integrated-volume guard; firmware adds a 50 mL safety margin. Separate from empirical model amplitude and not a physical capacity measurement.</td></tr>
                <tr><td>τin / s</td><td>Inspiratory first-order time constant fitted to measured filling data.</td></tr>
                <tr><td>τout / s</td><td>Expiratory first-order time constant fitted to measured evacuation data.</td></tr>
                <tr><td>PPV exponent γ</td><td>Nonlinear opening-to-flow shape fitted from PPV sweep data.</td></tr>
                <tr><td>Direction dead time</td><td>No fixed dead time; estimated PPV travel determines switching duration.</td></tr>
                <tr><td>Precondition time</td><td>Operational preparation time, chosen long enough for a repeatable initial volume.</td></tr></table>
                <p>The firmware uses a first-order internal model for feedforward and
                Smith prediction. A fitted value is still an approximation; keep the
                raw data, fitting method, test configuration and uncertainty.</p>
                <p>Current reference: vacuum magnitude 40–45 kPa; independent
                inspiration/expiration gamma and plateau-end voltages. Opening
                is an effective model coordinate, not 1 − voltage/5.
                The fitted 320.3 mL model amplitude is separate from the
                integrated-volume guard. It is not a verified physical capacity.</p>
                <p class='warn'><b>Parameter protection:</b> identified capacity,
                time constants, valve exponent and delay values are locked by default.
                Check <i>Re-edit measured modeling parameters</i> only when a new
                calibration has justified changing them.</p>
            """),
            ("Measured Delays", """
                <h2>Real delays, not a delay simulation</h2>
                <p>The hardware application does not insert command-to-lung,
                pneumatic-transport or sensor delays. Those delays already exist in
                the physical valves, tubing, electronics, sensor and sampling path.
                Values used for compensation must therefore be measured on the
                assembled system.</p>
                <table><tr><th>Quantity</th><th>How it is used</th></tr>
                <tr><td>Command-to-flow delay</td><td>Time from timestamped PPV/solenoid command to the first repeatable physical flow response.</td></tr>
                <tr><td>Sensor/acquisition delay</td><td>Sensor response, filtering, sampling and serial timing verified from synchronized records.</td></tr>
                <tr><td>Smith feedback delay</td><td>Residual transport/sensor latency only; PPV travel is modelled separately. Do not enter full-stroke time here.</td></tr>
                <tr><td>Direction dead time</td><td>Fixed time disabled; wait for estimated PPV positioning with both routes closed.</td></tr></table>
                <h3>Measurement rule</h3>
                <ol><li>Use synchronized command and sensor timestamps.</li>
                <li>Repeat both inspiration and expiration tests across the intended operating range.</li>
                <li>Report mean, spread and test conditions; do not select one favorable trace.</li>
                <li>Keep Smith disabled until the model and effective feedback delay are credible.</li></ol>
                <p class='warn'>Once accepted, delay-related fields remain locked.
                Re-identify after changing tubing, valves, sensor filtering, firmware
                sampling, load or any component that can alter timing.</p>
            """),
            ("PID, FF, Smith & Auto", """
                <h2>2026-10-14 PPV sweep</h2>
                <p>Measured full-open effective tau_in = 0.5853 s, tau_out = 0.2549 s.
                These use the new 0 V no-delay fits; the power-law curve is approximate.
                Model amplitude = 320.3 mL (fitted); volume guard stays separate. For each direction,
                a = clip((4.30 - U)/(4.30 - Ud), 0, 1), conductance = a^gamma,
                tau(U) = tau0 / a^gamma. ATM: Ud=4.1200 V, gamma=1.7861;
                VAC: Ud=4.1535 V, gamma=1.9915. a is an effective model coordinate,
                not a measured geometric opening.</p>
                <p>Fixed-voltage experiments were preset: no PPV motion delay was
                fitted. The existing 0-to-4.2 V / 2.436 s stroke model remains unchanged.
                The afternoon high-voltage fit uses 26 accepted segments.
                Maximum fitted tau deviations are 5.4% ATM and 4.9% VAC, within
                4.200-4.251 V ATM and 4.200-4.253 V VAC only. Lower voltages
                extrapolate this fit; full-open tau and model amplitude are retained.
                The earlier 4.20 V results disagree with this batch.
                ATM feedforward inverts the continuous conductance model;
                VAC feedforward and derivative action are disabled.
                4.30 V is the near-closed observation used as the model endpoint,
                not a verified exact seal threshold. Dynamic switching is unvalidated.
                Normal STOP uses recovery; faults close the routes and command 5 V.</p>
                <h2>Flow and VAC bypass calibration</h2>
                <p>Calibrated flow = 1.0471 * (raw flow - 0.0034 SLM), before
                applying the configured sensor direction. One gain is used for
                both directions and PPV voltages. During VAC-only extraction,
                the configured 1.0122 SLM is the calibrated VAC bypass plateau.
                This uses a fitted plateau and a provisional linear roll-off
                over the final 0.041 V below the configured VAC closure
                (4.279–4.320 V at the 4.32 V reference closure)
                before adding it to negative calibrated flow, capped at zero.
                ATM and closed-valve samples receive no leakage correction. The
                small closed-state residual is not subtracted.</p>
                <p>PID, Smith, per-cycle exchange telemetry and scoring use
                estimated net lung flow. The safety volume estimate, previous-cycle
                residual and expiration safety integral use uncompensated calibrated
                extraction; the flow-limit check also uses calibrated sensor flow. Feedforward
                and the flow reference remain on the lung-net basis: do not add
                leakage a second time. Raw, calibrated and estimated net flows
                are exported separately.</p>
                <p>Protocol v21 requires matching firmware. Developer Mode has
                an optional Inspiration FF (ATM open) checkbox. Expiration uses PI only;
                its FF and derivative controls are disabled. Inspiration FF
                also controls pre-positioning: off starts at zero opening.
                Apply changes with Send Config or Start. Calibration fields
                are locked with the measured model parameters. Set VAC leakage
                to zero to disable that compensation. Calibration spike exclusion
                was only for offline statistics; live measurements are not blanked.</p>
                <h2>Flow controller</h2>
                <pre>e = Qref − |Qfeedback|
u = clip(uff + Kp e + Ki ∫e dt + Kd(−d|Qfeedback|/dt), 0, 1)
Inspiration: u = conductance relative to 0 V; Ucmd = G⁻¹(u), continuous, u = 1 → 0 V
  G = ((Uc − U)/(Uc − Ud))^γ for Uc − U ≤ 0.10 V (PM fit),
  deeper: measured 2026-10-14 AM sweep shifted +0.05 V, joined continuously
Expiration: Ucmd = Uc − u(Uc − Ud,out) (v8 model opening, capped ≤ 0.4707)</pre>
                <p>Protocol v15 removes the former jump from the plateau end
                (about 4.12 V) to 0 V: inspiration PI now passes through the
                measured 4.2–0 V range smoothly and reaches 0 V only when full
                conductance is requested. Inspiration FF is Qref divided by the
                0 V model flow. Gains are per unit of relative conductance.
                Developer Mode sets the lowest inspiration voltage (default
                3.0 V; 0 allows full open). It becomes the PI and FF upper
                limit u_max = G(min V), so anti-windup remains consistent.</p>
                <p>Conditional integration limits windup.
                Each phase has its own gains and integral limit in SLM s:
                inspiration 12, expiration 8 in the hardware reference file.
                Expiration restores the v8 scheme: PI in a linear effective-opening
                coordinate (inspiration instead uses relative conductance),
                no FF, constant reference, VAC open for the whole timed phase.
                Its opening cap is 0.1883 for 250 ms, ramps to 0.3451 at 750 ms
                and 0.4707 at phase end (about 4.2724/4.2499/4.2310 V with
                Uc 4.30 V, Ud 4.1535 V). There is no flow guard, slew limit,
                taper or volume-budget closure.
                Model feedforward supplies an opening estimate; PID corrects residual error. The Smith predictor
                uses command-driven and estimated-position-driven lung models.
                PID feedback = measured net flow + weight * (ideal - actuator model flow).
                Per-cycle exchange integration and Auto PID scoring use measured net flow only.
                At every direction change both solenoids close, the PPV moves to
                the inspiration FF position before ATM opens. Before expiration, PPV
                moves from its inspiration position to zero opening (closure
                voltage plus optional over-close), approaching from the open side.
                VAC opens after estimated arrival plus the configured settle and
                minimum close-command dwell; it then returns to VAC closure and
                PI starts from zero opening. ATM also waits for estimated arrival
                and its minimum closed-route dwell. Reference dwell settings are
                1.0 s before VAC, 0.35 s before ATM, settle 0 s, over-close 0 V.
                Travel time is abs(target V - estimated V) / (4.2 / stroke time).
                Position is estimated, not measured. Transition deduction determines
                whether positioning shortens the active phases or adds cycle time.</p>
                <h3>Optional bounded Auto PID</h3>
                <p>Every run is scored with the unchanged objective J = 0.65 J_Q +
                0.35 J_V, where J_Q averages the normalized inspiration and expiration
                flow errors. Complete cycles are required; volume uses integrated
                measured net flow. Kd, integral limits and all locked model/delay
                parameters stay fixed. Each gain remains within 0.25x to 4x its
                starting value. Saturation is diagnostic only.</p>
                <p><b>Default method: discrete extremum seeking</b> (Killingsworth and
                Krstić 2006, equations (5)-(7)). One iteration is one complete hardware
                run; J(k) is computed when the run ends, the unscored warm-up cycle
                plays the role of the paper's t0.</p>
                <pre>ζ(k) = −h ζ(k−1) + J(k−1)
θ̂(k+1) = θ̂(k) − γ α d(k) [J(k) − (1+h) ζ(k)]
θ(k+1) = θ̂(k+1) + α d(k+1)</pre>
                <p><b>One pair per round.</b> Select Tune = Inspiration pair
                (kp_in cos, ki_in sin) or Expiration pair (kp_out cos, ki_out sin);
                either pair uses ω = aπ, a = 0.8 and h = 0.5 as in the paper. The
                inspiration round minimises J_ins = 0.65·0.5·J_Q_in + 0.35·J_V, the
                expiration round J_out = 0.65·0.5·J_Q_out (J_ins + J_out = J). Each
                pair only acts in its own phase, so its optimum is the same as for
                the full J, without the other phase's run-to-run noise. Runs:
                N_base baselines (default 3) + N iterations + 1 confirmation. The
                baselines give J_base (mean) and the noise floor of J_ins, J_out and
                J. Run the expiration round afterwards; it starts from the gains the
                inspiration round loaded. Implementation choices that
                are not in the paper: the update works on θ/θ0 (α_i = α_rel θ0_i,
                γ_i = γ_rel); θ and θ̂ are bounded to [0.25, 4] θ0; the baseline
                initialises the filter (ζ(−1) = J_base/(1+h), J(−1) = J_base); J is
                clipped to 3 J_base and an aborted or unscorable run counts as
                3 J_base (three in a row, FAULT or ERR stop the search). The result is
                θ̂(N), loaded only if the confirmation run improves the round's phase
                cost by more than 3x its measured noise and the full J is not worse
                than its noise; otherwise this pair keeps its original gains.
                α_rel and γ_rel defaults are uncalibrated starting points; the
                sensitivity pre-experiment suggests values (never applied).</p>
                <p><b>Expiration round:</b> refused (unless the health check is
                ignored) when a baseline has more than 20% of expiration pinned at the
                v8 opening cap, where the expiration gains have little gradient. The
                high-pass filter shifts the phase by about 44° at 0.8π; the optional
                demodulation phase (the paper's Fig. 1 φ) is a deviation from the
                paper's equation (6) and is off by default. Coordinate search
                (one gain perturbed per candidate, not the paper's equations) remains
                available for comparison.</p>
                <h3>First-time use</h3>
                <ol><li>Before any ES round, check "Sensitivity pre-experiment only"
                and start: N_base identical baselines, then each gain of the selected
                pair at x0.6 and x1.4 (all other gains unchanged). Gains are not
                changed.</li>
                <li>The summary gives, per gain, ΔJ_phase, g = |J(x1.4) − J(x0.6)| / 0.8
                per unit θ/θ0, the signal-to-noise ratio against the measured noise
                floor, and suggested α_rel (dither effect ≥ 3x noise, at most 0.25)
                and γ_rel (step_statistics rule). Suggestions are never applied.</li>
                <li>If even α_rel = 0.25 cannot lift the dither effect above 3x the
                noise floor for a gain, ES cannot measure its gradient: do not run ES
                on that pair.</li>
                <li>If the tidal-volume error exceeds the health limit, fix the tidal
                volume first (operating point, fill time, volume settings). Do not
                use "Ignore health check" to get past it.</li>
                <li>After an ES round, read acceptance and step_statistics in the
                summary JSON; then run the expiration round from the loaded gains.</li></ol>
                <p>Each run adds one unscored warm-up cycle; Cycles keeps its
                meaning (scored cycles). The next run starts after DONE, which
                the firmware sends once recovery has ended; recovery timeouts are
                counted and the following run is flagged as possibly not comparable.
                Auto PID refuses to start (unless overridden) when the baseline
                misses tidal volume by more than 10% or extracts more than 10 mL in
                the first 0.2 s after VAC opens; when the expiration pair is tuned it
                also refuses a baseline pinned at the v8 expiration cap for more than
                50% of expiration samples or emptying less than 80% of the bag.
                saturation_fraction only detects a fully open PPV;
                pinned_fraction_exp detects the cap.</p>
                <p class='warn'>Use at least three repeatable cycles per candidate.
                Stop, disconnect or any fault cancels the sequence. Auto PID cannot
                recover missing pneumatic capacity or compensate for an incorrect
                model, incorrect delay, sensor fault or fully open PPV.</p>
            """),
            ("Calibration", """
                <h2>Identification-to-hardware workflow</h2>
                <ol><li>Start from a safe, manually verified hardware configuration
                with Auto PID and Smith disabled.</li>
                <li>Collect repeatable open-loop or conservative closed-loop records;
                measure delays and fit Vmax, τin, τout and γ with uncertainty.</li>
                <li><b>Before changing hardware control, enter the measured candidates
                into single_lung_pid_gui.py and run the digital twin.</b> Inspect tidal
                volume, flow tracking, saturation, timing and sensitivity to plausible
                parameter ranges.</li>
                <li>If simulation exposes saturation, instability or poor robustness,
                revise the operating point or controller before a physical trial.</li>
                <li>Return to Developer Mode, check <i>Re-edit measured modeling
                parameters</i>, enter the reviewed values, send the configuration,
                then lock the fields again.</li>
                <li>Validate first with a short conservative run. Compare Smith on/off at the same PI gains. Enable Auto PID after baseline results are repeatable.</li></ol>
                <h3>Traceability and troubleshooting</h3>
                <p>Save hardware CSV logs and record calibration date, hardware
                revision, tubing, sensor settings and fitted parameters. Recalibrate
                after physical changes. ERR,CFG_VALUE indicates invalid configuration;
                FAULT closes outputs. Flat or implausible flow requires stopping and
                checking sensor orientation, zero, tubing and wiring.</p>
            """),
        ]
        # Guide-only additions. Do not change hardware controls or defaults here.
        applied = parent.cfg if parent is not None and hasattr(parent, "cfg") else load_config(CONFIG_PATH)
        simulation = parent is not None and hasattr(parent, "virtual_settings")
        scope = ("<p class='warn'><b>SIMULATION / SYNTHETIC DATA:</b> this window uses a virtual "
                 "device; every curve, telemetry sample and Auto PID score is synthetic. "
                 "The shared manual describes the hardware control policy. Physical "
                 "identification claims do not validate the virtual plant.</p>" if simulation else
                 "<p><b>HARDWARE / PHYSICAL DEVICE:</b> commands go to the connected STM32 and curves "
                 "come from its sensor telemetry. The simulation is a separate application.</p>")
        current_rows = [
            ("Respiratory rate / bpm", applied.respiratory_rate_bpm),
            ("Target tidal volume / mL", applied.target_volume_ml),
            ("Cycles", applied.cycles),
            ("Inspiration Kp / Ki / integral limit", f"{applied.pi.kp:g} / {applied.pi.ki:g} / {applied.pi.integral_limit:g}"),
            ("Expiration Kp / Ki / integral limit", f"{applied.hardware_expiration_kp:g} / {applied.hardware_expiration_ki:g} / {applied.hardware_expiration_integral_limit:g}"),
            ("Tau in / out / s", f"{applied.lung.tau_in_s:g} / {applied.lung.tau_out_s:g}"),
            ("Empirical model amplitude / mL", applied.lung.hardware_model_volume_ml),
            ("Smith enabled / residual delay / weight", f"{applied.smith.enabled} / {applied.smith.delay_s:g} s / {applied.smith.correction_gain:g}"),
            ("ATM / VAC closure / V", f"{applied.lung.hardware_closure_v:g} / {applied.hardware_expiration_closure_v:g}"),
        ]
        current_table = "<h3>Applied configuration when this guide opened</h3><table>" + "".join(
            f"<tr><td>{name}</td><td>{value}</td></tr>" for name, value in current_rows) + "</table>"
        operation = """
            <h3>Current operation</h3>
            <ol><li>Select User Mode for bpm, I:E, tidal volume and cycles. Flow
            references are derived; select Developer Mode for independent phase PI,
            Inspiration FF, Smith, positioning and measured model fields.</li>
            <li>Hardware: Connect the matching protocol-21 firmware, Send Config,
            then Start. Simulation: the virtual device connects automatically;
            Send Config and Start use the same controls and validation. No physical
            serial port is accessed. Unlock measured parameters only when editing
            identification candidates; changes are local to this process.</li>
            <li>Inspect the actual period, transition warnings, measured net flow,
            raw/calibrated flow and per-cycle volume exchange. A reference is a
            target, not a measurement. Stop performs volume recovery; fault shutdown
            closes the routes and commands 5 V. D8 emergency-stop input is not used
            by this firmware.</li>
            <li>Auto PID: choose one gain pair and the algorithm, use at least three
            scored cycles, then run stabilization/baselines and inspect health checks.
            Sensitivity-probe results only suggest parameters; they never apply gains.
            Resume probe loads only missing/incomplete stages, with fresh baselines.
            Cancel restores starting gains. Keep hardware and simulated reports separate.</li>
            <li>Save CSV and its JSON pair. Compact CSV keeps telemetry only; frozen
            configuration, objective, score and events remain in JSON. Simulation files
            have a SIMULATED prefix and a simulation data-source marker, normally under
            output/simulation. Neither app copies simulation tuning into hardware defaults.</li></ol>
            <h3>Simulation scope and configuration</h3>
            <p>Run single_lung_pid_gui.py for the hardware-aligned virtual application.
            It loads single_lung_hardware_reference.json at startup; --config reads an
            explicit alternative. Settings in the two running apps do not live-sync.
            No application restart silently replaces the hardware reference. The old
            finite-fill model is retained only via --legacy and historical study APIs;
            single_lung_config.gui.json belongs to that legacy application.</p>
            <p>The virtual controller follows the directional PI, continuous ATM map,
            shifted VAC map, staged expiration cap, motion Smith correction, protocol-21
            timing, volume guard and recovery policies. Its plant is a nominal empirical
            model, not a model of every observed switching transient. Synthetic physical
            delay and sensor noise are independent simulation assumptions, applied at
            each run start; switching Smith off does not remove physical delay.
            Playback speed changes wall time only, not the 100 Hz simulated control clock.</p>
        """
        frequency = """
            <h3>Frequency analysis</h3>
            <p>100 Hz is the control update rate; 50 Hz is telemetry; bpm/60 is the
            breathing fundamental. None is a measured closed-loop bandwidth.
            No Bode, Nyquist, gain/phase margin or bandwidth calculation is built
            into this control panel. Periodic log harmonic ratios are descriptive
            tracking measures, not an identified LTI transfer function.</p>
            <p>For frequency-domain design, linearize a specified inspiration or
            expiration operating point with consistent actuator coordinates. Include
            the PI, actual plant and sensor, command/position Smith models and
            volume-dependent feedforward. Use closed-loop reference-to-output Bode
            response for bandwidth and the appropriate return-ratio Nyquist response
            for stability. Switching, rate limits and volume cutoffs limit this local
            approximation. Simulation success alone does not establish physical stability.</p>
        """
        pages[0] = (pages[0][0], scope + pages[0][1] + current_table + operation)
        pages[-1] = (pages[-1][0], pages[-1][1] + frequency)
        style = """
            <style>body{font-family:'Segoe UI',Arial;color:#24313d;font-size:10pt}
            h2{color:#17466d}h3{color:#245d80}li,p{line-height:1.4}
            table{border-collapse:collapse;width:100%}th,td{border:1px solid #cbd7e1;
            padding:6px;vertical-align:top}th{background:#eaf2f8}pre{background:#f2f6fa;
            padding:9px;font-family:Consolas,monospace}.warn{background:#fff1d6;
            padding:9px;border:1px solid #edc97d}</style>
        """
        for title, body in pages:
            page = QtWidgets.QTextBrowser()
            page.setHtml(style + body)
            tabs.addTab(page, title)
        layout.addWidget(tabs)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close, alignment=QtCore.Qt.AlignRight)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, config_path: Optional[Path] = None) -> None:
        super().__init__()
        self.setWindowTitle("LungLogic Hardware Controller | Directional PPV reference")
        self.resize(1360, 860)
        self.cfg = load_config(config_path or CONFIG_PATH)
        self.cfg.control_hz = HARDWARE_CONTROL_HZ
        self.cfg.dead_time_s = 0.0
        self.connection = None
        self.firmware_ready = False
        self.hardware_running = False
        self.reader: Optional[SerialReader] = None
        self.messages: queue.Queue[str] = queue.Queue()
        self.rows: List[HardwareTelemetry] = []
        self.inputs: Dict[str, QtWidgets.QDoubleSpinBox] = {}
        self.user_inputs: Dict[str, QtWidgets.QDoubleSpinBox] = {}
        self.metrics: Dict[str, QtWidgets.QLabel] = {}
        self.user_metrics: Dict[str, QtWidgets.QLabel] = {}
        self.log_path_text = ""
        # Where each run was first saved, and how many rows that save held:
        # later automatic saves of the same run rewrite this pair instead of
        # copying it under whatever name is typed for the next run.
        self.saved_log_paths: Dict[int, Path] = {}
        self.saved_log_rows: Dict[int, int] = {}
        self.log_name_edits: List[QtWidgets.QLineEdit] = []
        self.log_status_labels: List[QtWidgets.QLabel] = []
        self.pending_plot_row: Optional[HardwareTelemetry] = None
        self.model_drawn_state, self.model_drawn_at = -1, -math.inf
        self.last_ping = 0.0
        self.current_run_id: Optional[int] = None
        self.auto_tuner: Optional[CoordinateSearchTuner] = None
        self.auto_tune_active = False
        self.run_cfg: Optional[SingleLungConfig] = None
        self.run_stage: Optional[str] = None
        self.run_status = "not_started"
        self.run_events: List[dict] = []
        self.auto_base_cfg: Optional[SingleLungConfig] = None
        self.auto_report_path: Optional[Path] = None
        # Unscored warm-up cycles of the current run (Auto PID only).
        self.run_warmup = 0
        # Firmware sends WARN,RECOVERY_TIMEOUT before DONE; the flag of run N
        # describes the starting state of run N+1.
        self.run_recovery_timeout = False
        self.previous_recovery_timeout = False
        self.auto_recovery_timeouts = 0
        # Last scored, completed run: stands in for the Auto PID health check.
        self.last_run_metrics: Optional[Dict[str, float]] = None
        self.last_run_cfg: Optional[SingleLungConfig] = None
        self.compact_csv = False
        self.compact_csv_boxes: List[QtWidgets.QCheckBox] = []
        self.health_override_boxes: List[QtWidgets.QCheckBox] = []
        # Unscored stabilization run before the Auto PID baselines (both modes).
        self.stabilize_boxes: List[QtWidgets.QCheckBox] = []
        self.auto_stabilize_pending = False
        self.auto_stage_override: Optional[str] = None
        self.auto_stabilization_runs: List[dict] = []
        # Sensitivity probe limited to the stages missing from an earlier report.
        self.probe_resume: Optional[dict] = None
        # Auto PID pair selector and probe option exist in both modes, kept in step.
        self.pair_combos: List[QtWidgets.QComboBox] = []
        self.probe_boxes: List[QtWidgets.QCheckBox] = []
        self._build_ui()
        self._draw_dynamic_model(HardwareTelemetry(
            0, 0, STATE_IDLE, 0, 0.0, 0.0, 0.0, 0.0, 5.0, 0
        ), both=True)
        self.refresh_ports()
        self.timer = QtCore.QTimer(self)
        # This timer only services serial I/O.  The displayed experiment time
        # comes from the MCU telemetry timestamp, not from this timer.
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def _build_ui(self) -> None:
        root = QtWidgets.QWidget()
        root_layout = QtWidgets.QVBoxLayout(root)
        root_layout.setContentsMargins(8, 8, 8, 8)

        header = QtWidgets.QHBoxLayout()
        header.addWidget(QtWidgets.QLabel("Mode"))
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems(["User Mode", "Developer Mode"])
        self.mode_combo.setMinimumWidth(190)
        self.mode_combo.currentIndexChanged.connect(self._switch_mode)
        header.addWidget(self.mode_combo)
        header.addStretch(1)
        guide = QtWidgets.QPushButton("Modeling & Control Guide")
        guide.clicked.connect(lambda: HardwareGuideDialog(self).exec_())
        header.addWidget(guide)
        root_layout.addLayout(header)
        root_layout.addWidget(self._build_serial_bar())

        self.mode_stack = QtWidgets.QStackedWidget()
        self.mode_stack.addWidget(self._build_user_page())
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(self._build_control_panel())
        self.developer_tabs = QtWidgets.QTabWidget()
        self.model_canvas = HardwareLungCanvas()
        self.model_view = self._model_view(self.model_canvas)
        self.developer_tabs.addTab(self.model_view, "Dynamic Model")
        live_hardware = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(live_hardware)
        self.flow_canvas = FlowCanvas()
        right_layout.addWidget(self.flow_canvas, stretch=2)
        self.telemetry_table = QtWidgets.QTableWidget(0, 7)
        self.telemetry_table.setHorizontalHeaderLabels(["t / s", "State", "Cycle", "Flow", "Volume", "PPV output", "Mask"])
        self.telemetry_table.horizontalHeader().setStretchLastSection(True)
        right_layout.addWidget(self.telemetry_table, stretch=1)
        self.developer_tabs.addTab(live_hardware, "Live Hardware")
        # The flow plot is skipped while hidden; refresh it when its tab opens.
        self.developer_tabs.currentChanged.connect(
            lambda _: self.flow_canvas.draw_rows(self.rows) if self.rows else None)
        splitter.addWidget(self.developer_tabs)
        splitter.setSizes([410, 770])
        splitter.setStretchFactor(1, 1)
        self.mode_stack.addWidget(splitter)
        root_layout.addWidget(self.mode_stack, 1)
        self.setCentralWidget(root)
        # Both pages exist now: keep the two rounds controls in step and keep
        # the "runs x cycles" plan visible in both modes.
        self.user_auto_rounds.valueChanged.connect(self._sync_auto_rounds)
        self.inputs["auto_rounds"].valueChanged.connect(self._sync_auto_rounds)
        self.user_auto_iterations.valueChanged.connect(self._sync_auto_iterations)
        self.inputs["auto_iterations"].valueChanged.connect(self._sync_auto_iterations)
        for field in (self.user_inputs["rr"], self.user_inputs["cycles"],
                      self.inputs["rr"], self.inputs["cycles"],
                      self.inputs["transition_dwell"], self.inputs["insp_dwell"],
                      self.inputs["transition_settle"], self.inputs["overclose"],
                      self.inputs["exp_closure"]):
            field.valueChanged.connect(self._update_auto_pid_plan)
        self.within_cycle.toggled.connect(self._update_auto_pid_plan)
        self.auto_method.currentIndexChanged.connect(self._update_auto_pid_plan)
        self.es_phase_compensation.toggled.connect(self._update_auto_pid_plan)
        self.inputs["es_a"].valueChanged.connect(self._update_auto_pid_plan)
        self.inputs["auto_baselines"].valueChanged.connect(self._update_auto_pid_plan)
        self._update_auto_pid_plan()
        # Real-cycle estimate and warnings follow the developer transition fields.
        for key in ("rr", "ie_in", "ie_out", "volume", "transition_dwell", "insp_dwell",
                    "transition_settle", "overclose", "exp_closure", "insp_min_v"):
            self.inputs[key].valueChanged.connect(self._update_user_timing)
        self.within_cycle.toggled.connect(self._update_user_timing)
        self._update_user_timing()
        self.statusBar().showMessage("Connect NUCLEO-G474RE, send config, then start hardware control.")

    @staticmethod
    def _model_view(canvas: HardwareLungCanvas) -> QtWidgets.QScrollArea:
        view = QtWidgets.QScrollArea()
        view.setWidget(canvas)
        view.setWidgetResizable(False)
        view.setAlignment(QtCore.Qt.AlignCenter)
        view.setFrameShape(QtWidgets.QFrame.NoFrame)
        view.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Ignored)
        return view

    def _build_serial_bar(self) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox("Serial Connection")
        layout = QtWidgets.QHBoxLayout(group)
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setMinimumWidth(280)
        refresh = QtWidgets.QPushButton("Refresh")
        self.connect_button = QtWidgets.QPushButton("Connect")
        disconnect = QtWidgets.QPushButton("Disconnect")
        refresh.clicked.connect(self.refresh_ports)
        self.connect_button.clicked.connect(self.connect_serial)
        disconnect.clicked.connect(self.disconnect_serial)
        self.connection_label = QtWidgets.QLabel("Disconnected")
        layout.addWidget(self.port_combo, 1)
        layout.addWidget(refresh)
        layout.addWidget(self.connect_button)
        layout.addWidget(disconnect)
        layout.addWidget(self.connection_label)
        return group

    def _build_log_file_group(self) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox("CSV Log File")
        layout = QtWidgets.QGridLayout(group)
        edit = QtWidgets.QLineEdit(self.log_path_text)
        edit.setPlaceholderText("e.g. trial_01.csv; blank uses the run ID")
        edit.textChanged.connect(lambda text, source=edit: self._sync_log_name(text, source))
        choose = QtWidgets.QPushButton("Choose Location")
        choose.clicked.connect(self.choose_log_path)
        status = QtWidgets.QLabel("Run ID is appended automatically; blank: output/ by run ID")
        status.setWordWrap(True)
        status.setStyleSheet("color:#666; font-size:11px")
        self.log_name_edits.append(edit)
        self.log_status_labels.append(status)
        compact = QtWidgets.QCheckBox("Compact CSV")
        compact.setToolTip("Telemetry columns only; configuration and scores are saved in the JSON pair.")
        compact.setChecked(self.compact_csv)
        compact.toggled.connect(self._sync_compact_csv)
        self.compact_csv_boxes.append(compact)
        layout.addWidget(QtWidgets.QLabel("CSV filename / path"), 0, 0, 1, 2)
        layout.addWidget(edit, 1, 0)
        layout.addWidget(choose, 1, 1)
        layout.addWidget(status, 2, 0, 1, 2)
        layout.addWidget(compact, 3, 0, 1, 2)
        return group

    def _sync_compact_csv(self, checked: bool) -> None:
        self.compact_csv = checked
        for box in self.compact_csv_boxes:
            if box.isChecked() != checked:
                with QtCore.QSignalBlocker(box):
                    box.setChecked(checked)

    def _sync_health_override(self, checked: bool) -> None:
        for box in self.health_override_boxes:
            if box.isChecked() != checked:
                with QtCore.QSignalBlocker(box):
                    box.setChecked(checked)

    def _pair_combo(self) -> QtWidgets.QComboBox:
        combo = QtWidgets.QComboBox()
        combo.addItem("Inspiration pair (kp_in, ki_in) - cost J_ins", "inspiration")
        combo.addItem("Expiration pair (kp_out, ki_out) - cost J_out", "expiration")
        combo.currentIndexChanged.connect(self._sync_pair)
        self.pair_combos.append(combo)
        return combo

    def _sync_pair(self, index: int) -> None:
        for combo in self.pair_combos:
            if combo.currentIndex() != index:
                with QtCore.QSignalBlocker(combo):
                    combo.setCurrentIndex(index)
        self._update_auto_pid_plan()

    def _sync_probe(self, checked: bool) -> None:
        for box in self.probe_boxes:
            if box.isChecked() != checked:
                with QtCore.QSignalBlocker(box):
                    box.setChecked(checked)
        self._update_auto_pid_plan()

    def _selected_pair(self) -> str:
        return self.es_pair.currentData()

    def _probe_selected(self) -> bool:
        return self._es_selected() and self.es_probe.isChecked()

    def _health_override(self) -> bool:
        return any(box.isChecked() for box in self.health_override_boxes)

    def _health_override_box(self) -> QtWidgets.QCheckBox:
        box = QtWidgets.QCheckBox("Ignore health check")
        box.setToolTip("Auto PID health check: tidal error, onset surge, inspiration capped at "
                       "min_v and, when the expiration pair is tuned, expiration pinned at the cap "
                       "and emptying. Checked: problems are logged, not blocking. Fix tidal volume "
                       "first instead of ignoring it.")
        box.toggled.connect(self._sync_health_override)
        self.health_override_boxes.append(box)
        return box

    def _stabilize_box(self) -> QtWidgets.QCheckBox:
        box = QtWidgets.QCheckBox("Unscored stabilization run before the baselines")
        box.setToolTip("Auto PID and the sensitivity pre-experiment first run once with the "
                       "starting gains; that run is saved but never enters the baselines, the "
                       "noise floor or the search.")
        box.setChecked(True)
        box.toggled.connect(self._sync_stabilize)
        self.stabilize_boxes.append(box)
        return box

    def _sync_stabilize(self, checked: bool) -> None:
        for box in self.stabilize_boxes:
            if box.isChecked() != checked:
                with QtCore.QSignalBlocker(box):
                    box.setChecked(checked)
        self._update_auto_pid_plan()

    def _stabilize_selected(self) -> bool:
        return any(box.isChecked() for box in self.stabilize_boxes)

    def _sync_log_name(self, text: str, source: QtWidgets.QLineEdit) -> None:
        self.log_path_text = text
        for edit in self.log_name_edits:
            if edit is not source and edit.text() != text:
                with QtCore.QSignalBlocker(edit):
                    edit.setText(text)

    def _set_log_name(self, text: str) -> None:
        self.log_path_text = text
        for edit in self.log_name_edits:
            with QtCore.QSignalBlocker(edit):
                edit.setText(text)

    def _set_log_status(self, text: str) -> None:
        # A path has no spaces, so word wrap cannot break it and its full width
        # becomes the label's minimum width, which pushes the settings column
        # wider than its scroll area and clips the right-hand controls.  Zero-
        # width spaces after separators give Qt break points; the tooltip keeps
        # the exact text.
        wrapped = re.sub(r"([\\/_.])", "\\1\u200b", text)
        for label in self.log_status_labels:
            label.setText(wrapped)
            label.setToolTip(text)

    def choose_log_path(self) -> None:
        run_id = self.current_run_id or (self.rows[0].run_id if self.rows else 0)
        default = resolve_log_path(self.log_path_text, run_id)
        default.parent.mkdir(parents=True, exist_ok=True)
        filename, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Choose CSV save location", str(default), "CSV files (*.csv)"
        )
        if filename:
            self._set_log_name(str(Path(filename).with_suffix(".csv")))

    def _build_user_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        layout.addWidget(splitter)

        settings = QtWidgets.QWidget()
        side = QtWidgets.QVBoxLayout(settings)
        title = QtWidgets.QLabel("Single-Lung Hardware Control")
        title.setStyleSheet("font-size:18px; font-weight:bold; color:#17466d")
        side.addWidget(title)
        form_group = QtWidgets.QGroupBox("Breathing Settings")
        form = QtWidgets.QFormLayout(form_group)
        self._user_spin(form, "rr", "Rate / bpm", self.cfg.respiratory_rate_bpm, 0.01, UNBOUNDED, 1, 1)
        self._user_spin(form, "volume", "Tidal volume / mL", self.cfg.target_volume_ml, 0.1, UNBOUNDED, 5, 1)
        self._user_spin(form, "ie_in", "I:E inspiration", self.cfg.ie_inspiration, 0.01, UNBOUNDED, 0.1, 1)
        self._user_spin(form, "ie_out", "I:E expiration", self.cfg.ie_expiration, 0.01, UNBOUNDED, 0.1, 1)
        self.user_derived_flow = QtWidgets.QLabel()
        form.addRow("Insp. reference / SLM", self.user_derived_flow)
        self._user_spin(form, "cycles", "Cycles", self.cfg.cycles, 1, 65535, 1, 0)
        side.addWidget(form_group)
        self.user_timing = QtWidgets.QLabel()
        self.user_timing.setWordWrap(True)
        side.addWidget(self.user_timing)
        self.user_timing_warning = QtWidgets.QLabel()
        self.user_timing_warning.setWordWrap(True)
        self.user_timing_warning.setStyleSheet("color:#9a5b00; font-size:11px")
        side.addWidget(self.user_timing_warning)
        for field in self.user_inputs.values():
            field.valueChanged.connect(self._update_user_timing)

        self.user_auto_pid = QtWidgets.QCheckBox("Auto PID tuning")
        self.user_auto_pid.setToolTip(
            "Off: one normal hardware run. On: a sequence of hardware runs tunes "
            "Kp/Ki automatically (method and gain pairs: Developer Mode; default "
            "extremum seeking on the inspiration pair). Kd is unchanged. Stop "
            "cancels at any time.")
        self.user_auto_pid.toggled.connect(self._sync_auto_pid_checkboxes)
        side.addWidget(self.user_auto_pid)
        # Auto PID details are shown only while Auto PID is enabled.
        self.user_auto_details = QtWidgets.QWidget()
        details = QtWidgets.QVBoxLayout(self.user_auto_details)
        details.setContentsMargins(18, 0, 0, 0)
        # Only the run-count control of the selected method is shown.
        self.user_rounds_widget = QtWidgets.QWidget()
        rounds_row = QtWidgets.QHBoxLayout(self.user_rounds_widget)
        rounds_row.setContentsMargins(0, 0, 0, 0)
        rounds_row.addWidget(QtWidgets.QLabel("Auto PID search rounds"))
        self.user_auto_rounds = QtWidgets.QSpinBox()
        self.user_auto_rounds.setRange(1, 1000)
        self.user_auto_rounds.setValue(2)
        rounds_row.addWidget(self.user_auto_rounds)
        details.addWidget(self.user_rounds_widget)
        self.user_iterations_widget = QtWidgets.QWidget()
        iterations_row = QtWidgets.QHBoxLayout(self.user_iterations_widget)
        iterations_row.setContentsMargins(0, 0, 0, 0)
        iterations_row.addWidget(QtWidgets.QLabel("Auto PID iterations"))
        self.user_auto_iterations = QtWidgets.QSpinBox()
        self.user_auto_iterations.setRange(1, 1000)
        self.user_auto_iterations.setValue(ES_DEFAULT_ITERATIONS)
        iterations_row.addWidget(self.user_auto_iterations)
        details.addWidget(self.user_iterations_widget)
        pair_row = QtWidgets.QHBoxLayout()
        pair_row.addWidget(QtWidgets.QLabel("Tune"))
        self.user_es_pair = self._pair_combo()
        pair_row.addWidget(self.user_es_pair, 1)
        details.addLayout(pair_row)
        self.user_es_probe = QtWidgets.QCheckBox("Sensitivity pre-experiment only (no gain change)")
        self.probe_boxes.append(self.user_es_probe)
        self.user_es_probe.toggled.connect(self._sync_probe)
        details.addWidget(self.user_es_probe)
        details.addWidget(self._stabilize_box())
        self.user_auto_plan = QtWidgets.QLabel()
        self.user_auto_plan.setWordWrap(True)
        self.user_auto_plan.setStyleSheet("color:#684d19; font-size:11px")
        details.addWidget(self.user_auto_plan)
        details.addWidget(self._health_override_box())
        self.user_auto_details.setVisible(False)
        side.addWidget(self.user_auto_details)

        apply_button = QtWidgets.QPushButton("Apply User Settings")
        apply_button.setMinimumHeight(36)
        apply_button.clicked.connect(self._apply_user_settings)
        side.addWidget(apply_button)
        side.addWidget(self._build_log_file_group())
        buttons = QtWidgets.QHBoxLayout()
        user_start = QtWidgets.QPushButton("Start")
        user_stop = QtWidgets.QPushButton("Stop")
        user_save = QtWidgets.QPushButton("Save Log")
        user_start.clicked.connect(self.start_run)
        user_stop.clicked.connect(self.stop_run)
        user_save.clicked.connect(lambda: self.save_current_log())
        buttons.addWidget(user_start)
        buttons.addWidget(user_stop)
        buttons.addWidget(user_save)
        side.addLayout(buttons)

        readout = QtWidgets.QGroupBox("Live Hardware")
        values = QtWidgets.QFormLayout(readout)
        for key, label in (("state", "Phase"), ("cycle", "Cycle"),
                           ("flow", "Flow"), ("volume", "Volume"),
                           ("opening", "PPV output")):
            value = QtWidgets.QLabel("--")
            value.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            value.setStyleSheet("font-family:Consolas,monospace")
            self.user_metrics[key] = value
            values.addRow(label, value)
        side.addWidget(readout)
        self.user_auto_status = QtWidgets.QLabel("Auto PID: disabled")
        self.user_auto_status.setWordWrap(True)
        side.addWidget(self.user_auto_status)
        side.addStretch(1)
        # Scroll instead of letting Qt squash the form rows when space runs out.
        settings_scroll = QtWidgets.QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        settings_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        settings_scroll.setMinimumWidth(385)
        settings_scroll.setMaximumWidth(440)
        settings_scroll.setWidget(settings)
        splitter.addWidget(settings_scroll)

        charts = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.user_model_canvas = HardwareLungCanvas()
        self.user_model_view = self._model_view(self.user_model_canvas)
        charts.addWidget(self.user_model_view)
        self.user_flow_canvas = FlowCanvas()
        charts.addWidget(self.user_flow_canvas)
        charts.setSizes([580, 430])
        charts.setStretchFactor(1, 1)
        splitter.addWidget(charts)
        splitter.setSizes([395, 965])
        splitter.setStretchFactor(1, 1)
        self._update_user_timing()
        return page

    def _build_control_panel(self) -> QtWidgets.QWidget:
        outer = QtWidgets.QWidget()
        outer.setMinimumWidth(390)
        layout = QtWidgets.QVBoxLayout(outer)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        content = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(content)

        breathing = QtWidgets.QGroupBox("Breathing Parameters")
        form = QtWidgets.QFormLayout(breathing)
        self._spin(form, "rr", "Respiratory rate / bpm", self.cfg.respiratory_rate_bpm, 0.01, UNBOUNDED, 1, 2)
        self._spin(form, "ie_in", "I:E inspiration", self.cfg.ie_inspiration, 0.01, UNBOUNDED, 0.1, 2)
        self._spin(form, "ie_out", "I:E expiration", self.cfg.ie_expiration, 0.01, UNBOUNDED, 0.1, 2)
        self._spin(form, "volume", "Target tidal volume / mL", self.cfg.target_volume_ml, 0.1, UNBOUNDED, 5, 1)
        self._spin(form, "fill_time", "Target fill time / s", self.cfg.target_fill_time_s, 0.001, UNBOUNDED, 0.05, 3)
        self._spin(form, "cycles", "Cycles", self.cfg.cycles, 1, 65535, 1, 0)
        box.addWidget(breathing)

        pid = QtWidgets.QGroupBox("Flow PID Control")
        form = QtWidgets.QFormLayout(pid)
        self._spin(form, "kp", "Inspiration Kp / conductance per SLM", self.cfg.pi.kp, 0, UNBOUNDED, 0.001, 4)
        self._spin(form, "ki", "Inspiration Ki / conductance per SLM s", self.cfg.pi.ki, 0, UNBOUNDED, 0.01, 4)
        self._spin(form, "kp_out", "Expiration Kp / opening per SLM", self.cfg.hardware_expiration_kp, 0, UNBOUNDED, 0.0001, 4)
        self._spin(form, "ki_out", "Expiration Ki / opening per SLM s", self.cfg.hardware_expiration_ki, 0, UNBOUNDED, 0.0001, 4)
        self._spin(form, "kd_out", "Expiration Kd / opening s per SLM", self.cfg.hardware_expiration_kd, 0, UNBOUNDED, 0.001, 4)
        self._spin(form, "kd", "Inspiration Kd / conductance s per SLM", self.cfg.pi.kd, 0, UNBOUNDED, 0.001, 4)
        self._spin(form, "ilim", "Inspiration integral limit / SLM s", self.cfg.pi.integral_limit, 0.01, UNBOUNDED, 0.1, 2)
        self._spin(form, "ilim_out", "Expiration integral limit / SLM s", self.cfg.hardware_expiration_integral_limit, 0.01, UNBOUNDED, 0.1, 2)
        self._spin(form, "insp_min_v", "Inspiration PPV minimum / V (0 = full open)", self.cfg.hardware_inspiration_min_v, 0, 5.0, 0.1, 2)
        min_v_note = QtWidgets.QLabel(
            "Caps inspiration PI and FF at the conductance of this voltage, so "
            "anti-windup stays consistent. Default 3.0 V: about 4% less maximum "
            "ATM conductance than 0 V, but the return stroke to closure drops "
            "from about 2.5 s to 0.76 s. Set 0 to allow full open."
        )
        min_v_note.setWordWrap(True)
        min_v_note.setStyleSheet("color:#666; font-size:11px")
        form.addRow(min_v_note)
        box.addWidget(pid)

        feedforward = QtWidgets.QGroupBox("Feedforward (FF)")
        ff_layout = QtWidgets.QVBoxLayout(feedforward)
        self.ff_inspiration_enabled = QtWidgets.QCheckBox("Inspiration FF (ATM open)")
        self.ff_expiration_enabled = QtWidgets.QCheckBox("Expiration FF (VAC open)")
        self.ff_inspiration_enabled.setChecked(self.cfg.hardware_ff_inspiration_enabled)
        self.ff_expiration_enabled.setChecked(False)
        ff_layout.addWidget(self.ff_inspiration_enabled)
        self.ff_expiration_enabled.setEnabled(False)
        self.inputs["kd_out"].setValue(0.0)
        self.inputs["kd_out"].setEnabled(False)
        ff_layout.addWidget(self.ff_expiration_enabled)
        ff_note = QtWidgets.QLabel(
            "Unchecked: zero FF in that phase; PID remains active. Inspiration "
            "FF also controls pre-positioning (off: start at zero opening). "
            "Apply with Send Config or Start. Requires matching firmware protocol."
        )
        ff_note.setWordWrap(True)
        ff_layout.addWidget(ff_note)
        box.addWidget(feedforward)

        auto_pid = QtWidgets.QGroupBox("Optional Auto PID")
        form = QtWidgets.QFormLayout(auto_pid)
        self.auto_pid_enabled = QtWidgets.QCheckBox("Enable run-to-run PI tuning")
        self.auto_pid_enabled.toggled.connect(self._sync_auto_pid_checkboxes)
        form.addRow(self.auto_pid_enabled)
        self.auto_method = QtWidgets.QComboBox()
        self.auto_method.addItem("Extremum seeking (paper eq. 5-7)", ExtremumSeekingTuner.method)
        self.auto_method.addItem("Coordinate search (comparison)", CoordinateSearchTuner.method)
        form.addRow("Method", self.auto_method)
        self._spin(form, "auto_iterations", "ES iterations", ES_DEFAULT_ITERATIONS, 1, 1000, 1, 0)
        # One pair per round (Kp cos, Ki sin, ω = aπ), scored on its phase cost.
        self.es_pair = self._pair_combo()
        form.addRow("Tune", self.es_pair)
        low, high = AUTO_PID_BASELINE_RANGE
        self._spin(form, "auto_baselines", "Baseline runs (noise floor)", AUTO_PID_BASELINE_RUNS,
                   low, high, 1, 0)
        form.addRow(self._stabilize_box())
        self.es_probe = QtWidgets.QCheckBox(
            "Sensitivity pre-experiment only (baselines + each gain x0.6 / x1.4; no gain change)")
        self.probe_boxes.append(self.es_probe)
        self.es_probe.toggled.connect(self._sync_probe)
        form.addRow(self.es_probe)
        resume_row = QtWidgets.QHBoxLayout()
        resume = QtWidgets.QPushButton("Resume probe from report...")
        resume.setToolTip("Load a sensitivity-probe *_summary.json and run only its missing or "
                          "incomplete probes (new baselines; finished probes are carried over "
                          "as changes relative to their own baseline).")
        resume.clicked.connect(lambda: self.load_probe_resume())
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self.clear_probe_resume)
        resume_row.addWidget(resume, 1)
        resume_row.addWidget(clear)
        form.addRow(resume_row)
        self._spin(form, "es_alpha_rel", "ES α_rel (dither / initial value)", ES_DEFAULT_ALPHA_REL, 0.001, 1, 0.01, 3)
        self._spin(form, "es_gamma_rel", "ES γ_rel (uncalibrated)", ES_DEFAULT_GAMMA_REL, 0.001, UNBOUNDED, 1, 3)
        self._spin(form, "es_a", "ES a (ω = aπ)", ES_DEFAULT_A, 0.01, 0.99, 0.05, 3)
        self._spin(form, "es_h", "ES h (high-pass (z−1)/(z+h))", ES_DEFAULT_H, 0.01, 0.99, 0.05, 3)
        self.es_phase_compensation = QtWidgets.QCheckBox(
            "Demodulate with high-pass phase (paper Fig. 1 φ; off: 44° at 0.8π converges)")
        form.addRow(self.es_phase_compensation)
        self._spin(form, "auto_rounds", "Coordinate search rounds", 2, 1, 1000, 1, 0)
        auto_help = QtWidgets.QLabel(
            "Every run is scored with J = 0.65 J_Q + 0.35 J_V (unchanged), split into "
            "J_ins + J_out. One round tunes one pair on its own phase cost (inspiration: "
            "J_ins, expiration: J_out). ES: N baselines (mean + measured noise) + "
            "iterations + 1 confirmation; θ̂(N) is kept only if the confirmation improves "
            "the phase cost by > 3x its noise and the full J is not worse than its noise. "
            "Run the expiration round afterwards, from the loaded gains. Kd and integral "
            "limits stay fixed."
        )
        auto_help.setWordWrap(True)
        auto_help.setStyleSheet("color:#684d19; font-size:11px")
        form.addRow(auto_help)
        self.auto_pid_plan = QtWidgets.QLabel()
        self.auto_pid_plan.setWordWrap(True)
        form.addRow("Plan", self.auto_pid_plan)
        form.addRow(self._health_override_box())
        self.auto_pid_status = QtWidgets.QLabel("Disabled")
        self.auto_pid_status.setWordWrap(True)
        form.addRow("Status", self.auto_pid_status)
        box.addWidget(auto_pid)

        protection = QtWidgets.QGroupBox("Measured Model Parameter Protection")
        protection_layout = QtWidgets.QVBoxLayout(protection)
        self.remodel_enabled = QtWidgets.QCheckBox(
            "Re-edit measured modeling parameters"
        )
        self.remodel_enabled.setChecked(False)
        self.remodel_enabled.toggled.connect(self._set_model_parameters_editable)
        protection_layout.addWidget(self.remodel_enabled)
        protection_note = QtWidgets.QLabel(
            "Capacity, time constants, PPV exponent and measured delay are locked "
            "by default. Before applying new identified values to hardware, test "
            "them in single_lung_pid_gui.py and inspect stability and saturation."
        )
        protection_note.setWordWrap(True)
        protection_note.setStyleSheet("color:#684d19; font-size:11px")
        protection_layout.addWidget(protection_note)
        box.addWidget(protection)

        smith = QtWidgets.QGroupBox("PPV Positioning / Motion Smith Predictor")
        form = QtWidgets.QFormLayout(smith)
        self.smith_enabled = QtWidgets.QCheckBox("Enable Smith predictor for PPV motion")
        self.smith_enabled.setChecked(self.cfg.smith.enabled)
        self.smith_enabled.setEnabled(True)
        form.addRow(self.smith_enabled)
        self._spin(form, "smith_delay", "Residual transport/sensor delay / s", self.cfg.smith.delay_s, 0, SMITH_DELAY_MAX_S, 0.005, 3)
        self._spin(form, "ppv_open_time", "Equivalent 4.2 to 0 V time / s", self.cfg.smith.ppv_open_time_s, 0, UNBOUNDED, 0.01, 3)
        self._spin(form, "ppv_close_time", "Equivalent 0 to 4.2 V time / s", self.cfg.smith.ppv_close_time_s, 0, UNBOUNDED, 0.01, 3)
        self._spin(form, "smith_gain", "Motion correction weight", self.cfg.smith.correction_gain, 0, UNBOUNDED, 0.05, 2)
        delay_note = QtWidgets.QLabel(
            "At each phase change both solenoids close. PPV moves to the next "
            "phase starting position before its solenoid opens: inspiration FF "
            "position when enabled, otherwise zero opening. Expiration moves to zero "
            "opening (closure voltage) from the open side; VAC opens on arrival and PI "
            "regulates without FF under the v8 opening cap. Position is "
            "estimated, not measured. Speed: 5 V / 2.9 s; equivalent 4.2 V "
            "stroke: 2.436 s. Smith feedback = measured net flow + weight * "
            "(ideal model flow - actuator model flow), actuator branch delayed by the "
            "residual delay: 0.2 s, the measured VAC command-to-flow lag beyond the "
            "stroke model (2026-10-22; one shared value for both phases). "
            "Zero opening: closure voltage (4.30 V); fault closure 5 V."
        )
        delay_note.setWordWrap(True)
        delay_note.setStyleSheet("color:#666; font-size:11px")
        form.addRow(delay_note)
        self._spin(form, "transition_dwell", "Transition minimum dwell / s",
                   self.cfg.hardware_transition_dwell_min_s, 0, UNBOUNDED, 0.05, 2)
        self._spin(form, "overclose", "Transition over-close / V (0 = off)",
                   self.cfg.hardware_transition_overclose_v, 0, 5.0, 0.01, 2)
        self._spin(form, "insp_dwell", "Inspiration minimum dwell after VAC close / s",
                   self.cfg.hardware_inspiration_dwell_min_s, 0, UNBOUNDED, 0.01, 3)
        self._spin(form, "transition_settle", "Expiration: settle after PPV reaches close target / s",
                   self.cfg.hardware_transition_settle_s, 0, UNBOUNDED, 0.05, 3)
        self._spin(form, "exp_closure", "Expiration (VAC) closure = PI zero opening / V",
                   self.cfg.hardware_expiration_closure_v, 3.5, 5.0, 0.005, 3)
        exp_closure_note = QtWidgets.QLabel(
            "Protocol 20: set to the voltage that measurably seals VAC (default 4.32 V: "
            "4.30 V sealed in the morning of 2026-10-22 but leaked from ~15:00). The whole "
            "VAC map is shifted; inspiration keeps the model closure below.")
        exp_closure_note.setWordWrap(True)
        exp_closure_note.setStyleSheet("color:#666; font-size:11px")
        form.addRow(exp_closure_note)
        self.within_cycle = QtWidgets.QCheckBox("Deduct transition from expiration (cycle = 60/bpm)")
        self.within_cycle.setChecked(self.cfg.dead_time_within_cycle)
        form.addRow(self.within_cycle)
        transition_note = QtWidgets.QLabel(
            "Protocol 17: PPV starts closing as soon as ATM is cut. VAC opens only "
            "when the estimated position has arrived AND the close command is at "
            "least the minimum dwell old (the estimate is open-loop). Over-close "
            "holds closure + offset during the transition and returns to closure "
            "when VAC opens. With deduction on, expiration = 60/bpm minus elapsed "
            "cycle time (floor 0.25 s) and its reference is recomputed from it. "
            "Protocol 18: ATM opens only when the PPV estimate has arrived AND both "
            "routes have been closed for the inspiration minimum dwell (VAC flow tail). "
            "Protocol 19: VAC opens the settle time after the PPV estimate reaches the "
            "close target (closure + over-close), so deep inspiration ends wait longer; "
            "the transition minimum dwell is only an optional floor (0 = off)."
        )
        transition_note.setWordWrap(True)
        transition_note.setStyleSheet("color:#666; font-size:11px")
        form.addRow(transition_note)
        self.dev_timing = QtWidgets.QLabel()
        self.dev_timing.setWordWrap(True)
        form.addRow(self.dev_timing)
        box.addWidget(smith)

        model = QtWidgets.QGroupBox("Measured Hardware Model Parameters")
        form = QtWidgets.QFormLayout(model)
        self._spin(form, "capacity", "Volume guard / mL (+50 mL safety margin)", self.cfg.lung.capacity_ml, 0.1, UNBOUNDED, 5, 1)
        self._spin(form, "flow_offset", "Sensor zero offset / SLM", self.cfg.lung.hardware_flow_offset_slm, -UNBOUNDED, UNBOUNDED, 0.0001, 4)
        self._spin(form, "flow_gain", "Common flow calibration gain", self.cfg.lung.hardware_flow_gain, 0.0001, UNBOUNDED, 0.0001, 4)
        self._spin(form, "vac_leak", "VAC bypass plateau / SLM", self.cfg.lung.hardware_vac_leak_slm, 0, UNBOUNDED, 0.001, 3)
        self._spin(form, "model_volume", "Empirical model amplitude / mL", self.cfg.lung.hardware_model_volume_ml, 0.1, UNBOUNDED, 50, 1)
        self._spin(form, "tau_in", "Tau_in / s", self.cfg.lung.tau_in_s, 0.0001, UNBOUNDED, 0.01, 4)
        self._spin(form, "tau_out", "Tau_out / s", self.cfg.lung.tau_out_s, 0.0001, UNBOUNDED, 0.01, 4)
        self._spin(form, "gamma", "Inspiration gamma", self.cfg.lung.hardware_gamma_in, 0.0001, UNBOUNDED, 0.0001, 4)
        self._spin(form, "gamma_out", "Expiration gamma (40 kPa bench)", self.cfg.lung.hardware_gamma_out, 0.0001, UNBOUNDED, 0.0001, 4)
        self._spin(form, "dead_zone_in", "Inspiration law Ud / V (fit ≥ Uc−0.10 V)", self.cfg.lung.hardware_dead_zone_in_v, 0, 5.0, 0.0001, 4)
        self._spin(form, "dead_zone_out", "Expiration plateau end / V", self.cfg.lung.hardware_dead_zone_out_v, 0, 5.0, 0.0001, 4)
        self._spin(form, "closure", "PPV closure target / V", self.cfg.lung.hardware_closure_v, 0.1, 5, 0.01, 2)
        note = QtWidgets.QLabel("2026-10-14 afternoon high-voltage fit. Near-closed anchor: 4.30 V. VAC tail baseline: 1.0122 SLM; provisional roll-off 4.259-4.300 V. Low-voltage extrapolation and dynamic switching are unvalidated. Model amplitude is not physical capacity. PPV positions before each phase.")
        note.setWordWrap(True)
        form.addRow(note)
        self._spin(form, "dead", "Fixed dead time / s (unused)", self.cfg.dead_time_s, 0, 0.5, 0.01, 3)
        self._spin(form, "precondition", "Precondition time / s", self.cfg.precondition_s, 0, UNBOUNDED, 0.1, 2)
        box.addWidget(model)
        self._set_model_parameters_editable(False)

        manual = QtWidgets.QGroupBox("Manual Valves (experiment stopped)")
        manual_layout = QtWidgets.QGridLayout(manual)
        for index, (label, command) in enumerate((
            ("Open ATM", "MANUAL,ATM,OPEN"), ("Close ATM", "MANUAL,ATM,CLOSE"),
            ("Open VAC", "MANUAL,VAC,OPEN"), ("Close VAC", "MANUAL,VAC,CLOSE"),
            ("PPV Full Open (0 V)", "MANUAL,PPV,OPEN"),
        )):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(lambda checked=False, cmd=command: self.send_manual_command(cmd))
            manual_layout.addWidget(button, index // 2, index % 2)
        self.manual_status = QtWidgets.QLabel("Idle default: ATM/VAC closed, PPV 0 V. Opening a route closes the other.")
        self.manual_status.setWordWrap(True)
        manual_layout.addWidget(self.manual_status, 3, 0, 1, 2)
        box.addWidget(manual)

        guide = QtWidgets.QPushButton("Modeling & Control Guide")
        guide.clicked.connect(lambda: HardwareGuideDialog(self).exec_())
        box.addWidget(guide)

        box.addWidget(self._build_log_file_group())

        buttons = QtWidgets.QHBoxLayout()
        send = QtWidgets.QPushButton("Send Config")
        start = QtWidgets.QPushButton("Start")
        stop = QtWidgets.QPushButton("Stop")
        save = QtWidgets.QPushButton("Save Log")
        send.clicked.connect(self.send_config)
        start.clicked.connect(self.start_run)
        stop.clicked.connect(self.stop_run)
        save.clicked.connect(lambda: self.save_current_log())
        buttons.addWidget(send)
        buttons.addWidget(start)
        buttons.addWidget(stop)
        buttons.addWidget(save)
        box.addLayout(buttons)

        telemetry = QtWidgets.QGroupBox("Live Telemetry")
        form = QtWidgets.QFormLayout(telemetry)
        for key, label in (
            ("time", "Time"),
            ("state", "State"),
            ("cycle", "Cycle"),
            ("flow", "Flow"),
            ("reference", "Reference"),
            ("volume", "Volume"),
            ("opening", "PPV output"),
            ("command", "PPV command"),
            ("valves", "Valves"),
        ):
            value = QtWidgets.QLabel("--")
            value.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            value.setStyleSheet("font-family: Consolas, monospace;")
            self.metrics[key] = value
            form.addRow(label, value)
        box.addWidget(telemetry)

        self.log = QtWidgets.QTextBrowser()
        # Every telemetry line is appended; an unbounded document slows down.
        # The full event list is kept in run_events / the JSON log.
        self.log.document().setMaximumBlockCount(LOG_MAX_LINES)
        self.log.setMinimumHeight(145)
        box.addWidget(self.log)
        box.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll)
        return outer

    def _spin(self, form, key, label, value, low, high, step, decimals) -> None:
        widget = QtWidgets.QDoubleSpinBox()
        widget.setDecimals(decimals)
        widget.setRange(displayable_minimum(low, decimals), high)
        widget.setSingleStep(step)
        widget.setValue(value)
        widget.setKeyboardTracking(False)
        # Huge ranges (1e9) would otherwise make the size hint very wide.
        widget.setMinimumWidth(90)
        self.inputs[key] = widget
        form.addRow(label, widget)

    def _set_model_parameters_editable(self, enabled: bool) -> None:
        """Protect accepted physical-identification values from routine edits."""
        for key in ("capacity", "model_volume", "tau_in", "tau_out", "gamma", "gamma_out", "dead_zone_in", "dead_zone_out", "closure", "smith_delay", "ppv_open_time", "ppv_close_time", "flow_offset", "flow_gain", "vac_leak", "dead"):
            if key in self.inputs:
                self.inputs[key].setEnabled(enabled and key != "dead")
        if hasattr(self, "remodel_enabled"):
            state = "editable for re-identification" if enabled else "locked (accepted measurements)"
            self.remodel_enabled.setToolTip(f"Measured model parameters are {state}.")

    def _user_spin(self, form, key, label, value, low, high, step, decimals) -> None:
        widget = QtWidgets.QDoubleSpinBox()
        widget.setDecimals(decimals)
        widget.setRange(displayable_minimum(low, decimals), high)
        widget.setSingleStep(step)
        widget.setValue(value)
        widget.setKeyboardTracking(False)
        # Huge ranges (1e9) would otherwise make the size hint very wide.
        widget.setMinimumWidth(90)
        self.user_inputs[key] = widget
        form.addRow(label, widget)

    def _update_user_timing(self) -> None:
        rr = self.user_inputs["rr"].value()
        ratio_in = self.user_inputs["ie_in"].value()
        ratio_out = self.user_inputs["ie_out"].value()
        volume = self.user_inputs["volume"].value()
        period = 60.0 / rr
        inspiration = period * ratio_in / (ratio_in + ratio_out)
        expiration = period - inspiration
        # Match the transmitted precision without rounding beyond the phase.
        fill_time = math.floor(inspiration * 1000.0 + 1e-9) / 1000.0
        # Transition fields live in Developer Mode (built after this page).
        dwell = (self.inputs["transition_dwell"].value() if "transition_dwell" in self.inputs
                 else self.cfg.hardware_transition_dwell_min_s)
        within = (self.within_cycle.isChecked() if hasattr(self, "within_cycle")
                  else self.cfg.dead_time_within_cycle)
        min_v = (self.inputs["insp_min_v"].value() if "insp_min_v" in self.inputs
                 else self.cfg.hardware_inspiration_min_v)
        insp_dwell = (self.inputs["insp_dwell"].value() if "insp_dwell" in self.inputs
                      else self.cfg.hardware_inspiration_dwell_min_s)
        if all(k in self.inputs for k in ("transition_settle", "exp_closure", "overclose", "ppv_close_time")):
            settle = self.inputs["transition_settle"].value()
            dwell = expiration_transition_estimate_s(
                dwell, settle,
                self.inputs["exp_closure"].value() + self.inputs["overclose"].value(),
                self.inputs["ppv_close_time"].value())
        else:
            settle = self.cfg.hardware_transition_settle_s
            dwell = cfg_expiration_transition_s(self.cfg)
        timing = cycle_timing_summary(rr, ratio_in, ratio_out, volume, dwell, within, min_v,
                                      insp_dwell, settle)
        expiration = timing["expiration_s"]
        # Protocol 21: with deduction the inspiration also shrinks by I:E and the
        # firmware rescales the reference to the shorter phase.
        inspiration = timing["inspiration_s"]
        flow = 0.06 * volume / min(fill_time, inspiration)
        self.user_derived_flow.setText(f"{flow:.2f} (automatic)")
        # The firmware fixes the real expiration at VAC opening; the dwell is
        # the usual lower bound of the transition, so these are estimates.
        cycle_color = "#b00020" if timing["period_alert"] else "#36556c"
        self.user_timing.setText(
            f"<span style='color:{cycle_color}; font-weight:600'>Actual cycle ≈ "
            f"{timing['actual_period_s']:.2f} s (≈ {timing['actual_bpm']:.1f} bpm; set "
            f"{timing['period_s']:.2f} s)</span><br>"
            f"Inspiration {inspiration:.2f} s · Expiration {expiration:.2f} s"
            f"{' (transitions shared by I:E)' if within else ''}<br>"
            f"References: +{flow:.2f} / −{timing['expiration_flow_slm']:.2f} SLM")
        self.user_timing.setStyleSheet("color:#36556c; font-size:11px")
        self.user_timing.setToolTip(
            "Estimated from the settings. Without 'Deduct transition from expiration' the "
            "PPV positioning dwell is added to every cycle. References are targets, not "
            "measured flow.")
        self.user_timing_warning.setText("\n".join(f"⚠ {w}" for w in timing["short_warnings"]))
        self.user_timing_warning.setToolTip("\n\n".join(timing["warnings"]))
        self.user_timing_warning.setVisible(bool(timing["warnings"]))
        if hasattr(self, "dev_timing"):
            dev = cycle_timing_summary(
                self.inputs["rr"].value(), self.inputs["ie_in"].value(),
                self.inputs["ie_out"].value(), self.inputs["volume"].value(), dwell, within, min_v,
                insp_dwell, settle)
            self.dev_timing.setText(
                f"Actual cycle ≈ {dev['actual_period_s']:.2f} s (≈ {dev['actual_bpm']:.1f} bpm)"
                + "".join(f"\n⚠ {w}" for w in dev["warnings"]))
            self.dev_timing.setStyleSheet(
                "color:#b00020; font-size:11px" if dev["period_alert"] or dev["warnings"]
                else "color:#36556c; font-size:11px")

    def _apply_user_settings(self) -> bool:
        rr = self.user_inputs["rr"].value()
        ratio_in = self.user_inputs["ie_in"].value()
        ratio_out = self.user_inputs["ie_out"].value()
        volume = self.user_inputs["volume"].value()
        inspiration = (60.0 / rr) * ratio_in / (ratio_in + ratio_out)
        fill_time = math.floor(inspiration * 1000.0 + 1e-9) / 1000.0
        values = {
            "rr": rr,
            "ie_in": ratio_in,
            "ie_out": ratio_out,
            "volume": volume,
            "fill_time": fill_time,
            "cycles": self.user_inputs["cycles"].value(),
        }
        for key, value in values.items():
            with QtCore.QSignalBlocker(self.inputs[key]):
                self.inputs[key].setValue(value)
        try:
            self.cfg = self.cfg_from_ui()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Parameter Error", str(exc))
            return False
        self._sync_user_from_config()
        self.statusBar().showMessage("User settings applied; click Start for hardware control.")
        return True

    def _sync_user_from_config(self) -> None:
        values = {
            "rr": self.cfg.respiratory_rate_bpm,
            "ie_in": self.cfg.ie_inspiration,
            "ie_out": self.cfg.ie_expiration,
            "volume": self.cfg.target_volume_ml,
            "cycles": self.cfg.cycles,
        }
        for key, value in values.items():
            with QtCore.QSignalBlocker(self.user_inputs[key]):
                self.user_inputs[key].setValue(value)
        self._update_user_timing()
        self._update_auto_pid_plan()

    def _sync_developer_from_config(self) -> None:
        values = {
            "rr": self.cfg.respiratory_rate_bpm,
            "ie_in": self.cfg.ie_inspiration,
            "ie_out": self.cfg.ie_expiration,
            "volume": self.cfg.target_volume_ml,
            "fill_time": self.cfg.target_fill_time_s,
            "cycles": self.cfg.cycles,
            "kp": self.cfg.pi.kp,
            "ki": self.cfg.pi.ki,
            "kd": self.cfg.pi.kd,
            "ilim": self.cfg.pi.integral_limit,
            "ilim_out": self.cfg.hardware_expiration_integral_limit,
            "insp_min_v": self.cfg.hardware_inspiration_min_v,
            "kp_out": self.cfg.hardware_expiration_kp,
            "ki_out": self.cfg.hardware_expiration_ki,
            "kd_out": 0.0,
            "smith_delay": self.cfg.smith.delay_s,
            "smith_gain": self.cfg.smith.correction_gain,
            "ppv_open_time": self.cfg.smith.ppv_open_time_s,
            "ppv_close_time": self.cfg.smith.ppv_close_time_s,
            "transition_dwell": self.cfg.hardware_transition_dwell_min_s,
            "overclose": self.cfg.hardware_transition_overclose_v,
            "insp_dwell": self.cfg.hardware_inspiration_dwell_min_s,
            "transition_settle": self.cfg.hardware_transition_settle_s,
            "exp_closure": self.cfg.hardware_expiration_closure_v,
            "capacity": self.cfg.lung.capacity_ml,
            "flow_offset": self.cfg.lung.hardware_flow_offset_slm,
            "flow_gain": self.cfg.lung.hardware_flow_gain,
            "vac_leak": self.cfg.lung.hardware_vac_leak_slm,
            "model_volume": self.cfg.lung.hardware_model_volume_ml,
            "tau_in": self.cfg.lung.tau_in_s,
            "tau_out": self.cfg.lung.tau_out_s,
            "gamma": self.cfg.lung.hardware_gamma_in,
            "gamma_out": self.cfg.lung.hardware_gamma_out,
            "dead_zone_in": self.cfg.lung.hardware_dead_zone_in_v,
            "dead_zone_out": self.cfg.lung.hardware_dead_zone_out_v,
            "closure": self.cfg.lung.hardware_closure_v,
            "dead": self.cfg.dead_time_s,
            "precondition": self.cfg.precondition_s,
        }
        for key, value in values.items():
            with QtCore.QSignalBlocker(self.inputs[key]):
                self.inputs[key].setValue(value)
        with QtCore.QSignalBlocker(self.smith_enabled):
            self.smith_enabled.setChecked(self.cfg.smith.enabled)
        with QtCore.QSignalBlocker(self.ff_inspiration_enabled):
            self.ff_inspiration_enabled.setChecked(self.cfg.hardware_ff_inspiration_enabled)
        with QtCore.QSignalBlocker(self.ff_expiration_enabled):
            self.ff_expiration_enabled.setChecked(False)
        with QtCore.QSignalBlocker(self.within_cycle):
            self.within_cycle.setChecked(self.cfg.dead_time_within_cycle)
        self.smith_enabled.setEnabled(True)

    def _sync_auto_rounds(self, value) -> None:
        for widget in (self.user_auto_rounds, self.inputs["auto_rounds"]):
            if int(widget.value()) != int(value):
                with QtCore.QSignalBlocker(widget):
                    widget.setValue(int(value))
        self._update_auto_pid_plan()

    def _sync_auto_iterations(self, value) -> None:
        for widget in (self.user_auto_iterations, self.inputs["auto_iterations"]):
            if int(widget.value()) != int(value):
                with QtCore.QSignalBlocker(widget):
                    widget.setValue(int(value))
        self._update_auto_pid_plan()

    def _es_selected(self) -> bool:
        return self.auto_method.currentData() == ExtremumSeekingTuner.method

    def _auto_pid_search_runs(self) -> int:
        """Runs before the confirmation run (baselines included, stabilization not)."""
        if self._es_selected():
            baselines = int(self.inputs["auto_baselines"].value())
            if self._probe_selected():
                plan = self.probe_resume
                return baselines + (len(plan["rerun"]) if plan else 2 * len(SENSITIVITY_SCALES))
            return baselines + int(self.inputs["auto_iterations"].value())
        return 1 + 8 * int(self.inputs["auto_rounds"].value())

    def _update_auto_pid_plan(self, *_args) -> None:
        """Cycles = breathing cycles per run; rounds/iterations set the run count."""
        user = self.mode_stack.currentIndex() == 0
        source = self.user_inputs if user else self.inputs
        cfg = copy.deepcopy(self.cfg)
        cfg.respiratory_rate_bpm = source["rr"].value()
        cfg.cycles = int(source["cycles"].value())
        cfg.hardware_transition_dwell_min_s = self.inputs["transition_dwell"].value()
        cfg.hardware_inspiration_dwell_min_s = self.inputs["insp_dwell"].value()
        cfg.hardware_transition_settle_s = self.inputs["transition_settle"].value()
        cfg.hardware_expiration_closure_v = self.inputs["exp_closure"].value()
        cfg.hardware_transition_overclose_v = self.inputs["overclose"].value()
        cfg.dead_time_within_cycle = self.within_cycle.isChecked()
        es = self._es_selected()
        self.user_rounds_widget.setVisible(not es)
        self.user_iterations_widget.setVisible(es)
        for key in ("auto_iterations", "es_alpha_rel", "es_gamma_rel", "es_a", "es_h", "auto_baselines"):
            self.inputs[key].setEnabled(es)
        for widget in (*self.pair_combos, *self.probe_boxes, self.es_phase_compensation):
            widget.setEnabled(es)
        self.inputs["auto_rounds"].setEnabled(not es)
        runs = self._auto_pid_search_runs()
        probe = self._probe_selected()
        total = runs if probe else runs + 1
        stabilize = int(self._stabilize_selected())
        minutes = (total + stabilize) * auto_pid_run_estimate_s(cfg, AUTO_PID_WARMUP_CYCLES) / 60.0
        if es:
            baselines = int(self.inputs["auto_baselines"].value())
            pair = self._selected_pair()
            cost = PHASE_COST_KEYS[pair]
            if probe:
                text = (f"Sensitivity pre-experiment ({pair} pair, cost {cost}): {baselines} "
                        f"baselines + {runs - baselines} probe runs = {total} runs x {cfg.cycles} "
                        f"cycles each, about {minutes:.0f} min. Gains are not changed.")
            else:
                text = (f"Extremum seeking ({pair} pair, cost {cost}): {baselines} baselines + "
                        f"{runs - baselines} iterations + 1 confirmation = {total} runs x {cfg.cycles} "
                        f"cycles each (Cycles = scored breathing cycles per run; each run adds "
                        f"{AUTO_PID_WARMUP_CYCLES} unscored warm-up cycle), about {minutes:.0f} min.")
        else:
            text = (f"Up to {runs} runs x {cfg.cycles} cycles each "
                    f"(Cycles = scored breathing cycles per run; each run adds "
                    f"{AUTO_PID_WARMUP_CYCLES} unscored warm-up cycle) + 1 confirmation run "
                    f"of the best gains, about {minutes:.0f} min.")
        if stabilize:
            text += " Plus 1 unscored stabilization run first (included in the time)."
        if probe and self.probe_resume:
            text += (f" Resuming {self.probe_resume['source']}: only "
                     f"{', '.join(self.probe_resume['rerun'])}.")
        if cfg.cycles < 3:
            text += " Auto PID needs at least 3 cycles."
        self.user_auto_plan.setText(text)
        self.auto_pid_plan.setText(text)

    def _switch_mode(self, index: int) -> None:
        if index == 0:
            try:
                self.cfg = self.cfg_from_ui()
                self._sync_user_from_config()
            except Exception:
                pass
        self.mode_stack.setCurrentIndex(index)
        self._update_auto_pid_plan()
        if self.rows:
            # Hidden plots are skipped while running; bring the shown ones up to date.
            self.flow_canvas.draw_rows(self.rows)
            self.user_flow_canvas.draw_rows(self.rows)
            self._draw_dynamic_model(self.rows[-1])

    def _draw_dynamic_model(self, row: HardwareTelemetry, both: bool = False) -> None:
        """Update the visible schematic from measured hardware telemetry."""
        if both or self.mode_stack.currentIndex() == 0:
            self.user_model_canvas.draw_telemetry(row, self.cfg)
        if both or self.mode_stack.currentIndex() == 1:
            self.model_canvas.draw_telemetry(row, self.cfg)

    def _sync_auto_pid_checkboxes(self, checked: bool) -> None:
        source = self.sender()
        other = self.auto_pid_enabled if source is self.user_auto_pid else self.user_auto_pid
        with QtCore.QSignalBlocker(other):
            other.setChecked(checked)
        self.user_auto_details.setVisible(checked)
        state = "ready" if checked else "disabled"
        self.auto_pid_status.setText(state.capitalize())
        self.user_auto_status.setText(f"Auto PID: {state}")

    def refresh_ports(self) -> None:
        self.port_combo.clear()
        if list_ports is None:
            self.port_combo.addItem("pyserial not installed", "")
            self._log("pyserial is required for hardware connection.")
            return
        ports = list(list_ports.comports())
        if not ports:
            self.port_combo.addItem("No serial ports detected", "")
            return
        for port in ports:
            self.port_combo.addItem(f"{port.device} | {port.description}", port.device)

    def cfg_from_ui(self) -> SingleLungConfig:
        cfg = copy.deepcopy(self.cfg)
        cfg.control_hz = HARDWARE_CONTROL_HZ
        cfg.respiratory_rate_bpm = self.inputs["rr"].value()
        cfg.ie_inspiration = self.inputs["ie_in"].value()
        cfg.ie_expiration = self.inputs["ie_out"].value()
        cfg.target_volume_ml = self.inputs["volume"].value()
        cfg.target_fill_time_s = self.inputs["fill_time"].value()
        cfg.cycles = int(self.inputs["cycles"].value())
        cfg.pi.kp = self.inputs["kp"].value()
        cfg.pi.ki = self.inputs["ki"].value()
        cfg.pi.kd = self.inputs["kd"].value()
        cfg.pi.integral_limit = self.inputs["ilim"].value()
        cfg.hardware_expiration_kp = self.inputs["kp_out"].value()
        cfg.hardware_expiration_ki = self.inputs["ki_out"].value()
        cfg.hardware_expiration_kd = 0.0
        cfg.hardware_expiration_integral_limit = self.inputs["ilim_out"].value()
        cfg.hardware_inspiration_min_v = self.inputs["insp_min_v"].value()
        cfg.hardware_ff_inspiration_enabled = self.ff_inspiration_enabled.isChecked()
        cfg.hardware_ff_expiration_enabled = False
        cfg.smith.enabled = self.smith_enabled.isChecked()
        cfg.smith.delay_s = self.inputs["smith_delay"].value()
        cfg.smith.correction_gain = self.inputs["smith_gain"].value()
        cfg.smith.ppv_open_time_s = self.inputs["ppv_open_time"].value()
        cfg.smith.ppv_close_time_s = self.inputs["ppv_close_time"].value()
        cfg.hardware_transition_dwell_min_s = self.inputs["transition_dwell"].value()
        cfg.hardware_transition_overclose_v = self.inputs["overclose"].value()
        cfg.hardware_inspiration_dwell_min_s = self.inputs["insp_dwell"].value()
        cfg.hardware_transition_settle_s = self.inputs["transition_settle"].value()
        cfg.hardware_expiration_closure_v = self.inputs["exp_closure"].value()
        cfg.dead_time_within_cycle = self.within_cycle.isChecked()
        cfg.lung.capacity_ml = self.inputs["capacity"].value()
        cfg.max_lung_volume_ml = self.inputs["capacity"].value()
        cfg.lung.tau_in_s = self.inputs["tau_in"].value()
        cfg.lung.tau_out_s = self.inputs["tau_out"].value()
        cfg.lung.hardware_flow_offset_slm = self.inputs["flow_offset"].value()
        cfg.lung.hardware_flow_gain = self.inputs["flow_gain"].value()
        cfg.lung.hardware_vac_leak_slm = self.inputs["vac_leak"].value()
        cfg.lung.hardware_model_volume_ml = self.inputs["model_volume"].value()
        cfg.lung.hardware_gamma_in = self.inputs["gamma"].value()
        cfg.lung.hardware_gamma_out = self.inputs["gamma_out"].value()
        cfg.lung.hardware_dead_zone_in_v = self.inputs["dead_zone_in"].value()
        cfg.lung.hardware_dead_zone_out_v = self.inputs["dead_zone_out"].value()
        cfg.lung.hardware_closure_v = self.inputs["closure"].value()
        # Tidal volume has no fixed cap, but it must stay within the two
        # (locked) measured limits; say which one to raise.
        for limit, name in ((cfg.max_lung_volume_ml, "Volume guard"),
                            (cfg.lung.hardware_model_volume_ml, "Empirical model amplitude")):
            if cfg.target_volume_ml > limit:
                raise ValueError(
                    f"Tidal volume {cfg.target_volume_ml:g} mL exceeds {name} ({limit:g} mL). "
                    "Raise it in Developer Mode (check 'Re-edit measured modeling parameters').")
        if not (0 <= cfg.lung.hardware_dead_zone_in_v < cfg.lung.hardware_closure_v <= 5
                and 0 <= cfg.lung.hardware_dead_zone_out_v < cfg.lung.hardware_closure_v
                and cfg.lung.hardware_closure_v - cfg.lung.hardware_dead_zone_in_v > ATM_JOIN_DEPTH_V
                and cfg.lung.hardware_closure_v > 3.5
                and cfg.lung.hardware_model_volume_ml >= cfg.target_volume_ml):
            raise ValueError("Invalid directional PPV mapping or empirical amplitude")
        cfg.dead_time_s = 0.0
        cfg.precondition_s = self.inputs["precondition"].value()
        # Hardware firmware derives expiration flow; the simulation-only
        # optional override must not invalidate otherwise valid hardware input.
        cfg.target_expiration_flow_slm = None
        cfg.validate()
        return cfg

    def connect_serial(self) -> None:
        if serial is None:
            self._log("Cannot connect: pyserial is not installed.")
            return
        port = self.port_combo.currentData()
        if not port:
            self._log("Select a serial port first.")
            return
        self.disconnect_serial(save=False)
        try:
            self.connection = serial.Serial(port, BAUD, timeout=0.2)
            time.sleep(0.2)
            self.connection.reset_input_buffer()
            self.messages = queue.Queue()
            self.reader = SerialReader(self.connection, self.messages)
            self.reader.start()
            self.connection.write(b"HELLO\n")
            self.connection_label.setText(f"Connected to {port}")
            self.statusBar().showMessage(f"Connected to {port}")
            self._log(f"> HELLO ({port})")
        except Exception as exc:
            self.connection = None
            self.connection_label.setText("Disconnected")
            self._log(f"Connection failed: {exc}")

    def disconnect_serial(self, save: bool = True) -> None:
        self._cancel_auto_pid("disconnected")
        if self.run_status in ("running", "starting", "stop_requested"):
            self.run_status = "disconnected"
        self.hardware_running = False
        self.firmware_ready = False
        if self.connection is not None:
            try:
                self.connection.write(b"STOP\n")
            except Exception:
                pass
        if self.reader is not None:
            self.reader.stop_event.set()
            self.reader.join(timeout=1.0)
            self.reader = None
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass
            self.connection = None
        if save and self.rows:
            self._autosave_log()
        self.current_run_id = None
        self.connection_label.setText("Disconnected")

    def _write_config(self, cfg: SingleLungConfig) -> bool:
        if self.hardware_running:
            self._log("Stop the current run before changing hardware parameters.")
            return False
        if self.connection is None:
            self._log("Not connected.")
            return False
        if not self.firmware_ready:
            self._log(f"Waiting for compatible firmware: {READY}")
            return False
        try:
            cfg.validate()
            token = int(time.time_ns() // 1_000_000) & 0x7FFFFFFF
            command = config_command(cfg, token)
            self.connection.write((command + "\n").encode("ascii"))
            self.cfg = copy.deepcopy(cfg)
            self._log(f"> {command}")
            return True
        except Exception as exc:
            self._log(f"Config error: {exc}")
            return False

    def send_manual_command(self, command: str) -> bool:
        if self.connection is None or not self.firmware_ready:
            self.manual_status.setText("Connect compatible firmware first.")
            return False
        if self.hardware_running or self.auto_tune_active:
            self.manual_status.setText("Stop the experiment before manual valve control.")
            return False
        try:
            self.connection.write((command + "\n").encode("ascii"))
            self.manual_status.setText("Waiting for valve command acknowledgement...")
            self._log(f"> {command}")
            return True
        except Exception as exc:
            self.manual_status.setText(f"Manual command failed: {exc}")
            return False

    def send_config(self) -> bool:
        if self.auto_tune_active:
            self._log("Stop Auto PID before changing its experiment settings.")
            return False
        try:
            cfg = self.cfg_from_ui()
        except Exception as exc:
            self._log(f"Config error: {exc}")
            return False
        if not self._write_config(cfg):
            return False
        self._sync_developer_from_config()
        self._sync_user_from_config()
        return True

    def start_run(self) -> None:
        if self.hardware_running:
            self._log("A hardware run is already in progress.")
            return
        if self.connection is None:
            self._log("Not connected.")
            return
        if not self.firmware_ready:
            self._log(f"Cannot start: expected {READY}")
            return
        if self.auto_tune_active:
            self._log("Auto PID is already running. Click Stop to cancel it.")
            return
        if self.mode_stack.currentIndex() == 0 and not self._apply_user_settings():
            return
        try:
            cfg = self.cfg_from_ui()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Parameter Error", str(exc))
            return
        if self.auto_pid_enabled.isChecked():
            if cfg.cycles < 3:
                QtWidgets.QMessageBox.warning(
                    self, "Auto PID", "Use at least 3 cycles per Auto PID candidate."
                )
                return
            try:
                self.auto_tuner = self._make_auto_tuner(cfg)
            except ValueError as exc:
                QtWidgets.QMessageBox.warning(self, "Auto PID", str(exc))
                return
            # A recent completed run with the same settings stands in for the
            # baseline; otherwise the baseline candidate itself is checked.
            precheck = (self.last_run_metrics if self.last_run_cfg is not None
                        and comparable_runs(self.last_run_cfg, cfg) else None)
            if precheck is not None:
                issues = auto_pid_health_issues(precheck, **self._health_scope())
                if issues and not self._health_override():
                    self.auto_tuner = None
                    self._set_auto_pid_status("refused by health check")
                    QtWidgets.QMessageBox.warning(
                        self, "Auto PID health check",
                        "The most recent run with these settings is not a suitable "
                        "Auto PID baseline:\n\n- " + "\n- ".join(issues)
                        + "\n\nFix the operating point first, or check 'Ignore health "
                        "check'.")
                    return
                if issues:
                    self._log("Auto PID health check ignored: " + "; ".join(issues))
            if not self._confirm_auto_pid(cfg, self.auto_tuner.total_candidates):
                self.auto_tuner = None
                self._set_auto_pid_status("ready (start cancelled)")
                return
            # ES and the probe measure their noise floor from their own baselines;
            # only the coordinate search still uses an earlier identical run.
            if precheck is not None and isinstance(self.auto_tuner, CoordinateSearchTuner):
                self.auto_tuner.repeat_candidate_score = precheck.get("score", math.nan)
            if isinstance(self.auto_tuner, ExtremumSeekingTuner):
                self._log(f"Auto PID: extremum seeking on the {self.auto_tuner.pair} pair "
                          f"{self.auto_tuner.summary_extra()['tuned_gains']} with cost "
                          f"{self.auto_tuner.cost_key}, {self.auto_tuner.baselines.runs} baselines, "
                          f"{self.auto_tuner.iterations} iterations, alpha_rel={self.auto_tuner.alpha_rel}, "
                          f"gamma_rel={self.auto_tuner.gamma_rel} (uncalibrated), a={self.auto_tuner.a}, "
                          f"h={self.auto_tuner.h}")
                for warning in self.auto_tuner.correlation_warnings:
                    self._log(f"Auto PID warning: {warning}")
            elif isinstance(self.auto_tuner, SensitivityProbe):
                self._log(f"Auto PID: sensitivity pre-experiment on the {self.auto_tuner.pair} pair "
                          f"(cost {self.auto_tuner.cost_key}); gains are not changed.")
            self.auto_tune_active = True
            self.auto_recovery_timeouts = 0
            self.auto_stabilize_pending = self._stabilize_selected()
            self.auto_stabilization_runs = []
            self.auto_stage_override = None
            if isinstance(self.auto_tuner, SensitivityProbe) and self.probe_resume:
                self._log(f"Auto PID: resuming {self.probe_resume['source']}; probes "
                          f"{self.probe_resume['rerun']}; carried {sorted(self.probe_resume['carried'])}")
                self.probe_resume = None
            self.auto_base_cfg = copy.deepcopy(cfg)
            self.auto_report_path = output_dir() / f"single_lung_hardware_auto_pid_{time.time_ns()}.csv"
            self._set_auto_pid_status(
                f"Starting up to {self.auto_tuner.total_candidates} candidate runs"
            )
            self._start_next_auto_candidate()
            return
        self._launch_run(cfg)

    def _make_auto_tuner(self, cfg: SingleLungConfig):
        gains = (cfg.pi.kp, cfg.pi.ki, cfg.hardware_expiration_kp, cfg.hardware_expiration_ki)
        baselines = int(self.inputs["auto_baselines"].value())
        if self._probe_selected():
            plan, resume = self.probe_resume, {}
            if plan is not None:
                if plan["pair"] != self._selected_pair() or not all(
                        math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
                        for a, b in zip(plan["original"], gains)):
                    raise ValueError(
                        f"The resume plan from {plan['source']} is for the {plan['pair']} pair with "
                        f"gains {dict(zip(AUTO_PID_GAINS, plan['original']))}; the current selection "
                        "differs. Load the report again or click Clear.")
                resume = dict(probes=plan["rerun"], carried=plan["carried"])
            return SensitivityProbe(gains, pair=self._selected_pair(), baseline_runs=baselines,
                                    current_alpha_rel=self.inputs["es_alpha_rel"].value(), **resume)
        if self._es_selected():
            return ExtremumSeekingTuner(
                gains, pair=self._selected_pair(), baseline_runs=baselines,
                iterations=int(self.inputs["auto_iterations"].value()),
                alpha_rel=self.inputs["es_alpha_rel"].value(),
                gamma_rel=self.inputs["es_gamma_rel"].value(),
                a=self.inputs["es_a"].value(), h=self.inputs["es_h"].value(),
                phase_compensation=self.es_phase_compensation.isChecked())
        return CoordinateSearchTuner(gains, int(self.inputs["auto_rounds"].value()))

    def _health_scope(self) -> dict:
        tuner = self.auto_tuner
        return {"tune_inspiration": tuner.tune_inspiration, "tune_expiration": tuner.tune_expiration}

    def _confirm_auto_pid(self, cfg: SingleLungConfig, candidates: int) -> bool:
        """Auto PID chains candidate runs without further input; ask once first."""
        tuner = self.auto_tuner
        total = candidates + (1 if getattr(tuner, "needs_confirmation", True) else 0)
        stabilize = int(self._stabilize_selected())
        minutes = (total + stabilize) * auto_pid_run_estimate_s(cfg, AUTO_PID_WARMUP_CYCLES) / 60.0
        if isinstance(tuner, SensitivityProbe):
            plan = (f"The sensitivity pre-experiment ({tuner.pair} pair, cost {tuner.cost_key}) "
                    f"will run {total} hardware runs in total ({tuner.baselines.runs} baselines + "
                    f"{total - tuner.baselines.runs} probe runs at x0.6 / x1.4; gains are not "
                    f"changed) ")
            if tuner.carried:
                plan += (f"[resume: only {', '.join(p[0] for p in tuner._probes)}; carried over "
                         f"{', '.join(sorted(tuner.carried))}] ")
        elif isinstance(tuner, ExtremumSeekingTuner):
            plan = (f"Extremum seeking ({tuner.pair} pair, cost {tuner.cost_key}) will run "
                    f"{total} hardware runs in total ({tuner.baselines.runs} baselines + "
                    f"{tuner.iterations} iterations + 1 confirmation of theta_hat) ")
        else:
            plan = (f"Auto PID will run up to {candidates} hardware runs back to back, "
                    f"plus 1 confirmation run of the best gains ")
        answer = QtWidgets.QMessageBox.question(
            self, "Start Auto PID",
            plan + f"({cfg.cycles} scored cycles each + {AUTO_PID_WARMUP_CYCLES} unscored warm-up "
            f"cycle, about {minutes:.0f} min in total"
            + (", plus 1 unscored stabilization run first with the starting gains" if stabilize else "")
            + ").\n"
            "Each run starts automatically when the previous one finishes; "
            "Stop cancels at any time.\n\nStart now?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        return answer == QtWidgets.QMessageBox.Yes

    def _launch_run(self, cfg: SingleLungConfig) -> bool:
        # The finished run was already saved under its own name; only an
        # unsaved (or since extended) run is saved here, never under the name
        # just typed for the next run.
        if self.rows and self._autosave_log(skip_if_saved=True) is None:
            return False
        self.run_cfg = copy.deepcopy(cfg)
        self.run_stage = ((self.auto_stage_override or self.auto_tuner.current_stage)
                          if self.auto_tune_active and self.auto_tuner else None)
        self.run_tuning = ({
            "score_formula": OBJECTIVE["formula"],
            **{k: v for k, v in self.auto_tuner.summary_extra().items()
               if k not in ("theta_hat_trajectory", "J_curve", "best_measured", "final_decision",
                            "step_statistics", "baselines", "acceptance", "sensitivity")},
            "tuned_gains": (self.auto_tuner.summary_extra().get("tuned_gains")
                            or list(AUTO_PID_GAINS)),
            "lower": dict(zip(AUTO_PID_GAINS, self.auto_tuner.lower)),
            "upper": dict(zip(AUTO_PID_GAINS, self.auto_tuner.upper)),
            "original": dict(zip(AUTO_PID_GAINS, self.auto_tuner.original)),
        } if self.run_stage else None)
        self.run_warmup = AUTO_PID_WARMUP_CYCLES if self.run_stage else 0
        self.previous_recovery_timeout = self.run_recovery_timeout
        self.run_recovery_timeout = False
        self.run_status = "starting"
        self.run_events = []
        self.rows = []
        self.pending_plot_row: Optional[HardwareTelemetry] = None
        self.model_drawn_state, self.model_drawn_at = -1, -math.inf
        self.flow_canvas.draw_rows([])
        self.user_flow_canvas.draw_rows([])
        self.telemetry_table.setRowCount(0)
        self._draw_dynamic_model(HardwareTelemetry(
            0, 0, STATE_IDLE, 0, 0.0, 0.0, 0.0, 0.0, 5.0, 0
        ))
        if not self._write_config(cfg):
            return False
        self._sync_developer_from_config()
        self._sync_user_from_config()
        for warning in cycle_timing_summary(
                cfg.respiratory_rate_bpm, cfg.ie_inspiration, cfg.ie_expiration,
                cfg.target_volume_ml, cfg_expiration_transition_s(cfg),
                cfg.dead_time_within_cycle, cfg.hardware_inspiration_min_v,
                cfg.hardware_inspiration_dwell_min_s,
                cfg.hardware_transition_settle_s)["warnings"]:
            self._log(f"Timing warning: {warning}")
        self.current_run_id = int(time.time_ns() // 1_000_000) & 0x7FFFFFFF
        command = f"START,{self.current_run_id},{cfg.cycles}"
        try:
            self.connection.write((command + "\n").encode("ascii"))
            self.hardware_running = True
            self.run_status = "running"
            self._log(f"> {command}")
            return True
        except Exception as exc:
            self._log(f"Start failed: {exc}")
            return False

    def _start_next_auto_candidate(self) -> None:
        if not self.auto_tune_active or self.auto_tuner is None:
            return
        if self.auto_stabilize_pending:
            # Unscored: the tuner never sees this run (no stage, no record).
            cfg = copy.deepcopy(self.auto_base_cfg)
            apply_auto_pid_gains(cfg, self.auto_tuner.original)
            cfg.cycles += AUTO_PID_WARMUP_CYCLES
            self.auto_stage_override = AUTO_PID_STABILIZATION_STAGE
            self._set_auto_pid_status(
                f"Stabilization run (unscored) before {self.auto_tuner.total_candidates} "
                f"candidate runs, starting gains")
            if not self._launch_run(cfg):
                self._cancel_auto_pid("could not start the stabilization run")
            return
        self.auto_stage_override = None
        gains = self.auto_tuner.next_candidate()
        if gains is None:
            if (self.auto_tuner.confirmation_started or not self.auto_tuner.history
                    or not getattr(self.auto_tuner, "needs_confirmation", True)):
                self._finish_auto_pid()
                return
            gains = self.auto_tuner.begin_confirmation()
        cfg = copy.deepcopy(self.auto_base_cfg)
        apply_auto_pid_gains(cfg, gains)
        # Cycles keeps its meaning (scored cycles); the warm-up is added on top.
        cfg.cycles += AUTO_PID_WARMUP_CYCLES
        number = len(self.auto_tuner.history) + 1
        total = self.auto_tuner.total_candidates
        self._set_auto_pid_status(
            f"Run {number}/{total}" + (" (confirmation)" if number > total else "")
            + f": {self.auto_tuner.current_stage}; "
            f"insp Kp={gains[0]:.5f}, Ki={gains[1]:.5f}; exp Kp={gains[2]:.5f}, Ki={gains[3]:.5f}"
        )
        if not self._launch_run(cfg):
            self._cancel_auto_pid("could not start candidate")

    def _finish_auto_pid(self) -> None:
        if self.auto_tuner is None:
            return
        best, decision = self.auto_tuner.final_gains()
        for key, value in zip(("kp", "ki", "kp_out", "ki_out"), best):
            self.inputs[key].setValue(value)
        apply_auto_pid_gains(self.cfg, best)
        report_path = self._save_auto_pid_report(self.auto_tuner)
        self.auto_tune_active = False
        confirmation = self.auto_tuner.confirmation
        es = isinstance(self.auto_tuner, (ExtremumSeekingTuner, SensitivityProbe))
        if isinstance(self.auto_tuner, ExtremumSeekingTuner) and confirmation is not None:
            result = self.auto_tuner.acceptance(confirmation)
            confirm_text = (f", confirmation {result['phase_cost']} change "
                            f"{-result['phase_improvement_pct']:+.1f}% (threshold "
                            f"-{result['phase_threshold_pct']:.1f}%), full J "
                            f"{result['full_J_change_pct']:+.1f}%")
        else:
            confirm_text = (f", confirmation J={float(confirmation['score']):.4f}"
                            if confirmation is not None else "")
        score_text = (f"baseline {self.auto_tuner.cost_key}={self.auto_tuner.baseline_score:.4f}, "
                      f"noise {self.auto_tuner.noise_fraction:.1%}" if es
                      else f"J={self.auto_tuner.best_score:.4f}")
        if isinstance(self.auto_tuner, SensitivityProbe):
            sens = self.auto_tuner.analysis()
            for name, values in sens["per_gain"].items():
                self._log(f"Sensitivity {name}: g={values['g_abs_dJ_du']:.4g} per unit u, "
                          f"dJ x0.6={values['delta_J_phase']['x0.6']:+.4g}, "
                          f"x1.4={values['delta_J_phase']['x1.4']:+.4g}, SNR x0.6="
                          f"{values['snr']['x0.6']:.1f}, x1.4={values['snr']['x1.4']:.1f}")
            self._log(f"Sensitivity suggestion (not applied): alpha_rel="
                      f"{sens['suggested_alpha_rel']:.3f}, gamma_rel={sens['suggested_gamma_rel']:.3g}")
        self._set_auto_pid_status(
            f"Complete ({decision}): insp Kp={best[0]:.5f}, Ki={best[1]:.5f}; "
            f"exp Kp={best[2]:.5f}, Ki={best[3]:.5f}; "
            f"{score_text}{confirm_text}; "
            f"recovery timeouts {self.auto_recovery_timeouts}. Report: {report_path.name}"
        )
        self.statusBar().showMessage(f"Auto PID complete: {decision}; gains loaded.")
        self._log(f"Auto PID complete ({decision}); report saved to {report_path}")

    def _save_auto_pid_report(self, tuner) -> Path:
        if self.auto_report_path is None:
            self.auto_report_path = output_dir() / f"single_lung_hardware_auto_pid_{time.time_ns()}.csv"
        path = self.auto_report_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if tuner.history:
            # Rows can differ (e.g. confirmation run); keep first-seen order.
            fields = list(dict.fromkeys(key for row in tuner.history for key in row))
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(tuner.history)
        confirmation = tuner.confirmation
        final, decision = tuner.final_gains()
        probe = isinstance(tuner, SensitivityProbe)
        finished = confirmation is not None or (probe and tuner.finished)
        summary = {
            **tuner.summary_extra(),
            "final_gains": dict(zip(AUTO_PID_GAINS, final)),
            "final_decision": (decision if finished
                               else tuner.unfinished_text() if probe else "not finished"),
            "original_gains": dict(zip(AUTO_PID_GAINS, tuner.original)),
            "best_gains": dict(zip(AUTO_PID_GAINS, tuner.best_gains)),
            "best_score": tuner.best_score,
            # ES ranks by its phase cost (J_phase_raw), coordinate search by score.
            "best_stage": next((row["stage"] for row in tuner.history
                                if row["stage"] != "confirm_best"
                                and row.get("J_phase_raw", row.get("score")) == tuner.best_score), None),
            "confirmation_score": confirmation["score"] if confirmation else None,
            "confirmation_vs_best_fraction": (
                abs(confirmation["score"] - tuner.best_score) / tuner.best_score
                if confirmation and isinstance(tuner, CoordinateSearchTuner)
                and 0 < tuner.best_score < math.inf else None),
            "baseline_score": tuner.baseline_score,
            "noise_fraction": tuner.noise_fraction,
            "noise_source": tuner.noise_source,
            "significance_rule": getattr(tuner, "significance_rule",
                                         "candidate improvement vs baseline > 3 x noise_fraction"),
            "warmup_cycles_per_run": AUTO_PID_WARMUP_CYCLES,
            # Unscored runs before the baselines; never part of noise or search.
            "stabilization_runs": list(self.auto_stabilization_runs),
            "recovery_timeouts": self.auto_recovery_timeouts,
            "runs_started_after_recovery_timeout": [
                row["stage"] for row in tuner.history if row.get("start_after_recovery_timeout")],
            "objective": OBJECTIVE,
        }
        path.with_name(path.stem + "_summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return path

    def _finish_stabilization_run(self, run_id: int, saved_path: Optional[Path]) -> None:
        """The unscored run is logged and listed in the report, then the
        baselines start.  Its score never reaches the tuner."""
        self.auto_stabilize_pending = False
        self.auto_stage_override = None
        entry: Dict[str, object] = {
            "run_id": run_id, "log_csv": str(saved_path) if saved_path else None,
            "recovery_timeout": self.run_recovery_timeout,
            "start_after_recovery_timeout": self.previous_recovery_timeout}
        try:
            metrics = hardware_run_metrics(self.rows, self.run_cfg, run_completed=True,
                                           warmup_cycles=self.run_warmup)
            entry.update({key: metrics[key] for key in (
                "score", "J_Q_in", "J_Q_out", "J_V", "mean_tidal_ml", "N_c")})
            entry["health_issues"] = auto_pid_health_issues(metrics, **self._health_scope())
            self._log(f"Auto PID stabilization run (unscored): J={metrics['score']:.4f}, "
                      f"mean tidal={metrics['mean_tidal_ml']:.1f} mL")
        except ValueError as exc:
            entry["score_error"] = str(exc)
            self._log(f"Auto PID stabilization run could not be scored ({exc}); continuing")
        self.auto_stabilization_runs.append(entry)
        try:
            self._save_auto_pid_report(self.auto_tuner)
        except OSError as exc:
            self._log(f"Could not save Auto PID report: {exc}")
        QtCore.QTimer.singleShot(250, self._start_next_auto_candidate)

    def _record_incomplete_probe(self) -> None:
        """Stopped/faulted during a probe run: keep it in the report as
        incomplete (with the cycles it did complete)."""
        tuner = self.auto_tuner
        if (not isinstance(tuner, SensitivityProbe) or tuner.current_gains is None
                or not tuner.current_stage.startswith("probe_")
                or self.run_stage != tuner.current_stage):
            return
        requested = (self.run_cfg.cycles - self.run_warmup) if self.run_cfg else 0
        metrics = None
        if self.rows and self.run_cfg is not None:
            try:
                metrics = hardware_run_metrics(self.rows, self.run_cfg, warmup_cycles=self.run_warmup)
            except ValueError:
                metrics = None
        stage = tuner.current_stage
        tuner.record_incomplete(metrics, requested, run_id=self.current_run_id)
        self._log(f"Auto PID: {stage} marked incomplete "
                  f"({tuner.incomplete[stage]['complete_cycles']}/{requested} cycles)")

    def load_probe_resume(self, path: Optional[Path] = None) -> bool:
        """Arm a sensitivity probe that runs only the stages missing from a report."""
        if path is None:
            name, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Sensitivity probe report", str(output_dir()),
                "Auto PID summary (*_summary.json)")
            if not name:
                return False
            path = Path(name)
        try:
            plan = probe_resume_plan(json.loads(Path(path).read_text(encoding="utf-8")),
                                     source=Path(path).name)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QtWidgets.QMessageBox.warning(self, "Resume probe", f"Cannot use {Path(path).name}: {exc}")
            return False
        if not plan["rerun"]:
            QtWidgets.QMessageBox.information(
                self, "Resume probe", f"{Path(path).name}: every probe is complete; nothing to resume.")
            return False
        self.auto_method.setCurrentIndex(self.auto_method.findData(ExtremumSeekingTuner.method))
        self.es_probe.setChecked(True)
        self.es_pair.setCurrentIndex(self.es_pair.findData(plan["pair"]))
        for key, value in zip(("kp", "ki", "kp_out", "ki_out"), plan["original"]):
            self.inputs[key].setValue(value)
        apply_auto_pid_gains(self.cfg, plan["original"])
        self._sync_user_from_config()
        self.probe_resume = plan
        self._set_auto_pid_status(
            f"Resume armed from {plan['source']}: baselines + {', '.join(plan['rerun'])}; "
            f"carried {', '.join(sorted(plan['carried'])) or 'none'}. Press Start.")
        self._update_auto_pid_plan()
        return True

    def clear_probe_resume(self) -> None:
        if self.probe_resume is not None:
            self.probe_resume = None
            self._set_auto_pid_status("Resume plan cleared")
            self._update_auto_pid_plan()

    def _auto_pid_abnormal_run(self, run_id: int, reason: str) -> None:
        """ES: an unscorable/aborted iteration counts as J = 3 J_base and the
        search continues (stops after 3 in a row).  Baseline failures and the
        coordinate search still cancel.  FAULT/ERR always cancel elsewhere."""
        tuner = self.auto_tuner
        if not (isinstance(tuner, ExtremumSeekingTuner) and tuner.current_gains is not None
                and not tuner.current_stage.startswith("baseline")):
            self._cancel_auto_pid("candidate could not be scored")
            return
        tuner.record_failure(reason)
        tuner.history[-1].update({"run_id": run_id, "recovery_timeout": self.run_recovery_timeout,
                                  "start_after_recovery_timeout": self.previous_recovery_timeout})
        self._save_auto_pid_report(tuner)
        if tuner.consecutive_failures >= ES_MAX_CONSECUTIVE_FAILURES:
            self._cancel_auto_pid(f"{ES_MAX_CONSECUTIVE_FAILURES} abnormal runs in a row")
            return
        self._log(f"Auto PID: abnormal run recorded as J = {ES_FAILURE_FACTOR:g} x J_base ({reason})")
        QtCore.QTimer.singleShot(250, self._start_next_auto_candidate)

    def _set_auto_pid_status(self, text: str) -> None:
        self.auto_pid_status.setText(text)
        self.user_auto_status.setText(f"Auto PID: {text}")

    def _cancel_auto_pid(self, reason: str) -> None:
        if self.auto_tune_active:
            self.auto_tune_active = False
            self.auto_stabilize_pending = False
            self.auto_stage_override = None
            self._set_auto_pid_status(f"Cancelled: {reason}")
            self.statusBar().showMessage(f"Auto PID cancelled: {reason}")
            if self.auto_tuner is not None:
                self._record_incomplete_probe()
                # Each candidate run writes its gains into the UI/config; restore
                # the gains the search started from so the next start does not
                # silently begin at the last (dithered or probed) candidate.
                original = self.auto_tuner.original
                for key, value in zip(("kp", "ki", "kp_out", "ki_out"), original):
                    self.inputs[key].setValue(value)
                apply_auto_pid_gains(self.cfg, original)
                self._sync_user_from_config()
                self._log("Auto PID cancelled; gains restored to the starting values "
                          f"{dict(zip(AUTO_PID_GAINS, original))}")
                try:
                    self._save_auto_pid_report(self.auto_tuner)
                except OSError as exc:
                    self._log(f"Could not save Auto PID report: {exc}")

    def stop_run(self) -> None:
        self._cancel_auto_pid("stopped by user")
        self.run_status = "stop_requested"
        if self.rows:
            self._autosave_log()
        if self.connection is None:
            self._log("Not connected.")
            return
        self.connection.write(b"STOP\n")
        self._log("> STOP")

    def _autosave_log(self, skip_if_saved: bool = False) -> Optional[Path]:
        """Automatic save: a run already saved keeps its own file pair."""
        if not self.rows:
            # e.g. a late STOPPED after reconnecting: nothing to save (this
            # previously wrote an empty <name>_0 pair).
            return None
        run_id = self.current_run_id or self.rows[0].run_id
        saved = self.saved_log_paths.get(run_id)
        if (saved is not None and skip_if_saved
                and self.saved_log_rows.get(run_id) == len(self.rows)):
            return saved
        return self.save_current_log(path=saved)

    def save_current_log(self, path: Optional[Path] = None) -> Optional[Path]:
        """Save the current run; without `path`, under the name typed now."""
        if not self.rows:
            self._log("No telemetry to save.")
            return None
        run_id = self.current_run_id or (self.rows[0].run_id if self.rows else 0)
        auto_stage = self.run_stage
        # Auto PID candidates are always compact (25-33 MB each with context);
        # configuration and scores are in the JSON pair either way.
        compact = self.compact_csv or bool(auto_stage)
        if path is None:
            path = resolve_log_path(self.log_path_text, run_id)
            path = (auto_pid_log_path(path, auto_stage, run_id) if auto_stage
                    else with_run_id(path, run_id))
        # Repeated saves of a run update its pair; reusing a user filename for
        # another run must preserve the earlier experiment.
        if path.exists():
            try:
                existing_id = json.loads(path.with_suffix(".json").read_text(encoding="utf-8")).get("run_id")
            except (OSError, ValueError):
                existing_id = None
            if existing_id != run_id:
                path = path.with_name(f"{path.stem}_{run_id}{path.suffix}")
        metadata = {
            "protocol_version": PROTOCOL_VERSION,
            "requested_hardware_timing": {
                "control_hz": HARDWARE_CONTROL_HZ,
                "telemetry_hz": HARDWARE_TELEMETRY_HZ,
                "serial_baud": BAUD,
                "note": "Nominal firmware schedule; verify actual telemetry timestamps and device INFO.",
            },
            "pid_output_policy": {
                "inspiration_integral_limit_source": "configuration.pi.integral_limit",
                "expiration_integral_limit_source": "configuration.hardware_expiration_integral_limit",
                "gain_selection": "inspiration: configuration.pi; expiration: configuration.hardware_expiration_kp/ki/kd",
                "inspiration_min_voltage_source": "configuration.hardware_inspiration_min_v; caps PI output and FF at u_max = G(min V); 0 V means u_max = 1",
                "inspiration_pi_output_unit": "ATM conductance relative to 0 V; continuous voltage map (PM power law within 0.10 V of closure, 2026-10-14 AM sweep shifted +0.05 V below); 1 = 0 V",
                "inspiration_atm_cut": "per-cycle inspired net-flow integral >= target_volume_ml; safety cap: last-cycle residual + current cycle >= max_lung_volume_ml + 50 mL",
                "volume_ml_telemetry": "safety estimate restarted each inspiration from max(0, previous cycle inspired - UNCOMPENSATED expired); expiration also integrates uncompensated sensor flow; not a cumulative bag volume",
                "leak_compensation_scope": "VAC leak model affects only net_flow_slm (PI feedback, cycle_expired_ml telemetry, scoring); not the volume guard or residual estimate",
                "expiration_scheme": "v8 (2026-10-14 PM build)",
                "expiration_closure": "Protocol 20: VAC zero opening = configuration.hardware_expiration_closure_v; VAC map shifted (volts per opening = lung closure - dead_zone_out), so caps and gains keep their meaning; also the transition base and the VAC leak roll-off; inspiration keeps configuration.lung.hardware_closure_v",
                "expiration_transition": "Protocol 19/20: PPV commanded to hardware_expiration_closure_v + configuration.hardware_transition_overclose_v as soon as ATM is cut (or at inspiration end); VAC opens configuration.hardware_transition_settle_s after the estimated position reaches that target (response-based: the stroke actually needed + PPV lag) AND the close command is >= configuration.hardware_transition_dwell_min_s old (optional floor); command returns to closureV when VAC opens; no 5 V detour",
                "inspiration_transition": "Protocol 18: VAC and ATM closed at DEAD_TO_INSP entry; ATM opens when the estimated PPV position has arrived AND both routes have been closed for >= configuration.hardware_inspiration_dwell_min_s (VAC flow tail, ~80 ms in 2026-10-19 runs)",
                "cycle_timing": "configuration.dead_time_within_cycle (protocol 21): at INSPIRATION entry the period minus the measured DEAD_TO_INSP and the last cycle's measured DEAD_TO_EXP (cycle 0: max(dwell, settle)) is split by I:E; inspiration reference = 0.06*target/min(fill time, that inspiration); expiration = 60/bpm - time since cycle start (DEAD_TO_INSP entry; cycle 0: INSPIRATION entry), floor 0.25 s; otherwise nominal I:E phases",
                "expiration_pi_output_unit": "model opening, same coordinate as inspiration; FF and D disabled; Smith feedback when enabled",
                "expiration_opening_cap": {
                    "opening": [0.1883, 0.3451, 0.4707],
                    "breakpoints_ms": [250, 750, "phase end"],
                    "approx_volts_at_Uc_4.30_Ud_4.1535": [4.2724, 4.2499, 4.2310],
                },
                "expiration_reference": "per-cycle constant 0.06 * target_volume_ml / max(0.25 s, actual expiration duration fixed at VAC opening); transmitted per sample and used for scoring",
                "expiration_removed_v13_features": ["flow guard", "5 V recovery and 500 ms settle", "0.06 V/s slew", "60/10 mL taper and closure"],
                "expiration_budget": "telemetry only: min(target_volume_ml, measured per-cycle inspired integral)",
            },
            "model_reference": "2026-10-14_PM_UC430; afternoon high-voltage fit, 26 accepted segments; see analysis/high_voltage_2026-10-14/fit.json in flow_control_exp; high-range gamma/Ud with retained 0 V tau; fitted model amplitude 320.3 mL; low-range extrapolation and dynamic switching unvalidated; VAC baseline 1.0122 SLM, provisional 4.259-4.300 V roll-off; D8 input removed; PPV stroke 2.436 s per 4.2 V retained",
            "auto_pid_stage": auto_stage,
            "auto_pid_settings": getattr(self, "run_tuning", None),
            "configuration": asdict(self.run_cfg or self.cfg),
            "configuration_source": "frozen_host_run_configuration; CFG/START and acknowledgements in events; MCU does not echo all parameters",
            "schema_version": 2,
            "run_id": run_id,
            "run_status": self.run_status,
            "saved_at_utc": datetime.now(timezone.utc).isoformat(),
            "objective": OBJECTIVE,
            "events": list(self.run_events),
            "telemetry_fields": list(HardwareTelemetry.__dataclass_fields__),
            "telemetry_limitations": "Only MCU-transmitted runtime values are available; internal PID integrator and terms are not transmitted. Volume integration has telemetry sampling resolution.",
        }
        run_cfg = self.run_cfg or self.cfg
        metadata.update({
            "auto_pid_warmup_cycles": self.run_warmup,
            "recovery_timeout": self.run_recovery_timeout,
            "previous_run_recovery_timeout": self.previous_recovery_timeout,
            "start_state_note": ("previous run's recovery timed out; starting bag volume may "
                                 "not be comparable" if self.previous_recovery_timeout else None),
            "csv_layout": "telemetry_only" if compact else "telemetry_plus_context",
        })
        try:
            metadata["metrics"] = hardware_run_metrics(
                self.rows, run_cfg, run_completed=self.run_status == "completed",
                warmup_cycles=self.run_warmup)
            complete = (self.run_status == "completed"
                        and metadata["metrics"]["N_c"] == run_cfg.cycles - self.run_warmup)
            metadata["score_status"] = "complete" if complete else "partial_complete_cycles_only"
            if complete:
                # Auto PID precheck and repeat noise: score like a candidate.
                self.last_run_metrics = auto_pid_repeat_metrics(
                    self.rows, run_cfg, metadata["metrics"], self.run_warmup)
                self.last_run_cfg = copy.deepcopy(run_cfg) if self.last_run_metrics else None
        except ValueError as exc:
            metadata.update(metrics=None, score_status="unavailable", score_error=str(exc))
        try:
            save_log(self.rows, path, metadata, include_context=not compact)
            path.with_suffix(".json").write_text(
                json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except (OSError, ValueError) as exc:
            message = f"Could not save CSV: {exc}"
            self._set_log_status(message)
            self.statusBar().showMessage(message)
            self._log(message)
            return None
        self.saved_log_paths[run_id] = path
        self.saved_log_rows[run_id] = len(self.rows)
        self._set_log_status(f"Saved: {path}")
        self.statusBar().showMessage(f"Saved {path.name}")
        self._log(f"Saved {path} ({len(self.rows)} rows)")
        return path

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
        self._refresh_live_plots()

    def _refresh_live_plots(self) -> None:
        """Draw the newest telemetry once: visible flow plots every tick, the
        schematic at most every MODEL_REFRESH_S or when the state changes."""
        row = self.pending_plot_row
        if row is None:
            return
        self.pending_plot_row = None
        for canvas in (self.flow_canvas, self.user_flow_canvas):
            if canvas.isVisible():
                canvas.draw_rows(self.rows)
        now = time.monotonic()
        if row.state != self.model_drawn_state or now - self.model_drawn_at >= MODEL_REFRESH_S:
            self._draw_dynamic_model(row)
            self.model_drawn_state, self.model_drawn_at = row.state, now

    def _handle_line(self, line: str) -> None:
        self._log(f"< {line}")
        if line.startswith(("STARTED,", "RECOVERING,", "STOPPED,", "FAULT,")):
            try:
                message_run_id = int(line.split(",")[1])
            except (IndexError, ValueError):
                return
            if self.current_run_id is not None and message_run_id != self.current_run_id:
                self._log(f"Ignored stale run event: {line}")
                return
        if line == READY:
            self.hardware_running = False
            self.firmware_ready = True
            self.statusBar().showMessage(line)
        elif line.startswith("READY,"):
            self.firmware_ready = False
            self.statusBar().showMessage("Wrong firmware for single-lung STM32 control")
            self._log(f"Expected {READY}; refusing {line}")
        elif line.startswith("ACK,MANUAL,"):
            parts = line.split(",")
            if len(parts) == 4:
                mask = int(parts[2])
                self.manual_status.setText(
                    f"ATM {'open' if mask & 1 else 'closed'} | VAC {'open' if mask & 2 else 'closed'} | PPV command {parts[3]} V"
                )
        elif line.startswith("STARTED,"):
            self.hardware_running = True
        elif line.startswith("RECOVERING,"):
            self.hardware_running = True
            parts = line.split(",")
            reason = parts[2] if len(parts) > 2 else "STOP"
            if reason == "DONE":
                message = "Cycles complete; restoring bag volume with ATM open and PPV fully open..."
            else:
                message = "Stopping; restoring bag volume with ATM open and PPV fully open..."
            self.manual_status.setText(message)
            self.statusBar().showMessage(message)
        elif line.startswith("STOPPED,"):
            self.hardware_running = False
            self.run_status = "stopped"
            self.manual_status.setText(
                "Stopped after volume recovery: ATM/VAC closed, PPV fully open (0 V)."
            )
            self.statusBar().showMessage("Hardware stopped after bag-volume recovery")
            self._autosave_log()
        elif line.startswith("TEL,"):
            try:
                row = HardwareTelemetry.parse(line)
            except ValueError:
                return
            if self.current_run_id is not None and row.run_id != self.current_run_id:
                self._log(f"Ignored telemetry from stale run {row.run_id}.")
                return
            self.rows.append(row)
            self._update_metrics(row)
            # Plots are refreshed once per timer tick (_refresh_live_plots),
            # not per line, so a backlog cannot multiply the drawing work.
            self.pending_plot_row = row
            self._append_table_row(row)
        elif line.startswith("DONE,"):
            if self.run_status == "completed":
                return
            try:
                done_run_id = int(line.split(",", 1)[1])
            except (IndexError, ValueError):
                return
            if self.current_run_id is not None and done_run_id != self.current_run_id:
                self._log(f"Ignored completion from stale run {done_run_id}.")
                return
            self.hardware_running = False
            self.run_status = "completed"
            self.statusBar().showMessage("Hardware run complete")
            self.flow_canvas.draw_rows(self.rows)
            self.user_flow_canvas.draw_rows(self.rows)
            if self.rows:
                self._draw_dynamic_model(self.rows[-1])
            saved_path = self._autosave_log()
            if (self.auto_tune_active and self.auto_tuner is not None
                    and self.run_stage == AUTO_PID_STABILIZATION_STAGE):
                self._finish_stabilization_run(done_run_id, saved_path)
                return
            if self.auto_tune_active and self.auto_tuner is not None:
                try:
                    if saved_path is None:
                        raise ValueError("candidate log could not be saved")
                    metrics = hardware_run_metrics(self.rows, self.run_cfg, run_completed=True,
                                                   warmup_cycles=self.run_warmup)
                    if metrics["N_c"] != self.run_cfg.cycles - self.run_warmup:
                        raise ValueError("candidate is missing complete requested cycles")
                    stage = self.auto_tuner.current_stage
                    self.auto_tuner.record(metrics)
                    self.auto_tuner.history[-1].update({
                        "run_id": done_run_id, "log_csv": str(saved_path),
                        "log_json": str(saved_path.with_suffix(".json")),
                        "objective_json": json.dumps(OBJECTIVE, ensure_ascii=False),
                        "auto_pid_settings_json": json.dumps(getattr(self, "run_tuning", None)),
                        **flatten_parameters(asdict(self.run_cfg)),
                        "warmup_cycles": self.run_warmup,
                        "recovery_timeout": self.run_recovery_timeout,
                        "start_after_recovery_timeout": self.previous_recovery_timeout,
                    })
                    self._save_auto_pid_report(self.auto_tuner)
                    self._log(
                        "Auto PID result: "
                        f"J={metrics['score']:.4f}, J_Q={metrics['J_Q']:.4f}, J_V={metrics['J_V']:.4f}, "
                        f"mean tidal={metrics['mean_tidal_ml']:.1f} mL, "
                        f"saturation={metrics['saturation_fraction']:.0%}, "
                        f"pinned exp={metrics['pinned_fraction_exp']:.0%}"
                        + (" [started after a recovery timeout: start state may not be comparable]"
                           if self.previous_recovery_timeout else "")
                    )
                    if stage.startswith("baseline"):
                        issues = auto_pid_health_issues(metrics, **self._health_scope())
                        if issues and not self._health_override():
                            self._log("Auto PID health check failed: " + "; ".join(issues))
                            self._cancel_auto_pid("baseline failed health check: " + "; ".join(issues))
                            return
                        if issues:
                            self._log("Auto PID health check ignored: " + "; ".join(issues))
                    # DONE is sent after the firmware recovery phase has ended
                    # (WARN,RECOVERY_TIMEOUT, if any, arrives first).
                    QtCore.QTimer.singleShot(250, self._start_next_auto_candidate)
                except Exception as exc:
                    self._log(f"Auto PID scoring error: {exc}")
                    self._auto_pid_abnormal_run(done_run_id, f"scoring failed: {exc}")
        elif line.startswith("FAULT,") or line.startswith("ERR,"):
            # The D8 emergency-stop input is not used (firmware ESTOP_ENABLED=0).
            start_rejected = line in ("ERR,CONFIG_REQUIRED", "ERR,START_FORMAT")
            if line.startswith("FAULT,") or start_rejected:
                self.hardware_running = False
            self.run_status = "fault" if line.startswith("FAULT,") else "error"
            message = line
            self.manual_status.setText(message)
            self._cancel_auto_pid(line)
            if self.rows:
                self._autosave_log()
            self.statusBar().showMessage(message)
        elif line.startswith("WARN,RECOVERY_TIMEOUT,"):
            try:
                warn_run_id = int(line.split(",")[2])
            except (IndexError, ValueError):
                warn_run_id = self.current_run_id
            if self.current_run_id is None or warn_run_id == self.current_run_id:
                self.run_recovery_timeout = True
                if self.auto_tune_active:
                    self.auto_recovery_timeouts += 1
            self.manual_status.setText(
                "Recovery timed out: ATM/VAC were closed before target volume was reached."
            )
            self.statusBar().showMessage(self.manual_status.text())
        elif line.startswith(SERIAL_LOST):
            self.statusBar().showMessage("Serial connection lost")
            self.disconnect_serial(save=True)

    def _update_metrics(self, row: HardwareTelemetry) -> None:
        mask = row.valve_mask
        atm = bool(mask & 1)
        vac = bool(mask & 2)
        self.metrics["time"].setText(f"{row.time_ms / 1000.0:.2f} s")
        self.metrics["state"].setText(STATE_NAMES.get(row.state, "UNKNOWN"))
        shown_cycle = min(row.cycle + 1, self.cfg.cycles)
        self.metrics["cycle"].setText(f"{shown_cycle} / {self.cfg.cycles}")
        self.metrics["flow"].setText(f"{row.flow_slm:+.2f} SLM")
        self.metrics["reference"].setText(f"{row.flow_reference_slm:+.2f} SLM")
        self.metrics["volume"].setText(f"{row.volume_ml:.1f} mL")
        opening_text = f"{row.opening * 100.0:.1f} %"
        self.metrics["opening"].setText(opening_text)
        self.metrics["command"].setText(f"{row.command_v:.2f} V")
        self.metrics["valves"].setText(f"{'ATM open' if atm else 'ATM closed'}, {'VAC open' if vac else 'VAC closed'}")
        self.user_metrics["state"].setText(STATE_NAMES.get(row.state, "UNKNOWN"))
        self.user_metrics["cycle"].setText(f"{shown_cycle} / {self.cfg.cycles}")
        self.user_metrics["flow"].setText(f"{row.flow_slm:+.2f} SLM")
        self.user_metrics["volume"].setText(f"{row.volume_ml:.1f} mL")
        self.user_metrics["opening"].setText(opening_text)

    def _append_table_row(self, row: HardwareTelemetry) -> None:
        opening_text = f"{row.opening * 100.0:.1f} %"
        if self.telemetry_table.rowCount() > 250:
            self.telemetry_table.removeRow(0)
        i = self.telemetry_table.rowCount()
        self.telemetry_table.insertRow(i)
        values = [
            f"{row.time_ms / 1000.0:.2f}",
            STATE_NAMES.get(row.state, "UNKNOWN"),
            str(row.cycle + 1),
            f"{row.flow_slm:+.2f}",
            f"{row.volume_ml:.1f}",
            opening_text,
            f"{row.valve_mask:02b}",
        ]
        for col, value in enumerate(values):
            self.telemetry_table.setItem(i, col, QtWidgets.QTableWidgetItem(value))
        self.telemetry_table.scrollToBottom()

    def _log(self, text: str) -> None:
        if text.startswith(("> ", "< ")):
            self.run_events.append({"host_time_utc": datetime.now(timezone.utc).isoformat(), "message": text})
        self.log.append(text)

    def closeEvent(self, event) -> None:
        self.disconnect_serial(save=True)
        event.accept()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--dry-run", action="store_true", help="print the serial commands without opening the GUI")
    args = parser.parse_args()
    if args.dry_run:
        cfg = load_config(Path(args.config))
        token = 12345
        print(config_command(cfg, token))
        print(f"START,{token},{cfg.cycles}")
        return 0

    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QtGui.QFont("Segoe UI", 9))
    window = MainWindow(Path(args.config))
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
