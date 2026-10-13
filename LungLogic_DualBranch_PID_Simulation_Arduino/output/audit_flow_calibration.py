"""Read-only source-data audit; writes derived tables into calibration_audit."""
from pathlib import Path
import importlib.util
import json
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'calibration_audit'
OUT.mkdir(exist_ok=True)
inventory = []
for f in ROOT.rglob('*'):
    if not f.is_file() or OUT in f.parents:
        continue
    rel = f.relative_to(ROOT)
    if any('build' in x for x in rel.parts[:-1]):
        continue
    if f.suffix not in ('.csv', '.json', '.md'):
        continue
    entry = dict(file=str(rel), bytes=f.stat().st_size)
    if f.suffix == '.csv':
        d = pd.read_csv(f)
        entry.update(rows=len(d), columns='|'.join(d.columns))
    elif f.suffix == '.json':
        j = json.loads(f.read_text(encoding='utf-8-sig'))
        entry['keys'] = '|'.join(j) if isinstance(j, dict) else 'list'
    else:
        entry['characters'] = len(f.read_text(encoding='utf-8-sig'))
    inventory.append(entry)
pd.DataFrame(inventory).to_csv(OUT/'data_inventory.csv', index=False)

runs, cycles = [], []
for f in sorted(ROOT.glob('*.csv'), key=lambda f: f.stat().st_mtime):
    d = pd.read_csv(f)
    if not {'state_name', 'flow_slm'}.issubset(d):
        continue
    j = json.loads(f.with_suffix('.json').read_text(encoding='utf-8-sig')) if f.with_suffix('.json').exists() else {}
    c = j.get('configuration', {})
    lung = c.get('lung', {})
    dt = d.time_s.diff().median()
    q = d.get('net_flow_slm', d.flow_slm).fillna(d.flow_slm)
    cal_error = np.nan
    if 'raw_flow_slm' in d and 'hardware_flow_gain' in lung:
        cal_error = (d.flow_slm-lung.get('flow_sign',1)*lung['hardware_flow_gain']*(d.raw_flow_slm-lung['hardware_flow_offset_slm'])).abs().max()
    rr = dict(file=f.stem, rows=len(d), protocol=j.get('protocol_version'),
              bpm=c.get('respiratory_rate_bpm'), ie=str(c.get('ie_inspiration'))+':'+str(c.get('ie_expiration')),
              kp=c.get('pi',{}).get('kp'), ki=c.get('pi',{}).get('ki'), smith=c.get('smith',{}).get('enabled'),
              gain=lung.get('hardware_flow_gain'), offset=lung.get('hardware_flow_offset_slm'),
              leak=lung.get('hardware_vac_leak_slm'), max_calibration_error_slm=cal_error,
              dt_s=dt, max_dt_s=d.time_s.diff().max(), model_reference=j.get('model_reference'),
              auto_pid_stage=str(j.get('auto_pid_stage')))
    local=[]
    for cy, a in d[d.state_name=='INSPIRATION'].groupby('cycle'):
        if len(a)<2:
            continue
        b=d[(d.cycle==cy)&(d.state_name=='EXPIRATION')]
        start=d.loc[a.index[0]-1,'volume_ml'] if a.index[0]>0 else a.volume_ml.iloc[0]
        v=q.loc[a.index].clip(lower=0).sum()*dt*1000/60
        # Include all positive flow between consecutive inspiration starts.
        nxt=d[(d.index>a.index[-1])&(d.state_name=='INSPIRATION')]
        stop=nxt.index[0] if len(nxt) else d.index[-1]+1
        window=d.loc[(d.index>=a.index[0])&(d.index<stop)&(~d.state_name.isin(['RECOVERY','PRECONDITION']))]
        item=dict(file=f.stem,cycle=int(cy),inspired_integral_ml=v,
                  logged_increment_ml=a.volume_ml.max()-start,peak_ml=a.volume_ml.max(),
                  raw_inspired_ml=d.loc[a.index,'raw_flow_slm'].clip(lower=0).sum()*dt*1000/60 if 'raw_flow_slm' in d else np.nan,
                  outside_inspiration_positive_ml=q.loc[window.index[window.state_name!='INSPIRATION']].clip(lower=0).sum()*dt*1000/60,
                  residual_ml=b.volume_ml.iloc[-1] if len(b) else np.nan,
                  inspiration_peak_slm=q.loc[a.index].max(), expiration_peak_abs_slm=-q.loc[b.index].min() if len(b) else np.nan)
        local.append(item);cycles.append(item)
    if local:
        z=pd.DataFrame(local)
        rr.update(cycles=len(z),mean_inspired_ml=z.inspired_integral_ml.mean(),min_inspired_ml=z.inspired_integral_ml.min(),
                  max_inspired_ml=z.inspired_integral_ml.max(),mean_logged_increment_ml=z.logged_increment_ml.mean(),
                  last_residual_ml=z.residual_ml.iloc[-1],max_expiratory_peak_slm=z.expiration_peak_abs_slm.max(),
                  mean_outside_positive_ml=z.outside_inspiration_positive_ml.mean())
    runs.append(rr)
pd.DataFrame(runs).to_csv(OUT/'all_hardware_runs.csv',index=False)
pd.DataFrame(cycles).to_csv(OUT/'all_hardware_cycles.csv',index=False)

spec=importlib.util.spec_from_file_location('syringe',ROOT.parent/'analyze_syringe_calibration.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
d=pd.read_csv(m.SOURCE)
d=d[d.iloc[:,1].notna() & d.iloc[:,11].isna()]
t=(d.iloc[:,1].to_numpy()-d.iloc[0,1])/1000
strokes=m.strokes(t,d.iloc[:,2].to_numpy()-m.BIAS,d.iloc[:,5].to_numpy())
z=pd.DataFrame(strokes)
z['applied_gain_volume_ml']=z.measured_ml*1.0471
z.to_csv(OUT/'syringe_recomputed.csv',index=False)
print('Syringe:',len(z),'mean unscaled',z.measured_ml.mean(),'corrected',z.applied_gain_volume_ml.mean(),'range',z.applied_gain_volume_ml.min(),z.applied_gain_volume_ml.max())
cols=['file','rows','protocol','bpm','kp','ki','smith','gain','max_calibration_error_slm','mean_inspired_ml','min_inspired_ml','max_inspired_ml','last_residual_ml','max_expiratory_peak_slm','mean_outside_positive_ml']
print(pd.DataFrame(runs)[cols].round(4).to_string(index=False))
print('Inventoried data/document files:',len(inventory))
