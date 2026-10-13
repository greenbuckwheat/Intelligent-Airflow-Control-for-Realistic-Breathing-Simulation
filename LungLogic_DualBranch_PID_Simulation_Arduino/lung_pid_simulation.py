"""Command-line digital twin for the dual-branch LungLogic system."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from lung_model import STATE_NAMES, ExperimentConfig, SimulationResult, load_config, run_simulation


def save_csv(result: SimulationResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "time_s", "state", "state_name", "cycle", "left_flow_slm", "right_flow_slm",
            "left_flow_reference_slm", "right_flow_reference_slm", "left_volume_ml",
            "right_volume_ml", "left_opening", "right_opening", "left_command_v",
            "right_command_v", "valve_mask",
        ])
        for i in range(result.time_s.size):
            writer.writerow([
                result.time_s[i], result.state[i], STATE_NAMES[result.state[i]], result.cycle[i],
                result.q_left_slm[i], result.q_right_slm[i], result.qref_left_slm[i],
                result.qref_right_slm[i], result.v_left_ml[i], result.v_right_ml[i],
                result.opening_left[i], result.opening_right[i], result.command_left_v[i],
                result.command_right_v[i], result.valve_mask[i],
            ])


def cycle_metrics(result: SimulationResult, cfg: ExperimentConfig) -> str:
    lines = []
    for cycle in range(cfg.cycles):
        selection = result.cycle == cycle
        insp = selection & (result.state == 3)
        if not np.any(insp):
            continue
        idx = np.flatnonzero(insp)
        t0 = result.time_s[idx[0]]
        values = []
        for volume, target in ((result.v_left_ml, cfg.left_target_ml), (result.v_right_ml, cfg.right_target_ml)):
            hit = idx[volume[idx] >= 0.99 * target]
            values.append(result.time_s[hit[0]] - t0 if hit.size else float("nan"))
        lines.append(
            f"cycle {cycle + 1}: t99 left={values[0]:.3f} s, right={values[1]:.3f} s; "
            f"peak=({np.max(result.v_left_ml[idx]):.1f}, {np.max(result.v_right_ml[idx]):.1f}) mL"
        )
    return "\n".join(lines)


def build_figure(result: SimulationResult, cfg: ExperimentConfig):
    fig, axes = plt.subplots(4, 1, figsize=(11.5, 10.0), sharex=True, constrained_layout=True)
    t = result.time_s
    axes[0].plot(t, result.v_left_ml, label="Left")
    axes[0].plot(t, result.v_right_ml, label="Right")
    axes[0].axhline(cfg.left_target_ml, color="C0", ls="--", lw=0.9)
    axes[0].axhline(cfg.right_target_ml, color="C1", ls=":", lw=0.9)
    axes[0].set_ylabel("Volume / mL")
    axes[0].set_title("Dual-branch volume tracking (250 mL each; 500 mL total)")
    axes[0].legend(ncol=2)

    axes[1].plot(t, result.q_left_slm, label="Left measured")
    axes[1].plot(t, result.q_right_slm, label="Right measured")
    axes[1].plot(t, result.qref_left_slm, "k--", lw=1.0, label="Reference magnitude")
    axes[1].set_ylabel("Flow / SLM")
    axes[1].legend(ncol=3)

    axes[2].plot(t, result.opening_left, label="Left opening")
    axes[2].plot(t, result.opening_right, label="Right opening")
    axes[2].set_ylabel("Pinch opening")
    axes[2].set_ylim(-0.05, 1.05)
    axes[2].legend(ncol=2)

    axes[3].step(t, result.state, where="post", label="State")
    axes[3].step(t, result.valve_mask / 16.0, where="post", label="Valve mask / 16")
    axes[3].set_yticks(sorted(STATE_NAMES))
    axes[3].set_yticklabels([STATE_NAMES[k] for k in sorted(STATE_NAMES)], fontsize=8)
    axes[3].set_ylabel("State")
    axes[3].set_xlabel("Time / s")
    axes[3].legend(ncol=2)
    for ax in axes:
        ax.grid(True, ls="--", alpha=0.35)
    return fig


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.example.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()
    cfg = load_config(args.config)
    result = run_simulation(cfg)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    save_csv(result, output / "lung_simulation.csv")
    fig = build_figure(result, cfg)
    fig.savefig(output / "lung_simulation.png", dpi=180)
    print(cycle_metrics(result, cfg))
    print(f"Saved {output / 'lung_simulation.csv'}")
    print(f"Saved {output / 'lung_simulation.png'}")
    if args.show:
        plt.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
