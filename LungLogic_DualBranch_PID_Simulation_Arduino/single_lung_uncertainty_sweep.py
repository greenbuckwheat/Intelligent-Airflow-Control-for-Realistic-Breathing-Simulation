"""Explore unmeasured single-lung dynamics without claiming hardware validation.

The controlled grid keeps the assumed atmosphere-inspiration, fully open 1.30 s
endpoint fixed while varying the unknown intermediate fill-curve shape,
command delay and expiration time constant. The controller model stays at its
nominal parameters throughout the controlled grid. A separate analytic study explores
hypothetical slower full-open reach times for the eventual installed path.
All ranges are illustrative assumptions, not measured confidence intervals.
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
from single_lung_requirement_benchmark import evaluate, full_open_volume_from_zero


RATES_BPM = (12.0, 15.0, 16.0, 18.0)
FILL_SHAPES_S = (0.35, 0.57, 0.85)
INPUT_DELAYS_S = (0.0, 0.025, 0.060)
EXPIRATION_TAUS_S = (0.30, 0.50, 1.00)
DIRECT_REACH_S = 1.30
NOMINAL_CONTROLLER_TAU_IN_S = 0.57
NOMINAL_CONTROLLER_TAU_OUT_S = 0.50
NOMINAL_CONTROLLER_INPUT_DELAY_S = 0.025
STATUS_CODES = {"model_bound_fail": 0, "controlled_fail": 1, "simulated_pass": 2}
STATUS_COLORS = ("#d77b6c", "#e7b967", "#70ad8c")


def prototype_config(cfg: SingleLungConfig) -> SingleLungConfig:
    """Use the saved controller gains with an explicit 250 mL prototype task."""
    scenario = copy.deepcopy(cfg)
    scenario.target_volume_ml = 250.0
    scenario.max_lung_volume_ml = 250.0
    scenario.lung.capacity_ml = 250.0
    scenario.lung.full_open_fill_time_s = DIRECT_REACH_S
    scenario.ie_inspiration = 1.0
    scenario.ie_expiration = 2.0
    # A saved manual expiration-flow reference may be invalid at another bpm.
    # Derive it per phase so the sweep compares physical assumptions instead.
    scenario.target_expiration_flow_slm = None
    scenario.input_delay.enabled = True
    return scenario


def classify(row: dict) -> str:
    if not row["model_upper_bound_meets_5pct"]:
        return "model_bound_fail"
    if row["tidal_requirement_pass"] and row["rate_requirement_pass"]:
        return "simulated_pass"
    return "controlled_fail"


def run_sweep(
    cfg: SingleLungConfig,
    rates: tuple[float, ...] = RATES_BPM,
    fill_shapes: tuple[float, ...] = FILL_SHAPES_S,
    input_delays: tuple[float, ...] = INPUT_DELAYS_S,
    expiration_taus: tuple[float, ...] = EXPIRATION_TAUS_S,
) -> list[dict]:
    """Return 30-cycle outcomes for each explicitly hypothetical combination."""
    if not all((rates, fill_shapes, input_delays, expiration_taus)):
        raise ValueError("Every sweep axis needs at least one value")
    base = prototype_config(cfg)
    controller_model = copy.deepcopy(base.lung)
    controller_model.tau_in_s = NOMINAL_CONTROLLER_TAU_IN_S
    controller_model.tau_out_s = NOMINAL_CONTROLLER_TAU_OUT_S
    rows = []
    for rate in rates:
        for expiration_tau in expiration_taus:
            for fill_shape in fill_shapes:
                for input_delay in input_delays:
                    scenario = copy.deepcopy(base)
                    scenario.lung.tau_in_s = fill_shape
                    scenario.lung.tau_out_s = expiration_tau
                    scenario.input_delay.delay_s = input_delay
                    result = evaluate(
                        scenario, rate, True, controller_lung=controller_model,
                        controller_input_delay_s=NOMINAL_CONTROLLER_INPUT_DELAY_S,
                    )
                    # The zero-volume, fully open trace bounds each tidal gain.
                    if result["maximum_tidal_ml"] > result["ideal_full_open_peak_from_zero_ml"] + 1e-6:
                        raise AssertionError("Controlled tidal volume exceeded its full-open model bound")
                    rows.append({
                        "assumed_direct_full_open_reach_s": DIRECT_REACH_S,
                        "assumed_fill_shape_tau_s": fill_shape,
                        "assumed_input_delay_s": input_delay,
                        "assumed_expiration_tau_s": expiration_tau,
                        "controller_fill_shape_tau_s": NOMINAL_CONTROLLER_TAU_IN_S,
                        "controller_expiration_tau_s": NOMINAL_CONTROLLER_TAU_OUT_S,
                        "controller_input_delay_s": NOMINAL_CONTROLLER_INPUT_DELAY_S,
                        "status": classify(result),
                        **result,
                    })
        print(f"Completed {rate:g} bpm: {len(expiration_taus) * len(fill_shapes) * len(input_delays)} scenarios", flush=True)
    return rows


def installed_path_limits(rows: list[dict], cfg: SingleLungConfig,
                          reach_times: np.ndarray | None = None) -> list[dict]:
    """Analytic full-open limits if the unmeasured installed path is slower."""
    reach_times = np.linspace(1.30, 1.80, 11) if reach_times is None else reach_times
    sample_counts = {row["nominal_bpm"]: row["maximum_inspiration_samples"] for row in rows}
    base = prototype_config(cfg)
    base.input_delay.delay_s = NOMINAL_CONTROLLER_INPUT_DELAY_S
    limits = []
    for rate, inspiration_samples in sorted(sample_counts.items()):
        for fill_shape in FILL_SHAPES_S:
            for reach_time in reach_times:
                scenario = copy.deepcopy(base)
                scenario.respiratory_rate_bpm = rate
                scenario.lung.tau_in_s = fill_shape
                scenario.lung.full_open_fill_time_s = float(reach_time)
                upper = full_open_volume_from_zero(scenario, inspiration_samples)
                limits.append({
                    "rate_bpm": rate,
                    "assumed_installed_full_open_reach_s": float(reach_time),
                    "assumed_fill_shape_tau_s": fill_shape,
                    "assumed_input_delay_s": NOMINAL_CONTROLLER_INPUT_DELAY_S,
                    "maximum_inspiration_samples": inspiration_samples,
                    "full_open_tidal_limit_from_zero_ml": upper,
                    "meets_237_5_ml_lower_tolerance": upper >= 237.5,
                })
    return limits


def plot_controlled_map(rows: list[dict]) -> Figure:
    lookup = {
        (row["nominal_bpm"], row["assumed_expiration_tau_s"],
         row["assumed_fill_shape_tau_s"], row["assumed_input_delay_s"]): row
        for row in rows
    }
    fig = Figure(figsize=(11.5, 12.8), layout="constrained")
    axes = fig.subplots(len(RATES_BPM), len(EXPIRATION_TAUS_S), squeeze=False)
    cmap = ListedColormap(STATUS_COLORS)
    norm = BoundaryNorm((-0.5, 0.5, 1.5, 2.5), cmap.N)
    for i, rate in enumerate(RATES_BPM):
        for j, expiration_tau in enumerate(EXPIRATION_TAUS_S):
            axis = axes[i, j]
            cells = np.asarray([
                [STATUS_CODES[lookup[(rate, expiration_tau, shape, delay)]["status"]]
                 for delay in INPUT_DELAYS_S]
                for shape in FILL_SHAPES_S
            ])
            axis.imshow(cells, cmap=cmap, norm=norm, origin="lower", aspect="auto")
            axis.set_xticks(range(len(INPUT_DELAYS_S)), [f"{1000 * value:g}" for value in INPUT_DELAYS_S])
            axis.set_yticks(range(len(FILL_SHAPES_S)), [f"{value:.2f}" for value in FILL_SHAPES_S])
            axis.set_xlabel("Assumed command delay / ms")
            if j == 0:
                axis.set_ylabel(f"{rate:g} bpm\nFill shape tau / s")
            if i == 0:
                axis.set_title(f"Expiration tau = {expiration_tau:.2f} s")
            for y, shape in enumerate(FILL_SHAPES_S):
                for x, delay in enumerate(INPUT_DELAYS_S):
                    row = lookup[(rate, expiration_tau, shape, delay)]
                    axis.text(x, y, f"{row['minimum_tidal_ml']:.0f}",
                              ha="center", va="center", fontsize=10, color="#17232b")
    fig.suptitle("30-cycle modeled outcomes (cell number = minimum inspired tidal mL)\n"
                 "Assumed atmosphere-path full-open endpoint held at 250 mL in 1.30 s", fontsize=13)
    fig.legend(handles=[
        Patch(facecolor=STATUS_COLORS[0], label="Full-open model bound below 237.5 mL"),
        Patch(facecolor=STATUS_COLORS[1], label="Bound sufficient, 30-cycle control fails"),
        Patch(facecolor=STATUS_COLORS[2], label="30-cycle simulation passes"),
    ], loc="outside lower center", ncol=3, fontsize=9)
    return fig


def plot_installed_limits(limits: list[dict]) -> Figure:
    fig = Figure(figsize=(10.5, 10), layout="constrained")
    axes = fig.subplots(len(RATES_BPM), 1, sharex=True)
    colors = {0.35: "#2f78a9", 0.57: "#bd6e28", 0.85: "#8062a6"}
    for axis, rate in zip(axes, RATES_BPM):
        for shape in FILL_SHAPES_S:
            selected = [row for row in limits if row["rate_bpm"] == rate
                        and row["assumed_fill_shape_tau_s"] == shape]
            axis.plot(
                [row["assumed_installed_full_open_reach_s"] for row in selected],
                [row["full_open_tidal_limit_from_zero_ml"] for row in selected],
                marker=".", color=colors[shape], label=f"Fill shape tau = {shape:.2f} s",
            )
        axis.axhline(237.5, color="#a92d3b", linestyle="--", label="237.5 mL lower tolerance")
        axis.axvline(DIRECT_REACH_S, color="#555", linestyle=":", label="Observed direct-path time")
        axis.set_ylabel(f"{rate:g} bpm\nFull-open limit / mL")
        axis.grid(alpha=0.25)
    axes[-1].set_xlabel("Hypothetical full-open reach time after installing components / s")
    axes[0].legend(ncol=2, fontsize=8)
    fig.suptitle("Installed-path sensitivity (not measured)\n"
                 "25 ms configured input delay; fully open from 0 mL", fontsize=13)
    return fig


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_report(path: Path, rows: list[dict], limits: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Single-lung model uncertainty sweep\n\n")
        handle.write("This is a simulation stress study, not a physical acceptance test. "
                     "The selected ranges are illustrative assumptions, not measured "
                     "confidence intervals or scenario probabilities.\n\n")
        handle.write("The controlled grid fixes the assumed **atmosphere-inspiration** "
                     "fully open endpoint at 250 mL in 1.30 s. It varies the "
                     "unmeasured curve shape (tau_in = 0.35/0.57/0.85 s), command "
                     "delay (0/25/60 ms), and expiration time constant "
                     "(0.30/0.50/1.00 s). Each cell contains 30 simulated cycles "
                     "with the saved PID/Smith gains and the optional model-based "
                     "volume deadline guard. The controller model is fixed at "
                     "tau_in = 0.57 s, tau_out = 0.50 s and assumed input delay "
                     "25 ms while the simulated plant and actual command delay vary. "
                     "A 25 ms delay is rounded to two 10 ms "
                     "control samples by the current simulator. A saved manual "
                     "expiration-flow target is replaced by the phase-derived target "
                     "to keep all rates comparable; the saved GUI configuration "
                     "is not changed.\n\n")
        handle.write("| Rate | Model bound fails | Bound sufficient but control fails | "
                     "Simulation passes | Minimum tidal across grid / mL | "
                     "Maximum residual at inspiration start / mL |\n")
        handle.write("| ---: | ---: | ---: | ---: | ---: | ---: |\n")
        for rate in RATES_BPM:
            selected = [row for row in rows if row["nominal_bpm"] == rate]
            counts = Counter(row["status"] for row in selected)
            handle.write(
                f"| {rate:g} bpm | {counts['model_bound_fail']} | "
                f"{counts['controlled_fail']} | {counts['simulated_pass']} | "
                f"{min(row['minimum_tidal_ml'] for row in selected):.1f} | "
                f"{max(row['maximum_inspiration_start_volume_ml'] for row in selected):.1f} |\n"
            )
        handle.write("\n## 15 and 16 bpm sensitivity\n\n")
        handle.write("Each row below holds one assumed parameter fixed and varies the "
                     "other two over their listed values. Each group therefore has "
                     "nine 30-cycle cases. Counts are scenario counts, not "
                     "probabilities.\n\n")
        handle.write("| Rate | Fixed assumption | Value | Pass / 9 | "
                     "Model bound fail / 9 | Control fail / 9 |\n")
        handle.write("| ---: | --- | ---: | ---: | ---: | ---: |\n")
        for rate in (15.0, 16.0):
            rate_rows = [row for row in rows if row["nominal_bpm"] == rate]
            for key, label, values in (
                ("assumed_fill_shape_tau_s", "Fill shape tau / s", FILL_SHAPES_S),
                ("assumed_input_delay_s", "Command delay / s", INPUT_DELAYS_S),
                ("assumed_expiration_tau_s", "Expiration tau / s", EXPIRATION_TAUS_S),
            ):
                for value in values:
                    selected = [row for row in rate_rows if row[key] == value]
                    counts = Counter(row["status"] for row in selected)
                    handle.write(
                        f"| {rate:g} bpm | {label} | {value:.3f} | "
                        f"{counts['simulated_pass']} / {len(selected)} | "
                        f"{counts['model_bound_fail']} / {len(selected)} | "
                        f"{counts['controlled_fail']} / {len(selected)} |\n"
                    )
        passing_18 = [row for row in rows if row["nominal_bpm"] == 18.0
                      and row["status"] == "simulated_pass"]
        if passing_18:
            handle.write(
                f"\nThe {len(passing_18)} passing 18 bpm cells reach only "
                f"{min(row['minimum_tidal_ml'] for row in passing_18):.1f}-"
                f"{max(row['minimum_tidal_ml'] for row in passing_18):.1f} mL "
                "at their weakest cycle; mean inspiration valve saturation is "
                f"{min(row['mean_inspiration_saturation_fraction'] for row in passing_18) * 100:.0f}% "
                "or higher. Passing the numerical tolerance here leaves little "
                "or no modeled throttling margin.\n"
            )
        handle.write("\nA model-bound failure means even the fully open model from 0 mL "
                     "cannot reach the 237.5 mL lower tolerance within the sampled "
                     "inspiration slot. A control failure with a sufficient bound "
                     "may involve expiration residual, the chosen PID/reference, "
                     "or other simulated dynamics; it is not automatically a PID "
                     "gain problem. Green cells meet the target on every one of "
                     "30 cycles and meet the 5% cycle-spread and 1% cadence checks.\n\n")
        handle.write("![30-cycle model outcome map](controlled_outcome_map.png)\n\n")
        handle.write("## Hypothetical installed path\n\n")
        handle.write("The 1.30 s endpoint is assumed for the atmosphere path. The final "
                     "solenoid, PPV, sensor and tubing path has not been measured. "
                     "The second plot therefore asks what happens if its full-open "
                     "reach time is 1.30-1.80 s while holding the same candidate "
                     "curve shapes and a configured 25 ms input delay. These are "
                     "analytic fully open limits from 0 mL, not controlled runs.\n\n")
        handle.write("![Hypothetical installed-path capacity](installed_path_limit.png)\n\n")
        baseline = [row for row in rows if row["assumed_fill_shape_tau_s"] == 0.57
                    and row["assumed_input_delay_s"] == 0.025
                    and row["assumed_expiration_tau_s"] == 0.50]
        handle.write("## Current assumed point\n\n")
        handle.write("| Rate | Minimum/maximum inspired tidal / mL | Full-open bound / mL | Result |\n")
        handle.write("| ---: | ---: | ---: | --- |\n")
        for row in baseline:
            handle.write(
                f"| {row['nominal_bpm']:g} bpm | {row['minimum_tidal_ml']:.1f} to "
                f"{row['maximum_tidal_ml']:.1f} | "
                f"{row['ideal_full_open_peak_from_zero_ml']:.1f} | {row['status']} |\n"
            )
        boundary_16 = next(row for row in baseline if row["nominal_bpm"] == 16.0)
        handle.write(
            f"\nAt 16 bpm, the default case's weakest cycle is "
            f"{boundary_16['minimum_tidal_ml']:.1f} mL versus the 237.5 mL "
            "lower tolerance. This scan uses internal true-volume feedback; "
            "a sensor-integrated feedback configuration can cross this narrow "
            "boundary differently. Treat 16 bpm as a model-sensitive boundary, "
            "not a validated operating point.\n"
        )
        handle.write("\nThe count of passing cells is not a reliability estimate. "
                     "A different unknown real flow curve could change the result, "
                     "and the installed path may have a slower reach time than 1.30 s. "
                     "The controller model is fixed, but its guard and feedforward "
                     "still read simulator true volume. Physical flow "
                     "integration, pressure acquisition, valve control and hardware "
                     "repeatability remain unverified.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).parent / "output" / "uncertainty_sweep")
    args = parser.parse_args()
    cfg = load_config(args.config)
    rows = run_sweep(cfg)
    limits = installed_path_limits(rows, cfg)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "controlled_grid.csv", rows)
    write_csv(args.output_dir / "installed_path_limits.csv", limits)
    plot_controlled_map(rows).savefig(args.output_dir / "controlled_outcome_map.png", dpi=150)
    plot_installed_limits(limits).savefig(args.output_dir / "installed_path_limit.png", dpi=150)
    write_report(args.output_dir / "uncertainty_report.md", rows, limits)
    with (args.output_dir / "sweep_inputs.json").open("w", encoding="utf-8") as handle:
        json.dump({
            "prototype_config": asdict(prototype_config(cfg)),
            "rates_bpm": RATES_BPM,
            "fill_shapes_s": FILL_SHAPES_S,
            "input_delays_s": INPUT_DELAYS_S,
            "expiration_taus_s": EXPIRATION_TAUS_S,
            "fixed_controller_model": {
                "tau_in_s": NOMINAL_CONTROLLER_TAU_IN_S,
                "tau_out_s": NOMINAL_CONTROLLER_TAU_OUT_S,
                "assumed_input_delay_s": NOMINAL_CONTROLLER_INPUT_DELAY_S,
            },
            "controlled_cycles_per_cell": 30,
            "installed_path_reach_times_s": sorted({
                row["assumed_installed_full_open_reach_s"] for row in limits
            }),
        }, handle, indent=2)
        handle.write("\n")
    print(f"Saved model stress-study outputs to {args.output_dir}")


if __name__ == "__main__":
    main()
