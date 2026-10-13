"""Offline 250 mL stroke audit, with PPV grouping and boundary sensitivity."""
import csv
import json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
SOURCE = next(ROOT.parent.glob('*/flow_control_exp')) / 'data' / '流量积分校准.csv'
OUT = ROOT / 'output' / 'syringe_calibration'
BIAS = 0.003392689953186893
REFERENCE_ML = 250.0  # Assumes each full push/pull traverses 250 mL.


def strokes(t, q, volts, boundary=.04):
    result = []
    for sign in (1, -1):
        ix = np.flatnonzero(sign*q > .15)
        groups = np.split(ix, np.flatnonzero(np.diff(t[ix]) > .6)+1)
        for g in groups:
            if len(g) < 10:
                continue
            i, j = g[0], g[-1]
            while i > 0 and sign*q[i-1] > boundary:
                i -= 1
            while j+1 < len(q) and sign*q[j+1] > boundary:
                j += 1
            i, j = max(0, i-1), min(len(q)-1, j+1)
            v = float(sign*np.trapezoid(q[i:j+1], t[i:j+1])*1000/60)
            result.append(dict(direction='positive' if sign == 1 else 'negative',
                               start_s=float(t[i]), end_s=float(t[j]), ppv_v=float(np.median(volts[i:j+1])),
                               measured_ml=v, error_pct=100*(v/REFERENCE_ML-1),
                               apparent_gain=REFERENCE_ML/v))
    return sorted(result, key=lambda r: r['start_s'])


def summarize(rows):
    v = np.array([r['measured_ml'] for r in rows])
    return dict(n=len(v), mean_ml=float(v.mean()), min_ml=float(v.min()), max_ml=float(v.max()),
                sample_sd_ml=float(v.std(ddof=1)), error_pct=float(100*(v.mean()/REFERENCE_ML-1)),
                apparent_gain=float(REFERENCE_ML/v.mean()))


def main():
    OUT.mkdir(exist_ok=True, parents=True)
    with SOURCE.open(encoding='utf-8-sig', newline='') as f:
        raw = list(csv.reader(f))[1:]
    raw = [r for r in raw if r[1] and not r[11]]
    a = np.array([[float(r[i]) for i in (1, 2, 3, 4, 5)] for r in raw])
    assert np.all(a[:, 2] == 1) and np.all(a[:, 3] == 0)
    t = (a[:, 0]-a[0, 0])/1000
    q, volts = a[:, 1]-BIAS, a[:, 4]
    rows = strokes(t, q, volts)
    groups = {f'{voltage:g}V_{direction}': summarize([r for r in rows if r['ppv_v'] == voltage and r['direction'] == direction])
              for voltage in (0, 3.2) for direction in ('positive', 'negative')}
    direction_summary = {d: summarize([r for r in rows if r['direction'] == d]) for d in ('positive', 'negative')}
    voltage_summary = {str(v): summarize([r for r in rows if r['ppv_v'] == v]) for v in (0, 3.2)}
    total = summarize(rows)
    for r in rows:
        r['common_gain_corrected_ml'] = r['measured_ml']*total['apparent_gain']
    sensitivity = {str(b): [r['measured_ml'] for r in strokes(t, q, volts, b)] for b in (.02, .04, .08)}
    summary = dict(samples=len(t), duration_s=float(t[-1]), max_gap_s=float(np.diff(t).max()),
                   status=sorted({r[9] for r in raw}), faults=sorted({r[10] for r in raw}),
                   reference_ml=REFERENCE_ML, zero_bias_slm=BIAS, groups=groups,
                   by_direction=direction_summary, by_voltage=voltage_summary, pooled=total,
                   boundary_sensitivity_ml=sensitivity,
                   notes=['Assumes each stroke is a full 250 mL, without syringe bypass/leakage.',
                          'No VAC leakage subtraction: source stopped and VAC closed.',
                          'Apparent volume-matching gain; standard-vs-ambient volume conversion unavailable.',
                          'Only two repetitions per voltage and direction; no independent validation.',
                          'Applies to tested low flow strokes, not validated at high flow.'])
    with (OUT/'strokes.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    (OUT/'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    fig, axes = plt.subplots(2, 1, figsize=(12, 7))
    axes[0].plot(t, q, lw=.8)
    axes[0].axvline(t[np.flatnonzero(volts == 3.2)[0]], ls='--', color='black', label='PPV 0 -> 3.2 V')
    for n, r in enumerate(rows, 1):
        axes[0].axvspan(r['start_s'], r['end_s'], alpha=.12, color='tab:blue' if r['direction']=='positive' else 'tab:orange')
        axes[0].text((r['start_s']+r['end_s'])/2, 3.5, str(n), ha='center')
    axes[0].set(xlabel='MCU elapsed time (s)', ylabel='Zero-corrected flow (SLM)')
    axes[0].legend()
    n = np.arange(1, len(rows)+1)
    axes[1].bar(n-.18, [r['measured_ml'] for r in rows], width=.36, label='Zero corrected')
    axes[1].bar(n+.18, [r['common_gain_corrected_ml'] for r in rows], width=.36, label='Pooled gain, same-data fit')
    axes[1].axhline(250, color='black', ls='--', label='Nominal syringe stroke 250 mL')
    axes[1].set(ylim=(230, 255), xlabel='Stroke number', ylabel='Integrated volume (mL)', xticks=n)
    axes[1].legend()
    for ax in axes: ax.grid(alpha=.2)
    fig.tight_layout(); fig.savefig(OUT/'comparison.png', dpi=170); plt.close(fig)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
