"""Inspect recorded hardware runs; command state is not valve-position feedback."""
import csv
import json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT=Path(__file__).resolve().parents[2]/'LungLogic_DualBranch_PID_Simulation_Arduino'
OUT=Path(__file__).resolve().parent/'analysis'/'control_runs'

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    summary=[]
    for path in sorted((ROOT/'output').glob('single_lung_hardware_run_356*.csv')):
        with path.open(newline='',encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
        cfg=json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))['configuration']
        records=[]
        for cycle in sorted({int(r['cycle']) for r in rows}):
            for name in ('INSPIRATION','EXPIRATION'):
                group=[r for r in rows if int(r['cycle'])==cycle and r['state_name']==name]
                if not group:continue
                t=np.array([float(r['time_s']) for r in group]);q=np.array([float(r['flow_slm']) for r in group])
                mask=np.array([int(r['valve_mask']) for r in group]);v=np.array([float(r['volume_ml']) for r in group])
                positive=np.maximum(q,0); negative=np.minimum(q,0)
                on=np.flatnonzero(mask==(1 if name=='INSPIRATION' else 2))
                off=int(on[-1]+1) if len(on) else len(group)
                directional=positive if name=='INSPIRATION' else negative
                tail=float(np.trapezoid(directional[off:],t[off:])*1000/60) if off<len(t) else 0
                records.append(dict(cycle=cycle+1,phase=name,peak_slm=float(q.max() if name=='INSPIRATION' else q.min()),
                    displayed_volume_max_ml=float(v.max()),displayed_volume_end_ml=float(v[-1]),
                    directional_integral_ml=float(np.trapezoid(directional,t)*1000/60),
                    directional_integral_after_close_ml=tail))
        summary.append(dict(file=path.name,configuration=cfg,cycles=records))
        t=np.array([float(r['time_s']) for r in rows]);fig,axes=plt.subplots(3,1,figsize=(12,9),sharex=True)
        for key,label in (('flow_slm','Measured'),('flow_reference_slm','Reference')):
            axes[0].plot(t,[float(r[key]) for r in rows],label=label)
        axes[0].set_ylabel('Flow (SLM)');axes[0].legend()
        axes[1].plot(t,[float(r['command_v']) for r in rows],label='PPV target V')
        mask=np.array([int(r['valve_mask']) for r in rows]);axes[1].step(t,(mask==1).astype(float),label='ATM ON');axes[1].step(t,(mask==2).astype(float),label='VAC ON')
        axes[1].set_ylabel('Target V / valve command');axes[1].legend()
        axes[2].plot(t,[float(r['volume_ml']) for r in rows]);axes[2].axhline(cfg['target_volume_ml'],ls='--',color='k');axes[2].set(ylabel='Displayed integrated volume (mL)',xlabel='MCU run time (s)')
        for ax in axes:ax.grid(alpha=.3)
        fig.suptitle(path.name);fig.tight_layout();fig.savefig(OUT/(path.stem+'.png'),dpi=150);plt.close(fig)
    (OUT/'run_summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding='utf-8')
    report=['# 控制运行复核','', '日志：356599273（6BPM/150mL/1循环）、356639792（6BPM/150mL/5循环）、356755311（10BPM/250mL/5循环，截图对应）。', '',
        '表中积分用50Hz遥测的正/负流量分别进行梯形积分，包含关闭后的测得流量，未扣除泄漏基线。它不等于肺几何体积；与固件100Hz积分会有采样差异。', '',
        '|文件|循环|阶段|峰值SLM|显示终值mL|方向流量积分mL|关阀后积分mL|','|---|---:|---|---:|---:|---:|---:|']
    for run in summary:
        for row in run['cycles']:
            report.append(f"|{run['file']}|{row['cycle']}|{row['phase']}|{row['peak_slm']:.2f}|{row['displayed_volume_end_ml']:.1f}|{row['directional_integral_ml']:.1f}|{row['directional_integral_after_close_ml']:.1f}|")
    report += ['', '## 原因与限度', '',
        '150mL首轮显示终值220.2mL，ATM关闭后的测得正流量积分约64.6mL。10BPM首轮正流量积分约297.4mL，但显示被固件限制在250mL。因此显示250不能证明无超量。',
        '切换时存在短时强流量和关闭后的尾流，符合执行器/阀门/管路动态叠加，但没有阀位或压力反馈，不能独立确定pinch valve动作延迟。命令电压不是实际阀位。',
        '固件达到目标后没有清零opening；方向direction变为0时仍保留opening并按吸气映射重新计算电压。截图末尾opening62.4%并不代表实际阀位，valve_mask为0。',
        '呼气前馈采用当前小体积积分V/tau，但tau来自大体积塌缩实验；强负压小周期下该近似可能偏差很大，不能把跟踪误差全部归结为纯延迟。',
        '建议后续固件处理：关闭方向阀后明确PPV停止目标；切换前预定位并验证等待时间；避免体积截断掩盖超量；根据尾流和小体积工况重新验证停流与前馈。当前本次仅修改User Mode的流量设定方式，没有自动添加未知延迟或启用Smith。', '',
        'User Mode只设BPM、I:E、目标体积和循环数；吸气参考Q=0.06*V/Ti自动计算。SLM仍是实测及控制参考单位，不是独立用户设定。']
    (OUT/'控制运行复核.md').write_text('\n'.join(report)+'\n',encoding='utf-8')
    print('Saved',OUT)

if __name__=='__main__':main()
