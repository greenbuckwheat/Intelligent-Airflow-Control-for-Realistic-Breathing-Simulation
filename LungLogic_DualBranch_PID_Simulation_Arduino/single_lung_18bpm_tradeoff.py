"""Reproducible 18 bpm what-if study for the uncalibrated single-lung model.

This changes simulation assumptions only. A pass here is not a prediction for
the assembled pneumatic path or evidence of hardware repeatability.
"""

from __future__ import annotations

import argparse
import copy
import csv
from pathlib import Path

from single_lung_pid_gui import CONFIG_PATH, load_config
from single_lung_requirement_benchmark import evaluate


def study_rows(config_path: Path) -> list[dict]:
    base = load_config(config_path)
    # Compare all cases with a controller that retains the same nominal model.
    controller_lung = copy.deepcopy(base.lung)
    controller_delay_s = base.input_delay.delay_s if base.input_delay.enabled else 0.0

    cases = [
        ("Baseline: 1:2, 250 mL", {}),
        ("I:E 1:1.7", {"ie_expiration": 1.7}),
        ("I:E 1:1.6", {"ie_expiration": 1.6}),
        ("I:E 1:1.5", {"ie_expiration": 1.5}),
        ("I:E 1:1.4", {"ie_expiration": 1.4}),
        ("I:E 1:1.3", {"ie_expiration": 1.3}),
        ("I:E 1:1.2", {"ie_expiration": 1.2}),
        ("Zero assumed switch dead time", {"dead_time_s": 0.0}),
        ("Zero assumed input delay", {"input_delay.delay_s": 0.0}),
        ("235 mL target, I:E 1:2", {"target_volume_ml": 235.0}),
        ("1.10 s full-open reach, I:E 1:2", {"lung.full_open_fill_time_s": 1.10}),
        ("1.50 s installed path, I:E 1:1.4", {
            "lung.full_open_fill_time_s": 1.50, "ie_expiration": 1.4,
        }),
        ("1.50 s installed path, I:E 1:1.2", {
            "lung.full_open_fill_time_s": 1.50, "ie_expiration": 1.2,
        }),
        ("1.80 s installed path, I:E 1:1.2", {
            "lung.full_open_fill_time_s": 1.80, "ie_expiration": 1.2,
        }),
    ]
    rows = []
    for name, changes in cases:
        cfg = copy.deepcopy(base)
        cfg.volume_feedback_source = "sensor_integral"
        cfg.target_expiration_flow_slm = None
        for path, value in changes.items():
            parent, _, field = path.rpartition(".")
            setattr(getattr(cfg, parent) if parent else cfg, field, value)
        if cfg.input_delay.delay_s == 0.0:
            cfg.input_delay.enabled = False
        result = evaluate(
            cfg, 18.0, True, controller_lung=controller_lung,
            controller_input_delay_s=controller_delay_s,
            volume_feedback_source="sensor_integral",
        )
        rows.append({"case": name, "ie_expiration": cfg.ie_expiration,
                     "dead_time_s": cfg.dead_time_s,
                     "input_delay_s": cfg.input_delay.delay_s if cfg.input_delay.enabled else 0.0,
                     "assumed_full_open_reach_s": cfg.lung.full_open_fill_time_s,
                     **result})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).resolve().parent / "output" / "18bpm_tradeoff")
    args = parser.parse_args()
    rows = study_rows(args.config)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "18bpm_30_cycle_tradeoff.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    report_path = args.output_dir / "18bpm_tradeoff.md"
    with report_path.open("w", encoding="utf-8") as handle:
        handle.write("# 18 bpm single-lung simulation tradeoffs\n\n")
        handle.write("All rows are 30-cycle **simulations**, using a 250 mL capacity, "
                     "finite-fill plant, sensor-integrated flow feedback, volume deadline "
                     "guard, and a single controller model fixed to the saved nominal "
                     "parameters. A saved manual expiratory flow target is replaced by "
                     "a rate-derived target. The saved GUI config is not modified.\n\n")
        handle.write("| What-if change | Target / mL | Active Tin / s | Tidal range / mL | "
                     "Full-open peak from zero / mL | Inspiration full-open / % | "
                     "30-cycle ±5% pass |\n")
        handle.write("| --- | ---: | ---: | ---: | ---: | ---: | --- |\n")
        for row in rows:
            handle.write(
                f"| {row['case']} | {row['target_tidal_ml']:.0f} | "
                f"{row['active_inspiration_s']:.3f} | "
                f"{row['minimum_tidal_ml']:.1f}–{row['maximum_tidal_ml']:.1f} | "
                f"{row['ideal_full_open_peak_from_zero_ml']:.1f} | "
                f"{100 * row['mean_inspiration_saturation_fraction']:.0f} | "
                f"{'PASS' if row['tidal_requirement_pass'] and row['rate_requirement_pass'] else 'FAIL'} |\n"
            )
        handle.write("\nThe full-open peak is a model-only upper bound from an empty "
                     "lung; real tidal volume can be lower because expiration leaves "
                     "residual volume, the controller throttles, and the sensor estimate "
                     "lags. A case at 100% inspiration opening has no simulated "
                     "throttling margin. Changing I:E from 1:2 changes the original "
                     "nominal timing requirement; lowering the target changes the "
                     "250 mL requirement. A faster full-open reach time cannot be "
                      "claimed from the assumed 1.30 s atmosphere-path endpoint. "
                     "The assembled path, flow waveform, and delays are unmeasured.\n")
    print(f"Saved {report_path} and {csv_path}")
    for row in rows:
        print(f"{row['case']}: {row['minimum_tidal_ml']:.1f}–"
              f"{row['maximum_tidal_ml']:.1f} mL, "
              f"{'PASS' if row['tidal_requirement_pass'] and row['rate_requirement_pass'] else 'FAIL'}")


if __name__ == "__main__":
    main()
