"""Reproducible high-voltage analysis; this script never edits runtime settings."""
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd
from scipy.optimize import least_squares
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'analysis/high_voltage_2026-10-14'
NAMES = ('4.259_1 4.3_1 4.255 4.254 4.253 4.252_2 4.25_2 4.245 4.24 4.23 '
         'ppv_sweep_3p8_to_4p5_20261014_161024_1 '
         'ppv_sweep_3p8_to_4p5_20261014_161024').split()


def fit(t, flow, baseline):
    # Do not clip signed samples: doing so biases near-zero volume upwards.
    volume = np.r_[0, np.cumsum((flow[1:] + flow[:-1] - 2*baseline) / 2 * np.diff(t) / .06)]
    def predict(p):
        return p[0] * (-np.expm1(-t/p[1]))
    result = least_squares(lambda p: predict(p)-volume,
                           [max(1, min(1000, volume[-1])), 2],
                           bounds=([.1, .02], [1500, 200]), loss='soft_l1', f_scale=3)
    pred = predict(result.x)
    r2 = 1 - np.sum((pred-volume)**2)/max(1, np.sum((volume-volume.mean())**2))
    return result.x, float(r2), volume, pred


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    sources, segments = [], []
    for name in NAMES:
        path = ROOT/'data'/(name+'.csv')
        raw = pd.read_csv(path)
        assert len(raw.columns) == 21, path
        d = raw[raw.iloc[:,13].eq('telemetry')].copy()
        valid = d.iloc[:,9].eq('OK') & d.iloc[:,10].eq('NONE') & d.iloc[:,14].notna()
        sources.append(dict(file=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                            telemetry_rows=len(d), invalid_rows=int((~valid).sum()),
                            duplicates=int(d[valid].duplicated(d.columns[14]).sum())))
        d = d[valid].drop_duplicates(d.columns[14])
        a = d.iloc[:,[14,16,17,18,19,20]].to_numpy(float)
        assert np.isfinite(a).all() and (np.diff(a[:,0])>0).all(), path
        state = a[:,3] + 2*a[:,4]
        cuts = np.r_[0, np.flatnonzero((np.diff(state)!=0) | (np.diff(a[:,5])!=0)
                                      | (np.diff(a[:,2])!=0))+1, len(a)]
        sources[-1]['actual_voltages_v'] = np.unique(a[:,5]).tolist()
        for number, (i,j) in enumerate(zip(cuts[:-1],cuts[1:])):
            if state[i] not in (1,2) or j-i<20:
                continue
            direction = 'ATM' if state[i]==1 else 'VAC'
            t = (a[i:j,0]-a[i,0])/1000
            q = (a[i:j,1]-a[i:j,2]) * (1 if direction=='ATM' else -1)
            tail = q[t>=t[-1]-min(1,t[-1]/3)]
            segments.append(dict(file=path.name, segment=number, direction=direction,
                voltage=float(a[i,5]), pwm_code=int(np.floor(a[i,5]/5*4095+.5)),
                start_sample_ms=float(a[i,0]), duration_s=float(t[-1]), samples=j-i,
                observed_onset=bool(i>0 and state[i-1]!=state[i]),
                observed_end=bool(j<len(a)), max_gap_ms=float(np.diff(t).max()*1000),
                tail_sensor_slm=float(np.median(tail)), peak_sensor_slm=float(q.max()),
                tail_slope_slm_per_s=float(np.polyfit(t[t>=t[-1]-min(1,t[-1]/3)],tail,1)[0]),
                t=t, q=q))
    # The old free A/tau/b fit absorbed the late VAC plateau into a long transient.
    # Estimate its baseline from visibly settled long traces, then fix it in fits.
    tails = [s['tail_sensor_slm'] for s in segments if s['direction']=='VAC'
             and s['voltage']<4.26 and s['duration_s']>=6
             and abs(s['tail_slope_slm_per_s'])<.1]
    baseline = float(np.median(tails))
    records, curves = [], []
    for s in segments:
        row = {k:v for k,v in s.items() if k not in ('t','q')}
        reasons=[]
        if not s['observed_onset']: reasons.append('onset missing or voltage changed within phase')
        if not s['observed_end']: reasons.append('record ends within phase')
        if s['max_gap_ms']>100: reasons.append('sample gap >100ms')
        if s['voltage']>=4.3:
            row.update(classification='near_closed_observation', accepted=False,
                       reason='near-zero response cannot identify finite tau')
        else:
            b = baseline if s['direction']=='VAC' else 0
            p,r2,volume,pred = fit(s['t'],s['q'],b)
            low_tau = high_tau = float(p[1])
            if s['direction']=='VAC':
                sensitivity=[fit(s['t'],s['q'],v)[0][1] for v in (baseline-.1,baseline+.1)]
                low_tau,high_tau=float(min(sensitivity)),float(max(sensitivity))
            if p[0]<200: reasons.append('small exchange / incomplete initial state')
            if s['duration_s']<3*p[1]: reasons.append('less than 3 tau observed')
            if r2<.95: reasons.append('cumulative transient R2 <0.95')
            row.update(classification='transient_fit', accepted=not reasons, reason='; '.join(reasons),
                       amplitude_sensor_ml=float(p[0]), amplitude_prior_gain_ml=float(p[0]*1.0471),
                       tau_s=float(p[1]), baseline_sensor_slm=b, dynamic_r2=r2,
                       baseline_sensitivity_tau_min_s=low_tau, baseline_sensitivity_tau_max_s=high_tau)
            curves.append((row,s['t'],volume,pred))
        records.append(row)
    df=pd.DataFrame(records)
    df.to_csv(OUT/'segments.csv',index=False)
    good=df[df.accepted]
    summary=good.groupby(['direction','pwm_code']).agg(
        voltage=('voltage','median'), n=('tau_s','size'), tau_s=('tau_s','median'),
        tau_min_s=('tau_s','min'),tau_max_s=('tau_s','max'),
        amplitude_sensor_ml=('amplitude_sensor_ml','median')).reset_index()
    summary.to_csv(OUT/'voltage_summary.csv',index=False)
    observations=df.groupby(['direction','pwm_code']).agg(
        voltage=('voltage','median'), n=('samples','size'),
        max_peak_sensor_slm=('peak_sensor_slm','max'),
        median_tail_sensor_slm=('tail_sensor_slm','median')).reset_index()
    observations.to_csv(OUT/'observations.csv',index=False)
    # A high-range candidate only: do not overwrite the inconsistent older low-range fit.
    models={}
    for direction,g in summary.groupby('direction'):
        tau0=.5853 if direction=='ATM' else .2549
        u,tau=g.voltage.to_numpy(),g.tau_s.to_numpy()
        def residual(p):
            return np.log(tau0/np.clip((4.3-u)/(4.3-p[0]),1e-9,1)**p[1])-np.log(tau)
        result=least_squares(residual,[4.1,2],bounds=([0,.05],[4.19,8]))
        models[direction]=dict(tau0_s=tau0,plateau_end_v=float(result.x[0]),
            gamma=float(result.x[1]),closure_anchor_v=4.3,
            fitted_voltage_min_v=float(u.min()),fitted_voltage_max_v=float(u.max()),
            max_relative_tau_error=float(np.max(abs(np.exp(residual(result.x))-1))))
    result=dict(sources=sources,accepted_segments=len(good),total_segments=len(df),
        vac_baseline_sensor_slm=baseline,vac_baseline_prior_gain_slm=baseline*1.0471,
        closure=dict(last_observed_conducting_v=4.259,near_closed_test_v=4.3,
                     note='Not an exact closure threshold; 4.260..4.299 V unmeasured; residual VAC flow remains.'),
        high_range_candidate_models=models,
        deployment='Integrated in hardware reference and firmware as 2026-10-14_PM_UC430. Full-open tau retained. The same power law extrapolates below the high-range fit; older overlapping measurements disagree. Bench validation is pending; not flashed by this analysis script.',
        fit_method='Signed trapezoidal cumulative flow; fixed measured VAC tail baseline; free transient amplitude and tau; soft_l1 loss. Baseline +/-0.1 SLM sensitivity is not a confidence interval.',
        pwm_note='4.251 V and 4.252 V both map to PWM code 3482; file names do not determine voltage.',
        limitations=['No measured PPV position or supply pressure.',
                     'Final and partial-fill traces retained as observations but excluded from primary fit.',
                     'Near-closed short traces do not establish sealing under all initial bag pressures.',
                     'No new independent flow-gain or actuator-motion calibration.'])
    (OUT/'fit.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    fig,axes=plt.subplots(1,3,figsize=(15,4.5),constrained_layout=True)
    old=pd.read_csv(ROOT/'analysis/sweep_2026-10-14/voltage_summary.csv')
    for direction,color in [('ATM','C0'),('VAC','C1')]:
        g=summary[summary.direction.eq(direction)]
        axes[0].plot(g.voltage,g.tau_s,'o-',color=color,label=direction+' new accepted')
        previous=old[old.direction.eq(direction)&old.voltage.ge(4.1)]
        axes[0].plot(previous.voltage,previous.tau_s,'x--',color=color,alpha=.5,label=direction+' old')
        obs=observations[observations.direction.eq(direction)]
        axes[1].plot(obs.voltage,obs.max_peak_sensor_slm,'o-',color=color,label=direction)
        axes[2].plot(obs.voltage,obs.median_tail_sensor_slm,'o-',color=color,label=direction)
    for ax in axes:
        ax.set_xlabel('Recorded command voltage [V]'); ax.grid(alpha=.25);ax.legend(fontsize=8)
        ax.axvline(4.25,color='gray',ls=':')
    axes[0].set_ylabel('Fitted tau [s]');axes[1].set_ylabel('Maximum observed directional flow [sensor SLM]')
    axes[2].set_ylabel('Median segment tail [sensor SLM]')
    fig.savefig(OUT/'high_voltage_correction.png',dpi=160);plt.close(fig)
    fig,axes=plt.subplots(3,4,figsize=(16,10),constrained_layout=True)
    voltages=sorted({r['voltage'] for r,_,_,_ in curves})
    for ax,u in zip(axes.flat,voltages):
        for row,t,volume,pred in curves:
            if row['voltage']!=u:continue
            color='C0' if row['direction']=='ATM' else 'C1'
            ax.plot(t,volume,color=color,alpha=.7 if row['accepted'] else .25)
            ax.plot(t,pred,'--',color=color,alpha=.4)
        ax.set_title(f'{u:g} V');ax.set_xlabel('Phase time [s]');ax.set_ylabel('Baseline-subtracted mL');ax.grid(alpha=.2)
    for ax in list(axes.flat)[len(voltages):]: ax.axis('off')
    fig.savefig(OUT/'segment_fits.png',dpi=140);plt.close(fig)
    print(summary.round(4).to_string(index=False))
    print(json.dumps(result['high_range_candidate_models'],indent=2))
    print('VAC baseline',baseline,'accepted',len(good),'of',len(df))


if __name__=='__main__':
    main()
