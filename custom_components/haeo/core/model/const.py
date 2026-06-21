"""Constants for HAEO energy modeling."""

from enum import StrEnum, auto


class OutputType(StrEnum):
    """Output type categories for sensors and input fields.

    These values categorize model outputs and input fields by physical meaning,
    enabling automatic unit specification lookup for entity filtering.

    Power types:
        POWER: Active power (kW)
        POWER_FLOW: Directional power flow between elements (kW)
        POWER_LIMIT: Maximum power constraints (kW)

    Energy types:
        ENERGY: Energy quantity (kWh)

    Percentage types:
        SOC: State of charge ratio (0-1, displayed as %)
        EFFICIENCY: Efficiency ratio (0-1, displayed as %)

    Monetary types:
        PRICE: Price per energy unit ($/kWh, €/kWh, etc.)
        PRICE_RATE: Price rate per energy unit per hour ($/kWh/h) for holding costs
        COST: Total cost in currency units

    Other types:
        STATUS: Boolean or categorical status
        DURATION: Time duration
        SHADOW_PRICE: Shadow prices from LP constraints

    """

    POWER = auto()
    POWER_FLOW = auto()
    POWER_LIMIT = auto()
    ENERGY = auto()
    PRICE = auto()
    PRICE_RATE = auto()
    STATE_OF_CHARGE = auto()
    EFFICIENCY = auto()
    COST = auto()
    STATUS = auto()
    DURATION = auto()
    SHADOW_PRICE = auto()


class StateSource(StrEnum):
    """How a sensor's ``state`` is derived from the optimizer output ``values``.

    HORIZON_FIRST: ``state = values[0]`` (default). The first horizon period
        represents the optimizer's current-period setpoint.
    HORIZON_LAST: ``state = values[-1]``. Used for cumulative series
        (for example cost cumsums) where the running total at the end of the
        horizon is the meaningful current state.
    NONE: ``state = None``. The optimizer publishes a horizon but no
        scalar state. Downstream consumers must read the ``forecast`` or
        ``next_planned`` attributes. Useful where exposing a forecast value
        as state has been observed to drive physical actuators incorrectly
        (see hass-energy/haeo#477).
    """

    HORIZON_FIRST = auto()
    HORIZON_LAST = auto()
    NONE = auto()
