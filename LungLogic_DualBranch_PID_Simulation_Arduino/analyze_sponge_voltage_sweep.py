"""2026-10-09 sponge PPV sweep; source CSVs remain raw and unchanged."""
import csv
import hashlib
import json
from pathlib import Path
import numpy as np
from scipy.optimize import least_squares
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
BASE = next(ROOT.parent.glob('*/flow_control_exp')) / 'data'
OUT = ROOT / 'output' / 'sponge_voltage_sweep_2026-10-09'
NAMES = ['0', '2', '3.2', '3.5', '3.6', '3.7', '3.8', '3.9', '4.0', '4.05', '4.1', '4.15', '4.18', '4.2']
GAIN, OFFSET, LEAK = 1.0471, .0034, .931


def fit_segment(x, y, allow_delay=False, residual=True):
    v = np.r_[0, np.cumsum((y[1:]+y[:-1])*.5*np.diff(x)*1000/60)]
    # Residual term is diagnostic remaining through-flow, NOT a zero correction.
    def model(p):
        delay = p[3] if allow_delay else 0
        return p[0]*(-np.expm1(-np.maximum(x-delay, 0)/p[1])) + p[2]*x*1000/60
    tail = max(0, float(np.median(y[x >= x[-1]-min(1, x[-1]/3)])))
    init = [max(50, min(4000, v[-1]-tail*x[-1]*1000/60)), 2, min(tail, 5)]
    lo, hi = [1, .03, 0], [5000, 200, 10]
    if not residual:
        init[2] = 0
        # Fit reduced parameter vector to avoid near-equal bounds.
        def objective(p): return model([p[0], p[1], 0])-v
        f = least_squares(objective, init[:2], bounds=(lo[:2], hi[:2]), loss='soft_l1', f_scale=5)
        p = np.r_[f.x, 0]
    else:
        if allow_delay:
            init += [.02]; lo += [0]; hi += [2]
        f = least_squares(lambda p: model(p)-v, init, bounds=(lo, hi), loss='soft_l1', f_scale=5)
        p = f.x
    pred = model(p)
    return dict(amplitude_ml=float(p[0]), tau_s=float(p[1]), residual_slm=float(p[2]),
                apparent_delay_s=float(p[3]) if allow_delay else 0,
                r2=float(1-np.sum((v-pred)**2)/max(np.sum((v-v.mean())**2), 1)),
                rmse_ml=float(np.sqrt(np.mean((v-pred)**2)))), v, pred


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows, files = [], []
    for name in NAMES:
        path = BASE / (name+'.csv')
        with path.open(encoding='utf-8-sig', newline='') as f:
            raw = list(csv.reader(f))[1:]
        rr = [r for r in raw if r[1] and not r[11]]
        a = np.array([[float(r[i]) for i in (1, 2, 3, 4, 5)] for r in rr])
        t = (a[:, 0]-a[0, 0])/1000
        q = GAIN*(a[:, 1]-OFFSET)
        state = a[:, 2]+2*a[:, 3]
        edges = np.r_[0, np.flatnonzero(np.any(np.diff(a[:, 2:], axis=0), axis=1))+1, len(a)]
        files.append(dict(file=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                          date=raw[0][0], samples=len(a), max_gap_s=float(np.diff(t).max()),
                          status=sorted({r[9] for r in rr}), faults=sorted({r[10] for r in rr})))
        fig, axes = plt.subplots(3, 1, figsize=(12, 8), sharex=True)
        axes[0].plot(t, q, lw=.8); axes[0].set_ylabel('Calibrated SLM')
        axes[1].step(t, a[:, 4], where='post'); axes[1].set_ylabel('Command V')
        axes[2].step(t, state, where='post'); axes[2].set(ylabel='0 closed / 1 ATM / 2 VAC', xlabel='MCU elapsed s')
        for k, (i, j) in enumerate(zip(edges[:-1], edges[1:])):
            if state[i] not in (1, 2) or j-i < 20:
                continue
            direction = 'ATM' if state[i] == 1 else 'VAC'
            x = t[i:j]-t[i]
            y = q[i:j] if direction == 'ATM' else np.maximum(0, -q[i:j]-LEAK)
            # User confirmed PPV preset before each breath: no actuator-delay exclusion.
            # Mid-breath voltage changes still are not independent fixed-voltage breaths.
            switched_active = i == 0 or (state[i-1] == state[i] and a[i-1, 4] != a[i, 4])
            diagnostic, v, _ = fit_segment(x, y)
            delayed, _, _ = fit_segment(x, y, True)
            fixed, _, _ = fit_segment(x, y, residual=False)
            fit = fixed
            # Empirical selection, applied identically across files/directions.
            reasons = []
            if switched_active: reasons.append('record starts active or voltage changed during active phase')
            if name == '4.2' and a[i, 4] == 0: reasons.append('0 V recovery in mixed-voltage file; not baseline identification')
            if fit['amplitude_ml'] < 500: reasons.append('small excursion/uncertain initial state')
            if x[-1] < 3*fit['tau_s']: reasons.append('insufficient settling coverage')
            if fit['r2'] < .97: reasons.append('poor cumulative fit')
            if float(a[i, 4]) >= 4.3: reasons.append('closure check only')
            row = dict(file=path.name, segment=k, direction=direction, voltage=float(a[i, 4]),
                       start_s=float(t[i]), duration_s=float(x[-1]),
                       raw_tail_slm=float(np.median(q[i:j][x>=x[-1]-min(1, x[-1]/3)])),
                       net_integral_ml=float(v[-1]), **fit,
                       tau_with_delay_s=delayed['tau_s'], shape_delay_s=delayed['apparent_delay_s'],
                       tau_fixed_leak_only_s=fixed['tau_s'],
                       amplitude_fixed_leak_only_ml=fixed['amplitude_ml'],
                       diagnostic_extra_residual_slm=diagnostic['residual_slm'],
                       diagnostic_tau_extra_residual_s=diagnostic['tau_s'],
                       usable=not reasons, reason='; '.join(reasons) if reasons else 'accepted')
            rows.append(row)
            axes[0].text(t[i], float(q[i:j].max()), str(k), fontsize=7)
        for ax in axes: ax.grid(alpha=.25)
        fig.suptitle(path.name); fig.tight_layout(); fig.savefig(OUT/(name+'_trace.png'), dpi=140); plt.close(fig)
    with (OUT/'segments.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    summary = []
    for voltage in sorted({r['voltage'] for r in rows}):
        for direction in ('ATM', 'VAC'):
            allrows = [r for r in rows if r['voltage']==voltage and r['direction']==direction]
            good = [r for r in allrows if r['usable']]
            if not allrows: continue
            z = dict(voltage=voltage, direction=direction, n=len(good), total=len(allrows))
            for key in ('tau_s', 'amplitude_ml', 'residual_slm', 'tau_with_delay_s', 'tau_fixed_leak_only_s', 'amplitude_fixed_leak_only_ml'):
                if good: z[key] = float(np.median([r[key] for r in good]))
            summary.append(z)
    models = {}
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    for ax, direction in zip(axes, ('ATM', 'VAC')):
        good = [r for r in summary if r['direction']==direction and r['n']]
        u = np.array([r['voltage'] for r in good])
        tau = np.array([r['tau_fixed_leak_only_s'] for r in good])
        tau0 = float(tau[0])
        sensitivity = []
        for closure in (4.25, 4.3, 4.35):
            def residual(p):
                opening = np.clip((closure-u)/(closure-p[0]), 1e-6, 1)
                return np.log(tau0/opening**p[1])-np.log(tau)
            f = least_squares(residual, [3.6 if direction=='ATM' else 4, 1],
                              bounds=([0, .05], [closure-.001, 5]), loss='soft_l1', f_scale=.1)
            sensitivity.append(dict(closure_v=closure, dead_zone_v=float(f.x[0]), gamma=float(f.x[1]),
                                    log_rmse=float(np.sqrt(np.mean(residual(f.x)**2)))))
        model = dict(tau0_s=tau0, **sensitivity[1], closure_sensitivity=sensitivity)
        models[direction] = model
        xx = np.linspace(0, 4.29, 400)
        yy = tau0/np.clip((4.3-xx)/(4.3-model['dead_zone_v']), 1e-6, 1)**model['gamma']
        ax.scatter(u, tau, label='Accepted voltage medians')
        # Descriptive excluded fits are visible, but not used to identify gamma.
        bad = [r for r in rows if r['direction']==direction and not r['usable'] and r['voltage']>=4.18]
        ax.scatter([r['voltage'] for r in bad], [r['tau_fixed_leak_only_s'] for r in bad], marker='x', color='gray', label='Excluded/truncated fits')
        ax.plot(xx, yy, label='Adopted power-law model')
        ax.set(title=direction, xlabel='PPV command V', ylabel='Effective tau (s)', xlim=(3.2, 4.31), ylim=(0, 20))
        ax.grid(alpha=.25); ax.legend(fontsize=8)
    fig.tight_layout(); fig.savefig(OUT/'model_fit.png', dpi=170); plt.close(fig)
    amplitude = next(r['amplitude_fixed_leak_only_ml'] for r in summary if r['voltage']==0 and r['direction']=='ATM')
    parameters = dict(tau_in_s=round(models['ATM']['tau0_s'], 3), tau_out_s=round(models['VAC']['tau0_s'], 3),
                      hardware_model_volume_ml=round(amplitude, 1),
                      hardware_gamma_in=round(models['ATM']['gamma'], 4), hardware_gamma_out=round(models['VAC']['gamma'], 4),
                      hardware_dead_zone_in_v=round(models['ATM']['dead_zone_v'], 4),
                      hardware_dead_zone_out_v=round(models['VAC']['dead_zone_v'], 4), hardware_closure_v=4.3)
    mapping = []
    for u in sorted({float(v) for v in NAMES} | {4.3}):
        r = dict(voltage=u)
        for d in ('ATM', 'VAC'):
            m = models[d]; a = float(np.clip((4.3-u)/(4.3-m['dead_zone_v']), 0, 1)); k = a**m['gamma']
            obs = next((s for s in summary if s['voltage']==u and s['direction']==d), {})
            r.update({d+'_a': a, d+'_relative_conductance': k, d+'_model_tau_s': m['tau0_s']/k if k else None,
                      d+'_accepted_n': obs.get('n', 0), d+'_observed_tau_s': obs.get('tau_fixed_leak_only_s')})
        mapping.append(r)
    for name, data in [('voltage_mapping', mapping), ('voltage_summary', summary)]:
        keys = list(dict.fromkeys(k for r in data for k in r))
        with (OUT/(name+'.csv')).open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(data)
    (OUT/'analysis.json').write_text(json.dumps(dict(files=files, summary=summary, models=models, parameters=parameters,
        notes=['No-delay first-order fit for deployment; fixed .931 SLM VAC compensation retained.',
               'Additional fitted residual and shape delay are sensitivity diagnostics, not deployed parameters.',
               'User-selected model assumption: 4.3 V is the zero-conductance endpoint for both directions; raw residual observations are retained.',
               'Model volume is empirical amplitude, not geometric capacity or volume safety limit.',
               'Initial state/pressure unmeasured; coverage and excursion screening do not prove repeatable initial states.']), indent=2), encoding='utf-8')
    print(json.dumps(parameters, indent=2))


if __name__ == '__main__': main()
