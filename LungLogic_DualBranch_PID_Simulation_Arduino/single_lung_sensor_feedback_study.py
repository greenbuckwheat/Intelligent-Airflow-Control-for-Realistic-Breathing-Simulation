"""Compare true-state and sensor-integrated volume feedback in simulation.

The same 81 hypothetical plant/delay cases and fixed nominal controller model
are used for both feedback sources. A few additional sensor cases vary fixed
zero bias, noise and lag. This is an offline model study, not hardware control.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import numpy as np
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.figure import Figure
from matplotlib.patches import Patch

from single_lung_pid_gui import CONFIG_PATH, SingleLungConfig, load_config
from single_lung_requirement_benchmark import evaluate
from single_lung_uncertainty_sweep import (
    EXPIRATION_TAUS_S, FILL_SHAPES_S, INPUT_DELAYS_S,
    NOMINAL_CONTROLLER_INPUT_DELAY_S, NOMINAL_CONTROLLER_TAU_IN_S,
    NOMINAL_CONTROLLER_TAU_OUT_S, RATES_BPM, STATUS_CODES, STATUS_COLORS,
    prototype_config, run_sweep,
)


def study_config(cfg: SingleLungConfig) -> SingleLungConfig:
    scenario = prototype_config(cfg)
    scenario.lung.transport_delay_s = 0.025
    scenario.lung.sensor_tau_s = 0.020
    scenario.lung.sensor_noise_slm = 0.10
    scenario.lung.sensor_bias_slm = 0.0
    return scenario


def case_key(row: dict) -> tuple[float, float, float, float]:
    return (
        row["nominal_bpm"], row["assumed_expiration_tau_s"],
        row["assumed_fill_shape_tau_s"], row["assumed_input_delay_s"],
    )


def compare_grids(cfg: SingleLungConfig, **grid_axes) -> tuple[list[dict], list[dict], list[dict]]:
    """Re-run each plant case with only the controller feedback source changed."""
    base = study_config(cfg)
    oracle_cfg = copy.deepcopy(base)
    oracle_cfg.volume_feedback_source = "oracle"
    sensor_cfg = copy.deepcopy(base)
    sensor_cfg.volume_feedback_source = "sensor_integral"
    oracle = run_sweep(oracle_cfg, **grid_axes)
    sensor = run_sweep(sensor_cfg, **grid_axes)
    oracle_by_key = {case_key(row): row for row in oracle}
    paired = []
    for measured in sensor:
        true_state = oracle_by_key[case_key(measured)]
        paired.append({
            "rate_bpm": measured["nominal_bpm"],
            "plant_tau_in_s": measured["assumed_fill_shape_tau_s"],
            "plant_tau_out_s": measured["assumed_expiration_tau_s"],
            "plant_command_delay_s": measured["assumed_input_delay_s"],
            "oracle_status": true_state["status"],
            "sensor_status": measured["status"],
            "oracle_minimum_tidal_ml": true_state["minimum_tidal_ml"],
            "sensor_minimum_tidal_ml": measured["minimum_tidal_ml"],
            "sensor_minus_oracle_minimum_tidal_ml": (
                measured["minimum_tidal_ml"] - true_state["minimum_tidal_ml"]
            ),
            "oracle_maximum_tidal_ml": true_state["maximum_tidal_ml"],
            "sensor_maximum_tidal_ml": measured["maximum_tidal_ml"],
            "sensor_max_abs_end_inspiration_estimation_error_ml": (
                measured["maximum_abs_inspiration_end_estimation_error_ml"]
            ),
            "sensor_mean_end_inspiration_estimation_error_ml": (
                measured["mean_inspiration_end_estimation_error_ml"]
            ),
            "estimated_zero_bias_slm": measured["estimated_sensor_zero_bias_slm"],
            "sensor_mean_inspiration_saturation_fraction": (
                measured["mean_inspiration_saturation_fraction"]
            ),
        })
    return oracle, sensor, paired


def sensor_stress_cases(cfg: SingleLungConfig) -> list[dict]:
    """Selected sensor-only stresses around the nominal plant and controller."""
    base = study_config(cfg)
    controller_model = copy.deepcopy(base.lung)
    controller_model.tau_in_s = NOMINAL_CONTROLLER_TAU_IN_S
    controller_model.tau_out_s = NOMINAL_CONTROLLER_TAU_OUT_S
    cases = (
        ("nominal", 0.0, 0.10, 0.025, 0.020),
        ("fixed_zero_bias", 0.25, 0.10, 0.025, 0.020),
        ("high_sample_noise", 0.0, 0.50, 0.025, 0.020),
        ("long_sensor_lag", 0.0, 0.10, 0.075, 0.080),
    )
    rows = []
    for name, bias, noise, transport, sensor_tau in cases:
        for rate in RATES_BPM:
            scenario = copy.deepcopy(base)
            scenario.lung.sensor_bias_slm = bias
            scenario.lung.sensor_noise_slm = noise
            scenario.lung.transport_delay_s = transport
            scenario.lung.sensor_tau_s = sensor_tau
            result = evaluate(
                scenario, rate, True, controller_lung=controller_model,
                controller_input_delay_s=NOMINAL_CONTROLLER_INPUT_DELAY_S,
                volume_feedback_source="sensor_integral",
            )
            rows.append({
                "sensor_case": name,
                "configured_sensor_bias_slm": bias,
                "sensor_noise_sigma_slm": noise,
                "transport_delay_s": transport,
                "sensor_time_constant_s": sensor_tau,
                **result,
            })
    return rows


def plot_comparison(paired: list[dict]) -> Figure:
    lookup = {
        (row["rate_bpm"], row["plant_tau_out_s"],
         row["plant_tau_in_s"], row["plant_command_delay_s"]): row
        for row in paired
    }
    fig = Figure(figsize=(11.5, 3.3 * len(RATES_BPM)), layout="constrained")
    axes = fig.subplots(len(RATES_BPM), len(EXPIRATION_TAUS_S), squeeze=False)
    cmap = ListedColormap(STATUS_COLORS)
    norm = BoundaryNorm((-0.5, 0.5, 1.5, 2.5), cmap.N)
    for i, rate in enumerate(RATES_BPM):
        for j, expiration_tau in enumerate(EXPIRATION_TAUS_S):
            axis = axes[i, j]
            cells = np.asarray([
                [STATUS_CODES[lookup[(rate, expiration_tau, shape, delay)]["sensor_status"]]
                 for delay in INPUT_DELAYS_S]
                for shape in FILL_SHAPES_S
            ])
            axis.imshow(cells, cmap=cmap, norm=norm, origin="lower", aspect="auto")
            axis.set_xticks(range(len(INPUT_DELAYS_S)), [f"{1000 * value:g}" for value in INPUT_DELAYS_S])
            axis.set_yticks(range(len(FILL_SHAPES_S)), [f"{value:.2f}" for value in FILL_SHAPES_S])
            axis.set_xlabel("Plant command delay / ms")
            if j == 0:
                axis.set_ylabel(f"{rate:g} bpm\nPlant fill shape tau / s")
            if i == 0:
                axis.set_title(f"Plant expiration tau = {expiration_tau:.2f} s")
            for y, shape in enumerate(FILL_SHAPES_S):
                for x, delay in enumerate(INPUT_DELAYS_S):
                    row = lookup[(rate, expiration_tau, shape, delay)]
                    axis.text(
                        x, y,
                        f"{row['sensor_minimum_tidal_ml']:.0f}\n"
                        f"({row['sensor_minus_oracle_minimum_tidal_ml']:+.1f})",
                        ha="center", va="center", fontsize=9, color="#17232b",
                    )
    fig.suptitle("Sensor-integrated controller: 30-cycle modeled outcome\n"
                 "Cell = minimum true tidal mL (difference from oracle controller)", fontsize=13)
    fig.legend(handles=[
        Patch(facecolor=STATUS_COLORS[0], label="Plant full-open bound below 237.5 mL"),
        Patch(facecolor=STATUS_COLORS[1], label="Bound sufficient, sensor control fails"),
        Patch(facecolor=STATUS_COLORS[2], label="Sensor-control simulation passes"),
    ], loc="outside lower center", ncol=3, fontsize=9)
    return fig


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_report(path: Path, paired: list[dict], stress: list[dict],
                 cfg: SingleLungConfig) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Sensor-integrated versus true-volume control\n\n")
        handle.write("All figures are simulated. The 81-case grid uses the same "
                     "plant assumptions, saved PID/Smith gains and fixed nominal "
                     "controller model in both runs. The controller information "
                     "mode changes: sensor mode replaces both true-volume feedback "
                     "and knowledge of the plant-applied valve command. It "
                     "calibrates a constant zero offset "
                     f"during the initial {cfg.precondition_s:.2f} s precondition, integrates the delayed "
                     "and noisy signed flow, and gives Smith an input command "
                     "delayed by the controller's assumed 25 ms. Its feedback "
                     "does not read the plant's true volume.\n\n")
        handle.write("| Rate | Oracle simulated passes | Sensor simulated passes | "
                     "Oracle pass to sensor fail | Sensor pass to oracle fail | "
                     "Largest end-inspiration volume-estimate error / mL |\n")
        handle.write("| ---: | ---: | ---: | ---: | ---: | ---: |\n")
        for rate in RATES_BPM:
            selected = [row for row in paired if row["rate_bpm"] == rate]
            oracle_count = sum(row["oracle_status"] == "simulated_pass" for row in selected)
            sensor_count = sum(row["sensor_status"] == "simulated_pass" for row in selected)
            lost = sum(row["oracle_status"] == "simulated_pass" and
                       row["sensor_status"] != "simulated_pass" for row in selected)
            gained = sum(row["oracle_status"] != "simulated_pass" and
                         row["sensor_status"] == "simulated_pass" for row in selected)
            handle.write(
                f"| {rate:g} bpm | {oracle_count}/{len(selected)} | "
                f"{sensor_count}/{len(selected)} | "
                f"{lost} | {gained} | "
                f"{max(row['sensor_max_abs_end_inspiration_estimation_error_ml'] for row in selected):.1f} |\n"
            )
        gained = [row for row in paired if row["oracle_status"] != "simulated_pass"
                  and row["sensor_status"] == "simulated_pass"]
        if gained:
            handle.write("\nOne or more boundary cells change to a numerical pass "
                         "under sensor feedback. This does not show better sensor "
                         "control: the estimator and delayed-command assumption "
                         "change when the valve opens, and a case near 237.5 mL "
                         "can flip classification with little physical margin.\n")
        handle.write("\nThese case counts are not probabilities. A passing model cell "
                     "does not imply physical 30-cycle repeatability. Color shows "
                     "the sensor-controller result; each cell shows its minimum "
                     "true inspired tidal volume and the change from the oracle "
                     "controller in parentheses.\n\n")
        handle.write("![Sensor feedback comparison](sensor_feedback_map.png)\n\n")
        handle.write("## Nominal plant and sensor stress\n\n")
        handle.write("The following cases keep the plant at tau_in = 0.57 s, "
                     "tau_out = 0.50 s and direct-path reach time 1.30 s. "
                     "Fixed bias is an assumed constant SLM offset, and the "
                     "precondition zero calibration attempts to remove it.\n\n")
        handle.write("| Sensor case | Rate | True tidal range / mL | "
                     "Maximum end-inspiration estimate error / mL | "
                     "Estimated zero bias / SLM | Target check |\n")
        handle.write("| --- | ---: | ---: | ---: | ---: | --- |\n")
        for row in stress:
            passed = row["tidal_requirement_pass"] and row["rate_requirement_pass"]
            handle.write(
                f"| {row['sensor_case']} | {row['nominal_bpm']:g} bpm | "
                f"{row['minimum_tidal_ml']:.1f} to {row['maximum_tidal_ml']:.1f} | "
                f"{row['maximum_abs_inspiration_end_estimation_error_ml']:.1f} | "
                f"{row['estimated_sensor_zero_bias_slm']:+.3f} | "
                f"{'PASS' if passed else 'FAIL'} |\n"
            )
        transitions = Counter((row["oracle_status"], row["sensor_status"]) for row in paired)
        handle.write("\nStatus transitions across all cases: " + ", ".join(
            f"{old} -> {new}: {count}" for (old, new), count in sorted(transitions.items())
        ) + ".\n\n")
        handle.write("The estimator assumes the lung begins empty, both valves "
                     "are closed with zero physical flow during calibration, "
                     "the sensor offset remains fixed, and standard-flow integral "
                     "is comparable with the simulated geometric volume. It clips "
                     "the estimate to the known 250 mL model capacity. None of "
                     "these assumptions has been validated on the installed path. "
                     "Pressure sensing, physical PPV actuation and real-time "
                     "closed-loop control are still absent.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).parent / "output" / "sensor_feedback_study")
    args = parser.parse_args()
    cfg = load_config(args.config)
    oracle, sensor, paired = compare_grids(cfg)
    stress = sensor_stress_cases(cfg)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "feedback_comparison.csv", paired)
    write_csv(args.output_dir / "sensor_stress.csv", stress)
    plot_comparison(paired).savefig(args.output_dir / "sensor_feedback_map.png", dpi=150)
    write_report(args.output_dir / "sensor_feedback_report.md", paired, stress, cfg)
    with (args.output_dir / "study_inputs.json").open("w", encoding="utf-8") as handle:
        json.dump({
            "base_config": asdict(study_config(cfg)),
            "rates_bpm": RATES_BPM,
            "fill_shapes_s": FILL_SHAPES_S,
            "input_delays_s": INPUT_DELAYS_S,
            "expiration_taus_s": EXPIRATION_TAUS_S,
            "fixed_controller_model": {
                "tau_in_s": NOMINAL_CONTROLLER_TAU_IN_S,
                "tau_out_s": NOMINAL_CONTROLLER_TAU_OUT_S,
                "assumed_input_delay_s": NOMINAL_CONTROLLER_INPUT_DELAY_S,
            },
            "cycles_per_grid_case": 30,
        }, handle, indent=2)
        handle.write("\n")
    print(f"Saved sensor-feedback study to {args.output_dir}")


if __name__ == "__main__":
    main()
