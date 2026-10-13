"""Audit today's voltage sweep against the actual single-lung model."""
import csv
import json
from pathlib import Path
import numpy as np
from scipy.optimize import least_squares
from analyze_vac_atm_cycles import analyze, ROOT

OUT = ROOT / 'analysis' / 'today_hardware'
NAMES = ['00.csv','00.1.csv','00.2.csv','00.3.csv','1.1.csv','1.11.csv','2.csv','2.1.csv','25.1.csv','3.csv','31.csv','32.csv','33.csv','34.csv','345.csv','35.csv','355.csv','36.csv','365.csv','37.csv','375.csv','38.csv','385.csv','39.csv','4.csv','405.csv','41.csv','415.csv','42.csv']

def main():
    import analyze_vac_atm_cycles as base
    OUT.mkdir(parents=True,exist_ok=True)
    base.OUT=OUT
    results=[]; flat=[]
    for name in NAMES:
        result=analyze(ROOT/'data'/name); results.append(result)
        with (ROOT/'data'/name).open(encoding='utf-8-sig',newline='') as f:
            rows=[r for r in csv.DictReader(f) if not r['操作事件'] and r['MCU时间戳ms']]
        ts=np.array([int(r['MCU时间戳ms']) for r in rows])/1000; ts-=ts[0]
        q=np.array([float(r['流量SLM']) for r in rows])
        voltage=result['ppv_v']
        print(name,'V=',voltage,'status=',result['sensor_status'],result['faults'])
        for i,s in enumerate(result['segments']):
            if s['state'] not in (1,2): continue
            selected=(ts>=s['start_s']-1e-7)&(ts<=s['end_s']+1e-7)
            x=ts[selected]-s['start_s']; y=q[selected]-s['baseline_slm']
            sign=1 if s['state']==1 else -1
            y*=sign
            row=dict(file=name,segment=i,voltage=voltage[0] if len(voltage)==1 else None,**s)
            row['early_median_slm']=float(np.median(y[(x>=.1)&(x<=.5)])) if x[-1]>=.5 else None
            row['end_last_half_slm']=float(np.median(y[x>=max(0,x[-1]-.5)]))
            if len(x)>25 and abs(s['signed_volume_ml'])>10:
                # Check sensitivity to early switching spikes: fit cumulative
                # volume from 0.1 s as a separate reference, not replacement.
                mask=x>=.1; xx=x[mask]-.1; yy=y[mask]
                vv=np.r_[0,np.cumsum((yy[1:]+yy[:-1])/2*np.diff(xx)*1000/60)]
                def model(p): return p[0]*(1-np.exp(-np.maximum(xx-p[2],0)/p[1]))
                f=least_squares(lambda p:model(p)-vv,[max(vv[-1],1),max(s.get('tau_s',2),.04),.02],bounds=([1,.03,0],[10000,100,2]),loss='soft_l1',f_scale=5)
                row['tau_skip_0p1_s']=float(f.x[1])
            flat.append(row)
            print(' ',i,'ATM' if s['state']==1 else 'VAC','t',round(s['start_s'],2),'dur',round(s['duration_s'],2),'vol',round(s['signed_volume_ml']), 'tau',round(s.get('tau_s',float('nan')),3),'skip',round(row.get('tau_skip_0p1_s',float('nan')),3),'early',round(row.get('early_median_slm') or 0,2),'end',round(row['end_last_half_slm'],2))
    (OUT/'all_results.json').write_text(json.dumps(results,indent=2,ensure_ascii=False),encoding='utf-8')
    keys=list(dict.fromkeys(k for r in flat for k in r))
    with (OUT/'all_segments.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=keys); w.writeheader(); w.writerows(flat)

if __name__=='__main__':main()
