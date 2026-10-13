"""Conservative usable segment summaries and model compatibility audit."""
import csv
import json
from pathlib import Path
import numpy as np
from scipy.optimize import least_squares
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from analyze_today_hardware import OUT

def main():
    with (OUT/'all_segments.csv').open(encoding='utf-8-sig',newline='') as f:
        rows=list(csv.DictReader(f))
    for r in rows:
        for k in list(r):
            if k!='file': r[k]=float(r[k]) if r[k] else None
        u=r['voltage']; state=r['state']
        r['usable']=False; r['reason']='weak/partial response or insufficient excitation'
        if u==4.2:
            r['reason']='closed observation; no finite time constant'
        elif u==4.15:
            r['reason']='leakage-dominated VAC; ATM initial state uncertain; exclude from gamma'
        elif state==2 and abs(r['signed_volume_ml'])>=800 and r['early_median_slm']>= (30 if u<4 else 8):
            r['usable']=True; r['reason']='substantial expiration; tail leakage included in effective fit'
        elif state==1 and r['signed_volume_ml']>=800 and r['end_last_half_slm']<=1 and r.get('tau_s') and r['duration_s']/r['tau_s']>=3:
            r['usable']=True; r['reason']='substantial inspiration; late tail and fit coverage adequate'
        elif state==1 and r['signed_volume_ml']>=800:
            r['reason']='inspiration truncated before adequate tail; excluded from mapping'
    summaries=[]
    for u in sorted({r['voltage'] for r in rows}):
        for state in (1,2):
            group=[r for r in rows if r['voltage']==u and r['state']==state]
            keep=[r for r in group if r['usable']]
            s=dict(voltage=u,direction='ATM' if state==1 else 'VAC',total=len(group),n=len(keep))
            for key in ('tau_s','tau_skip_0p1_s','fitted_volume_ml','signed_volume_ml','early_median_slm','end_last_half_slm'):
                v=[r[key] for r in keep if r[key] is not None]
                if v:s[key]=dict(median=float(np.median(v)),min=min(v),max=max(v),q25=float(np.quantile(v,.25)),q75=float(np.quantile(v,.75)))
            summaries.append(s)
    models={}
    for state,label in ((1,'inspiration'),(2,'expiration')):
        s=[r for r in summaries if r['direction']==('ATM' if state==1 else 'VAC') and r['n']]
        u=np.array([r['voltage'] for r in s]); tau=np.array([r['tau_s']['median'] for r in s])
        def mapping(p):
            tau0,ud,uc,gamma=p
            a=np.clip((uc-u)/(uc-ud),1e-6,1)
            return tau0/a**gamma
        best=None
        for ud in (3.3,3.7,3.9):
            f=least_squares(lambda p:np.log(mapping(p))-np.log(tau),[np.median(tau[u<=3]),ud,4.18,1],bounds=([.1,2.5,4.151,.05],[10,4.05,4.25,5]),loss='soft_l1',f_scale=.1)
            if best is None or f.cost<best.cost:best=f
        pred=mapping(best.x)
        # The existing firmware has a=1-U/5; fit it separately and show error.
        f0=least_squares(lambda p:np.log(p[0]/(1-u/5)**p[1])-np.log(tau),[tau[0],1],bounds=([.1,.01],[10,5]))
        naive=f0.x[0]/(1-u/5)**f0.x[1]
        models[label]=dict(tau0_s=float(best.x[0]),dead_zone_end_v=float(best.x[1]),closure_v=float(best.x[2]),gamma=float(best.x[3]),
                           log_rmse=float(np.sqrt(np.mean((np.log(pred)-np.log(tau))**2))),
                           naive_tau0_s=float(f0.x[0]),naive_gamma=float(f0.x[1]),naive_log_rmse=float(np.sqrt(np.mean((np.log(naive)-np.log(tau))**2))),
                           voltage=u.tolist(),observed_tau=tau.tolist(),predicted_tau=pred.tolist(),naive_predicted_tau=naive.tolist())
        sensitivity=[]
        for closure in (4.15,4.175,4.2):
            def fixed_mapping(p):
                return p[0]/np.clip((closure-u)/(closure-p[1]),1e-6,1)**p[2]
            f=least_squares(lambda p:np.log(fixed_mapping(p))-np.log(tau),[best.x[0],best.x[1],best.x[3]],bounds=([.1,2.5,.05],[10,4.05,5]),loss='soft_l1',f_scale=.1)
            sensitivity.append(dict(assumed_closure_v=closure,tau0_s=float(f.x[0]),dead_zone_end_v=float(f.x[1]),gamma=float(f.x[2])))
        models[label]['closure_sensitivity']=sensitivity
    results=dict(selection='See reason column; thresholds are dataset-specific, not physical valve position.', summaries=summaries,models=models)
    (OUT/'model_summary.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    with (OUT/'screened_segments.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    print('voltage direction kept/total tau[min,max] fitVolume')
    for s in summaries:
        if s['n']:print(s['voltage'],s['direction'],str(s['n'])+'/'+str(s['total']),[round(s['tau_s'][k],3) for k in ('median','min','max')],round(s['fitted_volume_ml']['median']))
        else:print(s['voltage'],s['direction'],'0/'+str(s['total']))
    print(json.dumps(models,indent=2))
    fig,axes=plt.subplots(1,2,figsize=(12,5))
    for ax,(label,m) in zip(axes,models.items()):
        u=np.array(m['voltage']); obs=np.array(m['observed_tau']);ax.scatter(u,obs,label='Screened voltage medians')
        dense=np.linspace(0,4.13,350);a=np.clip((m['closure_v']-dense)/(m['closure_v']-m['dead_zone_end_v']),1e-6,1)
        ax.plot(dense,m['tau0_s']/a**m['gamma'],label='Piecewise descriptive fit')
        ax.plot(dense,m['naive_tau0_s']/(1-dense/5)**m['naive_gamma'],'--',label='Existing a=1-U/5 fit')
        ax.set(title=label,xlabel='PPV target voltage (V)',ylabel='Effective tau (s)');ax.grid(alpha=.3);ax.legend(fontsize=8)
    fig.tight_layout();fig.savefig(OUT/'model_mapping_comparison.png',dpi=180);plt.close(fig)
    report=['# 今日单肺参数辨识与 GUI 兼容性', '',
            '本报告只分析用户指定的29个文件，以遥测电压而非文件名作分组；全部为OK/NONE。旧报告的参数和日期不作为本批测量值。', '',
            '## 计算方法与筛选', '',
            '使用MCU时间，移除操作事件行，以初始稳定流量作为基线。VAC关闭后的正流量保留，不当作零点。标准体积积分为1000/60×积分Qdt。',
            '累计体积模型：V=A[1-exp(-max(t-d,0)/tau)]，采用稳健最小二乘。A是每段可交换气量的等效外推幅度，不等于实际几何容量；d是描述形状的综合参数，不是独立PPV延迟。',
            '0–3.9 V保留积分>=800mL且0.1–0.5s抽气中位数>=30SLM的VAC段；4–4.1 V降低早期流量筛选至8SLM。阈值基于本批正常与弱响应的明显分离，不能用于推断实际阀芯位置。',
            'ATM保留积分>=800mL、末0.5s流量<=1SLM且持续时间>=3tau的段，减少明显截断造成的终值/tau共线性。高电压下仍不能证明初态一致。',
            '4.15V不按800mL规则判半开：VAC近恒定低流量、拟合tau触及100s上界，单指数不可辨识；ATM可得到描述性tau约33.5–36.6s，但其抽气初态不明确，不用于gamma拟合。',
            '4.2V只记录约4.34s VAC及3.82s ATM，流量约零，是短时夹闭观察，不是无限时长零泄漏证明。', '',
            '## 分电压结果', '',
            '|PPV/V|吸气保留段|tau_in_eff/s|呼气保留段|tau_out_eff/s|',
            '|---:|---:|---:|---:|---:|']
    for voltage in sorted({r['voltage'] for r in summaries}):
        si=next(r for r in summaries if r['voltage']==voltage and r['direction']=='ATM')
        so=next(r for r in summaries if r['voltage']==voltage and r['direction']=='VAC')
        ti=f"{si['tau_s']['median']:.3f}" if si['n'] else '—'
        to=f"{so['tau_s']['median']:.3f}" if so['n'] else '—'
        report.append(f"|{voltage:g}|{si['n']}/{si['total']}|{ti}|{so['n']}/{so['total']}|{to}|")
    report += ['', '## 可以报告的参数', '',
               '- 严格0V基准：tau_in=1.809s（5段，1.697–2.277s）；tau_out=1.372s（12段，1.197–2.014s）。这是GUI基础时间常数的直接参考值。',
               '- 加入全批低电压平台后：吸气0–3.4V各电压中位数的中位数为2.536s（93段）；呼气0–3.9V为1.313s（216段）。全批平台拟合tau_in≈2.51s、tau_out≈1.31s。',
               '- 0V吸气比1–3.4V明显快，不能简单认定平台上所有数据同一初态。连续循环、等待时间、塌缩程度及慢尾都影响等效拟合。1.8s与2.5s是不同估计口径，不能同时称为已确定的唯一tau_in。建议敏感性测试1.8–2.8s，再用固定初态循环确认。',
               '- 低电压吸气平台等效拟合幅度约1538mL；呼气约1705mL。不闭合且高电压幅度变化，不能确定真实容量。若采用约1500mL，只能标为模型有效幅度，不能当作已验证几何容量或安全体积上限。',
               '- flow_sign=+1（ATM正、VAC负）。',
               '- 4.2V短时未观察到流量；4.15V仍有流量，因此目标电压夹闭点在(4.15,4.2]V内，实际模拟电压仍需电气校准。', '',
               '## gamma与电压映射', '',
               '经验形式：a_eff=1（U<=Ud）；a_eff=(Uc-U)/(Uc-Ud)（Ud<U<Uc）；a_eff=0（U>=Uc），tau=tau0/a_eff^gamma。两方向独立拟合。',
               '自由拟合Uc触及下界约4.151V，提示Uc/gamma高度相关，不能把最佳拟合小数当成精确物理参数。固定Uc敏感性如下：', '',
               '|方向|假设Uc/V|tau0/s|Ud/V|gamma|','|---|---:|---:|---:|---:|']
    for label,m in models.items():
        for s in m['closure_sensitivity']:
            report.append(f"|{label}|{s['assumed_closure_v']:.3f}|{s['tau0_s']:.3f}|{s['dead_zone_end_v']:.3f}|{s['gamma']:.3f}|")
    report += ['', '用Uc=4.20V时，吸气描述性gamma≈0.96、Ud≈3.73V；呼气gamma≈1.69、Ud≈3.98V。应优先采用两方向电压—相对导通查表k(U)=tau0/tau(U)，不要取两个gamma的平均值。', '',
               '## 当前GUI/固件不能直接准确承接这些参数', '',
               'single_lung_controller_stm32.ino的writePinchCommand使用U=5(1-opening)，updateSmith和前馈使用opening^gamma，吸呼气共用gamma。此形式预测整个0–5V连续幂律调节，不能描述实测平台和4.2V提前夹闭。',
               '按现有形式拟合全扫描：吸气tau0≈1.492s、gamma≈0.638；呼气tau0≈1.064s、gamma≈0.269。它们只是错误映射下的折中，明显漏掉4.1V慢响应，且呼气gamma低于GUI下限0.5，不推荐使用。',
               'GUI容量输入10–250mL，但实测模型幅度约1500mL；不能把参数直接填进去并期待模型前馈正确。250mL目标潮气量与1500mL模型幅度不是一个字段含义。',
               '配置同时有max_lung_volume_ml；GUI会把它设为capacity，因而扩展输入范围并写入1500还会改动软件体积保护。须区分模型幅度、目标体积与经过验证的保护上限。此分析未改GUI、固件或控制配置。', '',
               '## 未被本批实验识别的参数', '',
               '- Smith feedback delay：没有独立PPV阶跃或执行时间戳，不能从累计拟合d取得；建议Smith保持关闭。',
               '- validated direction dead time：手动切换和实验固件50ms互锁不证明正式系统最小安全等待时间；不能由本批确定。',
               '- precondition time：只能按目标初态验证选取，含泄漏时不能从3tau直接保证排空。正式固件预处理opening=0.35对应3.25V；应结合实测映射重新核对。',
               '- Kp、Ki、Kd、积分限幅、Smith gain和max flow保护：不是此开环扫描直接辨识出的参数，需要闭环测试/单独验证；开启瞬间有单点约130SLM尖峰，不能用该峰值直接校准增益或设置保护。',
               '- 多负压模型：没有负压值/档位记录；参数只按当前负压条件解释，前提是设置保持一致。', '',
               '全部逐段结果见screened_segments.csv；数值和敏感性见model_summary.json；每份文件均生成了周期图。']
    (OUT/'今日参数报告.md').write_text('\n'.join(report)+'\n',encoding='utf-8')

if __name__=='__main__':main()
