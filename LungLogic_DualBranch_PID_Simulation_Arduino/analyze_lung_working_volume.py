"""Measured cycle-volume audit; no controller or source-data changes."""
import csv
import json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from analyze_sponge_voltage_sweep import BASE, NAMES, GAIN, OFFSET, LEAK

OUT = Path(__file__).resolve().parent/'output/lung_working_volume_2026-10-09'


def summary(rows, key):
    a = np.array([r[key] for r in rows])
    if not len(a): return dict(n=0)
    return dict(n=len(a), median=float(np.median(a)), min=float(a.min()), max=float(a.max()),
                p10=float(np.quantile(a,.1)), p90=float(np.quantile(a,.9)))


def main():
    OUT.mkdir(exist_ok=True, parents=True)
    cycles = []
    for name in NAMES:
        with (BASE/(name+'.csv')).open(encoding='utf-8-sig',newline='') as f:
            rr = [r for r in list(csv.reader(f))[1:] if r[1] and not r[11]]
        a = np.array([[float(r[i]) for i in (1,2,3,4,5)] for r in rr])
        t = (a[:,0]-a[0,0])/1000
        q = GAIN*(a[:,1]-OFFSET)
        state = a[:,2]+2*a[:,3]
        # State-only segments retain mid-ATM voltage changes as one recovery.
        edges = np.r_[0,np.flatnonzero(np.diff(state))+1,len(a)]
        segments = [(i,j,int(state[i])) for i,j in zip(edges[:-1],edges[1:])]
        # Allocate each sample interval to its left-hand valve state, retaining
        # switching intervals instead of silently losing gaps between segments.
        dt = np.diff(t)
        midq = (q[:-1]+q[1:])*.5
        inc = midq*dt*1000/60
        netq = np.where(state[:-1]==2, np.where(midq<0,np.minimum(0,midq+LEAK),midq),midq)
        netinc = netq*dt*1000/60
        def integral(i,j,positive=False,negative=False,net=False):
            v=(netinc if net else inc)[i:min(j,len(inc))]
            if positive: v=np.maximum(v,0)
            if negative: v=np.minimum(v,0)
            return float(v.sum())
        for si,(vi,vj,vs) in enumerate(segments):
            if vs!=2: continue
            after=si+1
            while after<len(segments) and segments[after][2]==0: after+=1
            if after>=len(segments) or segments[after][2]!=1: continue
            ai,aj,_=segments[after]
            end=aj
            if after+1<len(segments) and segments[after+1][2]==0: end=segments[after+1][1]
            # Final expansion before next VAC or end of record.
            trajectory=np.r_[0,np.cumsum(inc[vj:min(end,len(inc))])]
            wait=integral(vj,ai); fill=integral(ai,aj); post=integral(aj,end)
            late=q[ai:aj][t[ai:aj]>=t[aj-1]-1]
            vcommands=sorted(set(a[ai:aj,4]))
            r=dict(file=name+'.csv',cycle=sum(r['file']==name+'.csv' for r in cycles)+1,
                   vac_voltage=float(np.median(a[vi:vj,4])),atm_voltage_min=min(vcommands),atm_voltage_max=max(vcommands),
                   vac_duration_s=float(t[min(vj,len(t)-1)]-t[vi]),
                   wait_duration_s=float(t[ai]-t[vj]),atm_duration_s=float(t[min(aj,len(t)-1)]-t[ai]),
                   vac_measured_out_ml=-integral(vi,vj),vac_leak_corrected_out_ml=-integral(vi,vj,net=True),
                   wait_signed_ml=wait,wait_positive_ml=integral(vj,ai,positive=True),
                   wait_negative_ml=integral(vj,ai,negative=True),atm_signed_ml=fill,post_atm_signed_ml=post,
                   recovery_from_vac_off_ml=wait+fill+post,
                   recovery_from_minimum_ml=float(trajectory[-1]-trajectory.min()),
                   atm_tail_slm=float(np.median(late)),
                   cycle_imbalance_ml=integral(vi,vj,net=True)+wait+fill+post)
            r['balance_implied_vac_leak_slm'] = (r['vac_measured_out_ml']-r['recovery_from_vac_off_ml'])*.06/r['vac_duration_s']
            r['complete_candidate']=bool(abs(r['atm_tail_slm'])<=.5 and fill>500)
            r['low_voltage_reference']=bool(r['complete_candidate'] and max(vcommands)<=3.8 and r['vac_voltage']<=3.8 and name!='4.2')
            cycles.append(r)
    with (OUT/'cycles.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(cycles[0]));w.writeheader();w.writerows(cycles)
    groups=[]
    for name in NAMES:
        rows=[r for r in cycles if r['file']==name+'.csv']
        good=[r for r in rows if r['complete_candidate']]
        groups.append(dict(file=name+'.csv',cycles=len(rows),complete=len(good),
                           recovery=summary(good,'recovery_from_minimum_ml'),
                           wait=summary(rows,'wait_signed_ml'),vac=summary(good,'vac_leak_corrected_out_ml')))
    low=[r for r in cycles if r['low_voltage_reference']]
    keys=['recovery_from_minimum_ml','atm_signed_ml','wait_signed_ml','wait_positive_ml','wait_negative_ml',
          'post_atm_signed_ml','vac_measured_out_ml','vac_leak_corrected_out_ml','cycle_imbalance_ml',
          'balance_implied_vac_leak_slm']
    sensitivity={}
    for threshold in (.3,.5,1.0):
        rr=[r for r in cycles if abs(r['atm_tail_slm'])<=threshold and r['atm_signed_ml']>500
            and r['atm_voltage_max']<=3.8 and r['vac_voltage']<=3.8 and r['file']!='4.2.csv']
        sensitivity[str(threshold)]=summary(rr,'recovery_from_minimum_ml')
    result=dict(low_voltage_reference={k:summary(low,k) for k in keys},groups=groups,
                tail_threshold_sensitivity=sensitivity,
                notes=['Working volume is measured exchange between evacuated and recovered states, not geometric capacity.',
                       'Fixed gain 1.0471 and offset .0034 applied. Fixed .931 SLM leakage only during VAC.',
                       'Closed intervals integrated with sign; positive and negative parts also exported.',
                       'Reference requires ATM final-second median <=.5 SLM and exchange >500 mL, voltage <=3.8.',
                       'Tail threshold is empirical; pressure/complete expansion not independently measured.',
                       'Recovery from minimum accounts for continued extraction after VAC off.',
                       'Balance-implied leakage assumes repeatable cycle endpoint states; diagnostic only, not a new leakage calibration.',
                       'Syringe residual ~0.8 percent is same-data low-flow repeatability, not a validated high-flow uncertainty bound.',
                       'No estimated post-record volume or assumed closed-state leak rate added.'])
    (OUT/'summary.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    fig,ax=plt.subplots(figsize=(11,5))
    for flag,label,color in [(True,'ATM tail <= 0.5 SLM','tab:blue'),(False,'Incomplete/uncertain recovery','gray')]:
        rows=[r for r in cycles if r['complete_candidate']==flag]
        ax.scatter([r['vac_voltage'] for r in rows],[r['recovery_from_minimum_ml'] for r in rows],label=label,color=color,alpha=.7)
    ax.set(xlabel='VAC PPV voltage (V)',ylabel='Measured recovery from minimum (mL)',title='Calibrated cycle exchange including closed intervals')
    ax.grid(alpha=.3);ax.legend();fig.tight_layout();fig.savefig(OUT/'working_volume.png',dpi=160);plt.close(fig)
    print(json.dumps(result,indent=2))


if __name__=='__main__': main()
