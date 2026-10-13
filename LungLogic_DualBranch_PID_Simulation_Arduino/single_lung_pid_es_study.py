"""Offline, bounded perturbation tuning of the simulated single-lung flow PI.

This is an ES-inspired two-sided perturbation search, not the exact discrete
filter/demodulator of Killingsworth and Krstic. It never writes GUI settings.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
from dataclasses import asdict
from pathlib import Path

import numpy as np

from single_lung_pid_gui import (
    CONFIG_PATH, STATE_EXPIRATION, STATE_INSPIRATION, SingleLungConfig,
    load_config, run_single_lung_simulation, simulated_inspiratory_tidal_ml,
)
from single_lung_requirement_benchmark import evaluate
from single_lung_uncertainty_sweep import (
    DIRECT_REACH_S, EXPIRATION_TAUS_S, FILL_SHAPES_S, INPUT_DELAYS_S,
    NOMINAL_CONTROLLER_INPUT_DELAY_S, NOMINAL_CONTROLLER_TAU_IN_S,
    NOMINAL_CONTROLLER_TAU_OUT_S, classify, prototype_config,
)

RATES = (12.0, 15.0, 16.0)
TUNE_RATE = 15.0
MIN_GAIN_FACTOR = 0.25
MAX_GAIN_FACTOR = 4.0
PERTURBATION_LOG = 0.12
ITERATIONS = 8


def configured_case(saved: SingleLungConfig, bpm: float, gains: tuple[float, float],
                    guard: bool = False) -> SingleLungConfig:
    cfg = prototype_config(saved)
    cfg.respiratory_rate_bpm = bpm
    cfg.cycles = 30
    cfg.dead_time_within_cycle = True
    cfg.volume_deadline_guard = guard
    cfg.volume_feedback_source = "sensor_integral"
    cfg.target_fill_time_s = min(cfg.target_fill_time_s, cfg.inspiration_s)
    cfg.pi.kp, cfg.pi.ki = gains
    cfg.pi.kd = 0.0
    cfg.lung.tau_in_s = NOMINAL_CONTROLLER_TAU_IN_S
    cfg.lung.tau_out_s = NOMINAL_CONTROLLER_TAU_OUT_S
    cfg.input_delay.delay_s = NOMINAL_CONTROLLER_INPUT_DELAY_S
    return cfg


def metrics(saved: SingleLungConfig, bpm: float, gains: tuple[float, float],
            guard: bool = False) -> dict:
    cfg = configured_case(saved, bpm, gains, guard)
    result = run_single_lung_simulation(cfg)
    tidals = np.array([simulated_inspiratory_tidal_ml(result, i) for i in range(30)])
    active = np.isin(result.state, (STATE_INSPIRATION, STATE_EXPIRATION))
    inspiration = result.state == STATE_INSPIRATION
    flow_error = result.flow_slm[active] - result.flow_reference_slm[active]
    # Normalize distinct requirements before summing them. Volume has priority.
    mean_volume_error = float(np.mean(np.abs(tidals - 250.0)) / 250.0)
    worst_volume_error = float(np.max(np.abs(tidals - 250.0)) / 250.0)
    spread = float((np.max(tidals) - np.min(tidals)) / 250.0)
    flow_rmse_slm = float(np.sqrt(np.mean(flow_error ** 2)))
    saturation = float(np.mean(result.opening[inspiration] >= 0.999))
    score = (4.0 * mean_volume_error + 2.0 * worst_volume_error
             + 2.0 * spread + 0.10 * flow_rmse_slm / 12.0
             + 0.01 * saturation)
    # A numerical score cannot substitute for the tidal acceptance gate.
    if worst_volume_error > 0.05:
        score += 10.0 * (worst_volume_error - 0.05)
    return {
        "bpm": bpm, "guard": guard, "kp": gains[0], "ki": gains[1],
        "minimum_tidal_ml": float(tidals.min()),
        "mean_tidal_ml": float(tidals.mean()),
        "maximum_tidal_ml": float(tidals.max()),
        "worst_volume_error_pct": 100.0 * worst_volume_error,
        "cycle_spread_pct": 100.0 * spread,
        "flow_rmse_slm": flow_rmse_slm,
        "inspiration_saturation_fraction": saturation,
        "tidal_pass": worst_volume_error <= 0.05 and spread <= 0.05,
        "score": score,
    }


def optimize(saved: SingleLungConfig, iterations: int = ITERATIONS) -> tuple[tuple[float, float], list[dict]]:
    """Use symmetric gain perturbations and retain the best feasible trial."""
    if iterations < 1:
        raise ValueError("iterations must be positive")
    original = (saved.pi.kp, saved.pi.ki)
    if min(original) <= 0:
        raise ValueError("ES log-gain search needs positive starting Kp and Ki")
    lo, hi = math.log(MIN_GAIN_FACTOR), math.log(MAX_GAIN_FACTOR)
    center = np.zeros(2)
    cache: dict[tuple[float, float], dict] = {}
    history: list[dict] = []

    def trial(log_factors: np.ndarray, stage: str) -> dict:
        point = np.clip(log_factors, lo, hi)
        key = (round(float(point[0]), 9), round(float(point[1]), 9))
        if key not in cache:
            gains = (original[0] * math.exp(key[0]), original[1] * math.exp(key[1]))
            cache[key] = metrics(saved, TUNE_RATE, gains)
        row = {"stage": stage, "log_kp_factor": key[0], "log_ki_factor": key[1], **cache[key]}
        history.append(row)
        return row

    best = trial(center, "baseline")
    best_point = center.copy()
    for iteration in range(iterations):
        gradient = np.zeros(2)
        for axis in range(2):
            offset = np.zeros(2)
            offset[axis] = PERTURBATION_LOG
            plus = trial(center + offset, f"iteration_{iteration + 1}_plus_{axis}")
            minus = trial(center - offset, f"iteration_{iteration + 1}_minus_{axis}")
            gradient[axis] = (plus["score"] - minus["score"]) / (2 * PERTURBATION_LOG)
            for candidate, point in ((plus, center + offset), (minus, center - offset)):
                if candidate["score"] < best["score"]:
                    best, best_point = candidate, np.clip(point, lo, hi).copy()
        # Projected, bounded update; reject steps that fail to improve the score.
        proposed = np.clip(center - 0.45 * gradient, lo, hi)
        candidate = trial(proposed, f"iteration_{iteration + 1}_center")
        if candidate["score"] < best["score"]:
            best, best_point = candidate, proposed.copy()
        center = best_point.copy()
    return (best["kp"], best["ki"]), history


def robustness_grid(saved: SingleLungConfig, gains: tuple[float, float],
                    guard: bool = False) -> list[dict]:
    """Hold controller model fixed while the assumed plant and delay vary."""
    nominal = configured_case(saved, 15.0, gains, guard)
    controller_lung = copy.deepcopy(nominal.lung)
    rows = []
    for bpm in RATES:
        for tau_out in EXPIRATION_TAUS_S:
            for tau_in in FILL_SHAPES_S:
                for delay in INPUT_DELAYS_S:
                    cfg = configured_case(saved, bpm, gains, guard)
                    cfg.lung.full_open_fill_time_s = DIRECT_REACH_S
                    cfg.lung.tau_in_s = tau_in
                    cfg.lung.tau_out_s = tau_out
                    cfg.input_delay.delay_s = delay
                    result = evaluate(
                        cfg, bpm, guard, controller_lung=controller_lung,
                        controller_input_delay_s=NOMINAL_CONTROLLER_INPUT_DELAY_S,
                        volume_feedback_source="sensor_integral",
                    )
                    rows.append({
                        "bpm": bpm, "plant_tau_in_s": tau_in,
                        "plant_tau_out_s": tau_out, "plant_input_delay_s": delay,
                        "status": classify(result), **result,
                    })
        print(f"Grid {bpm:g} bpm complete for Kp={gains[0]:.5f}, Ki={gains[1]:.5f}", flush=True)
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_report(path: Path, original: tuple[float, float], tuned: tuple[float, float],
                 nominal: list[dict], grid: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Offline single-lung PI optimization study\n\n")
        handle.write("This study implements a bounded, two-sided perturbation search "
                     "inspired by extremum seeking. It is not the paper's exact "
                     "discrete ES filter. Tuning uses 30 simulated breaths at "
                     "15 bpm with sensor-integrated volume feedback, fixed model "
                     "feedforward and Smith settings, and **no volume deadline "
                     "guard**. Kd remains zero. No GUI settings or physical hardware "
                     "are changed.\n\n")
        handle.write("The score is 4 x mean relative tidal error + 2 x worst "
                     "relative tidal error + 2 x relative cycle spread + 0.10 x "
                     "flow RMSE / 12 SLM + 0.01 x inspiration saturation fraction, "
                     "with a penalty of 10 x any worst tidal error beyond 5%. "
                     "These weights are project choices, not values from the paper. "
                     "The score is dimensionless and lower is better. Acceptance "
                     "still uses the separate 250 mL +/-5% and spread gates.\n\n")
        handle.write(f"Starting gains: Kp={original[0]:.5f}, Ki={original[1]:.5f}; "
                     f"selected gains: Kp={tuned[0]:.5f}, Ki={tuned[1]:.5f}.\n\n")
        handle.write("| Gains | Guard | Rate | Tidal range / mL | Worst error / % | "
                     "Spread / % | Flow RMSE / SLM | PPV full-open / % | Pass | Score |\n")
        handle.write("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |\n")
        for row in nominal:
            label = "baseline" if (row["kp"], row["ki"]) == original else "selected"
            handle.write(
                f"| {label} | {'on' if row['guard'] else 'off'} | {row['bpm']:g} | "
                f"{row['minimum_tidal_ml']:.1f}-{row['maximum_tidal_ml']:.1f} | "
                f"{row['worst_volume_error_pct']:.2f} | {row['cycle_spread_pct']:.2f} | "
                f"{row['flow_rmse_slm']:.2f} | "
                f"{100 * row['inspiration_saturation_fraction']:.0f} | "
                f"{'PASS' if row['tidal_pass'] else 'FAIL'} | {row['score']:.4f} |\n"
            )
        handle.write("\n## Hypothetical plant grid, guard off\n\n")
        handle.write("Each gain set is checked over 27 combinations per rate: "
                     "tau_in = 0.35/0.57/0.85 s, input delay = 0/25/60 ms, "
                     "tau_out = 0.30/0.50/1.00 s. The 1.30 s atmosphere-path endpoint "
                     "is fixed. Counts are illustrative scenarios, not probabilities.\n\n")
        handle.write("| Gains | Rate | Pass / 27 | Full-open bound fail | "
                     "Other control fail | Minimum tidal in grid / mL |\n")
        handle.write("| --- | ---: | ---: | ---: | ---: | ---: |\n")
        for label in ("baseline", "selected"):
            for rate in RATES:
                selected_rows = [r for r in grid if r["gains"] == label and r["bpm"] == rate]
                handle.write(
                    f"| {label} | {rate:g} | "
                    f"{sum(r['status'] == 'simulated_pass' for r in selected_rows)} / 27 | "
                    f"{sum(r['status'] == 'model_bound_fail' for r in selected_rows)} | "
                    f"{sum(r['status'] == 'controlled_fail' for r in selected_rows)} | "
                    f"{min(r['minimum_tidal_ml'] for r in selected_rows):.1f} |\n"
                )
        handle.write("\nThe guard-on comparison above is diagnostic: if it drives "
                     "the PPV fully open, PID gain changes cannot exceed the "
                     "full-open plant limit. The sensor-integrated feedback is "
                     "still simulated and is not a calibrated physical volume "
                     "measurement. A lower composite score can coexist with "
                     "worse flow RMSE; inspect each metric before selecting a "
                     "controller. The guard-on 16 bpm result can also differ from "
                     "the earlier true-volume-feedback benchmark because feedback "
                     "source is a separate model assumption. These gains are "
                     "research candidates, not settings validated for an assembled "
                     "pneumatic path.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).parent / "output" / "pid_es_study")
    parser.add_argument("--iterations", type=int, default=ITERATIONS)
    args = parser.parse_args()
    saved = load_config(args.config)
    original = (saved.pi.kp, saved.pi.ki)
    tuned, history = optimize(saved, args.iterations)
    nominal = [metrics(saved, bpm, gains, guard)
               for gains in (original, tuned) for guard in (False, True) for bpm in RATES]
    grid = []
    for label, gains in (("baseline", original), ("selected", tuned)):
        grid.extend({"gains": label, **row} for row in robustness_grid(saved, gains))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "search_history.csv", history)
    write_csv(args.output_dir / "nominal_comparison.csv", nominal)
    write_csv(args.output_dir / "plant_grid.csv", grid)
    write_report(args.output_dir / "report.md", original, tuned, nominal, grid)
    with (args.output_dir / "inputs.json").open("w", encoding="utf-8") as handle:
        json.dump({"saved_config": asdict(saved), "iterations": args.iterations,
                   "gain_factor_bounds": [MIN_GAIN_FACTOR, MAX_GAIN_FACTOR],
                   "perturbation_log": PERTURBATION_LOG, "rates_bpm": RATES,
                   "nominal_feedback": "sensor_integral", "tuning_guard": False},
                  handle, indent=2)
    print(f"Selected Kp={tuned[0]:.5f}, Ki={tuned[1]:.5f}; report: {args.output_dir / 'report.md'}")


if __name__ == "__main__":
    main()
