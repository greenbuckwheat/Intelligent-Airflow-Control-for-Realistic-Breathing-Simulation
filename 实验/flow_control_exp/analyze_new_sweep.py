"""Audit October 14 PPV sweep. Does not deploy parameters or edit input logs."""
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
OUT = ROOT / 'analysis' / 'sweep_2026-10-14'
NAMES = '0 0_1 0.5 1 1.5 2_1 3.0 3.2_1 3.4 3.5_1 3.6_1 3.7_1 3.8_1 3.85_1 3.9_1 3.95_1 4.0_1 4.05_1 4.1_1 4.15_1 4.2_1 4.25_1'.split()


def fit_response(t, y, direction, delay=False):
    volume = np.r_[0, np.cumsum((y[1:]+y[:-1])/2*np.diff(t)*1000/60)]
    tail = float(np.median(y[t >= t[-1]-min(1, t[-1]/3)]))
    # ATM has no demonstrated bypass. VAC fits a persistent through-flow term.
    def model(p):
        b = p[2] if direction == 'VAC' else 0
        lag = p[-1] if delay else 0
        return p[0]*(-np.expm1(-np.maximum(t-lag, 0)/p[1])) + b*t*1000/60
    b0 = min(2, max(0, tail)) if direction == 'VAC' else 0
    initial = [max(1, min(800, volume[-1]-b0*t[-1]*1000/60)), 1]
    low, high = [0.1, .02], [1500, 200]
    if direction == 'VAC':
        initial += [b0]; low += [0]; high += [3]
    if delay:
        initial += [.02]; low += [0]; high += [.5]
    f = least_squares(lambda p: model(p)-volume, initial, bounds=(low, high), loss='soft_l1', f_scale=3)
    p = f.x
    pred = model(p)
    b = p[2] if direction == 'VAC' else 0
    r2 = 1-np.sum((volume-pred)**2)/max(1, np.sum((volume-volume.mean())**2))
    # Also evaluate transient-only R2 so linear bypass cannot inflate fit quality.
    dyn = volume-b*t*1000/60
    dyn_pred = pred-b*t*1000/60
    dr2 = 1-np.sum((dyn-dyn_pred)**2)/max(1, np.sum((dyn-dyn.mean())**2))
    return dict(amplitude_sensor_ml=float(p[0]), tau_s=float(p[1]), bypass_sensor_slm=float(b),
                delay_s=float(p[-1]) if delay else 0, r2=float(r2), dynamic_r2=float(dr2),
                rmse_ml=float(np.sqrt(np.mean((volume-pred)**2))), integral_sensor_ml=float(volume[-1]),
                tail_sensor_slm=tail), volume, pred


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows, sources, curves = [], [], []
    for name in NAMES:
        path = ROOT/'data'/(name+'.csv')
        raw = pd.read_csv(path)
        meta = dict(file=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), rows=len(raw),
                    faults=raw.iloc[:,10].dropna().unique().tolist(), actual_voltages=raw.iloc[:,5].dropna().unique().tolist())
        if len(raw.columns) != 21:
            meta['excluded'] = 'Legacy ESTOP recording at 5 V; not a 0 V experiment'
            sources.append(meta)
            continue
        d = raw[raw.iloc[:,13].eq('telemetry')].copy()
        valid = d.iloc[:,9].eq('OK') & d.iloc[:,10].eq('NONE') & d.iloc[:,14].notna()
        meta['invalid_samples'] = int((~valid).sum())
        d = d[valid]
        meta['duplicate_samples'] = int(d.duplicated(d.columns[14]).sum())
        d = d.drop_duplicates(d.columns[14])
        a = d.iloc[:,[14,16,17,18,19,20]].to_numpy(float)
        if not np.isfinite(a).all() or (np.diff(a[:,0])<=0).any():
            raise ValueError('Invalid/nonmonotonic data: '+path.name)
        meta['max_sample_gap_ms'] = float(np.diff(a[:,0]).max())
        meta['offsets_slm'] = np.unique(a[:,2]).tolist()
        meta['first_host_time'] = str(d.iloc[0,0])
        sources.append(meta)
        t = a[:,0]/1000
        q = a[:,1]-a[:,2]
        state = a[:,3]+2*a[:,4]
        cuts = np.r_[0,np.flatnonzero((np.diff(state)!=0)|(np.diff(a[:,5])!=0)|(np.diff(a[:,2])!=0))+1,len(a)]
        for k,(i,j) in enumerate(zip(cuts[:-1],cuts[1:])):
            if state[i] not in (1,2) or j-i<20: continue
            direction = 'ATM' if state[i]==1 else 'VAC'
            x = t[i:j]-t[i]
            y = q[i:j]*(1 if direction=='ATM' else -1)
            fit, vol, pred = fit_response(x,y,direction)
            delayed, _, _ = fit_response(x,y,direction,True)
            reasons=[]
            if i==0 or state[i-1]==state[i]: reasons.append('unknown onset or in-phase parameter change')
            if j==len(a): reasons.append('record ended before observed phase end')
            if np.diff(x).max()>.1: reasons.append('within-phase sampling gap >100ms')
            if fit['amplitude_sensor_ml']<150: reasons.append('small exchange / near-closure / incomplete initial state')
            if x[-1]<3*fit['tau_s']: reasons.append('less than 3 tau observed')
            if fit['dynamic_r2']<.95: reasons.append('transient cumulative R2 <0.95')
            if abs(fit['tail_sensor_slm']-fit['bypass_sensor_slm'])>.5: reasons.append('tail not settled')
            row=dict(file=path.name,segment=k,direction=direction,voltage=float(a[i,5]),start_sample_ms=float(a[i,0]),
                     duration_s=float(x[-1]),samples=j-i,peak_sensor_slm=float(max(y)),**fit,
                     amplitude_prior_gain_ml=fit['amplitude_sensor_ml']*1.0471,
                     tau_with_delay_s=delayed['tau_s'],shape_delay_s=delayed['delay_s'],
                     accepted=not reasons,reason='; '.join(reasons))
            rows.append(row)
            curves.append((row,x,y,vol,pred))
    segments=pd.DataFrame(rows)
    segments.to_csv(OUT/'segments.csv',index=False)
    good=segments[segments.accepted]
    summary=good.groupby(['direction','voltage']).agg(n=('tau_s','size'),tau_s=('tau_s','median'),
        tau_q25_s=('tau_s',lambda x:x.quantile(.25)),tau_q75_s=('tau_s',lambda x:x.quantile(.75)),
        amplitude_sensor_ml=('amplitude_sensor_ml','median'),amplitude_prior_gain_ml=('amplitude_prior_gain_ml','median'),
        bypass_sensor_slm=('bypass_sensor_slm','median'),tau_with_delay_s=('tau_with_delay_s','median')).reset_index()
    models={}
    for direction,g in summary.groupby('direction'):
        u,tau=g.voltage.to_numpy(),g.tau_s.to_numpy()
        tau0=float(g.loc[g.voltage.eq(0),'tau_s'].iloc[0])
        # Closure is weakly identified: report sensitivity, not a measured exact value.
        fits=[]
        for closure in (4.23,4.25,4.27,4.30,4.35):
            def residual(p):
                conductance=np.clip((closure-u)/(closure-p[0]),1e-9,1)**p[1]
                return np.log(tau0/conductance)-np.log(tau)
            best=None
            for dead in np.linspace(3.5,4.15,9):
                f=least_squares(residual,[dead,1.5],bounds=([0,.05],[4.19,8]))
                if best is None or f.cost<best.cost: best=f
            p=best.x
            fits.append(dict(closure_v=closure,tau0_s=tau0,plateau_end_v=float(p[0]),gamma=float(p[1]),
                             log_rmse=float(np.sqrt(np.mean(residual(p)**2))),max_relative_error=float(np.max(abs(np.exp(residual(p))-1)))))
        chosen=next(f for f in fits if f['closure_v']==4.25)
        models[direction]=dict(**chosen,closure_sensitivity=fits)
        summary.loc[g.index,'relative_conductance_observed']=chosen['tau0_s']/tau
        summary.loc[g.index,'effective_opening_model']=np.clip((chosen['closure_v']-u)/(chosen['closure_v']-chosen['plateau_end_v']),0,1)
    summary.to_csv(OUT/'voltage_summary.csv',index=False)
    old=json.loads((ROOT.parents[1]/'LungLogic_DualBranch_PID_Simulation_Arduino/single_lung_hardware_reference.json').read_text())['lung']
    for direction,g in summary.groupby('direction'):
        suffix='in' if direction=='ATM' else 'out'
        old_a=np.clip((old['hardware_closure_v']-g.voltage)/(old['hardware_closure_v']-old['hardware_dead_zone_'+suffix+'_v']),1e-9,1)
        summary.loc[g.index,'old_model_tau_s']=old['tau_'+suffix+'_s']/old_a**old['hardware_gamma_'+suffix]
    summary['tau_ratio_new_to_old']=summary.tau_s/summary.old_model_tau_s
    summary.to_csv(OUT/'voltage_summary.csv',index=False)
    result=dict(sources=sources,models=models,accepted_segments=len(good),total_segments=len(segments),
        tau0_definition='Fixed to observed 0 V no-delay median; directional voltage lookup is preferred over the approximate power law',
        primary_flow='SFM engineering SLM minus recorded zero offset; ATM no constant bypass; VAC fitted nonnegative bypass',
        volume_prior_gain='Multiply sensor-standard mL by existing independent gain 1.0471; new sweep does not identify gain',
        model='Cumulative volume A*(1-exp(-t/tau))+b*t*1000/60; no-delay primary; optional 0..0.5s delay diagnostic',
        closure_note='4.25 V used as provisional near-closure anchor, not verified zero leakage; only 4.20 and 4.25 bracket last responsive region')
    (OUT/'fit.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    # All segments remain visible for judging rejected fits and initial-state variation.
    for direction in ('ATM','VAC'):
        fig,axes=plt.subplots(5,5,figsize=(17,14),constrained_layout=True)
        for ax,u in zip(axes.flat,sorted(segments.voltage.unique())):
            for row,x,y,v,pred in curves:
                if row['direction']!=direction or row['voltage']!=u:continue
                color='C0' if row['accepted'] else '0.65'
                ax.plot(x,v-row['bypass_sensor_slm']*x*1000/60,color=color,alpha=.65,lw=.8)
                if row['accepted']:ax.plot(x,pred-row['bypass_sensor_slm']*x*1000/60,'k--',lw=.5)
            ax.set_title(f'{u:g} V');ax.grid(alpha=.2)
        for ax in list(axes.flat)[len(segments.voltage.unique()):]: ax.axis('off')
        fig.suptitle(direction+' cumulative exchanged volume [sensor-standard mL] versus time [s]; gray excluded')
        fig.savefig(OUT/(direction+'_segment_fits.png'),dpi=140);plt.close(fig)
    fig,axes=plt.subplots(1,3,figsize=(16,4.6),constrained_layout=True)
    for direction,g in summary.groupby('direction'):
        model=models[direction];u=np.linspace(0,4.22,500)
        opening=np.clip((model['closure_v']-u)/(model['closure_v']-model['plateau_end_v']),1e-8,1)
        line=axes[0].plot(g.voltage,g.tau_s,'o',label=direction)[0]
        axes[0].plot(u,model['tau0_s']/opening**model['gamma'],color=line.get_color())
        axes[1].plot(g.voltage,g.amplitude_prior_gain_ml,'o-',label=direction)
        axes[2].plot(g.voltage,g.relative_conductance_observed,'o-',label=direction)
    axes[0].set(ylabel='Effective tau [s]',ylim=(0,10))
    axes[1].set(ylabel='Transient amplitude [mL, prior gain 1.0471]')
    axes[1].axhline(250,color='gray',ls=':',label='Current target 250')
    axes[1].axhline(323.9,color='black',ls='--',label='Current model 323.9')
    axes[2].set(ylabel='Relative conductance tau0 / tau')
    for ax in axes:ax.set_xlabel('PPV command [V]');ax.grid(alpha=.25);ax.legend(fontsize=8)
    fig.savefig(OUT/'comparison.png',dpi=160);plt.close(fig)
    print(summary.round(4).to_string(index=False))
    print(json.dumps(models,indent=2))
    print('Accepted',len(good),'of',len(segments))


if __name__=='__main__':main()
