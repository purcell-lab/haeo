# Draft upstream issue: a horizon roll waits out the debounce window

**For:** hass-energy/haeo. **Status:** draft, not posted. Search the upstream issues for duplicates
before posting.

---

**Title:** A horizon roll waits out the debounce window, so the outputs plan from the previous
period for up to `debounce_seconds`

### System Health details

- HAEO 0.5.0rc3 (a fork build of upstream v0.5.0rc3, 07949db, with no changes to the coordinator).
  The same code is on `main` at 31c87cb (26 Oct 2026).
- Home Assistant 2026.10.4, Python 3.13.
- Advanced settings: `debounce_seconds` 30. The horizon comes from an entity ([hass-energy/haeo#461](https://github.com/hass-energy/haeo/issues/461)) with 5-minute
  periods aligned to the market interval, and the first period runs from the current minute to the
  next 5-minute boundary. So the horizon start moves every minute.

### Describe the issue

When the horizon start moves, the input stores reload for the new start straight away, and their
input entities show the new period 0 within a second. But the optimization for the new start only
runs when the debounce window allows. `_handle_horizon_change` and the stores' reload requests both
go through `_request_optimization` (`coordinator.py:609` on `main`), which throttles every request
the same way.

The solves settle into a rhythm one debounce window apart, which drifts. Until the next slot comes,
the output entities still plan from the old start, while the inputs have moved on:

- Every output's `forecast` attribute starts at the old period 0.
- Every output's **state**, which is that old period 0, describes a period that has already ended.
- At a 5-minute boundary, the outputs keep showing the previous interval's plan for up to 30 s.
- Anything that reads both inputs and outputs by time finds no plan for the current period. A
  dashboard table of the plan showed prices, PV and load in its first row, but no power, SoC or cost.

Measured on 27 Oct 2026 (+10:00), with `debounce_seconds` 30:

| Optimization started | Plan's period 0 | Input entities' period 0 at that time |
|---|---|---|
| 10:19:30 | 10:19 | 10:19 |
| (10:20:00, the horizon rolls) | still 10:19 | 10:20 |
| 10:20:03 (a `haeo.optimize` call, which bypasses the debounce) | 10:20 | 10:20 |
| 10:20:20, 10:20:50 | 10:20 | 10:20 |
| (10:21:00, the horizon rolls) | still 10:20 | 10:21 |
| 10:21:20 | 10:21 | 10:21 |

Sampled every 5 s from 10:16 to 10:18:30, the outputs were a period behind the inputs in 13 of 26
samples, for 20 to 30 s after every roll.

With the 7-day preset, the same thing happens whenever the first period ends. It's less visible
there because the periods are longer.

### A related point about `haeo.optimize`

The obvious workaround is an automation that calls `haeo.optimize` 1 s after each roll. That fixes
the alignment (1 out-of-step sample in 44 afterwards). But it adds a solve instead of replacing the
next one:
- `async_run_optimization` doesn't cancel the pending debounce timer.
- The timer was armed from the previous solve's start, and it fires on schedule. By then other
  inputs have changed, so `_rerun_wanted` is true again.

Measured after adding the automation, solves started at :01, :21 and :51 each minute, about 3 a
minute instead of about 1.7.

### Reproduction steps

1. Set `debounce_seconds` to 30. Use a horizon whose first period ends within the minute: a horizon
   entity whose boundaries start at the current minute, or any preset just before its first
   period ends.
2. Have an input that changes more often than every 30 s (any live power sensor), so the debounce
   timer is nearly always armed.
3. Every few seconds, compare the first `time` in an input entity's `forecast` (for example
   `number.<grid>_export_price`) with the first `time` in an output's `forecast` (for example
   `sensor.<grid>_active_power`), and record `sensor.optimizer_status`'s `last_run`.
4. After each roll, the output lags the input until the next debounced solve, by up to
   `debounce_seconds`.

### Proposed fix

1. **Don't debounce a horizon roll.** When `_handle_horizon_change` sees that the start has moved
   past `_planned_horizon_start`, cancel any pending debounce timer and run `_optimize()` now,
   instead of `signal_optimization_stale()`. The stores' reload for the new start should be
   treated the same way, but only for that first reload. The roll happens at most once a period,
   so this doesn't defeat the debounce. `_optimize()` already declines to run until every store is
   aligned with the new start, and the last store to load requests it again.
2. **A forced optimization should restart the debounce window.** In `async_run_optimization` (and
   the roll path above), cancel the pending debounce timer before optimizing. Later requests then
   arm a fresh timer from this solve's start. A forced solve then replaces the next debounced one
   instead of adding to it.

### Debug logs / diagnostics

We can attach the sampling log and diagnostics from the live system if wanted.
