# Objective evidence summary — overnight 2026-06-20 19:00 → 2026-06-21 06:00 AEST (UTC+10)

## Headline numbers

- 129 distinct negative export spikes at the physical meter `sensor.sigen_plant_grid_active_power`
  - peak: median −5.3 kW, range −10.1 to −1.8 kW
  - duration: median 0.0 s (single-sample) up to 2.7 s
- 131 events where HAEO output entity `sensor.battery_active_power` momentarily published exactly **25.26 kW** (the inverter's rated discharge cap)
- 95% correlation: HAEO 25.26 kW publish → meter spike (<−1 kW) within 10 s
- 96% reverse correlation: meter spike → preceded by HAEO 25.26 kW publish within 10 s
- Mean delay HAEO publish → meter spike: 4.2 s
- HAEO 25.26 kW publishes always at 0.7–1.9 s past minute → interval boundary artefact
- Coordinator update cadence: 10,205 `optimizer_duration` updates in 11 h, median gap 4.25 s (no `update_interval`, event-driven; `DEFAULT_DEBOUNCE_SECONDS = 2`)

## Sample event 19:44 AEST

| Time (AEST)      | Entity / event                                  | Value                            |
|------------------|--------------------------------------------------|----------------------------------|
| 19:43:51.214     | sensor.battery_active_power                      | 2.442 kW                         |
| 19:44:00.772     | sensor.battery_active_power                      | **25.263 kW** (horizon[0] swing) |
| 19:44:00.864     | select.sigen_plant_remote_ems_control_mode       | Command Discharging (PV First)   |
| 19:44:04.708     | sensor.battery_active_power                      | 2.446 kW (reverted)              |
| 19:44:05.044     | sensor.sigen_plant_grid_active_power (meter)     | **−5.65 kW** (export spike)      |
| 19:44:05.048     | select.sigen_plant_remote_ems_control_mode       | Maximum Self Consumption         |
| 19:44:06.804     | sensor.sigen_plant_grid_active_power             | −3.00 kW                         |
| 19:44:11.061     | sensor.sigen_plant_grid_active_power             | −0.08 kW                         |

The full publish→mode-switch→spike→revert cycle takes ~4 s, matching the coordinator MPC period.

## Pattern of HAEO 25.26 kW publish events (first 20)

```
2026-06-20T19:11:00.79  25.26
2026-06-20T19:16:00.79  25.26
2026-06-20T19:21:00.77  25.26
2026-06-20T19:31:00.79  25.26
2026-06-20T19:37:00.75  25.26
2026-06-20T19:39:00.76  25.26
2026-06-20T19:42:00.78  25.26
2026-06-20T19:43:00.77  25.26
2026-06-20T19:44:00.77  25.26
2026-06-20T19:48:00.77  25.26
2026-06-20T19:49:00.77  25.26
2026-06-20T19:51:00.78  25.26
2026-06-20T19:53:01.53  25.26
2026-06-20T19:54:00.74  25.26
2026-06-20T19:57:00.76  25.26
2026-06-20T19:58:00.78  25.26
2026-06-20T19:59:00.76  25.26
2026-06-20T20:04:00.74  25.26
2026-06-20T20:07:00.78  25.26
2026-06-20T20:14:00.77  25.26
```

(All 131 events occur 0.74–1.94 s past minute boundary, confirming interval-boundary origin.)
