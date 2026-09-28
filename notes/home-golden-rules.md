# Golden rules: home HAEO and Sigenergy deployment

These rules are fixed by the owner. A tuning change, policy rule or template edit must never
break them. When a change conflicts with a golden rule, the change is wrong, not the rule.

## Rule 1: EV coordination windows (29 Sep 2026)

The EVs are coordinated into HAEO's schedule, not just metered while plugged in. HAEO plans
around the owner being able to plug in or move a car at short notice:

| EV state | Planned availability |
|---|---|
| **Not on the DC charger** (unplugged or away) | **Back within 1 hour**: no DC charge or discharge for the next hour, then 25 kW DC charge and discharge for the rest of the horizon. |
| **On the AC charger** | **Swap within 2 hours**: AC charging up to 11 kW across the horizon, no DC charging, no DC discharge for the next 2 hours, then 25 kW DC discharge for the rest of the horizon. |
| **On the DC charger, session running** | Real availability from the session. |

The windows roll with `now()`. A plan that relies on a swap or a return keeps moving out
until the car is actually moved, and HAEO re-plans within 5 minutes of a plug change.

Implemented in `packages/haeo.yaml` (Home Assistant) by these sensors:

- `sensor.ev1_max_discharge_power_forecast` / `sensor.ev2_max_discharge_power_forecast`
- `sensor.ev1_max_charge_power_forecast` / `sensor.ev2_max_charge_power_forecast` (DC path)
- `sensor.ev1_ac_charge_power_forecast` / `sensor.ev2_ac_charge_power_forecast` (AC path)
- `sensor.ev1_max_charge_power_total_forecast` / `sensor.ev2_max_charge_power_total_forecast`
  (each car's own charge limit, the larger of the DC and AC paths)

The package carries the same rule as a header comment above these sensors.

### What this means for every policy change

HAEO can route energy through the planned future connections. For example, it can charge a
car on AC now while planning DC discharge from it once the swap window ends, or charge an
away car once its return window ends. On 29 Sep this defeated several policy attempts:

- A Solar to Battery cost was bypassed by solar → EV1 (AC) → EV1 DC discharge → battery.
- A Solar to EVs reward sent today's solar into EV2 while EV2 was away.
- Larger EV charge rewards made HAEO charge and discharge the same car in the same period.

So a policy change must:

1. **Keep the windows.** Never make a policy work by removing or shortening a window.
2. **Be dry-run first.** Replay a fresh diagnostic offline (`tools/diag.py`), with the
   policy rules compiled (`compile_policies`), for the change and for the live baseline.
3. **Pass the overlap check.** No EV may be planned to charge and discharge in the same
   period today, and any overlap later in the horizon must be explained.
4. **Check where each rule is priced.** List each rule's priced connections. A rule meant
   for a car's charging must land on that car's charge link (`EVn:charge`), not on a
   source's output. With the EV Port nodes (29 Sep), per-car and per-source rules do.
   A single rule targeting several cars can still collapse onto the source's output.
5. **Report the side effects.** Report today's solar split (EVs, battery, export), grid
   import, and end-of-day SoC for the battery and each EV, against the baseline.
