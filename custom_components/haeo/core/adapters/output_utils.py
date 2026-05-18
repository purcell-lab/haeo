"""Utilities for validating adapter output mappings."""

from dataclasses import replace
from typing import overload

from custom_components.haeo.core.model import ModelOutputValue
from custom_components.haeo.core.model.output_data import OutputData


@overload
def expect_output_data(value: None) -> None: ...


@overload
def expect_output_data(value: OutputData) -> OutputData: ...


@overload
def expect_output_data(value: ModelOutputValue) -> OutputData: ...


def expect_output_data(value: ModelOutputValue | None) -> OutputData | None:
    """Return OutputData when present, or None."""
    if value is None:
        return None
    if not isinstance(value, OutputData):
        raise TypeError
    return value


def per_period_dual(dual: OutputData | None, n_periods: int) -> OutputData | None:
    """Collapse a multi-block ``element_power_balance`` dual to one $/kWh series.

    After PR #426 (DEFAULT_TAG removal), ``Node.element_power_balance()``
    returns a flat list of per-period constraint duals laid out as
    ``[optional production-decomp block, optional consumption-decomp block,
    then one block per tag of per-tag balance]``. Each block is ``n_periods``
    long.

    For shadow-price reporting we want a single per-period $/kWh series, so
    we sum across blocks. Production/consumption decomposition duals are
    typically zero at the optimum (slack on equality constraints whose RHS
    is a free variable), so the summed value matches the prior single-block
    behaviour where only one block existed.

    Returns ``None`` when ``dual`` is missing or has an unexpected length
    (not a multiple of ``n_periods``); callers should treat that as "shadow
    price unavailable" and skip the affected sensor.
    """
    if dual is None:
        return None
    values = tuple(float(v) for v in dual.values)
    if len(values) == n_periods:
        return dual
    if not values or len(values) % n_periods != 0:
        return None
    n_blocks = len(values) // n_periods
    collapsed = tuple(
        sum(values[b * n_periods + i] for b in range(n_blocks)) for i in range(n_periods)
    )
    return replace(dual, values=collapsed)


__all__ = ["expect_output_data", "per_period_dual"]
