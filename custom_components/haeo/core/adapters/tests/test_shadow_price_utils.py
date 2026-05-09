"""Tests for shadow_price_utils module."""

import logging
from typing import Any

import numpy as np
import pytest

from custom_components.haeo.core.adapters.shadow_price_utils import shadow_price_per_energy, shadow_price_per_power
from custom_components.haeo.core.model.const import OutputType
from custom_components.haeo.core.model.output_data import OutputData


def _shadow(values: tuple[float, ...], unit: str = "$/kW", **kwargs: Any) -> OutputData:
    return OutputData(type=OutputType.SHADOW_PRICE, unit=unit, values=values, **kwargs)


def test_per_energy_uniform_one_hour_periods_is_identity_in_value() -> None:
    """1-hour periods give a value-identity conversion (only the unit changes)."""
    shadow = _shadow((0.10, 0.20, 0.30))
    periods = np.array([1.0, 1.0, 1.0])
    result = shadow_price_per_energy(shadow, periods)
    assert result is not None
    assert result.unit == "$/kWh"
    assert result.values == (0.10, 0.20, 0.30)


def test_per_energy_non_uniform_periods() -> None:
    """Variable-width periods produce per-element scaled values."""
    # 5-min, 5-min, 30-min, 1-hour periods
    shadow = _shadow((0.10, 0.20, 0.30, 0.40))
    periods = np.array([1.0 / 12.0, 1.0 / 12.0, 0.5, 1.0])
    result = shadow_price_per_energy(shadow, periods)
    assert result is not None
    assert result.unit == "$/kWh"
    assert result.values[0] == pytest.approx(0.10 * 12.0)
    assert result.values[1] == pytest.approx(0.20 * 12.0)
    assert result.values[2] == pytest.approx(0.60)
    assert result.values[3] == pytest.approx(0.40)


def test_per_energy_zero_period_yields_zero_no_division_error() -> None:
    """A zero-length period maps to 0.0 instead of raising ZeroDivisionError."""
    shadow = _shadow((0.10, 0.20))
    periods = np.array([0.0, 0.5])
    result = shadow_price_per_energy(shadow, periods)
    assert result is not None
    assert result.values[0] == 0.0
    assert result.values[1] == pytest.approx(0.40)


def test_per_energy_preserves_attributes() -> None:
    """Direction, advanced, state_last and type carry through the conversion."""
    shadow = _shadow(
        (0.10,),
        direction=None,
        advanced=True,
        state_last=True,
    )
    periods = np.array([0.5])
    result = shadow_price_per_energy(shadow, periods)
    assert result is not None
    assert result.advanced is True
    assert result.state_last is True
    assert result.direction is None
    assert result.type == OutputType.SHADOW_PRICE


def test_round_trip_per_energy_then_per_power() -> None:
    """per_energy followed by per_power recovers the original $/kW values."""
    shadow = _shadow((0.10, 0.25, 0.50))
    periods = np.array([1.0 / 12.0, 0.5, 1.0])
    energy = shadow_price_per_energy(shadow, periods)
    assert energy is not None
    back = shadow_price_per_power(energy, periods)
    assert back is not None
    assert back.unit == "$/kW"
    for original, recovered in zip(shadow.values, back.values, strict=True):
        assert recovered == pytest.approx(original)


def test_per_energy_tagged_shadow_is_chunked_per_period() -> None:
    """Tagged shadows (n_tags * n_periods values) divide each block by the period vector."""
    # 2 tags, 3 periods: tag-A=[0.1, 0.2, 0.3], tag-B=[0.4, 0.5, 0.6]
    shadow = _shadow((0.1, 0.2, 0.3, 0.4, 0.5, 0.6))
    periods = np.array([0.5, 1.0, 2.0])
    result = shadow_price_per_energy(shadow, periods)
    assert result is not None
    assert result.unit == "$/kWh"
    assert result.values[0] == pytest.approx(0.2)
    assert result.values[1] == pytest.approx(0.2)
    assert result.values[2] == pytest.approx(0.15)
    assert result.values[3] == pytest.approx(0.8)
    assert result.values[4] == pytest.approx(0.5)
    assert result.values[5] == pytest.approx(0.3)


def test_per_energy_misaligned_shape_returns_none() -> None:
    """If the shadow has a length that is not a multiple of n_periods, return None."""
    shadow = _shadow((0.1, 0.2, 0.3, 0.4, 0.5))
    periods = np.array([1.0, 1.0, 1.0])
    assert shadow_price_per_energy(shadow, periods) is None


def test_per_energy_empty_periods_returns_none() -> None:
    """An empty periods array cannot align to any shadow shape."""
    shadow = _shadow((0.1,))
    periods = np.array([])
    assert shadow_price_per_energy(shadow, periods) is None


def test_per_energy_wrong_unit_raises() -> None:
    """Calling per_energy on something that is already $/kWh is a programmer error."""
    shadow = _shadow((0.10,), unit="$/kWh")
    with pytest.raises(ValueError, match=r"\$/kW"):
        shadow_price_per_energy(shadow, np.array([1.0]))


def test_per_power_wrong_unit_raises() -> None:
    """Calling per_power on something that is already $/kW is a programmer error."""
    shadow = _shadow((0.10,), unit="$/kW")
    with pytest.raises(ValueError, match=r"\$/kWh"):
        shadow_price_per_power(shadow, np.array([1.0]))


def test_per_power_basic_conversion() -> None:
    """A $/kWh shadow is multiplied by period length to recover $/kW."""
    shadow = _shadow((0.10, 0.20), unit="$/kWh")
    periods = np.array([0.5, 2.0])
    result = shadow_price_per_power(shadow, periods)
    assert result is not None
    assert result.unit == "$/kW"
    assert result.values[0] == pytest.approx(0.05)
    assert result.values[1] == pytest.approx(0.40)


_UTILS_LOGGER = "custom_components.haeo.core.adapters.shadow_price_utils"


@pytest.fixture
def captured_logs(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> pytest.LogCaptureFixture:
    """Force propagation on the `custom_components.haeo` logger so caplog can see records.

    The session-scoped configure_logging fixture sets propagate=False on that logger to keep
    test output quiet, which also hides records from caplog's root-attached handler.
    """
    haeo_logger = logging.getLogger("custom_components.haeo")
    monkeypatch.setattr(haeo_logger, "propagate", True)
    caplog.set_level(logging.WARNING, logger=_UTILS_LOGGER)
    return caplog


def test_per_energy_misaligned_logs_warning(captured_logs: pytest.LogCaptureFixture) -> None:
    """Misaligned shapes emit a WARNING describing the skip so operators can debug."""
    shadow = _shadow((0.1, 0.2, 0.3, 0.4, 0.5))
    periods = np.array([1.0, 1.0, 1.0])
    result = shadow_price_per_energy(shadow, periods)
    assert result is None
    matching = [
        r
        for r in captured_logs.records
        if r.name == _UTILS_LOGGER and r.levelno == logging.WARNING and "per-energy" in r.getMessage()
    ]
    assert matching, f"expected per-energy WARNING, got {[r.getMessage() for r in captured_logs.records]}"
    assert "5 values" in matching[0].getMessage()
    assert "3 periods" in matching[0].getMessage()


def test_per_power_misaligned_logs_warning(captured_logs: pytest.LogCaptureFixture) -> None:
    """Misaligned shapes on the inverse direction also emit a WARNING."""
    shadow = _shadow((0.1, 0.2, 0.3, 0.4, 0.5), unit="$/kWh")
    periods = np.array([1.0, 1.0, 1.0])
    result = shadow_price_per_power(shadow, periods)
    assert result is None
    matching = [
        r
        for r in captured_logs.records
        if r.name == _UTILS_LOGGER and r.levelno == logging.WARNING and "per-power" in r.getMessage()
    ]
    assert matching, f"expected per-power WARNING, got {[r.getMessage() for r in captured_logs.records]}"


def test_aligned_shape_does_not_log_warning(captured_logs: pytest.LogCaptureFixture) -> None:
    """Aligned shapes (1:1 and n_tags x n_periods) must not emit a warning."""
    shadow_1 = _shadow((0.1, 0.2, 0.3))
    periods = np.array([1.0, 1.0, 1.0])
    shadow_tagged = _shadow((0.1, 0.2, 0.3, 0.4, 0.5, 0.6))
    assert shadow_price_per_energy(shadow_1, periods) is not None
    assert shadow_price_per_energy(shadow_tagged, periods) is not None
    warnings_for_utils = [r for r in captured_logs.records if r.name == _UTILS_LOGGER and r.levelno >= logging.WARNING]
    assert warnings_for_utils == []
