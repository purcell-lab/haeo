# Draft upstream issue: `interpolation_mode: previous` drops a step down given as two points at one time

**For:** hass-energy/haeo. **Status:** draft, not posted. Search the upstream issues for duplicates
before posting.
**Files with this draft:** `2026-10-27-previous-mode-step-repro.py` (reproducer) and
`2026-10-27-previous-mode-step-fix.diff` (proposed patch).

---

**Title:** `interpolation_mode: previous` loses a step down when the step is two points at one time

### System Health details

- HAEO 0.5.0rc3 (a fork build of upstream v0.5.0rc3, 07949db, with no changes to forecast loading).
  The same code is on `main` at 31c87cb (26 Oct 2026).
- Home Assistant 2026.10.4, Python 3.13.

### Describe the issue

A forecast sensor that sets `interpolation_mode: previous` and writes each step as two points at
the same time loses every step **down**. The higher value holds until the next point.

For example, take a power limit of 25 kW until 22:00, 0 kW until 06:00, then 25 kW, written as:

```yaml
forecast:
  - {time: "…T21:00:00+10:00", value: 25}
  - {time: "…T22:00:00+10:00", value: 25}
  - {time: "…T22:00:00+10:00", value: 0}
  - {time: "…T06:00:00+10:00", value: 0}
  - {time: "…T06:00:00+10:00", value: 25}
interpolation_mode: previous
```

HAEO loads this as 25 kW through the night. The 0 kW block never reaches the optimizer.

**Cause.** `extract()` sorts the points by time with a stable sort, so 25 comes before 0. But for
`PREVIOUS`, `_apply_interpolation_mode` calls `step_points(hold_until_next(data))`, and
`hold_until_next` (`core/data/loader/extractors/utils/step_points.py:23`) sorts again with
`sorted(points)`. That compares whole `(time, value)` tuples, so a tie on time is broken by value:
`(22:00, 0)` sorts before `(22:00, 25)`. The 0 becomes a block from 22:00 to 22:00, which has no
length, and 25 is held from 22:00 to the next point at 06:00. A step **up** survives, because there
the value order matches the written order.

`step_points` (line 14) also sorts on whole tuples, `(start, end, value)`. Once `hold_until_next`
keeps the written order, that sort is safe for a step, because the zero-length block has the
smaller `end`. It's still clearer to sort on `(start, end)` only.

**Impact (how we found it).** We had EV availability forecasts that are 0 from 22:00 to 06:00, each
night drawn as a pair of points at every edge. The night blocks never reached HAEO:
- Its own loaded limit entities read 25 kW (discharge) and 11.52 kW (AC charging) at 23:00 and
  03:00 on every night.
- The plan had one EV discharging about 5 kW from Thursday 23:00 to Friday 05:30, during a block
  that was meant to be 0.

Switching those sensors to the default linear mode loads the blocks correctly, because paired
points step cleanly under linear interpolation (`separate_duplicate_timestamps`). That's our
workaround for now.

### Reproduction steps

1. In a HAEO checkout (v0.5.0rc3, or `main` at 31c87cb), run the attached reproducer:
   `PYTHONPATH=. python 2026-10-27-previous-mode-step-repro.py`
2. It loads 25 kW until t = 2 h, 0 kW until t = 10 h, then 25 kW (each step a pair of points at one
   time) onto 12 hourly periods, once in each mode.
3. Output:

   ```text
   linear   [25.0, 25.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 25.0, 25.0]
   previous [25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0]
   ```

   Expected: the same as the linear row in both modes.

Live, the same thing shows in an element's input entities: their `forecast` attribute holds the
step-down value through the block that should be 0.

### Proposed fix

Sort on time only, with Python's stable sort, so points at the same time keep the order they were
written in (see `2026-10-27-previous-mode-step-fix.diff`):

```diff
 from collections.abc import Iterable, Sequence
+from operator import itemgetter
@@
-    return [point for start, end, value in sorted(blocks) for point in ((start, value), (end, value))]
+    return [point for start, end, value in sorted(blocks, key=itemgetter(0, 1)) for point in ((start, value), (end, value))]
@@
-    ordered = sorted(points)
+    ordered = sorted(points, key=itemgetter(0))
```

With this change:
- The reproducer gives the expected row in both modes.
- The existing loader and forecast tests pass: `pytest custom_components/haeo/core/data/loader
  custom_components/haeo/core/data/util`, 699 passed.

A regression test could assert that `step_points(hold_until_next([(0, 25), (10, 25), (10, 0),
(20, 0)]))` holds 0 from 10 to 20.

`hold_until_next` is also used by the Flow Power extractor (`flow_power.py:79`), so the fix applies
there too.

### Debug logs / diagnostics

Not needed to reproduce: the reproducer is self-contained. We can attach diagnostics from the live
system if wanted.
