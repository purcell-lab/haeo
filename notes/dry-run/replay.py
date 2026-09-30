"""Offline replay of a HAEO diagnostic, with policies, and optional config variants.

Run from the repo root:  uv run python notes/dry-run/replay.py <diag.json> [variant ...]
"""
import copy, json, sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, '.')
from tools.diag import (DiagnosticsData, DiagnosticsStateProvider, normalize_participant_config_for_diag,
                        infer_interval_starts_from_outputs)
from custom_components.haeo.core.adapters.registry import ELEMENT_TYPES, collect_model_elements
from custom_components.haeo.core.adapters.policy_compilation import compile_policies
from custom_components.haeo.coordinator.network import _collect_policy_rules
from custom_components.haeo.core.data.loader.config_loader import load_element_config
from custom_components.haeo.core.model import Network

TZ = ZoneInfo('Australia/Brisbane')


def rule(cfg, name):
    return next(r for r in cfg['participants']['Policies']['rules'] if r['name'] == name)


def const(v):
    return {'type': 'constant', 'value': v}


VARIANTS = {
    'base': lambda c: None,
    'ev2grid+5': lambda c: rule(c, 'EVs to Grid').update(price=const(0.05)),
    'ev2grid0': lambda c: rule(c, 'EVs to Grid').update(price=const(0.0)),
    'miner_off': lambda c: rule(c, 'Miner consumption cost').update(price=const(0.0)),
    'minerneg': lambda c: rule(c, 'Miner consumption cost').update(price=const(-0.13608347)),
    'minerneg_ev5': lambda c: (rule(c, 'Miner consumption cost').update(price=const(-0.13608347)),
                               rule(c, 'EVs to Grid').update(price=const(0.05))),
}


def solve(diag_path, variant):
    diag = DiagnosticsData.from_file(Path(diag_path))
    cfg = copy.deepcopy(diag.config)
    for v in variant.split('+'):
        pass
    VARIANTS[variant](cfg)
    starts = infer_interval_starts_from_outputs(diag.outputs, cfg)
    steps = [round(starts[i + 1] - starts[i]) for i in range(len(starts) - 1)]
    steps.append(steps[-1])
    times = (*starts, starts[-1] + steps[-1])
    if '_times' in cfg:
        times = tuple(cfg.pop('_times'))
        steps = [round(times[i + 1] - times[i]) for i in range(len(times) - 1)]
    inputs = diag.inputs
    if '_scale' in cfg:
        v = cfg.pop('_scale')
        inputs = copy.deepcopy(inputs)
        for i in inputs:
            if i['entity_id'] == 'sensor.haeo_ev_charge_price':
                i['state'] = str(v) if float(i['state']) < 0 else i['state']
                for q in i['attributes'].get('forecast', []):
                    if q['value'] < 0: q['value'] = v
    if '_timing' in cfg:
        tv = cfg.pop('_timing')
        inputs = copy.deepcopy(inputs)
        for i in inputs:
            if i['entity_id'] == 'sensor.haeo_grid_charge_timing_price':
                pts = [q for q in i['attributes']['forecast'] if not q['time'].startswith('2026-09-29')]
                pts += [{'time': '2026-09-29T09:00:00+10:00', 'value': 0.0}, {'time': '2026-09-29T11:00:00+10:00', 'value': tv},
                        {'time': '2026-09-29T15:00:00+10:00', 'value': 0.0}]
                i['attributes']['forecast'] = sorted(pts, key=lambda q: q['time'])
    if '_extra_inputs' in cfg:
        inputs = list(inputs) + cfg.pop('_extra_inputs')
    if cfg.pop('_ev1nodis', False):
        inputs = copy.deepcopy(inputs)
        for i in inputs:
            if i['entity_id'] == 'sensor.ev1_max_discharge_power_forecast':
                i['state'] = '0'
                for q in i['attributes']['forecast']: q['value'] = 0
    if cfg.pop('_clean', False):
        inputs = copy.deepcopy(inputs)
        for i in inputs:
            e = i['entity_id']
            if e in ('sensor.ev1_max_discharge_power_forecast', 'sensor.ev2_max_discharge_power_forecast',
                     'sensor.ev2_max_charge_power_forecast', 'sensor.ev2_max_charge_power_total_forecast'):
                i['state'] = '0'
                for q in i['attributes'].get('forecast', []): q['value'] = 0
            if e == 'sensor.haeo_ev1_min_soc_target':
                i['attributes']['forecast'] = [
                    {'time': '2026-09-29T09:00:00+10:00', 'value': 20}, {'time': '2026-09-29T15:00:00+10:00', 'value': 90},
                    {'time': '2026-09-29T16:00:00+10:00', 'value': 20}, {'time': '2026-10-07T09:00:00+10:00', 'value': 20}]
                i['attributes']['interpolation_mode'] = 'previous'
    if '_overrides' in cfg:
        ov = cfg.pop('_overrides')
        inputs = copy.deepcopy(inputs)
        for i in inputs:
            if i['entity_id'] in ov:
                st, fc = ov[i['entity_id']]
                i['state'] = str(st)
                if fc is not None:
                    i['attributes']['forecast'] = fc
                    i['attributes']['interpolation_mode'] = 'previous'
    sp = DiagnosticsStateProvider(inputs)
    parts = {}
    for n, ec in cfg['participants'].items():
        parts[n] = load_element_config(n, normalize_participant_config_for_diag(ec), sp, times)
    elems = list(collect_model_elements(parts))
    res = compile_policies(elems, _collect_policy_rules(parts))
    net = Network(name='replay', periods=np.asarray(steps, dtype=float) / 3600)
    for e in res['elements']:
        net.add(e)
    cost = net.optimize()
    mo = {n: e.outputs() for n, e in net.elements.items()}
    out = {}
    for n, ec in parts.items():
        t = ec['element_type']
        if t == 'policy':
            continue
        try:
            for dev, o in ELEMENT_TYPES[t].outputs(name=n, model_outputs=mo, config=ec, periods=net.periods).items():
                for k, d in o.items():
                    out[f'{n}:{k}'] = np.asarray(d.values, dtype=float)
        except Exception as ex:  # noqa: BLE001
            out[f'{n}:error'] = str(ex)
    return cost, times, np.asarray(steps, dtype=float) / 3600, out


COLS = [('Battery:battery_power_discharge', 'bdis'), ('EV1:battery_power_discharge', 'e1dis'),
        ('EV2:battery_power_discharge', 'e2dis'), ('Grid:grid_power_export', 'exp'),
        ('Grid:grid_power_import', 'imp'), ('Miner:load_power', 'miner'), ('Load:load_power', 'load'),
        ('Battery:battery_state_of_charge', 'bsoc'), ('EV1:battery_state_of_charge', 'e1soc')]

if __name__ == '__main__':
    path = sys.argv[1]
    for var in sys.argv[2:] or ['base']:
        cost, times, hrs, out = solve(path, var)
        keys = [k for k, _ in COLS if k in out]
        missing = [k for k, _ in COLS if k not in out]
        print(f'\n=== {var}: cost {cost:.2f}  missing={missing}')
        print('time   ' + ' '.join(f'{a:>6}' for k, a in COLS if k in out))
        night = {a: 0.0 for _, a in COLS}
        for i, t in enumerate(times[:-1]):
            dt = datetime.fromtimestamp(t, TZ)
            if dt.hour == 12 and dt.day == 29:
                break
            vals = {a: out[k][i] for k, a in COLS if k in out}
            for a in ('bdis', 'e1dis', 'e2dis', 'exp', 'imp', 'miner'):
                if a in vals and (dt.hour >= 18 or dt.hour < 7):
                    night[a] += vals[a] * hrs[i]
            if dt.minute in (0, 30):
                print(dt.strftime('%a %H:%M') + ' ' + ' '.join(
                    f'{vals[a] * (100 if "soc" in a else 1):6.1f}' for k, a in COLS if k in out))
        print('kWh 18:00-07:00:', {a: round(v, 1) for a, v in night.items() if v})
