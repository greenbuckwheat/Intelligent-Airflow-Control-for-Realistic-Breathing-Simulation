"""Preliminary identification of PPV inspiration tests recorded by the bench UI.

The analysis is intentionally conservative:
* 37.csv is excluded because its telemetry records 0 V, not the named 3.7 V.
* 4.2 V is retained as a measured closed point but excluded from log fits.
* Flow baseline is the median of the last two seconds before ATM opens.
* A first-order cumulative-volume model is fitted robustly to each trial.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import least_squares


HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUT = HERE / "analysis"
NAME_RE = re.compile(r"^(\d+)(?:\.(\d+))?\.csv$")


def voltage_and_repeat(path: Path) -> tuple[float, int] | None:
    match = NAME_RE.match(path.name)
    if not match:
        return None
    token, repeat = match.groups()
    voltage = float(int(token)) if len(token) == 1 else int(token) / 10 ** (len(token) - 1)
    return voltage, int(repeat or 0)


def read_trial(path: Path) -> dict[str, object]:
    voltage, repeat = voltage_and_repeat(path)  # type: ignore[misc]
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in list(csv.reader(handle))[1:] if len(row) >= 13 and row[1] and row[2]]

    stamp = np.array([int(row[1]) / 1000.0 for row in rows])
    flow = np.array([float(row[2]) for row in rows])
    atm = np.array([int(row[3]) for row in rows])
    vac = np.array([int(row[4]) for row in rows])
    ppv = np.array([float(row[5]) for row in rows])
    statuses = {row[9] for row in rows}
    faults = {row[10] for row in rows}
    atm_indices = np.flatnonzero(atm == 1)
    if not len(atm_indices):
        raise ValueError(f"{path.name}: ATM never opened")
    if np.max(vac) != 0:
        raise ValueError(f"{path.name}: VAC opened during an inspiration trial")

    start = int(atm_indices[0])
    baseline = float(np.median(flow[max(0, start - 100):start]))
    time_s = stamp[start:] - stamp[start]
    corrected_flow = flow[start:] - baseline
    volume_ml = np.r_[
        0.0,
        np.cumsum(
            0.5 * (corrected_flow[1:] + corrected_flow[:-1])
            * np.diff(time_s) * 1000.0 / 60.0
        ),
    ]

    observed_volume = max(float(np.max(volume_ml)), 1.0)
    crossing = np.flatnonzero(volume_ml >= 0.632 * observed_volume)
    tau_guess = float(time_s[crossing[0]]) if len(crossing) else max(float(time_s[-1]) / 3.0, 0.2)

    def model(parameters: np.ndarray) -> np.ndarray:
        final_volume, tau_s, delay_s = parameters
        effective_time = np.maximum(time_s - delay_s, 0.0)
        return final_volume * (1.0 - np.exp(-effective_time / tau_s))

    scale = max(observed_volume * 0.01, 2.0)
    fit = least_squares(
        lambda parameters: (model(parameters) - volume_ml) / scale,
        x0=[observed_volume, max(tau_guess, 0.1), 0.02],
        bounds=([1.0, 0.03, 0.0], [5000.0, 100.0, 1.0]),
        loss="soft_l1",
        max_nfev=10000,
    )
    fitted = model(fit.x)
    sse = float(np.sum((volume_ml - fitted) ** 2))
    sst = float(np.sum((volume_ml - np.mean(volume_ml)) ** 2))
    r_squared = 1.0 - sse / sst if sst else float("nan")
    early = corrected_flow[(time_s >= 0.10) & (time_s <= 0.30)]

    excluded_reason = ""
    if path.name == "37.csv":
        excluded_reason = "filename says 3.7 V but telemetry stayed at 0 V"
    elif voltage >= 4.2:
        excluded_reason = "measured closed point; excluded from logarithmic fit"
    elif abs(float(np.max(ppv)) - voltage) > 0.011:
        excluded_reason = "PPV telemetry does not match filename"
    elif statuses != {"OK"} or faults != {"NONE"}:
        excluded_reason = "sensor or firmware fault"

    return {
        "file": path.name,
        "voltage_v": voltage,
        "repeat": repeat,
        "samples": len(rows),
        "sample_rate_hz": (len(stamp) - 1) / (stamp[-1] - stamp[0]),
        "ppv_min_v": float(np.min(ppv)),
        "ppv_max_v": float(np.max(ppv)),
        "baseline_slm": baseline,
        "early_flow_0p1_0p3_slm": float(np.mean(early)) if len(early) else float("nan"),
        "fitted_tau_s": float(fit.x[1]),
        "fitted_final_volume_ml": float(fit.x[0]),
        "fitted_delay_s": float(fit.x[2]),
        "fit_r_squared": r_squared,
        "excluded_reason": excluded_reason,
    }


def power_fit(voltage: np.ndarray, values: np.ndarray, opening: np.ndarray, inverse: bool) -> dict[str, float]:
    slope, intercept = np.polyfit(np.log(opening), np.log(values), 1)
    exponent = -slope if inverse else slope
    predicted = np.exp(intercept) * opening ** slope
    r_squared = 1.0 - float(np.sum((values - predicted) ** 2) / np.sum((values - np.mean(values)) ** 2))
    return {"gamma": float(exponent), "coefficient": float(np.exp(intercept)), "r_squared": r_squared}


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    paths = [path for path in DATA.glob("*.csv") if voltage_and_repeat(path) is not None]
    trials = sorted((read_trial(path) for path in paths), key=lambda row: (row["voltage_v"], row["repeat"]))
    write_csv(OUT / "ppv_inspiration_trial_fits.csv", trials)

    valid = [row for row in trials if not row["excluded_reason"]]
    grouped: dict[float, list[dict[str, object]]] = {}
    for row in valid:
        grouped.setdefault(float(row["voltage_v"]), []).append(row)

    summaries: list[dict[str, object]] = []
    for voltage, rows in sorted(grouped.items()):
        tau = np.array([float(row["fitted_tau_s"]) for row in rows])
        flow = np.array([float(row["early_flow_0p1_0p3_slm"]) for row in rows])
        volume = np.array([float(row["fitted_final_volume_ml"]) for row in rows])
        summaries.append({
            "voltage_v": voltage,
            "repeat_count": len(rows),
            "tau_median_s": float(np.median(tau)),
            "tau_min_s": float(np.min(tau)),
            "tau_max_s": float(np.max(tau)),
            "early_flow_median_slm": float(np.median(flow)),
            "early_flow_min_slm": float(np.min(flow)),
            "early_flow_max_slm": float(np.max(flow)),
            "fitted_volume_median_ml": float(np.median(volume)),
        })
    write_csv(OUT / "ppv_inspiration_voltage_summary.csv", summaries)

    voltage = np.array([float(row["voltage_v"]) for row in summaries])
    tau = np.array([float(row["tau_median_s"]) for row in summaries])
    flow = np.array([float(row["early_flow_median_slm"]) for row in summaries])

    active = (voltage >= 3.4) & (voltage <= 4.1)
    project_opening = 1.0 - voltage[active] / 5.0
    project_tau = power_fit(voltage[active], tau[active], project_opening, inverse=True)
    project_flow = power_fit(voltage[active], flow[active], project_opening, inverse=False)

    measured = (voltage >= 3.3) & (voltage <= 4.1)
    measured_opening = (4.2 - voltage[measured]) / (4.2 - 3.2)
    measured_tau = power_fit(voltage[measured], tau[measured], measured_opening, inverse=True)
    measured_flow = power_fit(voltage[measured], flow[measured], measured_opening, inverse=False)

    results = {
        "method": "baseline-corrected flow integration and robust first-order cumulative-volume fit",
        "excluded": {"37.csv": "telemetry stayed at 0 V", "42.csv": "confirmed fully closed at 4.2 V"},
        "project_opening_a_equals_1_minus_v_over_5": {
            "fit_voltage_range_v": [3.4, 4.1],
            "tau_based": project_tau,
            "early_flow_based": project_flow,
            "recommended_preliminary_gamma": (project_tau["gamma"] + project_flow["gamma"]) / 2.0,
        },
        "measured_active_range_opening": {
            "definition": "a_eff=(4.2-V)/(4.2-3.2), clipped to 0..1",
            "tau_based": measured_tau,
            "early_flow_based": measured_flow,
            "recommended_preliminary_gamma": (measured_tau["gamma"] + measured_flow["gamma"]) / 2.0,
        },
    }
    (OUT / "ppv_inspiration_fit_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    fig, axes = plt.subplots(2, 1, figsize=(8.5, 8), sharex=True, constrained_layout=True)
    tau_low = np.array([float(row["tau_median_s"]) - float(row["tau_min_s"]) for row in summaries])
    tau_high = np.array([float(row["tau_max_s"]) - float(row["tau_median_s"]) for row in summaries])
    flow_low = np.array([float(row["early_flow_median_slm"]) - float(row["early_flow_min_slm"]) for row in summaries])
    flow_high = np.array([float(row["early_flow_max_slm"]) - float(row["early_flow_median_slm"]) for row in summaries])
    axes[0].errorbar(voltage, flow, yerr=[flow_low, flow_high], fmt="o-", capsize=3)
    axes[0].scatter([4.2], [0], marker="x", s=70, label="measured closed")
    axes[0].set_ylabel("Early flow (SLM)")
    axes[0].legend()
    axes[1].errorbar(voltage, tau, yerr=[tau_low, tau_high], fmt="o-", capsize=3)
    axes[1].set_ylabel("First-order tau (s)")
    axes[1].set_xlabel("PPV command voltage (V)")
    for axis in axes:
        axis.grid(True, alpha=0.3)
    fig.savefig(OUT / "ppv_inspiration_fit.png", dpi=180)

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
