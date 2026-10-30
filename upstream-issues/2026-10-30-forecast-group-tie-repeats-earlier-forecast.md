# Draft upstream issue: a short forecast that starts with a longer one takes over its follow-on

**For:** hass-energy/haeo. **Status:** draft, not posted. Search the upstream issues for duplicates
before posting.
**Files with this draft:** `2026-10-30-forecast-group-tie-repro.py` (reproducer) and
`2026-10-30-forecast-group-tie-fix.diff` (proposed patch).

---

**Title:** Combining forecasts: a short forecast that starts with a longer one takes over the
forecast that follows the longer one, which then repeats past its end and is added in

### System Health details

- HAEO 0.5.0 (upstream release, 705bde0), installed through HACS.
- Home Assistant 2026.10.4, Python 3.13.
- A horizon entity ([hass-energy/haeo#461](https://github.com/hass-energy/haeo/issues/461)) with
  5-minute periods aligned to the market interval. Period 0 runs from the current minute to the
  next 5-minute boundary.

### Describe the issue

Since [hass-energy/haeo#639](https://github.com/hass-energy/haeo/pull/639), `_group` in
`core/data/util/forecast_combiner.py` works like this:
- It sorts an input's forecasts by (start, end).
- Each forecast joins the **first** group it follows on from, meaning it starts at or after the
  group's end and ends after it.
- Each group repeats by time of day past its own end, and the groups are summed.

Suppose two forecasts start at the same time and a third follows on from both of them. The third
joins whichever of the two **ends sooner**, because that one sorts first. It should join the one it
actually follows.

Our grid import price is a list of four sensors:
- a live price for the next ~16 hours, starting at the current interval's start;
- a longer-range tariff that starts where the live price ends and runs for 6 days;
- a peak shape adjustment covering the horizon;
- a small adjustment that starts at the current minute and ends at the next 5-minute boundary.

In most minutes the small adjustment starts after the live price. The tariff joins the live
price's group, and the plan's prices are right.

In the **first minute of each 5-minute interval**, the small adjustment starts at the same time as
the live price and ends sooner, so it sorts first. Then:
- The tariff joins the adjustment's group instead of the live price's.
- The live price is now a group on its own, so it repeats by time of day past its end.
- That repeat is added to the tariff for the rest of the horizon.

The far-ahead prices about double for that minute, then go back to normal. The prices below were
loaded from the same sources at 11:30 +10:00 on 30 Oct 2026, the first minute of an interval:

| Price at | Loaded (first minute) | Sum of the sources | Same sources on 0.5.0rc3 |
|---|---|---|---|
| 31 Oct 17:00 | 41.31 c | 22.14 c | 22.14 c |
| 1 Nov 17:00 | 58.63 c | 39.46 c | 39.46 c |
| 1 Nov 18:00 | 56.85 c | 37.40 c | 37.40 c |
| 3 Nov 12:00 | 17.85 c | 9.66 c | 9.66 c |

**Impact.** One solve in five (with a solve each minute) plans against prices that are wrong for
everything after the live price's end. Plans that hold one interval's decision, or anything acting
on the solve at the interval start, take the wrong one. For us it flipped a 25 kW EV charge from the
grid on and off between minutes, against a battery-only plan.

**Workaround.** We extend the short adjustment to the end of the horizon with a value of 0. It then
overlaps every other forecast, so nothing can join its group.

### Reproduction steps

1. In a HAEO checkout (v0.5.0, 705bde0), run the attached reproducer:
   `PYTHONPATH=. python 2026-10-30-forecast-group-tie-repro.py`
2. It combines three forecasts onto hourly periods:
   - `near`: 10 from 0 to 16 h;
   - `far`: 20 from 16 h to 7 d;
   - `adjust`: 0 to 5 min, starting either 1 min after `near` or at the same time.
3. Output:

   ```text
   adjust starts 1 min after near  groups [['near', 'far'], ['adjust']]  t=8h 10  t=24h 20  t=48h 20
   adjust starts with near         groups [['adjust', 'far'], ['near']]  t=8h 10  t=24h 30  t=48h 30
   ```

   Expected: 20 at 24 h and 48 h in both rows, with `far` grouped with `near`.

### Proposed fix

Join the group the forecast follows on from **most closely**: of the groups it qualifies for, the
one that ends last. Keep the first such group on a tie. See
`2026-10-30-forecast-group-tie-fix.diff`:

```diff
-        group = next(
-            (group for group in groups if group[-1][-1][0] <= series[0][0] and group[-1][-1][0] < series[-1][0]),
-            None,
-        )
+        candidates = [
+            group for group in groups if group[-1][-1][0] <= series[0][0] and group[-1][-1][0] < series[-1][0]
+        ]
+        group = max(candidates, key=lambda group: group[-1][-1][0], default=None)
```

With this change:
- The reproducer gives 20 in both rows, with groups `[['adjust'], ['near', 'far']]`.
- Replaying our live sources (without the workaround) gives the "sum of the sources" column above,
  in the first minute and after it.
- `pytest custom_components/haeo/core/data` passes: 804 passed.
- `ruff check` and `ruff format --check` are clean.

A regression test could assert that, for the three forecasts in the reproducer with `adjust`
starting with `near`, `far` lands in `near`'s group.

### A related point

A short forecast that ends early, like the adjustment here, also repeats by time of day across the
whole horizon. That's by design in #639, and it's harmless when the value is 0. But a short
additive adjustment with a non-zero value repeats every day, which surprised us. Some guidance in
the docs would help: to apply an adjustment only once, cover the horizon with 0 after it.

### Debug logs / diagnostics

The reproducer is self-contained, so these aren't needed to reproduce it. We can attach the
captured sources and diagnostics if wanted.
