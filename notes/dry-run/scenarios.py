"""Golden-rule scenario suite (notes/home-golden-rules.md): today's conditions plus market events, live rules vs
v3 (carbon price from Amber renewables). Golden Rule 1 windows are kept as recorded in the diagnostic.

Run from the repo root:
  uv run python notes/dry-run/scenarios.py <diag.json> [S0,S1,...] [LIVE:tier,V3:tier,LIVE:fine,V3:fine]
<diag.json> is the "data" object of a HAEO diagnostic. The renewables proxy is read from AMBER_DF (the
detailedForecast attribute of sensor.amber_express_amber_general_price saved as {"df": [...]}).
"tier" uses HAEO's own period tiers; "fine" uses 5-minute periods for 24 h (what the plan sees once an event
is inside HAEO's short-period range)."""
import os
import sys, json
from datetime import datetime, timedelta
SP = os.path.dirname(os.path.abspath(__file__)) + '/'
sys.path.insert(0, SP); sys.path.insert(0, '.')
import numpy as np, replay
from replay import rule, const, TZ

DIAG = sys.argv[1]
D = json.load(open(DIAG)); D = D.get('data', D)
T0 = datetime.fromisoformat(D['environment']['optimization_start_time'])
DAY0 = T0.replace(hour=0, minute=0, second=0, microsecond=0)
at_ = lambda d, h, m=0: DAY0 + timedelta(days=d, hours=h, minutes=m)
EF, CARBON = 0.8, 35.0

def series(k): return sorted((datetime.fromisoformat(p['time']).timestamp(), p['value']) for p in D['outputs'][k]['attributes']['forecast'])
PI, PE, PV = series('number.grid_import_price'), series('number.grid_export_price'), series('number.solar_forecast')
def at(ps, t):
    t = t.timestamp() if isinstance(t, datetime) else t
    v = ps[0][1]
    for tt, vv in ps:
        if tt <= t + 1: v = vv
        else: break
    return v

# renewables proxy from Amber detailedForecast, time-of-day fill beyond the horizon
df = json.load(open(os.environ.get('AMBER_DF', SP + 'amber_general_detailed_forecast_2026-09-30.json')))['df']
REN = [(datetime.fromisoformat(x['nem_time']) - timedelta(minutes=x['duration']), x['renewables']) for x in df]
tod = sorted({(t.hour * 60 + t.minute): r for t, r in REN}.items())
def tod_at(m):
    pts = tod + [(tod[0][0] + 1440, tod[0][1])]
    for (m0, r0), (m1, r1) in zip(pts, pts[1:]):
        for mm in (m, m + 1440):
            if m0 <= mm < m1: return r0 + (r1 - r0) * (mm - m0) / (m1 - m0)
    return tod[0][1]
t = REN[-1][0] + timedelta(minutes=30)
while t < T0 + timedelta(days=8):
    REN.append((t, tod_at(t.hour * 60 + t.minute))); t += timedelta(minutes=30)
def ren_base(t):
    v = REN[0][1]
    for tt, r in REN:
        if tt <= t: v = r
        else: break
    return v

# events: (start, end, buy $/kWh, sell $/kWh, renewables %)
SCEN = {
    'S0 today':                 [],
    'S1 MSL 11:00-15:00':       [(at_(0, 11), at_(0, 15), -0.28, -0.30, 98)],
    'S2 LOR 06:00-06:30 (tmrw)':[(at_(1, 6), at_(1, 6, 30), 2.30, 2.00, 30)],
    'S3 LOR 18:00-21:00':       [(at_(0, 18), at_(0, 21), 1.35, 1.00, 12)],
    'S4 spike 13:00-13:05':     [(at_(0, 13), at_(0, 13, 5), 15.5, 15.0, 80)],
}
def ev_of(scen, t):
    for a, b, buy, sell, ren in SCEN[scen]:
        if a <= t < b: return buy, sell, ren
    return None
def ren_at(scen, t):
    e = ev_of(scen, t); return e[2] if e else ren_base(t)
def buy_at(scen, t):
    e = ev_of(scen, t); return e[0] if e else at(PI, t)
def sell_at(scen, t):
    e = ev_of(scen, t); return e[1] if e else at(PE, t)

def step_series(scen, which):
    """Adder input so the fused grid price equals the event price inside each event (0 elsewhere)."""
    pts = []
    for a, b, buy, sell, ren in SCEN[scen]:
        t = a
        while t < b:
            t2 = min(t + timedelta(minutes=5), b)
            base = at(PI if which == 'buy' else PE, t)
            v = (buy if which == 'buy' else sell) - base
            pts += [{'time': (t).isoformat(), 'value': v}, {'time': (t2 - timedelta(seconds=1)).isoformat(), 'value': v}]
            t = t2
        pts = [{'time': (a - timedelta(seconds=1)).isoformat(), 'value': 0.0}] + pts + [{'time': b.isoformat(), 'value': 0.0}]
    return pts

def carbon_pts(scen):
    pts, t = [], T0 - timedelta(minutes=5)
    while t < T0 + timedelta(days=8):
        pts.append({'time': t.isoformat(), 'value': round(CARBON / 1000 * EF * (1 - ren_at(scen, t) / 100), 5)}); t += timedelta(minutes=5)
    return pts

def fine_times():
    ts, t = [], T0
    while t < T0 + timedelta(hours=24):
        ts.append(t.timestamp()); t = (t + timedelta(minutes=5)).replace(second=0, microsecond=0) if not ts[1:] else t + timedelta(minutes=5)
    while t <= T0 + timedelta(days=7):
        ts.append(t.timestamp()); t += timedelta(hours=1)
    return ts

def make(scen, rules, grid):
    def f(c):
        rule(c, 'Miner consumption cost').update(price=const(-0.13608347))
        extra = []
        if SCEN[scen]:
            gp = c['participants']['Grid']['pricing']
            gp['price_source_target']['value'] = list(gp['price_source_target']['value']) + ['sensor.scen_buy_adder']
            gp['price_target_source']['value'] = list(gp['price_target_source']['value']) + ['sensor.scen_sell_adder']
            for n, w in (('sensor.scen_buy_adder', 'buy'), ('sensor.scen_sell_adder', 'sell')):
                extra.append({'entity_id': n, 'state': '0.0', 'attributes': {'forecast': step_series(scen, w), 'unit_of_measurement': '$/kWh'}})
        if rules == 'V3':
            cp = carbon_pts(scen); neg = [{'time': q['time'], 'value': -q['value']} for q in cp]
            c['participants']['Policies']['rules'] += [
                {'enabled': True, 'name': 'Grid carbon cost', 'price': {'type': 'entity', 'value': ['sensor.haeo_grid_carbon_price']}, 'source': ['Grid']},
                {'enabled': True, 'name': 'Grid carbon credit', 'price': {'type': 'entity', 'value': ['sensor.haeo_grid_carbon_credit']}, 'target': ['Grid']}]
            extra += [{'entity_id': 'sensor.haeo_grid_carbon_price', 'state': str(cp[0]['value']), 'attributes': {'forecast': cp, 'interpolation_mode': 'previous'}},
                      {'entity_id': 'sensor.haeo_grid_carbon_credit', 'state': str(neg[0]['value']), 'attributes': {'forecast': neg, 'interpolation_mode': 'previous'}}]
        if extra: c['_extra_inputs'] = extra
        if grid == 'fine': c['_times'] = fine_times()
    return f

def run(scen, rules, grid):
    key = f'{scen}|{rules}|{grid}'; replay.VARIANTS[key] = make(scen, rules, grid)
    cost, times, hrs, out = replay.solve(DIAG, key)
    g = lambda k: out.get(k, np.zeros(len(hrs)))
    dt = [datetime.fromtimestamp(x, TZ) for x in times[:-1]]
    h24 = [i for i in range(len(hrs)) if times[i] < (T0 + timedelta(hours=24)).timestamp()]
    E = lambda k, idx: float(sum(g(k)[i] * hrs[i] for i in idx))
    imp, exp = g('Grid:grid_power_import'), g('Grid:grid_power_export')
    # event window rows
    rows = []
    for a, b, *_ in SCEN[scen] or [(at_(0, 16), at_(0, 21), 0, 0, 0)]:
        idx = [i for i in range(len(hrs)) if dt[i] < b and dt[i] + timedelta(hours=hrs[i]) > a]
        # energy inside the event share of each period
        w = [max(0.0, (min(b, dt[i] + timedelta(hours=hrs[i])) - max(a, dt[i])).total_seconds() / 3600) for i in idx]
        ee = lambda k: float(sum(g(k)[i] * wi for i, wi in zip(idx, w)))
        prd = f"{min(hrs[i] for i in idx) * 60:.0f}-{max(hrs[i] for i in idx) * 60:.0f} min" if idx else '-'
        rows.append(dict(win=f"{a:%d %H:%M}-{b:%H:%M}", prd=prd, imp=ee('Grid:grid_power_import'), exp=ee('Grid:grid_power_export'),
                         bchg=ee('Battery:battery_power_charge'), bdis=ee('Battery:battery_power_discharge'),
                         evc=ee('EV1:battery_power_charge') + ee('EV2:battery_power_charge'), evd=ee('EV1:battery_power_discharge') + ee('EV2:battery_power_discharge'),
                         pool=ee('Pool Heater:load_power'), miner=ee('Miner:load_power'),
                         curt=float(sum(max(0.0, at(PV, times[i]) - g('Solar:solar_power')[i]) * wi for i, wi in zip(idx, w))),
                         bsoc=g('Battery:battery_state_of_charge')[idx[-1] + 1] * 100 if idx else float('nan'),
                         e1=g('EV1:battery_state_of_charge')[idx[-1] + 1] * 100 if idx else float('nan'),
                         e2=g('EV2:battery_state_of_charge')[idx[-1] + 1] * 100 if idx else float('nan')))
    def avg(fn, i):
        a = dt[i]; n = max(1, int(round(hrs[i] * 60)))
        return sum(fn(a + timedelta(minutes=m)) for m in range(n)) / n
    B = [avg(lambda t: buy_at(scen, t), i) for i in h24]
    S = [avg(lambda t: sell_at(scen, t), i) for i in h24]
    R = [avg(lambda t: ren_at(scen, t), i) for i in h24]
    cash = sum((imp[i] * B[j] - exp[i] * S[j]) * hrs[i] for j, i in enumerate(h24))
    co2 = sum((imp[i] - exp[i]) * EF * (1 - R[j] / 100) * hrs[i] for j, i in enumerate(h24))
    ov = sum(1 for i in range(len(hrs)) for n in '12' if g(f'EV{n}:battery_power_charge')[i] > 0.1 and g(f'EV{n}:battery_power_discharge')[i] > 0.1)
    band = lambda arr, P, lo, hi: sum(arr[i] * hrs[i] for j, i in enumerate(h24) if lo <= P[j] < hi)
    hi_imp = band(imp, B, 0.25, 99); mid_imp = band(imp, B, 0.05, 0.25); low_imp = band(imp, B, -99, 0.05)
    lo_exp = band(exp, S, -99, 0.05); mid_exp = band(exp, S, 0.05, 0.15); hi_exp = band(exp, S, 0.15, 99)
    lows = [f"{dt[i]:%d %H:%M} {exp[i]:.1f}kW@{100*S[j]:.1f}c" for j, i in enumerate(h24) if exp[i] > 0.3 and S[j] < 0.05][:6]
    return dict(cash=-cash, co2=co2, ov=ov, imp=(low_imp, mid_imp, hi_imp), exp=(lo_exp, mid_exp, hi_exp), lows=lows, rows=rows)

if __name__ == '__main__':
    scens = [s for s in SCEN if s[:2] in sys.argv[2].split(',')] if len(sys.argv) > 2 else list(SCEN)
    combos = [c.split(':') for c in (sys.argv[3].split(',') if len(sys.argv) > 3 else ['LIVE:tier', 'V3:tier'])]
    res = {}
    for s in scens:
        for r, gr in combos:
            x = run(s, r, gr); res[f'{s}|{r}|{gr}'] = x
            for row in x['rows']:
                print(f"{s:27s} {r:4s} {gr:4s} | {row['win']} [{row['prd']}] imp {row['imp']:5.1f} exp {row['exp']:5.1f} | batt +{row['bchg']:4.1f}/-{row['bdis']:4.1f} EV +{row['evc']:5.1f}/-{row['evd']:4.1f} | pool {row['pool']:4.1f} miner {row['miner']:3.1f} curtail {row['curt']:4.1f} | end SoC B {row['bsoc']:5.1f} EV1 {row['e1']:5.1f} EV2 {row['e2']:5.1f}"
                      f" || 24h net ${x['cash']:6.2f} CO2e {x['co2']:6.1f} kg | import <5c/5-25c/>25c {x['imp'][0]:5.1f}/{x['imp'][1]:4.1f}/{x['imp'][2]:4.1f} | export <5c/5-15c/>15c {x['exp'][0]:4.1f}/{x['exp'][1]:4.1f}/{x['exp'][2]:5.1f} | overlaps {x['ov']} | low-price exports {x['lows']}", flush=True)
    json.dump(res, open(os.environ.get('SCEN_OUT', 'scen_results.json'), 'w'), indent=1, default=str)
