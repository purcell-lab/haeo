"""Load element adapter for model layer integration."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, Final, Literal

import numpy as np
from numpy.typing import NDArray

from custom_components.haeo.core.adapters.output_utils import expect_output_data
from custom_components.haeo.core.adapters.shadow_price_utils import shadow_price_per_energy
from custom_components.haeo.core.const import ConnectivityLevel
from custom_components.haeo.core.model import ModelElementConfig, ModelOutputName, ModelOutputValue
from custom_components.haeo.core.model.const import OutputType
from custom_components.haeo.core.model.element import ELEMENT_POWER_BALANCE
from custom_components.haeo.core.model.elements import MODEL_ELEMENT_TYPE_CONNECTION, MODEL_ELEMENT_TYPE_NODE
from custom_components.haeo.core.model.elements.connection import CONNECTION_POWER, CONNECTION_SEGMENTS
from custom_components.haeo.core.model.output_data import OutputData
from custom_components.haeo.core.model.util import broadcast_to_sequence
from custom_components.haeo.core.schema import extract_connection_target
from custom_components.haeo.core.schema.elements import ElementType
from custom_components.haeo.core.schema.elements.load import ELEMENT_TYPE, LoadConfigData
from custom_components.haeo.core.schema.sections import (
    CONF_CONNECTION,
    CONF_CURTAILMENT,
    CONF_FORECAST,
    CONF_THRESHOLD_PRICE,
    SECTION_CURTAILMENT,
    SECTION_FORECAST,
    SECTION_THRESHOLD,
)

# Load output names
type LoadOutputName = Literal[
    "load_power",
    "load_forecast_limit_shadow_energy_price",
    "load_threshold_price",
    "load_total_energy",
    "load_total_cost",
    "load_total_runtime",
    "load_total_average_cost",
    "load_daily_energy",
    "load_daily_cost",
    "load_daily_runtime",
    "load_daily_average_cost",
]

LOAD_OUTPUT_NAMES: Final[frozenset[LoadOutputName]] = frozenset(
    (
        LOAD_POWER := "load_power",
        # Per-energy shadow price ($/kWh) on the forecast-limit constraint
        LOAD_FORECAST_LIMIT_SHADOW_ENERGY_PRICE := "load_forecast_limit_shadow_energy_price",
        # Configured willingness-to-pay ceiling for sheddable loads
        LOAD_THRESHOLD_PRICE := "load_threshold_price",
        # Cumulative-horizon statistics (over the entire optimization horizon)
        LOAD_TOTAL_ENERGY := "load_total_energy",
        LOAD_TOTAL_COST := "load_total_cost",
        LOAD_TOTAL_RUNTIME := "load_total_runtime",
        LOAD_TOTAL_AVERAGE_COST := "load_total_average_cost",
        # Rolling-24h-forward statistics (next 24h from horizon start)
        LOAD_DAILY_ENERGY := "load_daily_energy",
        LOAD_DAILY_COST := "load_daily_cost",
        LOAD_DAILY_RUNTIME := "load_daily_runtime",
        LOAD_DAILY_AVERAGE_COST := "load_daily_average_cost",
    )
)

# Rolling-window length for the "daily" sensors (hours, forward from horizon start)
_DAILY_WINDOW_HOURS: Final[float] = 24.0
# Power threshold below which a timestep is considered "shed" (not running), in kW.
# Small but non-zero to ignore floating-point noise from the LP solver.
_RUNTIME_POWER_EPS: Final[float] = 1e-9

type LoadDeviceName = Literal[ElementType.LOAD]

LOAD_DEVICE_NAMES: Final[frozenset[LoadDeviceName]] = frozenset(
    (LOAD_DEVICE_LOAD := ElementType.LOAD,),
)


def _threshold_price(config: LoadConfigData) -> NDArray[np.floating[Any]] | float | None:
    """Return the configured threshold price, or None when absent / disabled.

    Returns None when curtailment is disabled (the load is fixed and the
    threshold has no effect) or when no threshold value is configured.
    """
    if not config.get(SECTION_CURTAILMENT, {}).get(CONF_CURTAILMENT, False):
        return None
    threshold_section = config.get(SECTION_THRESHOLD) or {}
    return threshold_section.get(CONF_THRESHOLD_PRICE)


class LoadAdapter:
    """Adapter for Load elements."""

    element_type: str = ELEMENT_TYPE
    advanced: bool = False
    connectivity: ConnectivityLevel = ConnectivityLevel.ADVANCED
    can_source: bool = False
    can_sink: bool = True

    def model_elements(self, config: LoadConfigData) -> list[ModelElementConfig]:
        """Create model elements for Load configuration."""
        sheddable = config.get(SECTION_CURTAILMENT, {}).get(CONF_CURTAILMENT, False)
        segments: dict[str, Any] = {
            "power_limit": {
                "segment_type": "power_limit",
                "max_power": config[SECTION_FORECAST][CONF_FORECAST],
                "fixed": not sheddable,
            },
        }
        # When curtailment is enabled, add a pricing segment encoding the willingness
        # to pay. ``transfer_cost`` is added to the LP objective (which is minimised);
        # we want a *benefit* when load is served, so the segment price is the
        # negation of the user-facing threshold price. Mirrors the grid export
        # convention (``price = -CONF_PRICE_TARGET_SOURCE``).
        threshold = _threshold_price(config)
        if threshold is not None:
            segments["pricing"] = {
                "segment_type": "pricing",
                "price": _negate(threshold),
            }
        return [
            {
                "element_type": MODEL_ELEMENT_TYPE_NODE,
                "name": config["name"],
                "is_source": False,
                "is_sink": True,
            },
            {
                "element_type": MODEL_ELEMENT_TYPE_CONNECTION,
                "name": f"{config['name']}:connection",
                "source": extract_connection_target(config[CONF_CONNECTION]),
                "target": config["name"],
                "is_time_sensitive": True,
                "segments": segments,
            },
        ]

    def outputs(
        self,
        name: str,
        model_outputs: Mapping[str, Mapping[ModelOutputName, ModelOutputValue]],
        *,
        config: LoadConfigData,
        periods: NDArray[np.floating[Any]],
        **_kwargs: Any,
    ) -> Mapping[LoadDeviceName, Mapping[LoadOutputName, OutputData]]:
        """Map model outputs to load-specific output names."""
        connection = model_outputs[f"{name}:connection"]
        fixed = not config.get(SECTION_CURTAILMENT, {}).get(CONF_CURTAILMENT, False)

        power = expect_output_data(connection[CONNECTION_POWER])
        load_outputs: dict[LoadOutputName, OutputData] = {
            LOAD_POWER: replace(power, type=OutputType.POWER, direction="-", fixed=fixed),
        }

        # Per-energy ($/kWh) shadow price from the forecast-limit constraint
        if (
            isinstance(segments_output := connection.get(CONNECTION_SEGMENTS), Mapping)
            and isinstance(power_limit_outputs := segments_output.get("power_limit"), Mapping)
            and (shadow := expect_output_data(power_limit_outputs.get("power_limit"))) is not None
            and (energy_shadow := shadow_price_per_energy(shadow, periods)) is not None
        ):
            load_outputs[LOAD_FORECAST_LIMIT_SHADOW_ENERGY_PRICE] = energy_shadow

        # Configured threshold-price visible as a $/kWh sensor (sheddable loads only)
        threshold = _threshold_price(config)
        if threshold is not None:
            load_outputs[LOAD_THRESHOLD_PRICE] = OutputData(
                type=OutputType.PRICE,
                unit="$/kWh",
                values=tuple(broadcast_to_sequence(threshold, len(periods))),
            )

        # Cumulative + rolling-24h statistics. Marginal cost uses the source-node
        # power-balance shadow price (node_dual, $/kW per timestep, already
        # period-integrated) so that cost = p_load * node_dual is in $.
        source_name = extract_connection_target(config[CONF_CONNECTION])
        node_dual = _node_dual_values(model_outputs, source_name, len(periods))
        if node_dual is not None:
            power_values = tuple(float(v) for v in power.values)
            period_values = tuple(float(v) for v in periods)
            load_outputs.update(_stats_outputs(power_values, node_dual, period_values))

        return {LOAD_DEVICE_LOAD: load_outputs}


def _negate(value: NDArray[np.floating[Any]] | float) -> NDArray[np.floating[Any]] | float:
    """Negate a scalar price or each element of an array of prices."""
    if isinstance(value, np.ndarray):
        return -value
    return -float(value)


def _node_dual_values(
    model_outputs: Mapping[str, Mapping[ModelOutputName, ModelOutputValue]],
    source_name: str,
    n_periods: int,
) -> tuple[float, ...] | None:
    """Return the source-node power-balance shadow price ($/kW per timestep).

    The shadow price on an element power-balance constraint is already
    period-integrated by the LP (the objective sums ``power * price * dt``),
    so ``power_load * node_dual`` gives a per-timestep cost in dollars.

    Returns None when the source node has no ``element_power_balance`` output
    (e.g. unconstrained pass-through nodes) so that stats sensors are simply
    omitted rather than reporting zero.
    """
    source_outputs = model_outputs.get(source_name)
    if source_outputs is None:
        return None
    dual = expect_output_data(source_outputs.get(ELEMENT_POWER_BALANCE))
    if dual is None:
        return None
    values = tuple(float(v) for v in dual.values)
    if len(values) != n_periods:
        return None
    return values


def _stats_outputs(
    power: Sequence[float],
    node_dual: Sequence[float],
    periods: Sequence[float],
) -> dict[LoadOutputName, OutputData]:
    """Build the 8 cumulative + rolling-24h statistics outputs.

    Cost is integrated as ``cost[t] = power[t] * node_dual[t]`` ($), where
    ``node_dual[t]`` is the source-node power-balance shadow price
    ($/kW per timestep, already period-integrated by the LP objective).

    Energy is ``power[t] * periods[t]`` (kWh) and runtime is ``periods[t]``
    for timesteps where ``power[t] > _RUNTIME_POWER_EPS`` (h).

    Average cost is energy-weighted (``total_cost / total_energy``, $/kWh),
    falling back to 0.0 when ``total_energy <= 0`` to avoid division by zero.

    """
    n = len(periods)
    energy_per_step = tuple(p * dt for p, dt in zip(power, periods, strict=True))
    cost_per_step = tuple(p * d for p, d in zip(power, node_dual, strict=True))
    runtime_per_step = tuple(dt if abs(p) > _RUNTIME_POWER_EPS else 0.0 for p, dt in zip(power, periods, strict=True))

    # Cumulative time series for the "total" sensors. state_last=True so the
    # sensor value reflects the horizon total rather than the first timestep.
    energy_cumsum = _cumsum(energy_per_step)
    cost_cumsum = _cumsum(cost_per_step)
    runtime_cumsum = _cumsum(runtime_per_step)
    avg_cost_cumsum = tuple(_safe_divide(c, e) for c, e in zip(cost_cumsum, energy_cumsum, strict=True))

    # Rolling-24h-forward window starting from t=0 (the present): include each
    # timestep whose cumulative duration up to (and including) it is within
    # _DAILY_WINDOW_HOURS. Anything beyond is clipped to zero, with the partial
    # final timestep included whole when it straddles the boundary (matches the
    # period-quantised LP semantics).
    daily_mask = _daily_window_mask(periods)
    daily_energy = sum(e for e, m in zip(energy_per_step, daily_mask, strict=True) if m)
    daily_cost = sum(c for c, m in zip(cost_per_step, daily_mask, strict=True) if m)
    daily_runtime = sum(r for r, m in zip(runtime_per_step, daily_mask, strict=True) if m)
    daily_avg_cost = _safe_divide(daily_cost, daily_energy)

    return {
        LOAD_TOTAL_ENERGY: OutputData(type=OutputType.ENERGY, unit="kWh", values=energy_cumsum, state_last=True),
        LOAD_TOTAL_COST: OutputData(type=OutputType.COST, unit="$", values=cost_cumsum, direction="-", state_last=True),
        LOAD_TOTAL_RUNTIME: OutputData(type=OutputType.DURATION, unit="h", values=runtime_cumsum, state_last=True),
        LOAD_TOTAL_AVERAGE_COST: OutputData(
            type=OutputType.PRICE, unit="$/kWh", values=avg_cost_cumsum, state_last=True
        ),
        LOAD_DAILY_ENERGY: OutputData(type=OutputType.ENERGY, unit="kWh", values=(daily_energy,) * n),
        LOAD_DAILY_COST: OutputData(type=OutputType.COST, unit="$", values=(daily_cost,) * n, direction="-"),
        LOAD_DAILY_RUNTIME: OutputData(type=OutputType.DURATION, unit="h", values=(daily_runtime,) * n),
        LOAD_DAILY_AVERAGE_COST: OutputData(type=OutputType.PRICE, unit="$/kWh", values=(daily_avg_cost,) * n),
    }


def _cumsum(values: Sequence[float]) -> tuple[float, ...]:
    """Return a tuple containing the running sum of ``values``."""
    out: list[float] = []
    running = 0.0
    for v in values:
        running += v
        out.append(running)
    return tuple(out)


def _safe_divide(numerator: float, denominator: float) -> float:
    """Divide ``numerator`` by ``denominator``, returning 0.0 when denominator is non-positive."""
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _daily_window_mask(periods: Sequence[float]) -> tuple[bool, ...]:
    """Return a per-timestep mask selecting the rolling 24h forward window.

    A timestep is included when its start lies strictly within the first
    ``_DAILY_WINDOW_HOURS`` of the horizon. This keeps each timestep whole and
    matches the period-quantised LP semantics: an hourly horizon yields 24
    selected timesteps; a 30-minute horizon yields 48; and a sparser schedule
    keeps timesteps whose start time falls within the window.

    """
    mask: list[bool] = []
    elapsed = 0.0
    for dt in periods:
        mask.append(elapsed < _DAILY_WINDOW_HOURS)
        elapsed += dt
    return tuple(mask)


adapter = LoadAdapter()
