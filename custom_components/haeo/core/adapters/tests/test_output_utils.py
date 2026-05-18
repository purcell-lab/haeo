"""Tests for adapter output helpers."""

import pytest

from custom_components.haeo.core.adapters.output_utils import expect_output_data, per_period_dual
from custom_components.haeo.core.model.const import OutputType
from custom_components.haeo.core.model.output_data import OutputData


def _dual(values: tuple[float, ...]) -> OutputData:
    return OutputData(type=OutputType.SHADOW_PRICE, unit="$/kWh", values=values)


def test_expect_output_data_passthrough() -> None:
    """``expect_output_data`` returns the input dual unchanged or ``None``."""
    dual = _dual((0.1, 0.2))
    assert expect_output_data(dual) is dual
    assert expect_output_data(None) is None


class TestPerPeriodDual:
    """``per_period_dual`` collapses multi-block ``element_power_balance`` duals."""

    def test_none_input_returns_none(self) -> None:
        """A missing dual maps to ``None`` so callers can skip the sensor."""
        assert per_period_dual(None, 4) is None

    def test_single_block_returned_unchanged(self) -> None:
        """When ``len(values) == n_periods`` the dual is the per-period series already."""
        dual = _dual((0.10, 0.20, 0.30, 0.40))
        result = per_period_dual(dual, 4)
        assert result is dual

    def test_two_block_dual_sums_across_blocks(self) -> None:
        """A 2-block dual is summed across blocks to recover the per-period series."""
        # Block A: (0.1, 0.2, 0.3, 0.4) — e.g. production decomposition (zeros at optimum)
        # Block B: (0.5, 0.6, 0.7, 0.8) — per-tag balance
        dual = _dual((0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8))
        result = per_period_dual(dual, 4)
        assert result is not None
        assert result.values == pytest.approx((0.6, 0.8, 1.0, 1.2))
        # Other dataclass fields preserved.
        assert result.type == OutputType.SHADOW_PRICE
        assert result.unit == "$/kWh"

    def test_three_block_dual_sums_across_blocks(self) -> None:
        """A 3-block dual (production + consumption + per-tag balance) is summed."""
        # 12 values across 4 periods = 3 blocks (production + consumption + tag balance).
        values = tuple(float(i) for i in range(12))
        dual = _dual(values)
        result = per_period_dual(dual, 4)
        assert result is not None
        # Expected per-period sums: i=0: 0+4+8=12, i=1: 1+5+9=15, i=2: 2+6+10=18, i=3: 3+7+11=21
        assert result.values == (12.0, 15.0, 18.0, 21.0)

    def test_empty_values_returns_none(self) -> None:
        """An empty values tuple maps to ``None`` (no shadow price to report)."""
        dual = _dual(())
        assert per_period_dual(dual, 4) is None

    def test_non_multiple_length_returns_none(self) -> None:
        """A length not divisible by ``n_periods`` maps to ``None`` (defensive)."""
        # 7 values cannot be split into n_periods=4 blocks evenly.
        dual = _dual((0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7))
        assert per_period_dual(dual, 4) is None

    def test_preserves_other_dataclass_fields_on_collapse(self) -> None:
        """Collapsing preserves non-values fields (direction/fixed/advanced/etc.)."""
        dual = OutputData(
            type=OutputType.SHADOW_PRICE,
            unit="$/kWh",
            values=(0.1, 0.2, 0.3, 0.4),
            direction="+",
            fixed=True,
            advanced=True,
        )
        result = per_period_dual(dual, 2)
        assert result is not None
        assert result.values == pytest.approx((0.4, 0.6))
        assert result.direction == "+"
        assert result.fixed is True
        assert result.advanced is True
