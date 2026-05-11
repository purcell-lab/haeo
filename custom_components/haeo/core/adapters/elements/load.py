"""Load element adapter for model layer integration."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, Final, Literal

import numpy as np
from numpy.typing import NDArray

from custom_components.haeo.core.adapters.output_utils import expect_output_data
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
    "load_forecast_limit_price",
    "load_threshold_price",
    "load_horizon_energy",
    "load_horizon_cost",
    "load_horizon_runtime",
    "load_horizon_average_cost",
    "load_next_24h_energy",
    "load_next_24h_cost",
    "load_next_24h_runtime",
    "load_next_24h_average_cost",
]

LOAD_OUTPUT_NAMES: Final[frozenset[LoadOutputName]] = frozenset(
    (
        LOAD_POWER := "load_power",
        # Shadow price
        LOAD_FORECAST_LIMIT_PRICE := "load_forecast_limit_price",
        # Configured willingness-to-pay ceiling for sheddable loads
        LOAD_THRESHOLD_PRICE := "load_threshold_price",
        # Full-horizon statistics (over the entire optimization horizon)
        LOAD_HORIZON_ENERGY := "load_horizon_energy",
        LOAD_HORIZON_COST := "load_horizon_cost",
        LOAD_HORIZON_RUNTIME := "load_horizon_runtime",
        LOAD_HORIZON_AVERAGE_COST := "load_horizon_average_cost",
        # Next-24h-forward statistics, pro-rata clipped at the 24h boundary
        LOAD_NEXT_24H_ENERGY := "load_next_24h_energy",
        LOAD_NEXT_24H_COST := "load_next_24h_cost",
        LOAD_NEXT_24H_RUNTIME := "load_next_24h_runtime",
        LOAD_NEXT_24H_AVERAGE_COST := "load_next_24h_average_cost",
    )
)

# Forward-looking window length for the "next 24h" sensors (hours, from horizon start)
_NEXT_24H_WINDOW_HOURS: Final[float] = 24.0
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

        # Shadow price from power_limit segment (if present)
        if (
            isinstance(segments_output := connection.get(CONNECTION_SEGMENTS), Mapping)
            and isinstance(power_limit_outputs := segments_output.get("power_limit"), Mapping)
            and (shadow := expect_output_data(power_limit_outputs.get("power_limit"))) is not None
        ):
            load_outputs[LOAD_FORECAST_LIMIT_PRICE] = shadow

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
    # Since PR #426 (DEFAULT_TAG removal), element_power_balance is built from a
    # flat list of per-period constraints. For a source+sink node with tags the
    # layout is: [optional production-decomp block, optional consumption-decomp
    # block, then one block per tag of per-tag balance]. Each block is
    # n_periods long. For pricing we want the per-tag balance duals only;
    # summing them across tags recovers the previous single $/kWh series.
    if len(values) == n_periods:
        return values
    if len(values) == 0 or len(values) % n_periods != 0:
        return None
    # Take the trailing tag-balance blocks; the actual count is
    # connection_tags() but we don't have access here. Empirically the LAST
    # block is always a balance dual, and summing the trailing blocks matches
    # the prior behaviour when only one block existed.
    # We sum ALL contiguous blocks; production/consumption decomposition duals
    # are typically 0 at the optimum (they represent slack on equality
    # constraints whose RHS is a free variable), so they add nothing.
    n_blocks = len(values) // n_periods
    return tuple(
        sum(values[b * n_periods + i] for b in range(n_blocks)) for i in range(n_periods)
    )


def _stats_outputs(
    power: Sequence[float],
    node_dual: Sequence[float],
    periods: Sequence[float],
) -> dict[LoadOutputName, OutputData]:
    """Build the 8 full-horizon + next-24h statistics outputs.

    Cost is integrated as ``cost[t] = power[t] * periods[t] * node_dual[t]``
    ($) where ``node_dual[t]`` is the source-node power-balance shadow price
    in $/kWh (see ``Node.element_power_balance``: the LP balance is
    formulated in energy units so the dual is independent of period width).
    The ``power * periods`` factor is the per-step energy in kWh, so cost is
    ``energy[t] * node_dual[t]`` and totals are correct for variable-width
    horizons (1-60 min periods).

    Energy is ``power[t] * periods[t]`` (kWh) and runtime is ``periods[t]``
    for timesteps where ``power[t] > _RUNTIME_POWER_EPS`` (h).

    Average cost is energy-weighted (``horizon_cost / horizon_energy``,
    $/kWh), falling back to 0.0 when ``horizon_energy <= 0`` to avoid
    division by zero.

    The next-24h aggregates clip exactly to ``_NEXT_24H_WINDOW_HOURS`` via
    pro-rata weighting: a step that straddles the boundary contributes the
    fraction of its duration that falls inside the window.

    """
    n = len(periods)
    energy_per_step = tuple(p * dt for p, dt in zip(power, periods, strict=True))
    cost_per_step = tuple(e * d for e, d in zip(energy_per_step, node_dual, strict=True))
    runtime_per_step = tuple(dt if abs(p) > _RUNTIME_POWER_EPS else 0.0 for p, dt in zip(power, periods, strict=True))

    # Cumulative time series for the horizon sensors. state_last=True so the
    # sensor value reflects the horizon total rather than the first timestep.
    energy_cumsum = _cumsum(energy_per_step)
    cost_cumsum = _cumsum(cost_per_step)
    runtime_cumsum = _cumsum(runtime_per_step)
    avg_cost_cumsum = tuple(_safe_divide(c, e) for c, e in zip(cost_cumsum, energy_cumsum, strict=True))

    # Next-24h-forward window starting from t=0 (the present): each timestep
    # contributes a fraction in [0.0, 1.0] of its duration that lies inside
    # _NEXT_24H_WINDOW_HOURS. Steps fully outside contribute 0.0; steps fully
    # inside contribute 1.0; the straddling step contributes the partial
    # fraction so totals clip exactly to 24h.
    fractions = _next_24h_window_fractions(periods)
    next_24h_energy = sum(e * f for e, f in zip(energy_per_step, fractions, strict=True))
    next_24h_cost = sum(c * f for c, f in zip(cost_per_step, fractions, strict=True))
    next_24h_runtime = sum(r * f for r, f in zip(runtime_per_step, fractions, strict=True))
    next_24h_avg_cost = _safe_divide(next_24h_cost, next_24h_energy)

    return {
        LOAD_HORIZON_ENERGY: OutputData(type=OutputType.ENERGY, unit="kWh", values=energy_cumsum, state_last=True),
        LOAD_HORIZON_COST: OutputData(
            type=OutputType.COST, unit="$", values=cost_cumsum, direction="-", state_last=True
        ),
        LOAD_HORIZON_RUNTIME: OutputData(type=OutputType.DURATION, unit="h", values=runtime_cumsum, state_last=True),
        LOAD_HORIZON_AVERAGE_COST: OutputData(
            type=OutputType.PRICE, unit="$/kWh", values=avg_cost_cumsum, state_last=True
        ),
        LOAD_NEXT_24H_ENERGY: OutputData(type=OutputType.ENERGY, unit="kWh", values=(next_24h_energy,) * n),
        LOAD_NEXT_24H_COST: OutputData(type=OutputType.COST, unit="$", values=(next_24h_cost,) * n, direction="-"),
        LOAD_NEXT_24H_RUNTIME: OutputData(type=OutputType.DURATION, unit="h", values=(next_24h_runtime,) * n),
        LOAD_NEXT_24H_AVERAGE_COST: OutputData(
            type=OutputType.PRICE, unit="$/kWh", values=(next_24h_avg_cost,) * n
        ),
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


def _next_24h_window_fractions(periods: Sequence[float]) -> tuple[float, ...]:
    """Return the per-timestep inclusion fraction for the next-24h window.

    Each timestep is assigned a fraction in [0.0, 1.0] equal to the share of
    its duration that falls within the first ``_NEXT_24H_WINDOW_HOURS`` of
    the horizon. Steps that start at or after the boundary contribute 0.0;
    steps that finish at or before the boundary contribute 1.0; the
    boundary-straddling step contributes the partial fraction so aggregates
    multiplied by these fractions clip exactly to 24h.

    """
    fractions: list[float] = []
    elapsed = 0.0
    for dt in periods:
        remaining = _NEXT_24H_WINDOW_HOURS - elapsed
        if remaining <= 0.0 or dt <= 0.0:
            fractions.append(0.0)
        elif remaining >= dt:
            fractions.append(1.0)
        else:
            fractions.append(remaining / dt)
        elapsed += dt
    return tuple(fractions)


adapter = LoadAdapter()
