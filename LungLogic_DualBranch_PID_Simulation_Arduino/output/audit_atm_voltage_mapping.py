"""Audit actual ATM voltage sweep, retaining date and incomplete-segment flags."""
from pathlib import Path
import importlib.util,json
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT=Path(__file__).resolve().parent
BASE=next(ROOT.parent.parent.glob('*/flow_control_exp/data'))
OUT=ROOT/'atm_voltage_audit'
OUT.mkdir(exist_ok=True)
spec=importlib.util.spec_from_file_location('sweep',ROOT.parent/'analyze_sponge_voltage_sweep.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
names=['0','2','3.2','3.5','3.6','3.7','3.8','3.85','3.9','3.95','4.0','4.05','4.1','4.15','4.18','4.2','4.25','4.3','4.35','4.5']
rows=[];traces=[]
for name in names:
    raw=pd.read_csv(BASE/(name+'.csv'))
    d=raw[raw.iloc[:,1].notna() & raw.iloc[:,11].isna()].copy()
    a=d.iloc[:,[1,2,3,4,5]].to_numpy(dtype=float)
    t=(a[:,0]-a[0,0])/1000; q=1.0471*(a[:,1]-.0034)
    state=a[:,2]+2*a[:,3]
    edges=np.r_[0,np.flatnonzero(np.any(np.diff(a[:,2:],axis=0),axis=1))+1,len(a)]
    for k,(i,j) in enumerate(zip(edges[:-1],edges[1:])):
        if state[i]!=1 or j-i<15:continue
        x=t[i:j]-t[i];y=q[i:j]
        fixed,v,pred=m.fit_segment(x,y,residual=False)
        late=np.median(y[x>=max(0,x[-1]-1)])
        reason=[]
        if i==0:reason.append('record starts active')
        if i>0 and state[i-1]==1:reason.append('voltage changed during ATM')
        if j==len(a):reason.append('record ends active')
        if x[-1]<3*fixed['tau_s']:reason.append('less than 3 tau')
        if fixed['amplitude_ml']<500:reason.append('small excursion')
        if fixed['r2']<.97:reason.append('poor fit')
        if a[i,4]>=4.35:reason.append('closure observation')
        if name=='4.3' and a[i,4]==0:reason.append('0V recovery')
        prevvac=np.flatnonzero(state[:i]==2)
        previous_vac_duration=np.nan
        if len(prevvac):
            last=prevvac[-1];first=last
            while first>0 and state[first-1]==2:first-=1
            previous_vac_duration=t[last]-t[first]
        # Eligibility for the smaller October 12 excursion must not inherit
        # the old 500 mL amplitude threshold. Tail completion is observational.
        onset_known=i>0 and state[i-1]!=1
        tail_reached=late<=.35 and v[-1]>200
        times={}
        for fraction in (.5,.9):
            hits=np.flatnonzero(v>=fraction*v[-1])
            times['t'+str(int(100*fraction))+'_s']=float(x[hits[0]]) if len(hits) else np.nan
        r=dict(file=name+'.csv',date=str(d.iloc[0,0])[:10],segment=k,voltage=a[i,4],
            duration_s=x[-1],integral_ml=v[-1],tail_slm=late,peak_slm=y.max(),
            first_1p67_ml=float(np.trapezoid(np.interp(np.r_[x[x<1.67],1.67],x,y),np.r_[x[x<1.67],1.67])*1000/60) if x[-1]>=1.67 else np.nan,
            previous_vac_duration_s=previous_vac_duration,**times,**fixed,usable=not reason,reason='; '.join(reason),
            new_batch_descriptive=onset_known and tail_reached and str(d.iloc[0,0]).startswith('2026-10-12') and a[i,4]>0)
        rows.append(r);traces.append((r,x,y,v,pred))
r=pd.DataFrame(rows);r.to_csv(OUT/'atm_segments.csv',index=False)
summary=r[r.new_batch_descriptive].groupby('voltage').agg(n=('file','size'),volume_ml=('integral_ml','median'),first_1p67_ml=('first_1p67_ml','median'),tau_s=('tau_s','median'),t50_s=('t50_s','median'),t90_s=('t90_s','median'),amplitude_ml=('amplitude_ml','median')).reset_index()
summary['relative_rate_vs_3p8']=summary.tau_s.iloc[0]/summary.tau_s
summary['current_model_relative_rate']=np.clip((4.35-summary.voltage)/.2,0,1)**.93
summary.to_csv(OUT/'new_batch_voltage_summary.csv',index=False)
print(summary.round(3).to_string(index=False))
# Preserve historical 10/09 results separately, never relabel overwritten files.
old=pd.read_csv(ROOT/'sponge_voltage_sweep_2026-10-09/segments.csv')
old=old[(old.direction=='ATM') & old.usable.astype(str).str.lower().eq('true')]
old.to_csv(OUT/'historical_oct09_accepted_atm.csv',index=False)
fig,axes=plt.subplots(2,1,figsize=(11,9),constrained_layout=True)
for date,g in r[r.usable | r.new_batch_descriptive].groupby('date'):
    med=g.groupby('voltage').tau_s.median()
    axes[0].plot(med.index,med.values,'o-',label=date+' current source files')
hist=old.groupby('voltage').tau_s.median()
axes[0].plot(hist.index,hist.values,'x--',label='Oct09 archived accepted fits')
u=np.linspace(3.2,4.349,300)
axes[0].plot(u,2.694/np.clip((4.35-u)/.2,1e-6,1)**.93,label='Current controller model')
axes[0].set(xlim=(3.2,4.36),ylim=(0,35),xlabel='PPV command (V)',ylabel='No-delay fitted tau (s)')
axes[0].legend(fontsize=8);axes[0].grid(alpha=.2)
for row,x,y,v,pred in traces:
    if row['date']=='2026-10-12' and row['voltage']>=4.15:
        axes[1].plot(x,v,label=f"{row['file']} seg{row['segment']} {row['voltage']:g}V")
axes[1].set(xlabel='Time since ATM segment start (s)',ylabel='Calibrated signed integral (mL)',xlim=(0,25))
axes[1].legend(fontsize=7);axes[1].grid(alpha=.2)
fig.savefig(OUT/'atm_mapping_comparison.png',dpi=160)
fig,axes=plt.subplots(1,2,figsize=(12,4.7),constrained_layout=True)
axes[0].plot(summary.voltage,summary.relative_rate_vs_3p8,'o-',label='Oct12 tau(3.8V)/tau(U), descriptive')
axes[0].plot(summary.voltage,summary.current_model_relative_rate,'s--',label='Current controller k(U)')
axes[0].set(xlabel='PPV command (V)',ylabel='Relative response rate',ylim=(0,1.1))
axes[1].plot(summary.voltage,summary.first_1p67_ml,'o-',label='Oct12 first 1.67 s integral')
axes[1].axhline(250,color='gray',ls='--',label='250 mL target')
axes[1].set(xlabel='PPV command (V)',ylabel='Calibrated signed volume (mL)')
for ax in axes:ax.grid(alpha=.2);ax.legend(fontsize=8)
fig.savefig(OUT/'new_batch_mapping.png',dpi=170)
