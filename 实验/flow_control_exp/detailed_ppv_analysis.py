"""Detailed, reproducible analysis of the PPV inspiration bench tests.

This script supplements ``analyze_ppv_inspiration.py``.  It keeps the original
per-trial first-order fits, audits trial timing and telemetry, and compares two
ways of mapping command voltage to normalized valve opening.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import least_squares

from analyze_ppv_inspiration import read_trial, voltage_and_repeat


HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUT = HERE / "analysis"


def load_raw(path: Path) -> dict[str, np.ndarray | list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.reader(handle)][1:]
    rows = [row for row in rows if len(row) >= 13 and row[1] and row[2]]
    return {
        "t": np.asarray([float(row[1]) / 1000.0 for row in rows]),
        "flow": np.asarray([float(row[2]) for row in rows]),
        "atm": np.asarray([int(row[3]) for row in rows]),
        "vac": np.asarray([int(row[4]) for row in rows]),
        "ppv": np.asarray([float(row[5]) for row in rows]),
        "pressure_lung": [row[7] for row in rows],
        "pressure_syringe": [row[8] for row in rows],
        "status": [row[9] for row in rows],
        "fault": [row[10] for row in rows],
        "event": [row[11] for row in rows],
    }


def audit_trial(path: Path, fit: dict[str, object]) -> dict[str, object]:
    raw = load_raw(path)
    t = raw["t"]
    flow = raw["flow"]
    atm = raw["atm"]
    ppv = raw["ppv"]
    assert isinstance(t, np.ndarray)
    assert isinstance(flow, np.ndarray)
    assert isinstance(atm, np.ndarray)
    assert isinstance(ppv, np.ndarray)

    start = int(np.flatnonzero(atm == 1)[0])
    t0 = float(t[start])
    elapsed = t - t0
    tail = flow[(elapsed >= max(float(elapsed[-1]) - 2.0, 0.0))]

    target = float(fit["voltage_v"])
    stable_start = start
    while stable_start > 0 and abs(float(ppv[stable_start - 1]) - target) <= 0.011:
        stable_start -= 1
    settled_s = max(t0 - float(t[stable_start]), 0.0)

    dt = np.diff(t)
    regular = dt[dt > 0]
    duplicate_rows = int(np.sum(dt == 0))
    events = [str(x) for x in raw["event"] if x]
    return {
        "file": path.name,
        "voltage_v": target,
        "pre_atm_record_s": t0 - float(t[0]),
        "post_atm_record_s": float(t[-1]) - t0,
        "ppv_settled_before_atm_s": settled_s,
        "ppv_at_atm_v": float(ppv[start]),
        "peak_flow_slm": float(np.max(flow[start:])),
        "tail_mean_slm": float(np.mean(tail)),
        "tail_min_slm": float(np.min(tail)),
        "negative_below_minus1_samples": int(np.sum(flow[start:] < -1.0)),
        "median_positive_dt_ms": float(np.median(regular) * 1000.0),
        "duplicate_timestamp_rows": duplicate_rows,
        "sensor_all_ok": set(raw["status"]) == {"OK"},
        "fault_all_none": set(raw["fault"]) == {"NONE"},
        "pressure_channels_populated": any(raw["pressure_lung"]) or any(raw["pressure_syringe"]),
        "events": "|".join(events),
        "fitted_tau_s": float(fit["fitted_tau_s"]),
        "fitted_volume_ml": float(fit["fitted_final_volume_ml"]),
        "fitted_delay_s": float(fit["fitted_delay_s"]),
        "fit_r_squared": float(fit["fit_r_squared"]),
        "baseline_slm": float(fit["baseline_slm"]),
        "excluded_reason": str(fit["excluded_reason"]),
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def r_squared(y: np.ndarray, predicted: np.ndarray) -> float:
    return 1.0 - float(np.sum((y - predicted) ** 2) / np.sum((y - np.mean(y)) ** 2))


def fit_log_power(opening: np.ndarray, tau: np.ndarray) -> dict[str, float]:
    slope, intercept = np.polyfit(np.log(opening), np.log(tau), 1)
    predicted = np.exp(intercept) * opening**slope
    return {
        "tau_at_opening_1_s": float(np.exp(intercept)),
        "gamma": float(-slope),
        "r_squared_tau": r_squared(tau, predicted),
        "r_squared_log_tau": r_squared(np.log(tau), np.log(predicted)),
    }


def fit_piecewise(voltage: np.ndarray, tau: np.ndarray, closure_v: float = 4.2) -> dict[str, float]:
    """Fit tau=tau0/a_eff**gamma with a fitted dead-zone endpoint."""

    def residual(parameters: np.ndarray) -> np.ndarray:
        log_tau0, dead_v, gamma = parameters
        opening = np.where(
            voltage <= dead_v,
            1.0,
            np.clip((closure_v - voltage) / (closure_v - dead_v), 1e-8, 1.0),
        )
        return np.log(np.exp(log_tau0) / opening**gamma) - np.log(tau)

    fit = least_squares(
        residual,
        x0=np.asarray([np.log(1.9), 3.1, 0.7]),
        bounds=([np.log(0.2), 1.0, 0.05], [np.log(10.0), 3.8, 3.0]),
        loss="soft_l1",
    )
    tau0 = float(np.exp(fit.x[0]))
    dead_v = float(fit.x[1])
    gamma = float(fit.x[2])
    opening = np.where(
        voltage <= dead_v,
        1.0,
        np.clip((closure_v - voltage) / (closure_v - dead_v), 1e-8, 1.0),
    )
    predicted = tau0 / opening**gamma
    return {
        "tau0_s": tau0,
        "dead_zone_end_v": dead_v,
        "closure_v": closure_v,
        "gamma": gamma,
        "r_squared_tau": r_squared(tau, predicted),
        "r_squared_log_tau": r_squared(np.log(tau), np.log(predicted)),
    }


def main() -> None:
    OUT.mkdir(exist_ok=True)
    paths = sorted(
        (p for p in DATA.glob("*.csv") if voltage_and_repeat(p) is not None),
        key=lambda p: voltage_and_repeat(p),
    )
    fits = {p.name: read_trial(p) for p in paths}
    audits = [audit_trial(path, fits[path.name]) for path in paths]
    write_csv(OUT / "ppv_detailed_trial_audit.csv", audits)

    valid = [row for row in audits if not row["excluded_reason"]]
    grouped: dict[float, list[dict[str, object]]] = {}
    for row in valid:
        grouped.setdefault(float(row["voltage_v"]), []).append(row)

    summary: list[dict[str, object]] = []
    for voltage, rows in sorted(grouped.items()):
        tau = np.asarray([float(row["fitted_tau_s"]) for row in rows])
        peak = np.asarray([float(row["peak_flow_slm"]) for row in rows])
        volume = np.asarray([float(row["fitted_volume_ml"]) for row in rows])
        delay = np.asarray([float(row["fitted_delay_s"]) for row in rows])
        tau_mean = float(np.mean(tau))
        summary.append(
            {
                "voltage_v": voltage,
                "n": len(rows),
                "tau_median_s": float(np.median(tau)),
                "tau_mean_s": tau_mean,
                "tau_sd_s": float(np.std(tau, ddof=1)) if len(tau) > 1 else float("nan"),
                "tau_cv_percent": float(np.std(tau, ddof=1) / tau_mean * 100.0) if len(tau) > 1 else float("nan"),
                "tau_min_s": float(np.min(tau)),
                "tau_max_s": float(np.max(tau)),
                "peak_flow_median_slm": float(np.median(peak)),
                "peak_flow_min_slm": float(np.min(peak)),
                "peak_flow_max_slm": float(np.max(peak)),
                "volume_median_ml": float(np.median(volume)),
                "volume_min_ml": float(np.min(volume)),
                "volume_max_ml": float(np.max(volume)),
                "delay_median_s": float(np.median(delay)),
                "r_squared_min": float(min(float(row["fit_r_squared"]) for row in rows)),
            }
        )
    write_csv(OUT / "ppv_detailed_voltage_summary.csv", summary)

    voltage = np.asarray([float(row["voltage_v"]) for row in summary])
    tau = np.asarray([float(row["tau_median_s"]) for row in summary])
    peak = np.asarray([float(row["peak_flow_median_slm"]) for row in summary])

    # The 4.2 V point is a closure observation and has no finite time constant.
    finite = voltage <= 4.1
    naive_all = fit_log_power(1.0 - voltage[finite] / 5.0, tau[finite])
    local = (voltage >= 3.4) & (voltage <= 4.1)
    naive_local = fit_log_power(1.0 - voltage[local] / 5.0, tau[local])
    piecewise = fit_piecewise(voltage[finite], tau[finite])

    # Sensitivity with the visually defensible dead-zone endpoint fixed at 3.2 V.
    active = (voltage >= 3.3) & (voltage <= 4.1)
    effective_opening = (4.2 - voltage[active]) / (4.2 - 3.2)
    fixed_dead_zone = fit_log_power(effective_opening, tau[active])

    # Repeat-selection sensitivity: select one measured trial per voltage and
    # refit.  This is an empirical robustness range, not a formal confidence
    # interval, because several voltages have only one trial.
    rng = np.random.default_rng(20261006)
    sensitivity: list[tuple[float, float, float]] = []
    finite_groups = {u: rows for u, rows in grouped.items() if u <= 4.1}
    finite_voltages = np.asarray(sorted(finite_groups))
    for _ in range(2000):
        selected_tau = np.asarray(
            [float(rng.choice(finite_groups[u])["fitted_tau_s"]) for u in finite_voltages]
        )
        result = fit_piecewise(finite_voltages, selected_tau)
        sensitivity.append(
            (result["tau0_s"], result["dead_zone_end_v"], result["gamma"])
        )
    sensitivity_array = np.asarray(sensitivity)

    def interval(column: int) -> dict[str, float]:
        values = sensitivity_array[:, column]
        return {
            "median": float(np.median(values)),
            "p05": float(np.quantile(values, 0.05)),
            "p95": float(np.quantile(values, 0.95)),
        }

    model_results = {
        "data_scope": {
            "csv_files": len(paths),
            "accepted_dynamic_trials": len(valid),
            "excluded_files": {
                "37.csv": "filename/telemetry mismatch; user requested exclusion",
                "42.csv": "confirmed zero-flow closure point, not a finite-dynamics trial",
            },
        },
        "first_order_model": "V(t)=V_inf*(1-exp(-max(t-t_d,0)/tau_eff))",
        "naive_global_a_equals_1_minus_U_over_5": naive_all,
        "naive_local_3p4_to_4p1V": naive_local,
        "piecewise_fitted_dead_zone": piecewise,
        "piecewise_fixed_3p2V_dead_zone": fixed_dead_zone,
        "repeat_selection_sensitivity_not_formal_CI": {
            "method": "2000 fits after randomly selecting one recorded trial per voltage",
            "tau0_s": interval(0),
            "dead_zone_end_v": interval(1),
            "gamma": interval(2),
        },
        "interpretation": (
            "Use the piecewise gamma for the measured valve.  The local naive gamma is only "
            "an empirical slope and is inconsistent with the measured full-open tau when extrapolated."
        ),
    }
    (OUT / "ppv_detailed_model_results.json").write_text(
        json.dumps(model_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    fig, axes = plt.subplots(3, 1, figsize=(9, 11), sharex=True, constrained_layout=True)
    for row in audits:
        if row["excluded_reason"]:
            continue
        axes[0].scatter(float(row["voltage_v"]), float(row["peak_flow_slm"]), color="#4C78A8", alpha=0.7)
        axes[1].scatter(float(row["voltage_v"]), float(row["fitted_tau_s"]), color="#F58518", alpha=0.7)
        axes[2].scatter(float(row["voltage_v"]), float(row["fitted_volume_ml"]), color="#54A24B", alpha=0.7)
    axes[0].plot(voltage, peak, color="#1F4E79", marker="o", label="voltage median")
    axes[1].plot(voltage, tau, color="#A64B00", marker="o", label="voltage median")
    axes[2].plot(voltage, [float(row["volume_median_ml"]) for row in summary], color="#2E7D32", marker="o", label="voltage median")

    dense_v = np.linspace(0.0, 4.1, 300)
    dead_v = piecewise["dead_zone_end_v"]
    dense_a = np.where(dense_v <= dead_v, 1.0, (4.2 - dense_v) / (4.2 - dead_v))
    dense_tau = piecewise["tau0_s"] / dense_a ** piecewise["gamma"]
    axes[1].plot(dense_v, dense_tau, "--", color="black", label="piecewise physical fit")
    axes[0].scatter([4.2], [0], marker="x", s=80, color="black", label="confirmed closure")

    axes[0].set_ylabel("Peak flow (SLM)")
    axes[1].set_ylabel("Effective time constant (s)")
    axes[2].set_ylabel("Fitted final volume (mL)")
    axes[2].set_xlabel("PPV command voltage (V)")
    for axis in axes:
        axis.grid(True, alpha=0.25)
        axis.legend()
    fig.savefig(OUT / "ppv_detailed_results.png", dpi=200)

    print(json.dumps(model_results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
