"""Fit a first-order lung time constant from preliminary bench-test CSV data.

Accepted input:
  1. time column plus a volume column; or
  2. time column plus a flow column, which is integrated to volume.

The fitted model is V(t) = A + B exp(-t/tau).  For passive atmospheric
recovery A is the estimated relaxed capacity; for vacuum contraction A is the
residual volume.  No SciPy dependency is required.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np


def read_column(path: Path, name: str) -> np.ndarray:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or name not in rows[0]:
        available = list(rows[0]) if rows else []
        raise ValueError(f"Column {name!r} not found. Available: {available}")
    return np.asarray([float(row[name]) for row in rows], dtype=float)


def integrate_flow(time_s: np.ndarray, flow_slm: np.ndarray) -> np.ndarray:
    # 1 SLM = 1000/60 mL/s. Trapezoidal integration preserves signed flow.
    volume = np.zeros_like(time_s)
    dt = np.diff(time_s)
    volume[1:] = np.cumsum(0.5 * (flow_slm[1:] + flow_slm[:-1]) * dt * 1000.0 / 60.0)
    return volume


def fit_exponential(time_s: np.ndarray, volume_ml: np.ndarray):
    valid = np.isfinite(time_s) & np.isfinite(volume_ml)
    t = time_s[valid]
    y = volume_ml[valid]
    if t.size < 8:
        raise ValueError("At least 8 valid samples are required")
    order = np.argsort(t)
    t = t[order] - t[order][0]
    y = y[order]
    if np.any(np.diff(t) <= 0):
        unique = np.r_[True, np.diff(t) > 0]
        t, y = t[unique], y[unique]

    candidates = np.geomspace(max(np.median(np.diff(t)), 0.005), max(20.0, t[-1] * 5.0), 4000)
    best = None
    for tau in candidates:
        x = np.exp(-t / tau)
        design = np.column_stack((np.ones_like(x), x))
        coeff, *_ = np.linalg.lstsq(design, y, rcond=None)
        prediction = design @ coeff
        sse = float(np.sum((prediction - y) ** 2))
        if best is None or sse < best[0]:
            best = (sse, tau, coeff, prediction)
    assert best is not None
    sse, tau, coeff, prediction = best
    ss_total = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - sse / ss_total if ss_total > 0 else float("nan")
    return {
        "tau_s": float(tau),
        "asymptote_ml": float(coeff[0]),
        "initial_ml": float(coeff[0] + coeff[1]),
        "rmse_ml": float(np.sqrt(sse / y.size)),
        "r_squared": r2,
        "time_s": t,
        "measured_ml": y,
        "fitted_ml": prediction,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--time-column", default="time_s")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--volume-column")
    source.add_argument("--flow-column")
    parser.add_argument("--flow-sign", type=float, default=1.0)
    parser.add_argument("--output", type=Path, default=Path("output/fitted_parameters.json"))
    args = parser.parse_args()

    time_s = read_column(args.csv_file, args.time_column)
    if args.volume_column:
        volume_ml = read_column(args.csv_file, args.volume_column)
    else:
        flow = args.flow_sign * read_column(args.csv_file, args.flow_column)
        volume_ml = integrate_flow(time_s, flow)
    fit = fit_exponential(time_s, volume_ml)
    serializable = {key: value for key, value in fit.items() if not isinstance(value, np.ndarray)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(serializable, handle, indent=2)
        handle.write("\n")
    print(json.dumps(serializable, indent=2))
    print("Use tau_s as tau_in_s for atmospheric recovery or tau_out_s for vacuum contraction.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
