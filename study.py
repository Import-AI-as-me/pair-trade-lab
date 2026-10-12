"""A small pair trade test. Run: python study.py

No orders are sent. All prices come from saved public files.
"""
import argparse
import hashlib
import json
import math
import platform
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
LEGS = ['BTC', 'ETH']


def fit(data, lookback=720, q_scale=1e-4, train_end='2023-01-01'):
    """Fit on the first part. Later rows only use data seen so far."""
    x = np.log(data.BTC_close)
    y = np.log(data.ETH_close)
    x = x - x.iloc[0]
    y = y - y.iloc[0]
    cut = pd.Timestamp(train_end, tz='UTC')
    old = data.index < cut
    xx = np.column_stack([np.ones(old.sum()), x.loc[old]])
    theta = np.linalg.lstsq(xx, y.loc[old].to_numpy(), rcond=None)[0]
    err = y.loc[old].to_numpy() - xx @ theta
    noise = float(err @ err / (len(err)-2))
    cov = noise * np.linalg.inv(xx.T @ xx)
    step = noise*q_scale*np.diag([1., 1./x.loc[old].var()])
    beta = float(theta[1])
    spread = y-beta*x
    past = spread.shift(1).rolling(lookback, min_periods=lookback)
    z = (spread-past.mean()) / past.std()

    mx = x.rolling(lookback).mean()
    my = y.rolling(lookback).mean()
    vx = x.pow(2).rolling(lookback).mean()-mx.pow(2)
    cv = (x*y).rolling(lookback).mean()-mx*my
    out = pd.DataFrame({'z':z, 'fixed':beta, 'roll':cv/vx}, index=data.index)
    out['kalman'] = np.nan
    out['beta_sd'] = np.nan
    k_beta = np.full(len(data), np.nan)
    k_sd = k_beta.copy()
    eye = np.eye(2)
    for i in np.flatnonzero(~old):
        h = np.array([1., x.iloc[i]])
        prior = cov + step
        gain = prior@h / (h@prior@h + noise)
        theta = theta + gain*(y.iloc[i]-h@theta)
        a = eye - np.outer(gain,h)
        cov = a@prior@a.T + noise*np.outer(gain,gain)
        cov = (cov+cov.T)/2
        k_beta[i] = theta[1]
        k_sd[i] = math.sqrt(max(0.,cov[1,1]))
    out['kalman'] = k_beta
    out['beta_sd'] = k_sd

    s = spread.loc[old].to_numpy()
    ar = np.linalg.lstsq(np.column_stack([np.ones(len(s)-1),s[:-1]]),s[1:],rcond=None)[0]
    phi = float(ar[1])
    info = {'fixed_beta':beta, 'train_noise':noise, 'train_spread_ar1':phi,
            'train_half_life_hours':-math.log(2)/math.log(phi) if 0<phi<1 else None,
            'ar_note':'A train fit is not proof of a stable spread in later years.'}
    return out, info


def targets(features, method, entry, start, end, size=1., lag=2,
            gate=0.25, max_hold=72, trade_hours=(1,5,9,13,17,21)):
    """NaN means keep the same coin units. Zero means close the trade."""
    seen = features.shift(lag)
    seen = seen.loc[(seen.index>=pd.Timestamp(start,tz='UTC')) &
                    (seen.index<pd.Timestamp(end,tz='UTC'))]
    weights = pd.DataFrame(np.nan,index=seen.index,columns=LEGS)
    state = 0
    since = None
    hedge = 'kalman' if method in ('gate','small') else method
    for t,row in seen.iterrows():
        if t.hour not in trade_hours:
            continue
        z = row.z
        b = row[hedge]
        if not np.isfinite(z) or not np.isfinite(b):
            state = 0
        elif state:
            if state*z>=0 or (t-since).total_seconds()>=max_hold*3600:
                state = 0
        elif abs(z)>=entry:
            ok = method!='gate' or row.beta_sd/max(abs(b),1e-12)<=gate
            if ok:
                state = -1 if z>0 else 1
                since = t
        b = float(np.clip(b,0.1,3.)) if np.isfinite(b) else 1.
        weights.loc[t] = [-state*size*b/(1+b), state*size/(1+b)]
    return weights


def book(prices, funding, marks, weights, cost_bps=5., close_last=True):
    """Use coin units for cash PnL. Funding uses the old units first.

    Start with 1 USDT. Target size uses equity just before the trade cost.
    Mark prices are hourly open proxies for the funding fee price.
    """
    if not prices.index.equals(weights.index):
        raise ValueError('Price and trade rows must match.')
    if list(weights.columns)!=LEGS:
        raise ValueError('Trade leg names must be BTC then ETH.')
    for frame in (prices,marks,funding):
        if not frame.index.equals(prices.index) or list(frame.columns)!=LEGS:
            raise ValueError('Bad rows or leg names.')
        if not np.isfinite(frame.to_numpy()).all():
            raise ValueError('Data has a gap or bad value.')
    if (prices.to_numpy()<=0).any() or (marks.to_numpy()<=0).any():
        raise ValueError('Prices must be above zero.')
    v = prices.to_numpy(float)
    f = funding.to_numpy(float)
    m = marks.to_numpy(float)
    w = weights.to_numpy(float)
    q = np.zeros(2)
    cash = 1.
    rows = []
    for i,t in enumerate(prices.index):
        before = cash
        pnl = float(q@(v[i]-v[i-1])) if i else 0.
        pay = float(-q@(m[i]*f[i]))
        cash += pnl+pay
        if cash<=0:
            raise ValueError('The test lost all its cash.')
        turn = 0.
        if (i==len(v)-1) and close_last:
            next_q = np.zeros(2)
        elif np.isfinite(w[i]).all():
            next_q = w[i]*cash/v[i]
        elif np.isnan(w[i]).all():
            next_q = q.copy()
        else:
            raise ValueError('Each trade row needs two weights or two NaNs.')
        turn = float(np.abs(next_q-q)@v[i])
        fee = turn*cost_bps/10000.
        cash -= fee
        if cash<=0:
            raise ValueError('The trade cost used all the cash.')
        q = next_q
        gross = float(np.abs(q)@v[i]/cash)
        net = float(q@v[i]/cash)
        rows.append((cash,pnl,pay,fee,turn,(cash/before-1),gross,net,q[0],q[1]))
    return pd.DataFrame(rows,index=prices.index,columns=[
        'equity','price_pnl','funding_pnl','cost','turnover','return','gross','net','BTC_units','ETH_units'])


def run_one(data, features, method, entry, start, end, cost=5., size=1., lag=2):
    w = targets(features,method,entry,start,end,size=size,lag=lag)
    use = data.loc[w.index]
    def take(part):
        ans = use[[s+'_'+part for s in LEGS]].copy()
        ans.columns = LEGS
        return ans
    return book(take('open'),take('funding'),take('mark_open'),w,cost),w


def daily(log):
    eq = log.equity.resample('1D').last()
    ret = eq.pct_change()
    ret.iloc[0] = eq.iloc[0]-1
    return ret


def score(log):
    d = daily(log)
    vol = float(d.std(ddof=1))
    peak = np.maximum.accumulate(np.r_[1.,log.equity.to_numpy()])[1:]
    return {'net_return':float(log.equity.iloc[-1]-1),
            'sharpe':float(d.mean()/vol*np.sqrt(365)) if vol>0 else 0.,
            'max_drawdown':float((log.equity.to_numpy()/peak-1).min()),
            'price_pnl':float(log.price_pnl.sum()), 'funding_pnl':float(log.funding_pnl.sum()),
            'cost':float(log.cost.sum()), 'turnover':float(log.turnover.sum()),
            'mean_gross':float(log.gross.mean()),
            'mean_net':float(log.net.mean()),
            'active_share':float((log.gross>1e-10).mean()),
            'rebalance_count':int((log.turnover>1e-12).sum())}


def block_ci(a,b,block=14,draws=2000,seed=832):
    """Paired day blocks. These bounds are a sample check, not a promise."""
    diff = np.asarray(a)-np.asarray(b)
    n = len(diff)
    rng = np.random.default_rng(seed)
    vals = np.empty(draws)
    chunks = int(np.ceil(n/block))
    for i in range(draws):
        ix = (rng.integers(0,n,chunks)[:,None]+np.arange(block))%n
        vals[i] = diff[ix.ravel()[:n]].mean()*365
    return {'mean_day_difference':float(diff.mean()),
            'annual_mean_difference':float(diff.mean()*365),
            'lower':float(np.quantile(vals,.025)), 'upper':float(np.quantile(vals,.975)),
            'block_days':block,'draws':draws,'seed':seed,
            'note':'Annual mean return difference, not compound return or alpha. The interval holds the fitted choices fixed.'}


def write_plots(logs, rows, out):
    plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False})
    colors = {'fixed':'#596579','roll':'#bc8735','kalman':'#19768a','gate':'#bb5968','small':'#80815a'}
    names = {'fixed':'Fixed hedge','roll':'Rolling hedge','kalman':'Kalman hedge','gate':'Kalman + gate','small':'Kalman, small size'}
    fig,axes = plt.subplots(2,1,figsize=(10,7),sharex=True)
    for name,log in logs.items():
        eq=log.equity.resample('1D').last()
        axes[0].plot(eq.index,eq,label=names[name],color=colors[name],lw=1.5)
        peak=np.maximum.accumulate(np.r_[1.,eq.to_numpy()])[1:]
        axes[1].plot(eq.index,100*(eq.to_numpy()/peak-1),color=colors[name],lw=1.)
    axes[0].axhline(1,color='#888',ls=':',lw=1)
    axes[0].set_ylabel('Cash from 1 USDT')
    axes[0].legend(ncol=2,fontsize=9)
    axes[1].set_ylabel('Daily sampled drawdown (%)')
    axes[0].set_title('Same signal, five hedge rules | test only')
    for ax in axes:ax.grid(alpha=.15)
    fig.text(.5,.01,'BTC / ETH perpetual pair | 5 bp per traded leg | funding uses a mark price proxy',ha='center',fontsize=9)
    fig.tight_layout(rect=[0,.035,1,1]);fig.savefig(out/'test.png',dpi=170);plt.close(fig)
    fig,ax=plt.subplots(figsize=(9,4.6))
    for name in colors:
        part=rows[(rows.period=='test')&(rows.method==name)&(rows.lag==2)]
        ax.plot(part.cost_bps,part.net_return*100,'o-',label=names[name],color=colors[name])
    ax.axhline(0,color='#888',lw=1)
    ax.set(xlabel='Cost per traded leg (bp)',ylabel='Net return (%)',title='How much does the cost matter?')
    ax.set_xticks([0,5,10]);ax.legend(ncol=2,fontsize=9);ax.grid(alpha=.15)
    fig.tight_layout();fig.savefig(out/'cost.png',dpi=170);plt.close(fig)


def main():
    p = argparse.ArgumentParser(description='Test a BTC / ETH pair hedge.')
    p.add_argument('--out',default='run')
    p.add_argument('--cache',default='cache')
    args = p.parse_args()
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        p.error('Use a new empty output folder.')
    out.mkdir(parents=True,exist_ok=True)
    from get_data import build_cache
    data,events,checks,manifest = build_cache(args.cache,'2021-01','2025-12')
    expected = pd.date_range('2021-01-01','2026-01-01',freq='h',inclusive='left',tz='UTC')
    if not data.index.equals(expected):
        raise ValueError('The full hourly grid is needed. Do not fill a data gap.')
    features,fit_info = fit(data)
    picks=[]
    for e in (1.,1.5,2.):
        log,_=run_one(data,features,'fixed',e,'2023-01-01','2024-01-01')
        picks.append({'entry':e,**score(log)})
    chosen=max(picks,key=lambda r:(r['sharpe'],r['entry']))['entry']
    gate_log,_=run_one(data,features,'gate',chosen,'2023-01-01','2024-01-01')
    k_log,_=run_one(data,features,'kalman',chosen,'2023-01-01','2024-01-01')
    size=min(1.,gate_log.gross.mean()/k_log.gross.mean()) if k_log.gross.mean()>0 else 0.
    selection={'entry':chosen,'small_size':float(size),'pick_results':picks,'fit':fit_info}
    (out/'choice.json').write_text(json.dumps(selection,indent=2)+'\n')
    rows=[];test_logs={};day_frame={}
    for period,start,end in [('pick','2023-01-01','2024-01-01'),('test','2024-01-01','2026-01-01')]:
        for method in ('fixed','roll','kalman','gate','small'):
            for cost in (0.,5.,10.):
                log,w=run_one(data,features,method,chosen,start,end,cost,
                              size=float(size) if method=='small' else 1.)
                rows.append({'period':period,'method':method,'cost_bps':cost,'lag':2,**score(log)})
                if period=='test' and cost==5:
                    test_logs[method]=log
                    day_frame[method]=daily(log)
                    log.resample('1D').agg({'equity':'last','price_pnl':'sum','funding_pnl':'sum',
                        'cost':'sum','turnover':'sum','gross':'mean','net':'mean'}).to_csv(out/(method+'-days.csv'),index_label='day')
                    orders=w.dropna().copy()
                    # The last open closes all units, even off the trade clock.
                    orders.loc[log.index[-1]] = [0., 0.]
                    orders=orders.sort_index()
                    orders['BTC_units']=log.loc[orders.index,'BTC_units']
                    orders['ETH_units']=log.loc[orders.index,'ETH_units']
                    orders['turnover']=log.loc[orders.index,'turnover']
                    orders['cost']=log.loc[orders.index,'cost']
                    orders.to_csv(out/(method+'-orders.csv'),index_label='time')
        print(period+' done',flush=True)
    # Keep all rules fixed; add one more hour between the close and the trade.
    for method in ('fixed','roll','kalman','gate','small'):
        log,_=run_one(data,features,method,chosen,'2024-01-01','2026-01-01',5,
                      size=float(size) if method=='small' else 1.,lag=3)
        rows.append({'period':'test','method':method,'cost_bps':5.,'lag':3,**score(log)})
    table=pd.DataFrame(rows)
    table.to_csv(out/'scores.csv',index=False)
    days=pd.DataFrame(day_frame)
    days.to_csv(out/'day-returns.csv',index_label='day')
    ci={str(n):block_ci(days.kalman,days.fixed,n) for n in (7,14,28)}
    years=[]
    for method,log in test_logs.items():
        for year in (2024,2025):
            r=daily(log).loc[str(year)]
            years.append({'method':method,'year':year,'net_return':float((1+r).prod()-1),
                          'sharpe':float(r.mean()/r.std()*np.sqrt(365)) if r.std()>0 else 0.})
    pd.DataFrame(years).to_csv(out/'years.csv',index=False)
    report={'plan_sha256':hashlib.sha256((HERE/'plan.json').read_bytes()).hexdigest(),
            'code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'environment':{'python':platform.python_version(),'numpy':np.__version__,
                           'pandas':pd.__version__,'matplotlib':matplotlib.__version__},
            'selection':selection,'main_pair_check':ci,'rows':rows,
            'feature_notes':{'beta_clip_counts':{k:int(((features.loc['2023':,k]<.1)|(features.loc['2023':,k]>3)).sum()) for k in ('fixed','roll','kalman')},
                             'test_rows':len(test_logs['fixed']),'test_days':len(days),
                             'end':'2025-12-31 23:00 UTC; close all units at that open and pay the cost.'},
            'limits':['One chosen pair and venue. No claim of cointegration.',
                      'Funding uses hourly mark open prices, not exact fee marks.',
                      'Cost rates are cases, not real fills. No mark-to-market margin or liquidation model.',
                      'A sample test is not a live trade result. No cash yield is added.',
                      'Bootstrap bounds do not cover all fit and model choice error.']}
    (out/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    write_plots(test_logs,table,out)
    print(table[(table.period=='test')&(table.cost_bps==5)&(table.lag==2)][['method','net_return','sharpe','max_drawdown','mean_gross']].to_string(index=False))
    print('Saved to '+str(out))


if __name__=='__main__':
    main()
