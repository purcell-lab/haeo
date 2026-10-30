# ruff: noqa: INP001, T201  (a standalone script that prints its result)
"""Reproducer: a short forecast that starts with a longer one takes over the longer one's follow-on.

Run from a HAEO checkout (v0.5.0, or main at 705bde0):

    PYTHONPATH=. python 2026-10-30-forecast-group-tie-repro.py

Three forecasts on one input, as a price list might have them:
  near   10 from t = 0 to 16 h   (a live price for the next 16 hours)
  far    20 from t = 16 h to 7 d (a longer-range price that follows on from it)
  adjust 0 from t = 0 to 5 min   (a short additive adjustment, 0 here)

Expected at t = 24 h and 48 h: 20 (far only). Actual when adjust starts with near: 30, because far
joins adjust's group, so near repeats by time of day past its end and is added to far.
"""

import numpy as np

from custom_components.haeo.core.data.util import forecast_combiner as fc

H = 3600
T0 = 1_793_016_000  # an hour-aligned epoch second (2026-10-26 12:00 UTC)


def series(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return [(T0 + t * H, v) for t, v in points]


near = series([(0, 10.0), (16, 10.0)])
far = series([(16, 20.0), (168, 20.0)])
horizon = [float(T0 + t * H) for t in range(0, 169)]

for label, start in (("adjust starts 1 min after near", 1 / 60), ("adjust starts with near", 0.0)):
    adjust = series([(start, 0.0), (5 / 60, 0.0)])
    inputs = [far, near, adjust]
    groups = [[{id(near): "near", id(far): "far", id(adjust): "adjust"}[id(s)] for s in g] for g in fc._group(inputs)]
    fused = np.asarray(fc.fuse_sensor_payloads(None, inputs, horizon, boundaries=False))
    print(f"{label:31s} groups {groups}  t=8h {fused[8]:.0f}  t=24h {fused[24]:.0f}  t=48h {fused[48]:.0f}")
