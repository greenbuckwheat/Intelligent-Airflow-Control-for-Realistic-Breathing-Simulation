"""Compare new full-open sponge trial with screened pre-sponge 0 V trials."""
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import least_squares

ROOT = Path(__file__).resolve().parent
BASE = next(ROOT.parent.glob('*/flow_control_exp'))
OUT = ROOT / 'output' / 'sponge_comparison'


def read_samples(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f))[1:]
    rows = [r for r in rows if not r[11] and r[1]]
    a = np.array([[float(r[i]) for i in (1, 2, 3, 4, 5)] for r in rows])
    t = a[:, 0] / 1000
    t -= t[0]
    state = a[:, 2] + 2 * a[:, 3]
    edges = np.r_[0, np.flatnonzero(np.diff(state)) + 1, len(t)]
    baseline = np.median(a[:min(100, edges[1]), 1])
    return t, a[:, 1] - baseline, state, edges


def metrics(t, q, direction):
    x = t - t[0]
    y = q * direction
    v = np.r_[0, np.cumsum((y[1:] + y[:-1]) / 2 * np.diff(x) * 1000 / 60)]
    def fit(xx, vv, allow_delay=True):
        def model(p):
            delay = p[2] if allow_delay else 0
            return p[0] * (1 - np.exp(-np.maximum(xx - delay, 0) / p[1]))
        initial = [max(vv[-1], 1), 2] + ([.02] if allow_delay else [])
        lower = [1, .03] + ([0] if allow_delay else [])
        upper = [10000, 100] + ([2] if allow_delay else [])
        f = least_squares(lambda p: model(p) - vv, initial,
                          bounds=(lower, upper),
                          loss='soft_l1', f_scale=5)
        return f.x, model(f.x)
    p, pred = fit(x, v)
    p0, _ = fit(x, v, False)
    keep = x >= .1
    xx = x[keep] - x[keep][0]
    vv = v[keep] - v[keep][0]
    ps, _ = fit(xx, vv)
    out = dict(duration_s=float(x[-1]), volume_ml=float(v[-1]), amplitude_ml=float(p[0]),
               tau_s=float(p[1]), apparent_delay_s=float(p[2]), tau_no_delay_s=float(p0[1]),
               tau_skip_first_0p1_s=float(ps[1]),
               cumulative_r2=float(1 - np.sum((v - pred)**2) / np.sum((v - v.mean())**2)),
               tail_last_0p5_slm=float(np.median(y[x >= x[-1] - .5])),
               peak_slm=float(y.max()))
    for fraction in (.5, .9, .95):
        hits = np.flatnonzero(v >= fraction * v[-1])
        i = int(hits[0])
        out[f't{int(fraction*100)}_s'] = float(np.interp(fraction*v[-1], v[i-1:i+1], x[i-1:i+1]))
    return out, (x, y, v, pred)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    t, q, state, edges = read_samples(BASE / 'data/new.csv')
    new = []
    curves = {}
    for a, b in zip(edges[:-1], edges[1:]):
        if state[a] not in (1, 2):
            continue
        label = 'ATM' if state[a] == 1 else 'VAC'
        row, curve = metrics(t[a:b], q[a:b], 1 if label == 'ATM' else -1)
        new.append(dict(group='sponge', file='new.csv', direction=label, **row))
        curves[label] = curve
    with (BASE / 'analysis/today_hardware/screened_segments.csv').open(encoding='utf-8-sig') as f:
        selected = [r for r in csv.DictReader(f) if float(r['voltage']) == 0 and r['usable'] == 'True']
    old = []
    fig, axes = plt.subplots(2, 2, figsize=(11, 7))
    for row in selected:
        t, q, state, _ = read_samples(BASE / 'data' / row['file'])
        keep = (t >= float(row['start_s']) - 1e-7) & (t <= float(row['end_s']) + 1e-7)
        label = 'ATM' if float(row['state']) == 1 else 'VAC'
        m, (x, y, v, _) = metrics(t[keep], q[keep], 1 if label == 'ATM' else -1)
        old.append(dict(group='before', file=row['file'], direction=label, **m))
        col = 0 if label == 'ATM' else 1
        axes[0, col].plot(x, y, color='gray', alpha=.3)
        axes[1, col].plot(x, v / v[-1], color='gray', alpha=.3)
    summaries = {}
    for col, label in enumerate(('ATM', 'VAC')):
        x, y, v, pred = curves[label]
        axes[0, col].plot(x, y, color='tab:blue', label='Sponge: one trial')
        axes[1, col].plot(x, v/v[-1], color='tab:blue', label='Measured integral')
        axes[1, col].plot(x, pred/v[-1], '--', color='tab:orange', label='First-order fit')
        axes[0, col].set_title(f'{label}, PPV 0 V; gray = screened prior trials')
        axes[0, col].set_ylabel('Directional flow (SLM)')
        axes[1, col].set_ylabel('Fraction of recorded segment integral')
        axes[1, col].set_xlabel('Time since valve state change (s)')
        keys = ('tau_s', 't50_s', 't90_s', 't95_s', 'volume_ml', 'tail_last_0p5_slm')
        subset = [r for r in old if r['direction'] == label]
        summaries[label] = {'n': len(subset), **{k: {'median': float(np.median([r[k] for r in subset])),
                                                 'min': float(min(r[k] for r in subset)),
                                                 'max': float(max(r[k] for r in subset))} for k in keys}}
    for ax in axes.flat:
        ax.grid(alpha=.25)
        ax.legend(loc='best')
    fig.tight_layout()
    fig.savefig(OUT / 'comparison.png', dpi=170)
    plt.close(fig)
    with (OUT / 'segments.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(new[0]))
        writer.writeheader()
        writer.writerows(new + old)
    result = dict(new=new, before_full_open=summaries,
                  notes=['Only one new cycle; pressures unrecorded; gamma unidentifiable at opening=1.',
                         't90 is relative to recorded integral, not proven final lung volume.',
                         'Apparent fitted delay is curve shape, not measured PPV latency.',
                         'Old trials use existing screening; no leakage subtraction beyond initial baseline.'])
    (OUT / 'comparison.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
