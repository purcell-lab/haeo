"""LP-level integration tests for the load cumulative + rolling-24h sensors.

Build small grid+load networks, solve the LP, push the resulting model outputs
through ``LoadAdapter.outputs``, and verify the published statistics agree with
the analytic per-timestep integrals. This guards against unit-of-measure
regressions (e.g. forgetting that ``element_power_balance`` shadows are already
period-integrated) and ensures the rolling-24h window keeps its semantics when
fed real solver output.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import numpy as np
import pytest

from custom_components.haeo.core.adapters.elements.load import (
    LOAD_DAILY_AVERAGE_COST,
    LOAD_DAILY_COST,
    LOAD_DAILY_ENERGY,
    LOAD_DAILY_RUNTIME,
    LOAD_DEVICE_LOAD,
    LOAD_TOTAL_AVERAGE_COST,
    LOAD_TOTAL_COST,
    LOAD_TOTAL_ENERGY,
    LOAD_TOTAL_RUNTIME,
    LoadAdapter,
)
from custom_components.haeo.core.adapters.output_utils import expect_output_data
from custom_components.haeo.core.adapters.registry import collect_model_elements
from custom_components.haeo.core.model import ModelOutputName, ModelOutputValue
from custom_components.haeo.core.model.element import ELEMENT_POWER_BALANCE
from custom_components.haeo.core.model.elements.connection import CONNECTION_POWER
from custom_components.haeo.core.model.network import Network
from custom_components.haeo.core.schema import as_connection_target
from custom_components.haeo.core.schema.elements import ElementType
from custom_components.haeo.core.schema.elements.grid import GridConfigData
from custom_components.haeo.core.schema.elements.load import LoadConfigData

_LOAD_FORECAST = 5.0  # kW
_THRESHOLD_PRICE = 0.30  # $/kWh — keeps the load on for cheap periods, sheds when expensive
# Hourly prices: cheap, cheap, expensive, expensive
_IMPORT_PRICES = np.array([0.20, 0.25, 0.35, 0.40])
_PERIODS_HOURS = np.array([1.0, 1.0, 1.0, 1.0])


def _make_grid_config() -> GridConfigData:
    return cast(
        "GridConfigData",
        {
            "element_type": ElementType.GRID,
            "name": "grid",
            "connection": as_connection_target("main_bus"),
            "pricing": {
                "price_source_target": _IMPORT_PRICES,
                "price_target_source": np.array([0.0] * 4),
            },
            "power_limits": {},
        },
    )


def _make_load_config(threshold_price: float | None = _THRESHOLD_PRICE) -> LoadConfigData:
    config: LoadConfigData = cast(
        "LoadConfigData",
        {
            "element_type": ElementType.LOAD,
            "name": "load",
            "connection": as_connection_target("main_bus"),
            "forecast": {"forecast": np.array([_LOAD_FORECAST] * 4)},
            "curtailment": {"curtailment": True},
        },
    )
    if threshold_price is not None:
        config["threshold"] = {"threshold_price": threshold_price}
    return config


def _solve_network(grid_config: GridConfigData, load_config: LoadConfigData) -> Network:
    net = Network(name="stats-lp-test", periods=_PERIODS_HOURS)
    participants = {"grid": grid_config, "load": load_config}
    net.add({"element_type": "node", "name": "main_bus", "is_source": False, "is_sink": False})
    for cfg in collect_model_elements(participants):  # type: ignore[arg-type]
        net.add(cfg)
    net.optimize()
    return net


def _model_outputs(net: Network) -> Mapping[str, Mapping[ModelOutputName, ModelOutputValue]]:
    return cast(
        "Mapping[str, Mapping[ModelOutputName, ModelOutputValue]]",
        {element_name: element.outputs() for element_name, element in net.elements.items()},
    )


def test_total_cost_matches_p_load_times_node_dual_integral() -> None:
    """Verify ``total_cost = sum(p_load[t] * node_dual[t])`` and energy/runtime match dispatch.

    With grid prices [0.20, 0.25, 0.35, 0.40] and a threshold of $0.30/kWh, the LP
    serves the 5kW load in the first two cheap periods and sheds in the last two.
    The published cost must match the exact integral that the adapter computes from
    the LP outputs, and should sit close to (but not necessarily equal to) the
    price-only estimate because the source-node dual also reflects HAEO's secondary
    regularizer terms.
    """
    net = _solve_network(_make_grid_config(), _make_load_config())
    adapter = LoadAdapter()
    model_outputs = _model_outputs(net)

    # Pull the raw p_load and main_bus dual the adapter uses internally.
    p_load = tuple(float(v) for v in expect_output_data(model_outputs["load:connection"][CONNECTION_POWER]).values)
    main_bus_dual = tuple(float(v) for v in expect_output_data(model_outputs["main_bus"][ELEMENT_POWER_BALANCE]).values)
    expected_cost = sum(p * d for p, d in zip(p_load, main_bus_dual, strict=True))
    expected_energy = sum(p * dt for p, dt in zip(p_load, _PERIODS_HOURS, strict=True))
    expected_runtime = sum(dt for p, dt in zip(p_load, _PERIODS_HOURS, strict=True) if abs(p) > 1e-9)

    outputs = adapter.outputs(
        "load",
        model_outputs,
        config=_make_load_config(),
        periods=_PERIODS_HOURS,
    )[LOAD_DEVICE_LOAD]

    assert tuple(outputs[LOAD_TOTAL_ENERGY].values)[-1] == pytest.approx(expected_energy, abs=1e-6)
    assert tuple(outputs[LOAD_TOTAL_COST].values)[-1] == pytest.approx(expected_cost, abs=1e-6)
    assert tuple(outputs[LOAD_TOTAL_RUNTIME].values)[-1] == pytest.approx(expected_runtime, abs=1e-6)
    assert tuple(outputs[LOAD_TOTAL_AVERAGE_COST].values)[-1] == pytest.approx(
        expected_cost / expected_energy, abs=1e-6
    )

    # Sanity: dispatch should be 5kW in the two cheap periods, ~0 elsewhere; cost
    # should land near the price-only estimate (2.25 $) with regularizer-driven slack.
    assert p_load[0] == pytest.approx(_LOAD_FORECAST, abs=1e-6)
    assert p_load[1] == pytest.approx(_LOAD_FORECAST, abs=1e-6)
    assert p_load[2] == pytest.approx(0.0, abs=1e-6)
    assert p_load[3] == pytest.approx(0.0, abs=1e-6)
    assert expected_runtime == pytest.approx(2.0, abs=1e-6)
    price_only_estimate = _LOAD_FORECAST * (_IMPORT_PRICES[0] + _IMPORT_PRICES[1])
    assert expected_cost == pytest.approx(price_only_estimate, rel=0.05)


def test_daily_sensors_match_totals_when_horizon_is_under_24h() -> None:
    """Horizon of 4h <= 24h, so daily window covers every timestep."""
    net = _solve_network(_make_grid_config(), _make_load_config())
    adapter = LoadAdapter()

    outputs = adapter.outputs(
        "load",
        _model_outputs(net),
        config=_make_load_config(),
        periods=_PERIODS_HOURS,
    )[LOAD_DEVICE_LOAD]

    assert next(iter(outputs[LOAD_DAILY_ENERGY].values)) == pytest.approx(
        tuple(outputs[LOAD_TOTAL_ENERGY].values)[-1], abs=1e-6
    )
    assert next(iter(outputs[LOAD_DAILY_COST].values)) == pytest.approx(
        tuple(outputs[LOAD_TOTAL_COST].values)[-1], abs=1e-6
    )
    assert next(iter(outputs[LOAD_DAILY_RUNTIME].values)) == pytest.approx(
        tuple(outputs[LOAD_TOTAL_RUNTIME].values)[-1], abs=1e-6
    )
    assert next(iter(outputs[LOAD_DAILY_AVERAGE_COST].values)) == pytest.approx(
        tuple(outputs[LOAD_TOTAL_AVERAGE_COST].values)[-1], abs=1e-6
    )


def test_zero_dispatch_produces_zero_stats_and_zero_average_cost() -> None:
    """When the LP sheds the load entirely, runtime/energy/cost are 0 and avg-cost is 0 (not NaN)."""
    # Use a threshold below every grid price → LP sheds entirely
    grid_config = _make_grid_config()
    load_config = _make_load_config(threshold_price=0.05)
    net = _solve_network(grid_config, load_config)
    adapter = LoadAdapter()

    outputs = adapter.outputs(
        "load",
        _model_outputs(net),
        config=load_config,
        periods=_PERIODS_HOURS,
    )[LOAD_DEVICE_LOAD]

    assert tuple(outputs[LOAD_TOTAL_ENERGY].values)[-1] == pytest.approx(0.0, abs=1e-6)
    assert tuple(outputs[LOAD_TOTAL_COST].values)[-1] == pytest.approx(0.0, abs=1e-6)
    assert tuple(outputs[LOAD_TOTAL_RUNTIME].values)[-1] == pytest.approx(0.0, abs=1e-6)
    assert tuple(outputs[LOAD_TOTAL_AVERAGE_COST].values)[-1] == pytest.approx(0.0, abs=1e-6)
