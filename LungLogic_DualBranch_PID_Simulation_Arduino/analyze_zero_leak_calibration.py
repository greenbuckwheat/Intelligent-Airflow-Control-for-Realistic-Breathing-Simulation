"""Offline calibration audit; does not change controller settings."""
import csv
import json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
BASE = next(ROOT.parent.glob('*/flow_control_exp')) / 'data'
OUT = ROOT / 'output' / 'zero_leak_calibration_2026-10-09'


def read(name):
    with (BASE / name).open(encoding='utf-8-sig', newline='') as f:
        raw = list(csv.reader(f))[1:]
    rows = [r for r in raw if r[1] and not r[11]]
    a = np.array([[float(r[i]) for i in (1, 2, 3, 4, 5)] for r in rows])
    a[:, 0] = (a[:, 0] - a[0, 0]) / 1000
    edges = np.r_[0, np.flatnonzero(np.any(np.diff(a[:, 2:], axis=0) != 0, axis=1)) + 1, len(a)]
    meta = dict(samples=len(a), duration_s=float(a[-1, 0]),
                max_sample_gap_s=float(np.diff(a[:, 0]).max()),
                status=sorted({r[9] for r in rows}), faults=sorted({r[10] for r in rows}))
    return a, [(i, j) for i, j in zip(edges[:-1], edges[1:])], meta


def stats(q):
    return dict(n=len(q), mean_slm=float(np.mean(q)), median_slm=float(np.median(q)),
                std_slm=float(np.std(q)), min_slm=float(np.min(q)), max_slm=float(np.max(q)),
                p05_slm=float(np.quantile(q, .05)), p95_slm=float(np.quantile(q, .95)))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    zero, zs, zm = read('传感器零偏校准.csv')
    leak, ls, lm = read('整机漏气实验.csv')
    stable = np.zeros(len(zero), dtype=bool)
    segments = []
    for k, (i, j) in enumerate(zs):
        x = zero[i:j, 0] - zero[i, 0]
        keep = x >= 2  # Calibration exclusion only, NOT controller blanking.
        stable[i:j] = keep
        q = zero[i:j, 1]
        early = x < 2
        segments.append(dict(segment=k, start_s=float(zero[i, 0]), duration_s=float(x[-1]),
                             atm=int(zero[i, 2]), vac=int(zero[i, 3]), ppv_v=float(zero[i, 4]),
                             transient_min_slm=float(q[early].min()),
                             transient_max_slm=float(q[early].max()), **stats(q[keep])))
    baseline = stats(zero[stable, 1])
    b = baseline['mean_slm']
    grouped = []
    for state in np.unique(zero[:, 2:], axis=0):
        keep = stable & np.all(zero[:, 2:] == state, axis=1)
        grouped.append(dict(atm=int(state[0]), vac=int(state[1]), ppv_v=float(state[2]), **stats(zero[keep, 1])))
    sensitivity = {}
    for skip in (.5, 1, 2, 3):
        q = np.concatenate([zero[i:j, 1][zero[i:j, 0]-zero[i, 0] >= skip] for i, j in zs])
        sensitivity[str(skip)] = stats(q)
    vac_i, vac_j = max(((i, j) for i, j in ls if leak[i, 3] == 1), key=lambda ij: ij[1]-ij[0])
    close_i, close_j = next((i, j) for i, j in ls if i == vac_j)
    x = leak[vac_i:vac_j, 0] - leak[vac_i, 0]
    qc = leak[vac_i:vac_j, 1] - b
    plateau = stats(-qc[x >= 20])
    windows = []
    for label, i, j in [('VAC', vac_i, vac_j), ('CLOSED', close_i, close_j)]:
        xx = leak[i:j, 0]-leak[i, 0]
        yy = leak[i:j, 1]-b
        for lo in np.arange(0, xx[-1], 10):
            mask = (xx >= lo) & (xx < lo+10)
            windows.append(dict(phase=label, from_s=float(lo), to_s=float(min(lo+10, xx[-1])), **stats(yy[mask])))
    cx = leak[close_i:close_j, 0]-leak[close_i, 0]
    cq = leak[close_i:close_j, 1]-b
    closed = dict(after_10s=stats(cq[cx >= 10]),
                  net_integral_ml=float(np.trapezoid(cq, cx)*1000/60),
                  positive_integral_ml=float(np.trapezoid(np.maximum(cq, 0), cx)*1000/60),
                  negative_integral_ml=float(np.trapezoid(np.minimum(cq, 0), cx)*1000/60))
    result = dict(zero_file=zm, leakage_file=lm, zero_stable=baseline,
                  zero_by_state=grouped, zero_exclusion_sensitivity=sensitivity,
                  vac_plateau_after_20s=plateau, closed=closed,
                  candidate_zero_offset_slm=b, candidate_gain_in=1, candidate_gain_out=1,
                  notes=['User confirmed zero-test steady states are equal-pressure/no-flow.',
                         'Gains are assumed 1, not calibrated. PPV 0 V only for leakage.',
                         'Source setting 40 kPa reported by user; no lung pressure measurement.',
                         'VAC plateau is not a validated transient leakage correction.',
                         'Closed flow near telemetry quantization cannot prove leak-free behavior.',
                         'Two-second exclusion is for calibration statistics, not live control.'])
    for name, rows in [('zero_segments', segments), ('zero_by_state', grouped), ('leak_windows', windows)]:
        with (OUT / (name+'.csv')).open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    (OUT / 'calibration_summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    fig, axes = plt.subplots(3, 1, figsize=(11, 9))
    axes[0].plot(zero[:, 0], zero[:, 1], lw=.7)
    axes[0].set(title='Zero-flow calibration: switching transients', ylabel='Raw flow (SLM)', xlabel='Record time (s)')
    axes[1].plot(x, -qc, lw=.8)
    axes[1].axhline(plateau['mean_slm'], color='tab:red', ls='--', label=f"20 s onward mean = {plateau['mean_slm']:.3f} SLM")
    axes[1].set(ylim=(0, 3), title='Long VAC phase (initial large flow outside plot range)', ylabel='Extraction magnitude (SLM)', xlabel='Time since VAC ON (s)')
    axes[1].legend()
    axes[2].plot(cx, cq, lw=.8)
    axes[2].axhline(0, color='black', lw=.5)
    axes[2].set(ylim=(-.08, .06), title='Both valves closed after VAC (initial transient outside plot range)', ylabel='Zero-corrected flow (SLM)', xlabel='Time since VAC OFF (s)')
    for ax in axes: ax.grid(alpha=.3)
    fig.tight_layout(); fig.savefig(OUT / 'calibration.png', dpi=170); plt.close(fig)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
