"""Batch cycle screening; thresholds flag weak responses, not proven valve position."""
import csv
import json
import numpy as np
import matplotlib.pyplot as plt
from analyze_vac_atm_cycles import analyze, ROOT, OUT

NAMES = ['00.csv', '00.1.csv', '00.2.csv', '00.3.csv', '1.1.csv', '2.csv']

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = [analyze(ROOT / 'data' / name) for name in NAMES]
    flat = []
    for result in results:
        for i, segment in enumerate(result['segments']):
            if segment['state'] not in (1, 2):
                continue
            row = dict(file=result['file'], segment=i, voltage=result['ppv_v'][0], **segment)
            # Large separation in observed expired volume separates weak and
            # full-collapse responses for these recordings. Never infer valve
            # position from a threshold alone.
            row['response'] = 'substantial' if abs(row['signed_volume_ml']) >= 800 else 'weak_or_partial'
            row['complete_tail'] = abs(row['tail_median_slm']) <= 1
            flat.append(row)
    (OUT / 'voltage_cycle_results.json').write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding='utf-8')
    fields = list(dict.fromkeys(k for row in flat for k in row))
    with (OUT / 'voltage_cycle_segments.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader(); writer.writerows(flat)
    summaries = []
    for voltage in (0, 1, 2):
        for state in (2, 1):
            group = [r for r in flat if r['voltage'] == voltage and r['state'] == state]
            keep = [r for r in group if r['response'] == 'substantial' and 'tau_s' in r]
            complete = [r for r in keep if r['complete_tail']]
            summary = dict(voltage=voltage, direction='VAC' if state == 2 else 'ATM', total=len(group), retained=len(keep), complete=len(complete))
            for key in ('tau_s', 'signed_volume_ml', 'duration_s', 'cumulative_r2', 'tail_median_slm'):
                values = [r[key] for r in keep]
                if values:
                    summary[key] = dict(median=float(np.median(values)), min=min(values), max=max(values))
            summary['complete_tau_s'] = [r['tau_s'] for r in complete]
            summaries.append(summary)
    (OUT / 'voltage_cycle_summary.json').write_text(json.dumps(summaries, indent=2), encoding='utf-8')
    print(json.dumps(summaries, indent=2))
    print('SEGMENTS: file state start duration volume tau tail classification')
    for r in flat:
        print(r['file'], r['state'], round(r['start_s'],2), round(r['duration_s'],2), round(r['signed_volume_ml']), round(r.get('tau_s',float('nan')),3), round(r['tail_median_slm'],2), r['response'])
    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    for ax, state, label in zip(axes, (2,1), ('Expiration','Inspiration')):
        for voltage in (0,1,2):
            keep = [r for r in flat if r['state']==state and r['voltage']==voltage and r['response']=='substantial' and 'tau_s' in r]
            vals=[r['tau_s'] for r in keep]
            ax.scatter(np.full(len(vals),voltage),vals,alpha=.7)
            if vals: ax.plot(voltage,np.median(vals),'k_',markersize=20)
        ax.set(title=label,xlabel='PPV target (V)',ylabel='Effective fitted tau (s)',xticks=[0,1,2]); ax.grid(alpha=.3)
    fig.tight_layout(); fig.savefig(OUT/'voltage_tau_comparison.png',dpi=160); plt.close(fig)

if __name__ == '__main__':
    main()
