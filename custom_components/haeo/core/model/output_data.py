"""Output data specification for model elements."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
import warnings

import numpy as np

from .const import OutputType, StateSource


@dataclass(slots=True)
class OutputData:
    """Specification for an output exposed by a model element.

    Attributes:
        type: The output type (power, energy, SOC, etc.).
        unit: The unit of measurement for the output values (e.g., "W", "Wh", "%").
        values: The sequence of output values.
        direction: Power flow direction from the energy system's perspective.
            "+" = production: power added to the system (solar generation, battery discharge, grid import).
            "-" = consumption: power removed from the system (load demand, battery charge, grid export).
            None = non-directional output (SOC, prices, energy, shadow prices).
        advanced: Whether the output is intended for advanced diagnostics only.
        state_source: How the sensor ``state`` is derived from ``values``.
            See ``StateSource`` for the options. Defaults to ``HORIZON_FIRST``.
        is_forecast: If True, the sensor state is a planned (forecast) value from the
            optimizer rather than a measurement. Two extra-state attributes are
            emitted so downstream automations can distinguish planned values from
            measurements:
              - ``is_forecast: true``
              - ``next_planned: <values[0]>``
            Mitigates hass-energy/haeo#477 (transient grid disturbances when horizon[0]
            swings are interpreted as measured setpoints). Orthogonal to
            ``state_source``: a forecast can still publish a scalar state
            (``HORIZON_FIRST``) or suppress it (``NONE``).
        priority: Connection time-preference priority. Lower values are preferred
            earlier by the secondary objective. None for non-connection outputs.
        fixed: Whether the output is constrained to equal its forecast (no curtailment).

    """

    type: OutputType
    unit: str | None
    values: Sequence[Any]
    direction: Literal["+", "-"] | None = None
    advanced: bool = False
    state_source: StateSource = StateSource.HORIZON_FIRST
    is_forecast: bool = False
    priority: int | None = None
    fixed: bool = False

    def __init__(
        self,
        type: OutputType,  # noqa: A002 (shadows builtin but matches OutputType field naming convention)
        unit: str | None,
        values: Sequence[Any] | Any,
        direction: Literal["+", "-"] | None = None,
        *,
        advanced: bool = False,
        state_source: StateSource | None = None,
        state_last: bool | None = None,
        is_forecast: bool = False,
        priority: int | None = None,
        fixed: bool = False,
    ) -> None:
        """Initialize OutputData.

        Args:
            type: The output type (power, energy, SOC, etc.).
            unit: The unit of measurement for the output values.
            values: A single value or sequence of values (already extracted from HiGHS types).
            direction: Power flow direction relative to the element.
            advanced: Whether the output is intended for advanced diagnostics only.
            state_source: How ``state`` is derived from ``values`` (see ``StateSource``).
                Defaults to ``StateSource.HORIZON_FIRST``.
            state_last: Deprecated. If True, equivalent to
                ``state_source=StateSource.HORIZON_LAST``. Cannot be combined with
                an explicit ``state_source`` argument. Will be removed in a future
                release.
            is_forecast: If True, mark the state as a planned value (see class docstring).
            priority: The connection priority for this output, if applicable.
            fixed: Whether the output is constrained to equal its forecast (no curtailment).

        """
        # Resolve state_source from the (possibly deprecated) state_last alias.
        if state_last is not None:
            if state_source is not None:
                msg = "Pass either state_source= or state_last=, not both."
                raise TypeError(msg)
            warnings.warn(
                "OutputData(state_last=...) is deprecated; use "
                "state_source=StateSource.HORIZON_LAST instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            state_source = StateSource.HORIZON_LAST if state_last else StateSource.HORIZON_FIRST
        self.type = type
        self.unit = unit
        self.direction = direction
        self.advanced = advanced
        self.state_source = state_source if state_source is not None else StateSource.HORIZON_FIRST
        self.is_forecast = is_forecast
        self.priority = priority
        self.fixed = fixed

        # Normalize to a tuple
        if isinstance(values, np.ndarray):
            # Convert numpy arrays to tuple (flattens properly)
            self.values = tuple(values.flat)
        elif isinstance(values, Sequence) and not isinstance(values, str):
            # Convert sequences to tuple
            self.values = tuple(values)
        else:
            # Wrap single values in tuple
            self.values = (values,)

    @property
    def state_last(self) -> bool:
        """Back-compat shim. True when ``state_source`` is ``HORIZON_LAST``.

        Deprecated: read ``state_source`` directly.
        """
        return self.state_source == StateSource.HORIZON_LAST


type ModelOutputValue = OutputData | Mapping[str, OutputData] | Mapping[str, Mapping[str, OutputData]]
