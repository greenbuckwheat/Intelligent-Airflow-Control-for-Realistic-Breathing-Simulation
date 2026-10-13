"""Inspect VAC/ATM cycles without mixing command-event rows into samples."""
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import least_squares

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "analysis" / "vac_atm_cycles"

def analyze(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        all_rows = list(csv.DictReader(f))
    rows = [r for r in all_rows if not r["操作事件"] and r["MCU时间戳ms"]]
    t = np.array([int(r["MCU时间戳ms"]) for r in rows]) / 1000
    t -= t[0]
    q = np.array([float(r["流量SLM"]) for r in rows])
    state = np.array([int(r["ATM状态"]) + 2 * int(r["VAC状态"]) for r in rows])
    edges = np.r_[0, np.flatnonzero(np.diff(state)) + 1, len(rows)]
    initial_baseline = float(np.median(q[:min(100, int(edges[1]))]))
    result = dict(file=path.name, samples=len(rows), duration_s=float(t[-1]),
                  sensor_status=sorted({r["传感器状态"] for r in rows}),
                  faults=sorted({r["故障"] for r in rows}),
                  ppv_v=sorted({float(r["PPV目标电压V"]) for r in rows}),
                  dt_median_s=float(np.median(np.diff(t))), dt_max_s=float(np.max(np.diff(t))),
                  events=[{k:r[k] for k in ("主机时间", "MCU时间戳ms", "操作事件")} for r in all_rows if r["操作事件"]], segments=[])
    fig, axes = plt.subplots(3, 1, figsize=(11, 8), sharex=True)
    axes[0].plot(t, q); axes[0].set_ylabel("Flow (SLM)")
    axes[1].step(t, state == 1, where="post", label="ATM")
    axes[1].step(t, state == 2, where="post", label="VAC"); axes[1].legend(); axes[1].set_ylabel("Valve ON")
    cumulative = np.r_[0, np.cumsum((q[1:]+q[:-1])/2*np.diff(t)*1000/60)]
    axes[2].plot(t, cumulative); axes[2].set_ylabel("Net flow integral (mL)"); axes[2].set_xlabel("MCU elapsed time (s)")
    for a,b in zip(edges[:-1], edges[1:]):
        x=t[a:b]-t[a]; y=q[a:b]
        # Closed-valve flow after VAC is a real observed rebound/leak candidate,
        # not a proven sensor zero. Use only the initial stable baseline.
        baseline=initial_baseline
        corrected=y-baseline
        v=np.r_[0,np.cumsum((corrected[1:]+corrected[:-1])/2*np.diff(x)*1000/60)]
        seg=dict(state=int(state[a]), start_s=float(t[a]), end_s=float(t[b-1]), duration_s=float(x[-1]),
                 min_slm=float(y.min()), max_slm=float(y.max()), baseline_slm=baseline,
                 signed_volume_ml=float(v[-1]), tail_median_slm=float(np.median(y[x>=max(0,x[-1]-2)])))
        peak=int(np.argmax(np.abs(corrected)))
        seg["peak_time_from_state_s"]=float(x[peak])
        if state[a] in (1,2) and len(x)>20 and abs(v[-1])>10:
            sign=1 if v[-1]>0 else -1
            obs=v*sign
            def model(p):
                return p[0]*(1-np.exp(-np.maximum(x-p[2],0)/p[1]))
            fit=least_squares(lambda p:model(p)-obs,[max(obs[-1],1),2,.02],bounds=([1,.03,0],[10000,100,2]),loss="soft_l1",f_scale=5)
            pred=model(fit.x)
            seg.update(fitted_volume_ml=float(fit.x[0]),tau_s=float(fit.x[1]),apparent_delay_s=float(fit.x[2]),
                       cumulative_r2=float(1-np.sum((obs-pred)**2)/np.sum((obs-obs.mean())**2)),
                       cumulative_rmse_ml=float(np.sqrt(np.mean((obs-pred)**2))))
        result["segments"].append(seg)
    for ax in axes: ax.grid(alpha=.3)
    fig.suptitle(path.name+" — VAC / ATM cycle, PPV "+str(result["ppv_v"])+" V")
    fig.tight_layout(); fig.savefig(OUT/(path.stem+"_cycle.png"),dpi=160); plt.close(fig)
    return result

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    results=[analyze(ROOT/"data"/name) for name in ("0.csv","0.1.csv")]
    output=json.dumps(results,ensure_ascii=False,indent=2)
    (OUT/"results.json").write_text(output,encoding="utf-8")
    print(output)
