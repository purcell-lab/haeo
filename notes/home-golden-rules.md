# Golden rules: home HAEO and Sigenergy deployment

These rules are fixed by the owner. A tuning change, policy rule or template edit must never
break them. When a change conflicts with a golden rule, the change is wrong, not the rule.

## Rule 1: EV coordination windows (29 Sep 2026)

The EVs are coordinated into HAEO's schedule, not just metered while plugged in. HAEO plans
around the owner being able to plug in or move a car at short notice:

| EV state | Planned availability |
|---|---|
| **Not on the DC charger** (unplugged or away) | **Back within 1 hour**: no charge or discharge for the next hour, then 25 kW DC charge and discharge and 11 kW AC charge for the rest of the horizon (AC added 29 Sep, so both cars can charge together, one on DC and one on AC, when power is cheap). |
| **On the AC charger** | **Swap within 2 hours**: AC charging up to 11 kW across the horizon, no DC charging, no DC discharge for the next 2 hours, then 25 kW DC discharge for the rest of the horizon. |
| **On the DC charger, session off** (charger "Occupied", charge switch off) | Same as not on the DC charger: **back within 1 hour** (29 Sep). |
| **On the DC charger, session running** (charge switch on, or charger Charging/Discharging) | Real availability from the session. |

The windows roll with `now()`. A plan that relies on a swap or a return keeps moving out
until the car is actually moved, and HAEO re-plans within 5 minutes of a plug change.

Planned EV actions inside these windows are real plan actions, not artefacts. HAEO assumes
the car returns (or is swapped) as the window says, and the plan will carry them out when
it does. If it doesn't, the plan reschedules to the next time it expects the car. Never
describe or discount a planned EV charge, discharge or export as "won't happen because the
car is away".

Implemented in `packages/haeo.yaml` (Home Assistant) by these sensors:

- `sensor.ev1_max_discharge_power_forecast` / `sensor.ev2_max_discharge_power_forecast`
- `sensor.ev1_max_charge_power_forecast` / `sensor.ev2_max_charge_power_forecast` (DC path)
- `sensor.ev1_ac_charge_power_forecast` / `sensor.ev2_ac_charge_power_forecast` (AC path; the
  state is the present availability, 11 only while on the AC charger, and gates the AC
  follower; the forecast carries the window)
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
   period today, and any overlap later in the horizon must be explained. The check is per
   car. Across the two cars (29 Sep, owner): EV1 and EV2 may charge at the same time (one
   on AC, one on DC). Both discharging in the same period is physically impossible (one DC
   charger), but HAEO is a linear program and cannot exclude it; the DCEV Inverter only
   caps their sum at 25 kW. The owner accepts this in the plan and handles it in
   execution, so it is not a failed check and needs no policy change. A dry run on 29 Sep
   showed per-car discharge-cost tie-breaks (0.1-0.5 c) do not reduce it.
4. **Check where each rule is priced.** List each rule's priced connections. A rule meant
   for a car's charging must land on that car's charge link (`EVn:charge`), not on a
   source's output. With the EV Port nodes (29 Sep), per-car and per-source rules do.
   A single rule targeting several cars can still collapse onto the source's output.
5. **Report the side effects.** Report today's solar split (EVs, battery, export), grid
   import, and end-of-day SoC for the battery and each EV, against the baseline.

## Rule 2: EVs charge early, the battery charges late (29 Sep 2026)

- **EV priority, early:** a plugged-in EV takes charge as early in the day as possible.
- **Battery priority, late:** the home battery charges late in the day.
- **Solar when possible, grid when needed:** solar is always used before the grid, and the
  grid only fills what solar cannot (for example the battery reserve or a "full by 3pm"
  target).

Express this with prices on the devices, not path rules (Rule 3). Since 29 Sep the only
one is the EV charge price (`sensor.haeo_ev_charge_price` on the "EVn charge cost" rules,
* -> EVn): -0.02 $/kWh at 06:00 falling linearly to -0.01 at 16:00, 0 otherwise. The tilt
makes the EVs charge at the start of any equally cheap window at full rate, both cars
together (one on DC, one on AC) up to their limits, and the battery fills after them.
It stays below the 0.03 EV discharge cost so a car is never charged and discharged
together for the incentive (Rule 1 checks).

## Rule 3: the HAEO plan is the coordinator (29 Sep 2026)

Behaviour is changed by changing HAEO's plan: its policy rules, element configuration and
the input sensors that feed it. The Sigen battery-management automation and the AC charging
follower only carry out the plan. Do not add logic, thresholds or smoothing to them to get
a different behaviour; if the plan is wrong, fix the plan.

Prefer prices on devices and markets (a device's own cost, discharge costs, import and
export prices) over rules that hard-code an energy path (source → target). The switchboard
shadow price must reflect what energy is really worth there, for example the sell price when
export is possible, so price gates such as the pool heater's 5¢ shedding cost work as set.
A path rule that bends that price (for example an extra cost on Battery → Grid or EVs → Grid)
breaks those gates (29 Sep).

## Standard scenario suite (30 Sep 2026)

Every policy, price or input-sensor change is dry-run against these scenarios before it goes
live, alongside the Rule 1 checks. Prices are the signal, not clock windows: each event is
injected as an extra price input on the Grid (HAEO sums its price inputs), so the event price
replaces the forecast inside the event window. The runner is `notes/dry-run/scenarios.py`;
run it on a fresh diagnostic ("data" object), with Amber's `detailedForecast` for the
renewables proxy.

| Scenario | Event | Buy | Sell | Renewables | Expected outcome |
|---|---|---|---|---|---|
| **S0 Today** | none (today's forecast) | forecast | forecast | forecast | Import only when the buy price is low; export stored energy only when the sell price beats keeping it; storage full before the evening. |
| **S1 MSL** (minimum system load) | 11:00-15:00 | -28 c | -30 c | 98% | Fill all storage (EVs to their limits, battery to 100%), pool heater and miner on, no export, curtail only what cannot be stored. Emptying storage at about 0 c just before the event, to make room for paid energy, is allowed. |
| **S2 LOR, morning** (lack of reserve) | tomorrow 06:00-06:30 | 230 c | 200 c | 30% | No import. Export everything above the EV and battery floors at the inverter limit, inside the event. |
| **S3 LOR, evening** | 18:00-21:00 | 135 c | 100 c | 12% | No import. Export EVs and battery at the limit for the whole event, keeping only what the night needs. |
| **S4 5-minute spike** | 13:00-13:05 | 1550 c | 1500 c | 80% | No import. Export at the full limit for the 5 minutes, then refill in the cheap window. |

Each scenario is run with the live rules and any proposed rules, on two time grids:
HAEO's own period tiers ("tier": 1, 5, 30 then 60-minute periods), and 5-minute periods for
24 h ("fine": what the plan does once the event is inside HAEO's short-period range).
Report for each: energy in the event window (import, export, battery and EV charge and
discharge, pool, miner, curtailment, SoC at the end of the event), 24 h net grid cash and net
CO2e (0.8 kg/kWh x (1 - renewables)), import and export by price band, and Rule 1 overlaps.

### Results, 30 Sep 2026 (diagnostic 09:47, both EVs off the chargers, live rules)

| Scenario | In the event (fine grid) | 24 h net grid cash | Pass |
|---|---|---|---|
| S0 | 16:00-21:00: export 11 kWh at 5-15 c, no import | $5.57 | Yes |
| S1 MSL | import 141 kWh at -28 c, storage full, pool 22 kWh, miner 3 kWh, no export, 0.5 kWh curtailed | $47.71 | Yes, but EV1 planned to 96.9% (see below) |
| S2 LOR 06:00 | export 15 kWh in 30 min (30 kW, the inverter limit), EVs at their 20% floor | $34.62 | Yes |
| S3 LOR 18:00 | export 86 kWh at 100 c, EVs to about 20%, battery kept at 47% for the night | $81.71 | Yes |
| S4 spike | export 3.4 kWh in 5 min (about 41 kW) | $56.04 | Yes |

All imports in every scenario were at buy prices under 5 c (except 1.9 kWh at 5-25 c in S3).
No Rule 1 overlaps. Findings:

- **Event resolution.** In HAEO's tiers a 5-minute spike 3 h ahead sits in a 30-minute period
  and a 30-minute event tomorrow in a 60-minute period, so the plan spreads the export over
  the whole period (S2 tier drained the battery to 0% by 07:00; fine kept 34%). The plan
  concentrates the export into the event once it is inside the 5-minute tier (the last ~35
  minutes), so execution follows the fine result.
- **Export ceiling.** The battery, the DC charger and DC-coupled solar share the 30 kW
  inverter, which caps event exports at about 30-41 kW.
- **EV1 overcharge.** EV1 may be planned above its 90% charge limit up to 100% at a cost of
  0.20 $/kWh. At MSL prices (-28 c) that pays, so the plan fills EV1 to 96.9%, which the
  car will not accept. The overcharge cost needs to be above the most negative expected buy
  price.
- **Carbon price (v3 proposal, $35/t, import cost and export credit).** On the fine grid it
  lowers 24 h CO2e by 1.4-2.1 kg and raises cash by $0.42-0.50 in every scenario. On HAEO's
  tiers the result is mixed (CO2e +3.1 to -1.6 kg, cash -$0.31 to +$0.39): with 30 and 60-minute
  periods it holds more battery energy through the evening. In both it adds 5-9 kWh of
  morning solar export at 4.9 c (the credit tips it past the pool heater's 5 c gate).
