# ruff: noqa: INP001, T201  (a standalone script that prints its result)
"""Reproducer: under interpolation_mode "previous", a step down given as two points at one time is lost.

Run from a HAEO checkout (v0.5.0rc3, or main at 31c87cb):

    PYTHONPATH=. python 2026-10-27-previous-mode-step-repro.py

Expected (both modes): 25 for the first two hours, 0 for hours 2-10, then 25.
Actual: linear is right; previous reads 25 throughout.
"""

import numpy as np

from custom_components.haeo.core.data.loader.extractors.haeo import InterpolationMode, _apply_interpolation_mode
from custom_components.haeo.core.data.loader.extractors.utils.separate_timestamps import separate_duplicate_timestamps
from custom_components.haeo.core.data.util.forecast_combiner import fuse_sensor_payloads

H = 3600
# An hour-aligned epoch second (2026-10-26 12:00 UTC). Live timestamps are this size; near 0 the
# 24-hour cycling in the fuser loses the one-ulp separation between the two points of a step.
T0 = 1_793_016_000

# 25 until t = 2 h, 0 until t = 10 h, then 25. Each step is a pair of points at the same time.
points = [(T0 + t * H, v) for t, v in [(0, 25.0), (2, 25.0), (2, 0.0), (10, 0.0), (10, 25.0), (12, 25.0)]]
horizon = [float(T0 + t * H) for t in range(13)]  # 12 hourly periods

for mode in (InterpolationMode.LINEAR, InterpolationMode.PREVIOUS):
    # extract() sorts by time with a stable sort, then applies the mode
    parsed = _apply_interpolation_mode(sorted(points, key=lambda p: p[0]), mode)
    ts = np.array([p[0] for p in parsed], dtype=float)
    vs = np.array([p[1] for p in parsed], dtype=float)
    series = separate_duplicate_timestamps(ts, vs)
    fused = fuse_sensor_payloads(None, [series], horizon, boundaries=False)
    print(f"{mode.value:8s}", [round(v, 1) for v in fused])
