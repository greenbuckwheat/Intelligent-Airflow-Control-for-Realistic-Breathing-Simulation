"""Reproduce descriptive statistics for the seven complete selected runs.

Telemetry integrals use 20 ms sampled, phase-gated net flow; they are estimates,
not independent volume measurements. Recovery/precondition are excluded.
"""
from pathlib import Path
import json
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'selected_runs_analysis'
OUT.mkdir(exist_ok=True)
NAMES = ['04', '05', '05_10bpm_IE21', '06', '07',
         'single_lung_hardware_run_774547421', 'single_lung_hardware_run_774946263']
records = []
fig, axs = plt.subplots(7, 2, figsize=(15, 20), constrained_layout=True)
for row, name in enumerate(NAMES):
    d = pd.read_csv(ROOT / (name + '.csv'))
    cfg = json.loads((ROOT / (name + '.json')).read_text())['configuration']
    dt = d.time_s.diff().median()
    for cycle, a in d[d.state_name == 'INSPIRATION'].groupby('cycle'):
        b = d[(d.cycle == cycle) & (d.state_name == 'EXPIRATION')]
        if b.empty:
            continue
        start = float(d.loc[a.index[0]-1, 'volume_ml']) if a.index[0] else float(a.volume_ml.iloc[0])
        rec = dict(run=name, cycle=int(cycle)+1, start_s=a.time_s.iloc[0],
                   inspiration_s=len(a)*dt, expiration_s=len(b)*dt,
                   baseline_ml=start, peak_ml=a.volume_ml.max(),
                   inspired_logged_ml=a.volume_ml.max()-start,
                   end_expiration_ml=b.volume_ml.iloc[-1],
                   inspired_integral_ml=a.net_flow_slm.clip(lower=0).sum()*dt*1000/60,
                   expired_integral_ml=-b.net_flow_slm.clip(upper=0).sum()*dt*1000/60,
                   inspiration_mean_slm=a.net_flow_slm.mean(),
                   expiration_mean_slm=b.net_flow_slm.mean(),
                   inspiration_rmse_slm=np.sqrt(np.mean((a.net_flow_slm-a.flow_reference_slm)**2)),
                   expiration_rmse_slm=np.sqrt(np.mean((b.net_flow_slm-b.flow_reference_slm)**2)),
                   inspiration_ref_slm=a.flow_reference_slm.median(),
                   expiration_ref_slm=b.flow_reference_slm.median())
        records.append(rec)
    active = d[d.state_name.isin(['INSPIRATION', 'EXPIRATION', 'DEAD_TO_INSP', 'DEAD_TO_EXP'])]
    title = name.replace('single_lung_hardware_run_', '')
    ax = axs[row, 0]
    ax.plot(active.time_s, active.volume_ml, lw=1.1)
    ax.axhline(250, color='gray', ls='--', lw=.8)
    ax.set(title=title+' | logged volume', ylabel='mL', xlabel='Time (s)')
    ax = axs[row, 1]
    ax.plot(active.time_s, active.net_flow_slm, lw=.7, label='Net flow')
    ax.plot(active.time_s, active.flow_reference_slm, lw=.8, label='Reference', alpha=.8)
    ax.set(title=title+' | flow', ylabel='SLM', xlabel='Time (s)')
    ax.legend(loc='upper right', fontsize=7)
    for ax in axs[row]:
        ax.grid(alpha=.2)
fig.savefig(OUT / 'waveforms.png', dpi=150)
plt.close(fig)
r = pd.DataFrame(records)
r.to_csv(OUT / 'per_cycle_metrics.csv', index=False)
summary = []
fig, axs = plt.subplots(1, 2, figsize=(12, 4.8), constrained_layout=True)
for name, g in r.groupby('run', sort=False):
    periods = np.diff(g.start_s)
    s = dict(run=name, cycles=len(g), inspired_mean_ml=g.inspired_logged_ml.mean(),
             inspired_first_ml=g.inspired_logged_ml.iloc[0], inspired_last_ml=g.inspired_logged_ml.iloc[-1],
             residual_first_ml=g.end_expiration_ml.iloc[0], residual_last_ml=g.end_expiration_ml.iloc[-1],
             actual_bpm=60/periods.mean(), inspiration_s=g.inspiration_s.mean(), expiration_s=g.expiration_s.mean(),
             inspiration_ref_slm=g.inspiration_ref_slm.mean(), inspiration_mean_slm=g.inspiration_mean_slm.mean(),
             expiration_ref_slm=g.expiration_ref_slm.mean(), expiration_mean_slm=g.expiration_mean_slm.mean())
    summary.append(s)
    label = name.replace('single_lung_hardware_run_', '')
    axs[0].plot(g.cycle, g.inspired_logged_ml, 'o-', ms=4, label=label)
    axs[1].plot(g.cycle, g.end_expiration_ml, 'o-', ms=4, label=label)
axs[0].axhline(250, color='gray', ls='--', label='Target')
axs[0].set(title='Inspired increment of logged volume', xlabel='Cycle', ylabel='mL')
axs[1].set(title='Logged end-expiration volume', xlabel='Cycle', ylabel='mL')
for ax in axs:
    ax.grid(alpha=.2)
    ax.legend(fontsize=7)
fig.savefig(OUT / 'cycle_comparison.png', dpi=170)
pd.DataFrame(summary).to_csv(OUT / 'run_summary.csv', index=False)
print(pd.DataFrame(summary).round(2).to_string(index=False))
print('\nPER CYCLE\n', r[['run','cycle','inspired_logged_ml','inspired_integral_ml','expired_integral_ml','end_expiration_ml']].round(1).to_string(index=False))
