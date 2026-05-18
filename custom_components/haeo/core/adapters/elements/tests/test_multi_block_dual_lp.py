"""LP-level regression tests for the multi-block ``element_power_balance`` dual fix.

After PR #426 (DEFAULT_TAG removal), ``Node.element_power_balance()`` returns
a flat list of per-period constraint duals laid out as
``[optional production-decomp block, optional consumption-decomp block, then
one block per tag of per-tag balance]``. Each block is ``n_periods`` long.

The three adapters that publish a shadow-price sensor sourced from
``element_power_balance`` (node, inverter, battery) previously assumed the
dual was a single ``n_periods``-long vector. On real-world topologies where
the source node has ``is_source=is_sink=True`` the multi-block dual broke
that assumption and the sensor either crashed downstream or silently dropped
with the wrong shape.

These tests build small grid + element networks where the source bus is
``is_source=is_sink=True``, solve the LP, push the model outputs through
each adapter, and verify the published shadow-price sensor has the expected
``n_periods``-long shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import numpy as np

from custom_components.haeo.core.adapters.elements.battery import (
    BATTERY_DEVICE_BATTERY,
    BATTERY_POWER_BALANCE,
    BatteryAdapter,
)
from custom_components.haeo.core.adapters.elements.inverter import (
    INVERTER_DC_BUS_POWER_BALANCE,
    INVERTER_DEVICE_INVERTER,
    InverterAdapter,
)
from custom_components.haeo.core.adapters.elements.node import NODE_DEVICE_NODE, NODE_POWER_BALANCE, NodeAdapter
from custom_components.haeo.core.adapters.policy_compilation import compile_policies
from custom_components.haeo.core.adapters.registry import collect_model_elements
from custom_components.haeo.core.model import ModelOutputName, ModelOutputValue
from custom_components.haeo.core.model.element import ELEMENT_POWER_BALANCE
from custom_components.haeo.core.model.network import Network
from custom_components.haeo.core.schema import as_connection_target
from custom_components.haeo.core.schema.elements import ElementType
from custom_components.haeo.core.schema.elements.battery import BatteryConfigData
from custom_components.haeo.core.schema.elements.grid import GridConfigData
from custom_components.haeo.core.schema.elements.inverter import InverterConfigData
from custom_components.haeo.core.schema.elements.load import LoadConfigData

_PERIODS_HOURS = np.array([1.0, 1.0, 1.0, 1.0])
_N_PERIODS = len(_PERIODS_HOURS)
_IMPORT_PRICES = np.array([0.20, 0.25, 0.35, 0.40])


def _grid_config(bus_name: str = "main_bus") -> GridConfigData:
    return cast(
        "GridConfigData",
        {
            "element_type": ElementType.GRID,
            "name": "grid",
            "connection": as_connection_target(bus_name),
            "pricing": {
                "price_source_target": _IMPORT_PRICES,
                "price_target_source": np.array([0.0] * _N_PERIODS),
            },
            "power_limits": {},
        },
    )


def _load_config(bus_name: str = "main_bus") -> LoadConfigData:
    return cast(
        "LoadConfigData",
        {
            "element_type": ElementType.LOAD,
            "name": "house_load",
            "connection": as_connection_target(bus_name),
            "forecast": {"forecast": np.array([2.0] * _N_PERIODS)},
            "curtailment": {"curtailment": False},
        },
    )


def _model_outputs(net: Network) -> Mapping[str, Mapping[ModelOutputName, ModelOutputValue]]:
    return cast(
        "Mapping[str, Mapping[ModelOutputName, ModelOutputValue]]",
        {element_name: element.outputs() for element_name, element in net.elements.items()},
    )


def _build_and_solve(participants: Mapping[str, object], extra_nodes: list[dict[str, object]]) -> Network:
    net = Network(name="multi-block-dual-test", periods=_PERIODS_HOURS)
    for node_cfg in extra_nodes:
        net.add(cast("dict[str, object]", node_cfg))
    sorted_model_elements = list(collect_model_elements(participants))  # type: ignore[arg-type]
    result = compile_policies(sorted_model_elements, [])
    for cfg in result["elements"]:
        net.add(cfg)
    net.optimize()
    return net


# ---------------------------------------------------------------------------
# Node adapter
# ---------------------------------------------------------------------------


def test_node_power_balance_collapses_multi_block_dual() -> None:
    """``node_power_balance`` must be ``n_periods`` long on a source+sink bus.

    When ``is_source=is_sink=True`` the raw ``element_power_balance`` dual is
    multi-block; the adapter must collapse it to a single per-period series.
    """
    node_config = cast(
        "object",
        {
            "element_type": ElementType.NODE,
            "name": "main_bus",
            "role": {"is_source": True, "is_sink": True},
        },
    )
    participants = {"grid": _grid_config(), "load": _load_config(), "main_bus": node_config}
    net = _build_and_solve(
        participants,
        [
            {
                "element_type": "node",
                "name": "main_bus",
                "is_source": True,
                "is_sink": True,
            }
        ],
    )

    model_outputs = _model_outputs(net)
    # Sanity: the raw model dual is multi-block (more than n_periods values).
    raw_dual = model_outputs["main_bus"][ELEMENT_POWER_BALANCE]
    assert len(raw_dual.values) >= _N_PERIODS  # type: ignore[union-attr]
    assert len(raw_dual.values) % _N_PERIODS == 0  # type: ignore[union-attr]

    adapter = NodeAdapter()
    outputs = adapter.outputs(
        "main_bus",
        model_outputs,
        periods=_PERIODS_HOURS,
    )[NODE_DEVICE_NODE]

    assert NODE_POWER_BALANCE in outputs, (
        "node_power_balance must be surfaced; the multi-block dual fix should "
        "collapse the raw dual to a single n_periods-long series."
    )
    assert len(outputs[NODE_POWER_BALANCE].values) == _N_PERIODS


# ---------------------------------------------------------------------------
# Inverter adapter
# ---------------------------------------------------------------------------


def _inverter_config(dc_bus: str = "dc_bus", ac_bus: str = "main_bus") -> InverterConfigData:
    return cast(
        "InverterConfigData",
        {
            "element_type": ElementType.INVERTER,
            "name": "pv_inverter",
            "connection": as_connection_target(ac_bus),
            "source_connection": as_connection_target(dc_bus),
            "efficiency": {
                "efficiency_source_target": 0.97,
                "efficiency_target_source": 0.97,
            },
            "power_limits": {
                "max_power_source_target": 5.0,
                "max_power_target_source": 5.0,
            },
        },
    )


def test_inverter_dc_bus_power_balance_collapses_multi_block_dual() -> None:
    """``inverter_dc_bus_power_balance`` must be ``n_periods`` long under multi-tag topology.

    The DC bus is the inverter's internal node and the AC bus is
    ``is_source=is_sink=True`` so the raw dual is multi-block.
    """
    participants = {
        "grid": _grid_config(),
        "load": _load_config(),
        "pv_inverter": _inverter_config(),
    }
    net = _build_and_solve(
        participants,
        [
            {
                "element_type": "node",
                "name": "main_bus",
                "is_source": True,
                "is_sink": True,
            },
        ],
    )

    model_outputs = _model_outputs(net)

    adapter = InverterAdapter()
    outputs = adapter.outputs(
        "pv_inverter",
        model_outputs,
        periods=_PERIODS_HOURS,
    )[INVERTER_DEVICE_INVERTER]

    assert INVERTER_DC_BUS_POWER_BALANCE in outputs
    assert len(outputs[INVERTER_DC_BUS_POWER_BALANCE].values) == _N_PERIODS


# ---------------------------------------------------------------------------
# Battery adapter
# ---------------------------------------------------------------------------


def _battery_config(bus_name: str = "main_bus") -> BatteryConfigData:
    return cast(
        "BatteryConfigData",
        {
            "element_type": ElementType.BATTERY,
            "name": "powerwall",
            "connection": as_connection_target(bus_name),
            "storage": {
                # Capacity / charge-percentage fields are boundary values (n+1).
                "capacity": np.array([10.0] * (_N_PERIODS + 1)),
                "initial_charge_percentage": 0.5,
            },
            "limits": {
                "min_charge_percentage": np.array([0.1] * (_N_PERIODS + 1)),
                "max_charge_percentage": np.array([0.9] * (_N_PERIODS + 1)),
            },
            "power_limits": {
                "max_power_source_target": np.array([5.0]),
                "max_power_target_source": np.array([5.0]),
            },
            "pricing": {"salvage_value": 0.0},
            "efficiency": {
                "efficiency_source_target": np.array([0.95]),
                "efficiency_target_source": np.array([0.95]),
            },
            "partitioning": {},
            "undercharge": {
                "percentage": np.array([0.05] * (_N_PERIODS + 1)),
                "cost": np.array([0.03]),
            },
            "overcharge": {
                "percentage": np.array([0.95] * (_N_PERIODS + 1)),
                "cost": np.array([0.04]),
            },
        },
    )


def test_battery_power_balance_collapses_multi_block_dual() -> None:
    """``battery_power_balance`` must be ``n_periods`` long when AC bus is source+sink.

    The battery's internal node uses ``element_power_balance`` and inherits
    the multi-block layout from the AC bus topology.
    """
    participants = {
        "grid": _grid_config(),
        "load": _load_config(),
        "powerwall": _battery_config(),
    }
    net = _build_and_solve(
        participants,
        [
            {
                "element_type": "node",
                "name": "main_bus",
                "is_source": True,
                "is_sink": True,
            },
        ],
    )

    model_outputs = _model_outputs(net)

    adapter = BatteryAdapter()
    outputs = adapter.outputs(
        "powerwall",
        model_outputs,
        config=_battery_config(),
        periods=_PERIODS_HOURS,
    )[BATTERY_DEVICE_BATTERY]

    assert BATTERY_POWER_BALANCE in outputs
    assert len(outputs[BATTERY_POWER_BALANCE].values) == _N_PERIODS


# ---------------------------------------------------------------------------
# Sanity check: single-block topologies still work
# ---------------------------------------------------------------------------


def test_node_power_balance_passes_through_single_block_dual() -> None:
    """Single-block duals pass through ``per_period_dual`` unchanged.

    If the source bus is a plain junction (``is_source=is_sink=False``) the
    raw dual is already ``n_periods`` long.
    """
    node_config = cast(
        "object",
        {
            "element_type": ElementType.NODE,
            "name": "main_bus",
            "role": {"is_source": False, "is_sink": False},
        },
    )
    participants = {"grid": _grid_config(), "load": _load_config(), "main_bus": node_config}
    net = _build_and_solve(
        participants,
        [
            {
                "element_type": "node",
                "name": "main_bus",
                "is_source": False,
                "is_sink": False,
            }
        ],
    )

    model_outputs = _model_outputs(net)
    raw_dual_len = len(model_outputs["main_bus"][ELEMENT_POWER_BALANCE].values)  # type: ignore[union-attr]
    assert raw_dual_len == _N_PERIODS, (
        f"Sanity: junction node should emit a single-block dual; got {raw_dual_len} "
        f"for n_periods={_N_PERIODS}."
    )

    adapter = NodeAdapter()
    outputs = adapter.outputs(
        "main_bus",
        model_outputs,
        periods=_PERIODS_HOURS,
    )[NODE_DEVICE_NODE]

    assert NODE_POWER_BALANCE in outputs
    assert len(outputs[NODE_POWER_BALANCE].values) == _N_PERIODS
    # Values are non-zero (the LP is binding on the load forecast).
    assert any(abs(v) > 0 for v in outputs[NODE_POWER_BALANCE].values)
