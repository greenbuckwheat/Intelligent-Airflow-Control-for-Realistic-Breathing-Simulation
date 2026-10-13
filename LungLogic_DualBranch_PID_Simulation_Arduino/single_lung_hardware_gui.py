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
import queue
import sys
import threading
import time
from dataclasses import asdict, dataclass
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
    CONFIG_PATH,
    STATE_EXPIRATION,
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
PROTOCOL_VERSION = 8
READY = "READY,LUNGLOGIC_SINGLE_G474,8"
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

    @property
    def control_flow_slm(self) -> float:
        return self.net_flow_slm if math.isfinite(self.net_flow_slm) else self.flow_slm

    @classmethod
    def parse(cls, line: str) -> "HardwareTelemetry":
        fields = line.strip().split(",")
        if len(fields) not in (11, 13, 16, 19) or fields[0] != "TEL":
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
            float(fields[16]) if len(fields) == 19 else math.nan,
            float(fields[17]) if len(fields) == 19 else math.nan,
            float(fields[18]) if len(fields) == 19 else math.nan,
        )


def hardware_run_metrics(
    rows: List[HardwareTelemetry], cfg: SingleLungConfig
) -> Dict[str, float]:
    """Score one completed hardware run for bounded run-to-run PI tuning.

    Lower is better.  Flow tracking, inspired tidal-volume error and actuator
    saturation are reported separately as well as combined into a transparent
    project-specific cost.  Initial precondition/dead-time samples are ignored.
    """
    active = [row for row in rows if row.state in (STATE_INSPIRATION, STATE_EXPIRATION)]
    if not active:
        raise ValueError("run contains no inspiration or expiration telemetry")
    references = [abs(row.flow_reference_slm) for row in active]
    errors = [abs(row.control_flow_slm) - reference for row, reference in zip(active, references)]
    reference_scale = max(1.0, sum(references) / len(references))
    flow_rmse = math.sqrt(sum(error * error for error in errors) / len(errors))
    flow_mae = sum(abs(error) for error in errors) / len(errors)

    tidal_values: List[float] = []
    cycles = sorted({row.cycle for row in rows if row.state == STATE_INSPIRATION})
    for cycle in cycles:
        inspiration = [
            row.volume_ml for row in rows
            if row.cycle == cycle and row.state == STATE_INSPIRATION
        ]
        if inspiration:
            tidal_values.append(max(inspiration) - min(inspiration))
    if not tidal_values:
        raise ValueError("run contains no complete inspiratory telemetry")
    mean_tidal = sum(tidal_values) / len(tidal_values)
    volume_error = sum(
        abs(value - cfg.target_volume_ml) for value in tidal_values
    ) / (len(tidal_values) * max(cfg.target_volume_ml, 1.0))
    saturation = sum(row.opening >= 0.999 for row in active) / len(active)
    score = flow_rmse / reference_scale + 0.5 * flow_mae / reference_scale
    score += 2.0 * volume_error + 0.05 * saturation
    return {
        "score": score,
        "flow_rmse_slm": flow_rmse,
        "flow_mae_slm": flow_mae,
        "mean_tidal_ml": mean_tidal,
        "mean_tidal_error_pct": 100.0 * volume_error,
        "saturation_fraction": saturation,
    }


class AutoPIDTuner:
    """Bounded coordinate search over Kp and Ki using completed hardware runs.

    This is deliberately run-to-run and leaves Kd unchanged.  Each candidate is
    evaluated using the same breathing settings and number of cycles.  It is a
    conservative engineering search, not a claim that the STM32 performs an
    online self-tuning algorithm.
    """

    def __init__(self, kp: float, ki: float, rounds: int = 2,
                 perturbation_log: float = 0.12) -> None:
        if kp <= 0.0 or ki <= 0.0:
            raise ValueError("Auto PID requires positive starting Kp and Ki")
        if not 1 <= rounds <= 5:
            raise ValueError("Auto PID rounds must be 1..5")
        self.original = (kp, ki)
        self.rounds = rounds
        self.perturbation_log = perturbation_log
        self.lower = (0.25 * kp, 0.25 * ki)
        self.upper = (4.0 * kp, 4.0 * ki)
        self.best_gains = self.original
        self.best_score = math.inf
        self.history: List[Dict[str, float | str]] = []
        self._round = 0
        self._queue: List[Tuple[str, Tuple[float, float]]] = [("baseline", self.original)]
        self.current_stage = ""
        self.current_gains: Optional[Tuple[float, float]] = None
        self._seen: set[Tuple[float, float]] = set()

    def _bounded(self, gains: Tuple[float, float]) -> Tuple[float, float]:
        return (
            min(self.upper[0], max(self.lower[0], gains[0])),
            min(self.upper[1], max(self.lower[1], gains[1])),
        )

    def _prepare_round(self) -> None:
        self._round += 1
        step = self.perturbation_log / (2 ** (self._round - 1))
        factor = math.exp(step)
        kp, ki = self.best_gains
        self._queue = [
            (f"round_{self._round}_kp_plus", self._bounded((kp * factor, ki))),
            (f"round_{self._round}_kp_minus", self._bounded((kp / factor, ki))),
            (f"round_{self._round}_ki_plus", self._bounded((kp, ki * factor))),
            (f"round_{self._round}_ki_minus", self._bounded((kp, ki / factor))),
        ]

    def next_candidate(self) -> Optional[Tuple[float, float]]:
        if self.current_gains is not None:
            raise RuntimeError("record the current Auto PID result first")
        while True:
            if not self._queue:
                if self._round >= self.rounds:
                    return None
                self._prepare_round()
            stage, gains = self._queue.pop(0)
            key = (round(gains[0], 10), round(gains[1], 10))
            if key in self._seen:
                continue
            self._seen.add(key)
            self.current_stage = stage
            self.current_gains = gains
            return gains

    def record(self, metrics: Dict[str, float]) -> None:
        if self.current_gains is None:
            raise RuntimeError("no Auto PID candidate is awaiting a result")
        row: Dict[str, float | str] = {
            "stage": self.current_stage,
            "kp": self.current_gains[0],
            "ki": self.current_gains[1],
            **metrics,
        }
        self.history.append(row)
        if metrics["score"] < self.best_score:
            self.best_score = metrics["score"]
            self.best_gains = self.current_gains
        self.current_gains = None


class SerialReader(threading.Thread):
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


def config_command(cfg: SingleLungConfig, token: int) -> str:
    p = cfg.lung
    values = (p.hardware_model_volume_ml, p.hardware_gamma_in, p.hardware_gamma_out,
              p.hardware_dead_zone_in_v, p.hardware_dead_zone_out_v, p.hardware_closure_v)
    if not all(math.isfinite(v) for v in values) or not (
        cfg.target_volume_ml <= p.hardware_model_volume_ml <= 5000
        and 0.05 <= p.hardware_gamma_in <= 5 and 0.05 <= p.hardware_gamma_out <= 5
        and 0 <= p.hardware_dead_zone_in_v < p.hardware_closure_v <= 5
        and 0 <= p.hardware_dead_zone_out_v < p.hardware_closure_v
    ):
        raise ValueError("Invalid empirical directional PPV model")
    calibration = (p.hardware_flow_offset_slm, p.hardware_flow_gain, p.hardware_vac_leak_slm)
    if not all(math.isfinite(v) for v in calibration) or not (
        -5 <= calibration[0] <= 5 and 0.1 <= calibration[1] <= 5 and 0 <= calibration[2] <= 20
    ):
        raise ValueError("Invalid hardware flow calibration")
    if cfg.dead_time_s != 0:
        raise ValueError("Fixed dead time is disabled; use PPV positioning")
    times = (cfg.smith.ppv_open_time_s, cfg.smith.ppv_close_time_s)
    if not all(math.isfinite(t) and 0 < t <= 60 for t in times):
        raise ValueError("PPV positioning requires measured stroke times within (0, 60] s")
    if cfg.smith.enabled and min(times) <= 0:
        raise ValueError("Measure both PPV stroke times before enabling hardware Smith")
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
    ]))


def save_log(rows: List[HardwareTelemetry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "run_id",
            "time_s",
            "state",
            "state_name",
            "cycle",
            "flow_slm",
            "volume_ml",
            "flow_reference_slm",
            "opening",
            "command_v",
            "valve_mask",
            "estimated_ppv_v",
            "ppv_remaining_s",
            "raw_flow_slm",
            "net_flow_slm",
            "sampled_valve_mask",
            "pid_feedback_slm",
            "smith_ideal_flow_slm",
            "smith_actuator_flow_slm",
        ])
        for row in rows:
            writer.writerow([
                row.run_id,
                row.time_ms / 1000.0,
                row.state,
                STATE_NAMES.get(row.state, "UNKNOWN"),
                row.cycle,
                row.flow_slm,
                row.volume_ml,
                row.flow_reference_slm,
                row.opening,
                row.command_v,
                row.valve_mask,
                row.estimated_ppv_v,
                row.ppv_remaining_s,
                row.raw_flow_slm,
                row.net_flow_slm,
                row.sampled_valve_mask,
                row.pid_feedback_slm,
                row.smith_ideal_flow_slm,
                row.smith_actuator_flow_slm,
            ])


def resolve_log_path(raw_name: str, run_id: int) -> Path:
    """Resolve a user-entered CSV name relative to the application directory."""
    raw_name = raw_name.strip()
    if not raw_name:
        return SCRIPT_DIR / "output" / f"single_lung_hardware_run_{run_id}.csv"
    path = Path(raw_name).expanduser()
    if not path.is_absolute():
        path = (SCRIPT_DIR / "output" / path) if path.parent == Path(".") else (SCRIPT_DIR / path)
    if path.suffix.lower() != ".csv":
        path = path.with_suffix(".csv")
    return path


def auto_pid_log_path(path: Path, stage: str, run_id: int) -> Path:
    """Give every Auto PID candidate its own CSV/JSON pair."""
    safe_stage = "".join(char if char.isalnum() else "_" for char in stage).strip("_")
    safe_stage = safe_stage or "candidate"
    return path.with_name(f"{path.stem}_{safe_stage}_{run_id}{path.suffix}")


class FlowCanvas(FigureCanvas):
    def __init__(self) -> None:
        self.figure = Figure(figsize=(7.5, 4.0), tight_layout=True)
        super().__init__(self.figure)
        self.ax = self.figure.add_subplot(111)
        self.draw_rows([])

    def draw_rows(self, rows: List[HardwareTelemetry], window_s: float = 10.0) -> None:
        self.ax.clear()
        self.ax.set_title("Hardware Flow Rate")
        self.ax.set_xlabel("Time / s")
        self.ax.set_ylabel("Flow / SLM")
        self.ax.grid(True, ls="--", alpha=0.3)
        if rows:
            now = rows[-1].time_ms / 1000.0
            start_time = max(0.0, now - window_s)
            view = [row for row in rows if row.time_ms / 1000.0 >= start_time]
            t = [row.time_ms / 1000.0 for row in view]
            measured = [row.flow_slm for row in view]
            reference = [row.flow_reference_slm for row in view]
            self.ax.plot(t, measured, label="Calibrated sensor", lw=1.8)
            net = [row.control_flow_slm for row in view]
            self.ax.plot(t, net, label="Estimated lung net", lw=1.3)
            self.ax.plot(t, reference, "k--", label="Reference", lw=1.0)
            self.ax.axvline(now, color="#d62728", lw=1.0)
            self.ax.set_xlim(start_time, max(start_time + window_s, now + 0.2))
            values = measured + net + reference
            self.ax.set_ylim(min(-2.0, min(values) - 2.0), max(2.0, max(values) + 2.0))
            self.ax.legend(loc="upper right", ncol=2, fontsize=8)
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
                <pre>Tcycle = 60 / respiratory rate
Tin = Tcycle × I / (I + E)       Tout = Tcycle × E / (I + E)
Qref,in = 0.06 × Vtarget / Tfill
Qref,out = 0.06 × Vtarget / max(0.25 s, Tout − Tdead)</pre>
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
                <tr><td>Lung Vmax / mL</td><td>Validated usable volume limit of the installed lung/path.</td></tr>
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
                The 323.9 mL empirical amplitude is separate from the 250 mL
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
                <h2>2026-10-12 near-closure PPV sweep</h2>
                <p>Replacement-hardware plateau tau_in = 0.743 s, tau_out = 0.2678 s.
                These are extrapolated plateau values, not new full-open measurements.
                Effective model amplitude = 323.9 mL; volume guard stays separate. For each direction,
                a = clip((4.35 - U)/(4.35 - Ud), 0, 1), conductance = a^gamma,
                tau(U) = tau0 / a^gamma. ATM: Ud=4.0986 V, gamma=1.496;
                VAC: Ud=4.1779 V, gamma=2.0621. a is an effective model coordinate,
                not a measured geometric opening.</p>
                <p>Fixed-voltage experiments were preset: no PPV motion delay was
                fitted. The existing 5 V / 2.9 s actuator model still applies when
                commands change during control. Only the October 12 replacement-hardware
                batch is fitted; October 9 data and 0 V recovery switches are excluded.
                Maximum voltage-median tau fit deviations are about 19% ATM and 14% VAC.
                4.35 V remains the provisional common zero-conductance endpoint.
                Normal STOP uses recovery; faults close the routes and command 5 V.</p>
                <h2>Flow and VAC bypass calibration</h2>
                <p>Calibrated flow = 1.0471 * (raw flow - 0.0034 SLM), before
                applying the configured sensor direction. One gain is used for
                both directions and PPV voltages. During VAC-only extraction,
                the configured 0.814 SLM is the calibrated VAC bypass plateau.
                The measured bypass remains nearly constant through 4.30 V and
                rolls off linearly only over the final 4.30–4.35 V pinch interval
                before adding it to negative calibrated flow, capped at zero.
                ATM and closed-valve samples receive no leakage correction. The
                small closed-state residual is not subtracted.</p>
                <p>PID, Smith and volume integration use estimated lung flow;
                the flow-limit check uses calibrated sensor flow. Feedforward
                and the flow reference remain on the lung-net basis: do not add
                leakage a second time. Raw, calibrated and estimated net flows
                are exported separately.</p>
                <p>Protocol v8 requires matching firmware. Calibration fields
                are locked with the measured model parameters. Set VAC leakage
                to zero to disable that compensation. Calibration spike exclusion
                was only for offline statistics; live measurements are not blanked.</p>
                <h2>Flow controller</h2>
                <pre>e = Qref − |Qfeedback|
a = clip(aff + Kp e + Ki ∫e dt + Kd(−d|Qfeedback|/dt), 0, 1)
Ucmd = Uclose - a(Uclose - Uplateau) V; full open = 0 V</pre>
                <p>Conditional integration limits windup. Model feedforward supplies
                an opening estimate; PID corrects residual error. The Smith predictor
                uses command-driven and estimated-position-driven lung models.
                PID feedback = measured net flow + weight * (ideal - actuator model flow).
                Volume integration and Auto PID scoring use measured net flow only.
                At every direction change both solenoids close, the PPV moves to
                the next phase feedforward position, then the selected solenoid
                opens. Travel time is abs(target V - estimated V) / (5 / 2.9).
                No fixed millisecond dead time is added. Position is estimated,
                not measured; waiting adds time to each breathing cycle.</p>
                <h3>Optional bounded Auto PID</h3>
                <p>Auto PID evaluates a stable baseline and nearby Kp/Ki values over
                completed physical runs. Kd and all locked model/delay parameters stay
                fixed. Kp and Ki remain within 0.25x to 4x their starting values; the
                score combines flow error, tidal error and PPV saturation.</p>
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
        self.log_name_edits: List[QtWidgets.QLineEdit] = []
        self.log_status_labels: List[QtWidgets.QLabel] = []
        self.last_plot_count = -1
        self.last_ping = 0.0
        self.current_run_id: Optional[int] = None
        self.auto_tuner: Optional[AutoPIDTuner] = None
        self.auto_tune_active = False
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
        self.telemetry_table.setHorizontalHeaderLabels(["t / s", "State", "Cycle", "Flow", "Volume", "Opening", "Mask"])
        self.telemetry_table.horizontalHeader().setStretchLastSection(True)
        right_layout.addWidget(self.telemetry_table, stretch=1)
        self.developer_tabs.addTab(live_hardware, "Live Hardware")
        splitter.addWidget(self.developer_tabs)
        splitter.setSizes([410, 770])
        splitter.setStretchFactor(1, 1)
        self.mode_stack.addWidget(splitter)
        root_layout.addWidget(self.mode_stack, 1)
        self.setCentralWidget(root)
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
        status = QtWidgets.QLabel("Blank: save automatically under output/ by run ID")
        status.setWordWrap(True)
        status.setStyleSheet("color:#666; font-size:11px")
        self.log_name_edits.append(edit)
        self.log_status_labels.append(status)
        layout.addWidget(QtWidgets.QLabel("CSV filename / path"), 0, 0, 1, 2)
        layout.addWidget(edit, 1, 0)
        layout.addWidget(choose, 1, 1)
        layout.addWidget(status, 2, 0, 1, 2)
        return group

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
        for label in self.log_status_labels:
            label.setText(text)

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
        settings.setMinimumWidth(300)
        settings.setMaximumWidth(370)
        side = QtWidgets.QVBoxLayout(settings)
        title = QtWidgets.QLabel("Single-Lung Hardware Control")
        title.setStyleSheet("font-size:18px; font-weight:bold; color:#17466d")
        side.addWidget(title)
        form_group = QtWidgets.QGroupBox("Breathing Settings")
        form = QtWidgets.QFormLayout(form_group)
        self._user_spin(form, "rr", "Rate / bpm", self.cfg.respiratory_rate_bpm, 1, 40, 1, 1)
        self._user_spin(form, "volume", "Tidal volume / mL", self.cfg.target_volume_ml, 10, 250, 5, 1)
        self._user_spin(form, "ie_in", "I:E inspiration", self.cfg.ie_inspiration, 0.1, 10, 0.1, 1)
        self._user_spin(form, "ie_out", "I:E expiration", self.cfg.ie_expiration, 0.1, 10, 0.1, 1)
        self.user_derived_flow = QtWidgets.QLabel()
        form.addRow("Calculated insp. reference / SLM", self.user_derived_flow)
        self._user_spin(form, "cycles", "Cycles", self.cfg.cycles, 1, 100, 1, 0)
        side.addWidget(form_group)
        self.user_timing = QtWidgets.QLabel()
        self.user_timing.setWordWrap(True)
        self.user_timing.setStyleSheet("color:#36556c")
        side.addWidget(self.user_timing)
        for field in self.user_inputs.values():
            field.valueChanged.connect(self._update_user_timing)

        self.user_auto_pid = QtWidgets.QCheckBox("Enable Auto PID (run-to-run PI tuning)")
        self.user_auto_pid.toggled.connect(self._sync_auto_pid_checkboxes)
        side.addWidget(self.user_auto_pid)
        auto_note = QtWidgets.QLabel(
            "Off: one normal hardware run. On: several bounded test runs are "
            "performed automatically; Kd is unchanged. Stop cancels at any time."
        )
        auto_note.setWordWrap(True)
        auto_note.setStyleSheet("color:#684d19; font-size:11px")
        side.addWidget(auto_note)

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
        user_save.clicked.connect(self.save_current_log)
        buttons.addWidget(user_start)
        buttons.addWidget(user_stop)
        buttons.addWidget(user_save)
        side.addLayout(buttons)

        readout = QtWidgets.QGroupBox("Live Hardware")
        values = QtWidgets.QFormLayout(readout)
        for key, label in (("state", "Phase"), ("cycle", "Cycle"),
                           ("flow", "Flow"), ("volume", "Volume"),
                           ("opening", "PPV opening")):
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
        splitter.addWidget(settings)

        charts = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.user_model_canvas = HardwareLungCanvas()
        self.user_model_view = self._model_view(self.user_model_canvas)
        charts.addWidget(self.user_model_view)
        self.user_flow_canvas = FlowCanvas()
        charts.addWidget(self.user_flow_canvas)
        charts.setSizes([580, 430])
        charts.setStretchFactor(1, 1)
        splitter.addWidget(charts)
        splitter.setSizes([340, 1010])
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
        self._spin(form, "rr", "Respiratory rate / bpm", self.cfg.respiratory_rate_bpm, 1, 40, 1, 2)
        self._spin(form, "ie_in", "I:E inspiration", self.cfg.ie_inspiration, 0.1, 10, 0.1, 2)
        self._spin(form, "ie_out", "I:E expiration", self.cfg.ie_expiration, 0.1, 10, 0.1, 2)
        self._spin(form, "volume", "Target tidal volume / mL", self.cfg.target_volume_ml, 10, 250, 5, 1)
        self._spin(form, "fill_time", "Target fill time / s", self.cfg.target_fill_time_s, 0.1, 10, 0.05, 3)
        self._spin(form, "cycles", "Cycles", self.cfg.cycles, 1, 100, 1, 0)
        box.addWidget(breathing)

        pid = QtWidgets.QGroupBox("Flow PID Control")
        form = QtWidgets.QFormLayout(pid)
        self._spin(form, "kp", "Kp / opening per SLM", self.cfg.pi.kp, 0, 1, 0.001, 4)
        self._spin(form, "ki", "Ki / opening per SLM s", self.cfg.pi.ki, 0, 5, 0.01, 4)
        self._spin(form, "kd", "Kd / opening s per SLM", self.cfg.pi.kd, 0, 1, 0.001, 4)
        self._spin(form, "ilim", "Integral limit", self.cfg.pi.integral_limit, 0.01, 20, 0.1, 2)
        box.addWidget(pid)

        auto_pid = QtWidgets.QGroupBox("Optional Auto PID")
        form = QtWidgets.QFormLayout(auto_pid)
        self.auto_pid_enabled = QtWidgets.QCheckBox("Enable bounded run-to-run PI tuning")
        self.auto_pid_enabled.toggled.connect(self._sync_auto_pid_checkboxes)
        form.addRow(self.auto_pid_enabled)
        self._spin(form, "auto_rounds", "Search rounds", 2, 1, 5, 1, 0)
        auto_help = QtWidgets.QLabel(
            "Tests baseline and nearby Kp/Ki values on hardware, scores completed "
            "runs, then keeps the best measured pair. Kd is not changed."
        )
        auto_help.setWordWrap(True)
        auto_help.setStyleSheet("color:#684d19; font-size:11px")
        form.addRow(auto_help)
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
        self._spin(form, "smith_delay", "Residual transport/sensor delay / s", self.cfg.smith.delay_s, 0, 1.0, 0.005, 3)
        self._spin(form, "ppv_open_time", "Equivalent 4.2 to 0 V time / s", self.cfg.smith.ppv_open_time_s, 0, 60, 0.01, 3)
        self._spin(form, "ppv_close_time", "Equivalent 0 to 4.2 V time / s", self.cfg.smith.ppv_close_time_s, 0, 60, 0.01, 3)
        self._spin(form, "smith_gain", "Motion correction weight", self.cfg.smith.correction_gain, 0, 2.0, 0.05, 2)
        delay_note = QtWidgets.QLabel(
            "At each phase change both solenoids close. PPV moves to the next "
            "phase feedforward position before its solenoid opens. Position is "
            "estimated, not measured. Speed: 5 V / 2.9 s; equivalent 4.2 V "
            "stroke: 2.436 s. Smith feedback = measured net flow + weight * "
            "(ideal model flow - actuator model flow). Residual delay stays 0 s. "
            "Normal zero opening uses 4.35 V; fault closure uses 5 V."
        )
        delay_note.setWordWrap(True)
        delay_note.setStyleSheet("color:#666; font-size:11px")
        form.addRow(delay_note)
        box.addWidget(smith)

        model = QtWidgets.QGroupBox("Measured Hardware Model Parameters")
        form = QtWidgets.QFormLayout(model)
        self._spin(form, "capacity", "Integrated volume guard / mL", self.cfg.lung.capacity_ml, 10, 250, 5, 1)
        self._spin(form, "flow_offset", "Sensor zero offset / SLM", self.cfg.lung.hardware_flow_offset_slm, -5, 5, 0.0001, 4)
        self._spin(form, "flow_gain", "Common flow calibration gain", self.cfg.lung.hardware_flow_gain, 0.1, 5, 0.0001, 4)
        self._spin(form, "vac_leak", "VAC bypass plateau / SLM", self.cfg.lung.hardware_vac_leak_slm, 0, 20, 0.001, 3)
        self._spin(form, "model_volume", "Empirical model amplitude / mL", self.cfg.lung.hardware_model_volume_ml, 250, 5000, 50, 1)
        self._spin(form, "tau_in", "Tau_in / s", self.cfg.lung.tau_in_s, 0.02, 10, 0.01, 4)
        self._spin(form, "tau_out", "Tau_out / s", self.cfg.lung.tau_out_s, 0.02, 10, 0.01, 4)
        self._spin(form, "gamma", "Inspiration gamma", self.cfg.lung.hardware_gamma_in, 0.05, 5, 0.0001, 4)
        self._spin(form, "gamma_out", "Expiration gamma (40 kPa bench)", self.cfg.lung.hardware_gamma_out, 0.05, 5, 0.0001, 4)
        self._spin(form, "dead_zone_in", "Inspiration plateau end / V", self.cfg.lung.hardware_dead_zone_in_v, 0, 4.19, 0.0001, 4)
        self._spin(form, "dead_zone_out", "Expiration plateau end / V", self.cfg.lung.hardware_dead_zone_out_v, 0, 4.19, 0.0001, 4)
        self._spin(form, "closure", "PPV closure target / V", self.cfg.lung.hardware_closure_v, 0.1, 5, 0.01, 2)
        note = QtWidgets.QLabel("2026-10-12 PPV sweep; source setting 40 kPa. Measured stop-flow point: 4.35 V. VAC bypass plateau: 0.814 SLM with final 50 mV roll-off. Model amplitude is not physical capacity. PPV positions before each phase; Smith compensates motion during regulation.")
        note.setWordWrap(True)
        form.addRow(note)
        self._spin(form, "dead", "Fixed dead time / s (unused)", self.cfg.dead_time_s, 0, 0.5, 0.01, 3)
        self._spin(form, "precondition", "Precondition time / s", self.cfg.precondition_s, 0, 10, 0.1, 2)
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
        save.clicked.connect(self.save_current_log)
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
            ("opening", "PPV opening"),
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
        self.log.setMinimumHeight(145)
        box.addWidget(self.log)
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
        widget.setRange(low, high)
        widget.setSingleStep(step)
        widget.setDecimals(decimals)
        widget.setValue(value)
        widget.setKeyboardTracking(False)
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
        flow = 0.06 * volume / fill_time
        self.user_derived_flow.setText(f"{flow:.2f} (automatic)")
        expiration_flow = 0.06 * volume / max(0.25, expiration)
        self.user_timing.setText(
            f"Inspiration {inspiration:.2f} s | Expiration {expiration:.2f} s\n"
            "PPV positioning adds variable time between phases.\n"
            f"Calculated references: inspiration {flow:.2f}, expiration "
            f"{expiration_flow:.2f} SLM. These are targets, not measured flow."
        )

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
            "smith_delay": self.cfg.smith.delay_s,
            "smith_gain": self.cfg.smith.correction_gain,
            "ppv_open_time": self.cfg.smith.ppv_open_time_s,
            "ppv_close_time": self.cfg.smith.ppv_close_time_s,
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
        self.smith_enabled.setEnabled(True)

    def _switch_mode(self, index: int) -> None:
        if index == 0:
            try:
                self.cfg = self.cfg_from_ui()
                self._sync_user_from_config()
            except Exception:
                pass
        self.mode_stack.setCurrentIndex(index)
        if self.rows:
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
        cfg.smith.enabled = self.smith_enabled.isChecked()
        cfg.smith.delay_s = self.inputs["smith_delay"].value()
        cfg.smith.correction_gain = self.inputs["smith_gain"].value()
        cfg.smith.ppv_open_time_s = self.inputs["ppv_open_time"].value()
        cfg.smith.ppv_close_time_s = self.inputs["ppv_close_time"].value()
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
        if not (0 <= cfg.lung.hardware_dead_zone_in_v < cfg.lung.hardware_closure_v <= 5
                and 0 <= cfg.lung.hardware_dead_zone_out_v < cfg.lung.hardware_closure_v
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
            self.save_current_log()
        self.current_run_id = None
        self.connection_label.setText("Disconnected")

    def _write_config(self, cfg: SingleLungConfig) -> bool:
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
                self.auto_tuner = AutoPIDTuner(
                    cfg.pi.kp,
                    cfg.pi.ki,
                    int(self.inputs["auto_rounds"].value()),
                )
            except ValueError as exc:
                QtWidgets.QMessageBox.warning(self, "Auto PID", str(exc))
                return
            self.auto_tune_active = True
            self._set_auto_pid_status(
                f"Starting {1 + 4 * self.auto_tuner.rounds} candidate runs"
            )
            self._start_next_auto_candidate()
            return
        self._launch_run(cfg)

    def _launch_run(self, cfg: SingleLungConfig) -> bool:
        self.rows = []
        self.last_plot_count = -1
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
        self.current_run_id = int(time.time_ns() // 1_000_000) & 0x7FFFFFFF
        command = f"START,{self.current_run_id},{cfg.cycles}"
        try:
            self.connection.write((command + "\n").encode("ascii"))
            self.hardware_running = True
            self._log(f"> {command}")
            return True
        except Exception as exc:
            self._log(f"Start failed: {exc}")
            return False

    def _start_next_auto_candidate(self) -> None:
        if not self.auto_tune_active or self.auto_tuner is None:
            return
        gains = self.auto_tuner.next_candidate()
        if gains is None:
            self._finish_auto_pid()
            return
        cfg = self.cfg_from_ui()
        cfg.pi.kp, cfg.pi.ki = gains
        number = len(self.auto_tuner.history) + 1
        total = 1 + 4 * self.auto_tuner.rounds
        self._set_auto_pid_status(
            f"Run {number}/{total}: {self.auto_tuner.current_stage}; "
            f"Kp={gains[0]:.5f}, Ki={gains[1]:.5f}"
        )
        if not self._launch_run(cfg):
            self._cancel_auto_pid("could not start candidate")

    def _finish_auto_pid(self) -> None:
        if self.auto_tuner is None:
            return
        best_kp, best_ki = self.auto_tuner.best_gains
        self.inputs["kp"].setValue(best_kp)
        self.inputs["ki"].setValue(best_ki)
        self.cfg.pi.kp, self.cfg.pi.ki = best_kp, best_ki
        report_path = self._save_auto_pid_report(self.auto_tuner)
        self.auto_tune_active = False
        self._set_auto_pid_status(
            f"Complete: best Kp={best_kp:.5f}, Ki={best_ki:.5f}, "
            f"score={self.auto_tuner.best_score:.4f}. Report: {report_path.name}"
        )
        self.statusBar().showMessage("Auto PID complete; best tested gains loaded.")
        self._log(f"Auto PID complete; report saved to {report_path}")

    def _save_auto_pid_report(self, tuner: AutoPIDTuner) -> Path:
        path = SCRIPT_DIR / "output" / "single_lung_hardware_auto_pid.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        if tuner.history:
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(tuner.history[0]))
                writer.writeheader()
                writer.writerows(tuner.history)
        return path

    def _set_auto_pid_status(self, text: str) -> None:
        self.auto_pid_status.setText(text)
        self.user_auto_status.setText(f"Auto PID: {text}")

    def _cancel_auto_pid(self, reason: str) -> None:
        if self.auto_tune_active:
            self.auto_tune_active = False
            self._set_auto_pid_status(f"Cancelled: {reason}")
            self.statusBar().showMessage(f"Auto PID cancelled: {reason}")

    def stop_run(self) -> None:
        self._cancel_auto_pid("stopped by user")
        if self.connection is None:
            self._log("Not connected.")
            return
        self.connection.write(b"STOP\n")
        self._log("> STOP")

    def save_current_log(self) -> Optional[Path]:
        run_id = self.current_run_id or (self.rows[0].run_id if self.rows else 0)
        path = resolve_log_path(self.log_path_text, run_id)
        auto_stage = None
        if self.auto_tune_active and self.auto_tuner is not None:
            auto_stage = self.auto_tuner.current_stage
            path = auto_pid_log_path(path, auto_stage, run_id)
        metadata = {
            "protocol_version": PROTOCOL_VERSION,
            "model_reference": "2026-10-14 refit of 2026-10-12 replacement hardware; independent ATM/VAC no-delay fits; plateau tau extrapolated; see output/directional_reference_2026-10-12/fit.json; common closure 4.35 V; VAC bypass 0.814 SLM",
            "auto_pid_stage": auto_stage,
            "configuration": asdict(self.cfg),
        }
        try:
            save_log(self.rows, path)
            path.with_suffix(".json").write_text(
                json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except (OSError, ValueError) as exc:
            message = f"Could not save CSV: {exc}"
            self._set_log_status(message)
            self.statusBar().showMessage(message)
            self._log(message)
            return None
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

    def _handle_line(self, line: str) -> None:
        self._log(f"< {line}")
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
            self.manual_status.setText(
                "Stopped after volume recovery: ATM/VAC closed, PPV fully open (0 V)."
            )
            self.statusBar().showMessage("Hardware stopped after bag-volume recovery")
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
            if len(self.rows) - self.last_plot_count >= 5:
                self.flow_canvas.draw_rows(self.rows)
                self.user_flow_canvas.draw_rows(self.rows)
                self._draw_dynamic_model(row)
                self.last_plot_count = len(self.rows)
            self._append_table_row(row)
        elif line.startswith("DONE,"):
            try:
                done_run_id = int(line.split(",", 1)[1])
            except (IndexError, ValueError):
                return
            if self.current_run_id is not None and done_run_id != self.current_run_id:
                self._log(f"Ignored completion from stale run {done_run_id}.")
                return
            self.hardware_running = False
            self.statusBar().showMessage("Hardware run complete")
            self.flow_canvas.draw_rows(self.rows)
            self.user_flow_canvas.draw_rows(self.rows)
            if self.rows:
                self._draw_dynamic_model(self.rows[-1])
            self.save_current_log()
            if self.auto_tune_active and self.auto_tuner is not None:
                try:
                    metrics = hardware_run_metrics(self.rows, self.cfg)
                    self.auto_tuner.record(metrics)
                    self._log(
                        "Auto PID result: "
                        f"score={metrics['score']:.4f}, RMSE={metrics['flow_rmse_slm']:.2f} SLM, "
                        f"mean tidal={metrics['mean_tidal_ml']:.1f} mL, "
                        f"saturation={metrics['saturation_fraction']:.0%}"
                    )
                    QtCore.QTimer.singleShot(250, self._start_next_auto_candidate)
                except Exception as exc:
                    self._log(f"Auto PID scoring error: {exc}")
                    self._cancel_auto_pid("candidate could not be scored")
        elif line.startswith("FAULT,") or line.startswith("ERR,"):
            if line.startswith("FAULT,"):
                self.hardware_running = False
            self.manual_status.setText(line)
            self.statusBar().showMessage(line)
            self._cancel_auto_pid(line)
        elif line.startswith("WARN,RECOVERY_TIMEOUT,"):
            self.manual_status.setText(
                "Recovery timed out: ATM/VAC were closed before target volume was reached."
            )
            self.statusBar().showMessage(self.manual_status.text())

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
        self.metrics["opening"].setText(f"{row.opening * 100.0:.1f} %")
        self.metrics["command"].setText(f"{row.command_v:.2f} V")
        self.metrics["valves"].setText(f"{'ATM open' if atm else 'ATM closed'}, {'VAC open' if vac else 'VAC closed'}")
        self.user_metrics["state"].setText(STATE_NAMES.get(row.state, "UNKNOWN"))
        self.user_metrics["cycle"].setText(f"{shown_cycle} / {self.cfg.cycles}")
        self.user_metrics["flow"].setText(f"{row.flow_slm:+.2f} SLM")
        self.user_metrics["volume"].setText(f"{row.volume_ml:.1f} mL")
        self.user_metrics["opening"].setText(f"{row.opening * 100.0:.1f} %")

    def _append_table_row(self, row: HardwareTelemetry) -> None:
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
            f"{row.opening * 100.0:.1f}%",
            f"{row.valve_mask:02b}",
        ]
        for col, value in enumerate(values):
            self.telemetry_table.setItem(i, col, QtWidgets.QTableWidgetItem(value))
        self.telemetry_table.scrollToBottom()

    def _log(self, text: str) -> None:
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
