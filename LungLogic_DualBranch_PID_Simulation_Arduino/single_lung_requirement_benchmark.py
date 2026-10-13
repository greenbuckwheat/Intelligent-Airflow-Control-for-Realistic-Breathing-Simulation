"""Audit the single-lung simulation against the prototype breathing targets.

This is a model-only study. The slide's 500 mL total target is represented as
250 mL for one prototype branch; pressure acquisition, two-branch independence,
and physical repeatability cannot be verified by this script.
"""

from __future__ import annotations

import argparse
import copy
import csv
import math
from pathlib import Path

import numpy as np

from single_lung_pid_gui import (
    CONFIG_PATH,
    STATE_INSPIRATION,
    LungParameters,
    SingleLungConfig,
    load_config,
    run_single_lung_simulation,
    simulated_inspiratory_tidal_ml,
)


def full_open_volume_from_zero(cfg: SingleLungConfig, inspiration_samples: int) -> float:
    """Volume limit from 0 mL at full opening for a sampled inspiration slot."""
    delay_samples = max(0, int(round(cfg.input_delay.delay_s / cfg.dt))) if cfg.input_delay.enabled else 0
    available = max(0, inspiration_samples - delay_samples) * cfg.dt
    drive = cfg.lung.inspiration_equilibrium_ml()
    return min(cfg.lung.capacity_ml, drive * (1.0 - math.exp(-available / cfg.lung.tau_in_s)))


def ideal_full_open_limit(cfg: SingleLungConfig, result) -> float:
    """Maximum from 0 mL at full opening using the simulator's discrete slots."""
    inspiration_samples = max(
        int(np.count_nonzero((result.cycle == cycle) & (result.state == STATE_INSPIRATION)))
        for cycle in range(cfg.cycles)
    )
    return full_open_volume_from_zero(cfg, inspiration_samples)


def evaluate(cfg: SingleLungConfig, bpm: float, guard: bool,
             controller_lung: LungParameters | None = None,
             controller_input_delay_s: float | None = None,
             volume_feedback_source: str | None = None) -> dict:
    scenario = copy.deepcopy(cfg)
    scenario.respiratory_rate_bpm = bpm
    scenario.cycles = 30
    scenario.dead_time_within_cycle = True
    scenario.volume_deadline_guard = guard
    if volume_feedback_source is not None:
        scenario.volume_feedback_source = volume_feedback_source
    # A saved manual expiration-flow target can be invalid at another rate.
    # Use a phase-derived target for a comparable 12/15/18 bpm benchmark.
    scenario.target_expiration_flow_slm = None
    # The entered average-flow reference must still fit the nominal I:E slot.
    scenario.target_fill_time_s = min(scenario.target_fill_time_s, scenario.inspiration_s)
    result = run_single_lung_simulation(
        scenario, controller_lung=controller_lung,
        controller_input_delay_s=controller_input_delay_s,
    )
    tidals = np.asarray([
        simulated_inspiratory_tidal_ml(result, cycle)
        for cycle in range(scenario.cycles)
    ])
    inspiration_indices = [
        np.flatnonzero((result.cycle == cycle) & (result.state == STATE_INSPIRATION))
        for cycle in range(scenario.cycles)
    ]
    starts = np.asarray([float(result.time_s[indices[0]]) for indices in inspiration_indices])
    start_volumes = np.asarray([
        float(result.volume_ml[indices[0] - 1]) if indices[0] else 0.0
        for indices in inspiration_indices
    ])
    saturated_fraction = float(np.mean([
        np.mean(result.opening[indices] >= 0.999) for indices in inspiration_indices
    ]))
    end_estimation_errors = np.asarray([
        float(result.estimated_volume_ml[indices[-1]] - result.volume_ml[indices[-1]])
        for indices in inspiration_indices
    ])
    actual_bpm = 60.0 / float(np.mean(np.diff(starts)))
    target = scenario.target_volume_ml
    max_error_pct = float(np.max(np.abs(tidals - target)) / target * 100)
    spread_pct = float((np.max(tidals) - np.min(tidals)) / target * 100)
    lower = 0.95 * target
    ideal = ideal_full_open_limit(scenario, result)
    return {
        "nominal_bpm": bpm,
        "control": "volume_deadline_guard" if guard else "flow_pid",
        "volume_feedback_source": scenario.volume_feedback_source,
        "cycles": scenario.cycles,
        "actual_bpm": actual_bpm,
        "inspiration_slot_s": scenario.inspiration_s,
        "active_inspiration_s": scenario.active_inspiration_s,
        "target_tidal_ml": target,
        "minimum_tidal_ml": float(np.min(tidals)),
        "mean_tidal_ml": float(np.mean(tidals)),
        "maximum_tidal_ml": float(np.max(tidals)),
        "maximum_inspiration_start_volume_ml": float(np.max(start_volumes)),
        "maximum_inspiration_samples": max(len(indices) for indices in inspiration_indices),
        "mean_inspiration_saturation_fraction": saturated_fraction,
        "estimated_sensor_zero_bias_slm": result.estimated_sensor_bias_slm,
        "mean_inspiration_end_estimation_error_ml": float(np.mean(end_estimation_errors)),
        "maximum_abs_inspiration_end_estimation_error_ml": float(np.max(np.abs(end_estimation_errors))),
        "maximum_target_error_pct": max_error_pct,
        "cycle_spread_pct_of_target": spread_pct,
        "ideal_full_open_peak_from_zero_ml": ideal,
        "model_upper_bound_meets_5pct": ideal >= lower,
        "tidal_requirement_pass": max_error_pct <= 5.0 and spread_pct <= 5.0,
        "rate_requirement_pass": abs(actual_bpm - bpm) / bpm <= 0.01,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).parent / "output" / "requirement_benchmark")
    args = parser.parse_args()
    cfg = load_config(args.config)
    rows = [evaluate(cfg, 15.0, False)]
    rows.extend(evaluate(cfg, bpm, True) for bpm in (12.0, 15.0, 16.0, 18.0))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    table = args.output_dir / "single_lung_30_cycle_benchmark.csv"
    with table.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    report = args.output_dir / "single_lung_30_cycle_benchmark.md"
    with report.open("w", encoding="utf-8") as handle:
        handle.write("# Single-lung simulation requirement audit\n\n")
        handle.write("One prototype branch represents a 250 mL share of the 500 mL "
                     "dual-branch target. All values below are simulated. "
                     "A saved manual expiration-flow target is replaced by the "
                     "phase-derived target in each scenario; the saved GUI "
                     "configuration is not modified.\n\n")
        handle.write("| Requested bpm | Control | Actual bpm | Tidal range / mL | "
                     "Max error / % | 30-cycle pass | Open peak from 0 / mL |\n")
        handle.write("| ---: | --- | ---: | ---: | ---: | --- | ---: |\n")
        for row in rows:
            handle.write(
                f"| {row['nominal_bpm']:.0f} | {row['control']} | {row['actual_bpm']:.2f} | "
                f"{row['minimum_tidal_ml']:.1f} to {row['maximum_tidal_ml']:.1f} | "
                f"{row['maximum_target_error_pct']:.1f} | "
                f"{'PASS' if row['tidal_requirement_pass'] and row['rate_requirement_pass'] else 'FAIL'} | "
                f"{row['ideal_full_open_peak_from_zero_ml']:.1f} |\n"
            )
        handle.write("\nThe model assumes a fully open atmosphere-inspiration path reaches the "
                     "250 mL capacity from 0 mL in 1.30 s. The intermediate curve, "
                     "input delay, valve response, and expiration dynamics are "
                     "assumptions, not physical calibration. At 18 bpm, the full-open "
                     "bound follows only from the chosen 0.57 s curve shape and "
                     "discretized 25 ms input delay. That model's bound is below the 237.5 mL "
                     "lower tolerance and cannot be corrected by changing PID gains. "
                     "A different real flow curve could change this conclusion.\n\n")
        handle.write("This audit does not verify physical pressure/flow acquisition, "
                     "host command timing, two-branch independent control, or "
                     "hardware repeatability. The UI playback is precomputed and "
                     "does not constitute a real-time hardware controller.\n")
    for row in rows:
        print(
            f"{row['nominal_bpm']:>2.0f} bpm {row['control']:<21} "
            f"actual {row['actual_bpm']:.2f} | tidal "
            f"{row['minimum_tidal_ml']:.1f}..{row['maximum_tidal_ml']:.1f} mL | "
            f"{'PASS' if row['tidal_requirement_pass'] and row['rate_requirement_pass'] else 'FAIL'}"
        )
    print(f"Saved {table} and {report}")


if __name__ == "__main__":
    main()
