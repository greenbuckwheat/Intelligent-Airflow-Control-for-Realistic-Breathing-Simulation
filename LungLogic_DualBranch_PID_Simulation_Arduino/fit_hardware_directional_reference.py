"""Fit the October 12 replacement hardware, without mixing October 9 plants.

Writes audit artifacts only; deployment of fitted parameters is a separate edit.
The fitted low-voltage plateau is an extrapolation, not a new 0 V measurement.
"""
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd
from scipy.optimize import least_squares
from analyze_sponge_voltage_sweep import fit_segment

ROOT = Path(__file__).resolve().parent
SOURCE = next(ROOT.parent.glob('*/flow_control_exp/data'))
OUT = ROOT / 'output' / 'directional_reference_2026-10-12'
NAMES = ['3.8', '3.85', '3.9', '3.95', '4.0', '4.05', '4.1', '4.15', '4.2', '4.25', '4.3', '4.35', '4.5']


def main():
    OUT.mkdir(exist_ok=True)
    rows, sources = [], []
    for name in NAMES:
        path = SOURCE / (name + '.csv')
        raw = pd.read_csv(path)
        d = raw[raw.iloc[:, 1].notna() & raw.iloc[:, 11].isna()]
        if not d.iloc[:, 0].astype(str).str.startswith('2026-10-12').all():
            raise ValueError(f'{path}: wrong hardware/date batch')
        if not (d.iloc[:, 9].eq('OK').all() and d.iloc[:, 10].eq('NONE').all()):
            raise ValueError(f'{path}: sensor status or fault')
        sources.append(dict(file=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        a = d.iloc[:, [1, 2, 3, 4, 5]].to_numpy(float)
        t = (a[:, 0] - a[0, 0]) / 1000
        q = 1.0471 * (a[:, 1] - .0034)
        state = a[:, 2] + 2*a[:, 3]
        edges = np.r_[0, np.flatnonzero(np.any(np.diff(a[:, 2:], axis=0), axis=1))+1, len(a)]
        for i, j in zip(edges[:-1], edges[1:]):
            if state[i] not in (1, 2) or j-i < 20:
                continue
            u = a[i, 4]
            x = t[i:j]-t[i]
            direction = 'ATM' if state[i] == 1 else 'VAC'
            # Current firmware leakage convention; clamp only VAC extraction.
            leak = .814*np.clip((4.35-u)/.05, 0, 1)
            y = q[i:j] if direction == 'ATM' else np.maximum(0, -q[i:j]-leak)
            fit, volume, _ = fit_segment(x, y, residual=False)
            reasons = []
            if i == 0 or state[i-1] == state[i]: reasons.append('unknown onset / mid-phase voltage change')
            if not 3.8 <= u <= 4.3: reasons.append('recovery or closure check')
            if volume[-1] < 200: reasons.append('small excursion')
            if x[-1] < 3*fit['tau_s']: reasons.append('less than three fitted time constants')
            if fit['r2'] < .95: reasons.append('cumulative R2 below 0.95')
            if np.median(y[x >= x[-1]-min(1, x[-1]/3)]) > .5: reasons.append('tail not settled')
            rows.append(dict(file=path.name, direction=direction, voltage=u, duration_s=x[-1],
                             integral_ml=volume[-1], **fit, accepted=not reasons, reason='; '.join(reasons)))
    segments = pd.DataFrame(rows)
    segments.to_csv(OUT/'segments.csv', index=False)
    good = segments[segments.accepted]
    med = good.groupby(['direction', 'voltage']).agg(tau_s=('tau_s', 'median'), amplitude_ml=('amplitude_ml', 'median'), n=('tau_s', 'size')).reset_index()
    parameters = {}
    for direction, g in med.groupby('direction'):
        u, tau = g.voltage.to_numpy(), g.tau_s.to_numpy()
        def residual(p):
            opening = np.clip((4.35-u)/(4.35-p[1]), 1e-8, 1)
            return np.log(p[0]/opening**p[2])-np.log(tau)
        fit = least_squares(residual, [tau[0], 4, 1.5], bounds=([.02, 3, .05], [3, 4.3, 5]))
        parameters[direction] = dict(tau0_s=round(float(fit.x[0]), 4), plateau_end_v=round(float(fit.x[1]), 4),
                                     gamma=round(float(fit.x[2]), 4), max_relative_tau_error=float(np.max(np.abs(np.exp(residual(fit.x))-1))))
        med.loc[g.index, 'predicted_tau_s'] = tau*np.exp(residual(fit.x))
    # Common state amplitude comes from ATM recovery, equally weighted by voltage.
    amplitude = round(float(med[(med.direction=='ATM') & (med.voltage<=4.2)].amplitude_ml.median()), 1)
    result = dict(sources=sources, parameters=parameters, model_amplitude_ml=amplitude, closure_v=4.35,
                  notes=['Replacement hardware batch only; October 9 is not pooled.',
                         '4.5.csv is confirmed 4.05 V, both routing valves closed; no response fit.',
                         'No-delay cumulative exponential fits, R2 >= .95, >=3 tau, >=200 mL, tail <=.5 SLM.',
                         'Each voltage median has equal weight in log-tau power-law fit.',
                         'Plateau tau is extrapolated below the measured 3.8 V boundary.',
                         'Amplitude is exchanged standard-flow volume, not geometric capacity.',
                         'Single shared closure retained: insufficient evidence for separate closure values.',
                         'This is a descriptive initial controller model, not closed-loop validation.'])
    med.to_csv(OUT/'voltage_summary.csv', index=False)
    (OUT/'fit.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='sources'}, indent=2))


if __name__ == '__main__':
    main()
