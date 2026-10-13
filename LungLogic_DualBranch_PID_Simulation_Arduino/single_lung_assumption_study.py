"""Compare hypothetical fill curves sharing one assumed 1.30 s endpoint.

The assumed atmosphere-path endpoint does not identify an intermediate flow trace.
This study deliberately varies its shape while holding the full-open endpoint,
physical capacity and saved controller settings fixed. No hardware is accessed.
"""

from __future__ import annotations

import argparse
import copy
import csv
import math
from pathlib import Path

import numpy as np
from matplotlib.figure import Figure

from single_lung_pid_gui import (
    CONFIG_PATH,
    STATE_INSPIRATION,
    SingleLungConfig,
    SingleLungPlant,
    load_config,
    run_single_lung_simulation,
)


def full_open_trace(cfg: SingleLungConfig) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Ideal direct path from 0 mL, without command or sensor delays."""
    plant = SingleLungPlant(cfg.lung, np.random.default_rng(0))
    count = int(math.ceil((cfg.lung.full_open_fill_time_s + 0.2) / cfg.dt))
    times, volumes, flows = [0.0], [plant.volume_ml], [0.0]
    for index in range(1, count + 1):
        flow, _ = plant.step(1.0, 1, cfg.dt)
        times.append(index * cfg.dt)
        volumes.append(plant.volume_ml)
        flows.append(flow)
    return np.asarray(times), np.asarray(volumes), np.asarray(flows)


def run_study(cfg: SingleLungConfig, taus: list[float]) -> tuple[list[dict], Figure]:
    if not taus or any(not math.isfinite(tau) or tau <= 0 for tau in taus):
        raise ValueError("Provide one or more positive, finite tau values")
    fig = Figure(figsize=(11, 8), tight_layout=True)
    axes = fig.subplots(2, 2)
    rows = []
    for tau in taus:
        scenario = copy.deepcopy(cfg)
        scenario.lung.tau_in_s = tau
        scenario.validate()
        open_t, open_v, open_q = full_open_trace(scenario)
        hits = np.flatnonzero(open_v >= scenario.lung.capacity_ml - 1e-6)
        reach_s = float(open_t[hits[0]]) if hits.size else math.nan

        result = run_single_lung_simulation(scenario)
        last_cycle = result.cycle == scenario.cycles - 1
        inspiratory = last_cycle & (result.state == STATE_INSPIRATION)
        indices = np.flatnonzero(inspiratory)
        if not indices.size:
            raise RuntimeError("Simulation produced no final inspiration")
        relative_t = result.time_s[indices] - result.time_s[indices[0]]
        start_volume = float(result.volume_ml[indices[0] - 1]) if indices[0] else 0.0
        delivered = float(np.max(result.volume_ml[indices]) - start_volume)
        tidal = delivered
        saturated = float(np.mean(result.opening[indices] >= 0.999))
        label = f"tau = {tau:.3f} s"
        axes[0, 0].plot(open_t, open_v, label=label)
        axes[0, 1].plot(open_t, open_q, label=label)
        axes[1, 0].plot(relative_t, result.volume_ml[indices] - start_volume, label=label)
        axes[1, 1].plot(relative_t, result.opening[indices], label=label)
        rows.append({
            "tau_in_s": tau,
            "assumed_full_open_reach_s": scenario.lung.full_open_fill_time_s,
            "ideal_reach_s": reach_s,
            "virtual_equilibrium_ml": scenario.lung.inspiration_equilibrium_ml(),
            "controlled_inspiration_ml": delivered,
            "last_cycle_tidal_ml": tidal,
            "tidal_error_ml": scenario.target_volume_ml - tidal,
            "inspiration_saturation_fraction": saturated,
            "target_ml": scenario.target_volume_ml,
            "capacity_ml": scenario.lung.capacity_ml,
        })

    axes[0, 0].axhline(cfg.lung.capacity_ml, color="black", ls="--", lw=0.8)
    axes[0, 0].set(title="Ideal fully open: volume", ylabel="Volume / mL")
    axes[0, 1].set(title="Ideal fully open: flow", ylabel="True flow / SLM")
    axes[1, 0].axhline(cfg.target_volume_ml, color="black", ls="--", lw=0.8)
    axes[1, 0].set(title="Existing controller: last inspiration", xlabel="Inspiration time / s",
                   ylabel="Delivered volume / mL")
    axes[1, 1].set(title="Existing controller: PPV opening", xlabel="Inspiration time / s",
                   ylabel="Normalized opening", ylim=(-0.05, 1.05))
    for axis in axes.flat:
        axis.axvline(cfg.lung.full_open_fill_time_s, color="#b33a3a", ls=":", lw=1)
        axis.grid(True, alpha=0.3)
        axis.legend(fontsize=8)
    fig.suptitle(
        "Hypothetical fill curves constrained by one assumed atmosphere-path endpoint\n"
        "Intermediate flow, PPV dynamics and final hardware response are unmeasured",
        fontsize=12,
    )
    return rows, fig


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "output" / "assumption_study")
    parser.add_argument("--taus", type=float, nargs="+", help="Candidate inspiratory curve time constants / s")
    args = parser.parse_args()
    cfg = load_config(args.config)
    taus = args.taus or [0.6 * cfg.lung.tau_in_s, cfg.lung.tau_in_s, 1.5 * cfg.lung.tau_in_s]
    rows, fig = run_study(cfg, taus)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    png = args.output_dir / "fill_curve_sensitivity.png"
    table = args.output_dir / "fill_curve_sensitivity.csv"
    fig.savefig(png, dpi=140)
    with table.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    for row in rows:
        print(
            f"tau={row['tau_in_s']:.3f} s | ideal reach={row['ideal_reach_s']:.3f} s | "
            f"controlled tidal={row['last_cycle_tidal_ml']:.1f} mL | "
            f"inspiration saturation={100 * row['inspiration_saturation_fraction']:.1f}%"
        )
    print(f"Saved {png} and {table}")


if __name__ == "__main__":
    main()
