"""Regression tests for battery limit reactivity (issue #467).

User-visible bug:
    When a user edits ``number.battery_min_charge`` (or ``number.battery_max_charge``),
    the optimization-context diagnostics dump kept reporting the original value
    while the underlying optimizer was using fresh values. This made it look like
    the integration required a full reload for the change to take effect.

Reactive chain (all links must work):
    1.  User changes ``number.battery_min_charge`` to 20.
    2.  ``HaeoInputNumber.async_set_native_value(20)`` -> ``store.set_value(20)``.
    3.  Store calls ``_resolve_from_constant(mark_ready=True)`` -> ``_notify()``.
    4.  Coordinator listener ``_handle_element_update("Battery")`` fires.
    5.  Listener calls ``_load_element_config("Battery")`` returning new config.
    6.  Config queued in ``_pending_element_updates["Battery"]``.
    7.  ``signal_optimization_stale()`` triggers debounced ``async_refresh()``.
    8.  ``_async_update_data()`` calls ``_apply_pending_element_updates()``.
    9.  Updater calls ``adapter.model_elements(new_config)`` and writes to
        ``TrackedParam`` descriptors on the live model element.
    10. ``network.optimize()`` runs with the new bounds.
    11. ``_build_optimization_context`` records the live subentry data so the
        resulting diagnostics dump reflects what the optimizer actually used.

Tests cover step 9 (in isolation), steps 3-6 (in isolation), step 11
(the diagnostics fix added by #467), and the integrated 3-9 chain.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, cast
from unittest.mock import MagicMock, patch

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
import numpy as np
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haeo import HaeoRuntimeData
from custom_components.haeo.const import CONF_INTEGRATION_TYPE, DOMAIN, INTEGRATION_TYPE_HUB
from custom_components.haeo.coordinator.coordinator import HaeoDataUpdateCoordinator
from custom_components.haeo.coordinator.network import create_network
from custom_components.haeo.core.const import (
    CONF_DEBOUNCE_SECONDS,
    CONF_ELEMENT_TYPE,
    CONF_NAME,
    CONF_TIER_1_COUNT,
    CONF_TIER_1_DURATION,
    CONF_TIER_2_COUNT,
    CONF_TIER_2_DURATION,
    CONF_TIER_3_COUNT,
    CONF_TIER_3_DURATION,
    CONF_TIER_4_COUNT,
    CONF_TIER_4_DURATION,
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_TIER_2_DURATION,
    DEFAULT_TIER_3_DURATION,
    DEFAULT_TIER_4_DURATION,
)
from custom_components.haeo.core.data.loader.config_loader import load_element_config_from_values
from custom_components.haeo.core.model.elements.battery import Battery
from custom_components.haeo.core.schema import as_connection_target, as_constant_value
from custom_components.haeo.core.schema.elements import ElementType
from custom_components.haeo.core.schema.elements.battery import (
    CONF_CAPACITY,
    CONF_EFFICIENCY_SOURCE_TARGET,
    CONF_EFFICIENCY_TARGET_SOURCE,
    CONF_INITIAL_CHARGE_PERCENTAGE,
    CONF_MAX_CHARGE_PERCENTAGE,
    CONF_MAX_POWER_SOURCE_TARGET,
    CONF_MAX_POWER_TARGET_SOURCE,
    CONF_MIN_CHARGE_PERCENTAGE,
    CONF_SALVAGE_VALUE,
    SECTION_LIMITS,
    SECTION_PARTITIONING,
    SECTION_STORAGE,
)
from custom_components.haeo.core.schema.elements.node import CONF_IS_SINK, CONF_IS_SOURCE, SECTION_ROLE
from custom_components.haeo.core.schema.sections import (
    CONF_CONNECTION,
    SECTION_EFFICIENCY,
    SECTION_POWER_LIMITS,
    SECTION_PRICING,
)
from custom_components.haeo.entities.auto_optimize_switch import AutoOptimizeSwitch
from custom_components.haeo.flows import HUB_SECTION_ADVANCED, HUB_SECTION_COMMON, HUB_SECTION_TIERS
from custom_components.haeo.horizon import HorizonManager
from custom_components.haeo.input_stores import build_input_stores


def _bare_battery_config(
    *,
    min_charge: float,
    max_charge: float = 100.0,
    initial: float = 50.0,
) -> dict[str, Any]:
    """Build a minimal ``ElementConfigData`` for the battery model.

    Values are passed as concrete arrays (not constant-value markers) because
    this helper is consumed by ``create_network`` directly, bypassing the
    input-store loading path.
    """
    return {
        CONF_ELEMENT_TYPE: ElementType.BATTERY,
        CONF_NAME: "Battery",
        SECTION_STORAGE: {
            CONF_CAPACITY: np.array([10.0, 10.0]),
            CONF_INITIAL_CHARGE_PERCENTAGE: initial,
        },
        SECTION_LIMITS: {
            CONF_MIN_CHARGE_PERCENTAGE: min_charge,
            CONF_MAX_CHARGE_PERCENTAGE: max_charge,
        },
        SECTION_POWER_LIMITS: {},
        SECTION_PRICING: {},
        SECTION_EFFICIENCY: {},
        CONF_CONNECTION: as_connection_target("bus"),
    }


def _bus_node_config() -> dict[str, Any]:
    return {
        CONF_ELEMENT_TYPE: ElementType.NODE,
        CONF_NAME: "bus",
        SECTION_ROLE: {CONF_IS_SOURCE: True, CONF_IS_SINK: True},
    }


async def test_battery_updater_recomputes_capacity_on_min_charge_change(
    hass: HomeAssistant,
) -> None:
    """Step 9: the updater closure must rewrite the live model element.

    When ``ElementConfigData`` changes ``min_charge_percentage``, calling the
    pre-resolved updater closure produced by ``_build_element_updater`` must
    re-run the battery adapter and write the recomputed ``capacity`` and
    ``initial_charge`` values to the model element's ``TrackedParam``
    descriptors.
    """
    entry = MockConfigEntry(domain=DOMAIN, entry_id="bat_repro")
    entry.add_to_hass(hass)

    participants_a = cast(
        "Any",
        {"Battery": _bare_battery_config(min_charge=20.0), "bus": _bus_node_config()},
    )
    network, updaters = await create_network(
        entry, periods_seconds=[3600], participants=participants_a
    )

    battery = network.elements["Battery"]
    assert isinstance(battery, Battery)
    # min=20%, max=100%, capacity=10: range = 0.8 * 10 * 100 (percent units) = 800
    cap_before = np.asarray(battery.capacity, dtype=float)
    init_before = float(battery.initial_charge)
    assert np.allclose(cap_before, 800.0), f"expected 800.0, got {cap_before}"
    # initial = (50 - 20) * 10 = 300
    assert init_before == pytest.approx(300.0)

    updaters["Battery"](cast("Any", _bare_battery_config(min_charge=50.0)))
    cap_after = np.asarray(battery.capacity, dtype=float)
    init_after = float(battery.initial_charge)
    # New range = (100-50)*10 = 500
    assert np.allclose(cap_after, 500.0), f"expected 500.0, got {cap_after}"
    # initial = (50 - 50) * 10 = 0
    assert init_after == pytest.approx(0.0)


# ----- Coordinator-level tests using real input stores and subentries -----


@pytest.fixture
def hub_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a minimal hub config entry for coordinator construction."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_INTEGRATION_TYPE: INTEGRATION_TYPE_HUB,
            HUB_SECTION_COMMON: {CONF_NAME: "Hub"},
            HUB_SECTION_TIERS: {
                CONF_TIER_1_COUNT: 2,
                CONF_TIER_1_DURATION: 30,
                CONF_TIER_2_COUNT: 0,
                CONF_TIER_2_DURATION: DEFAULT_TIER_2_DURATION,
                CONF_TIER_3_COUNT: 0,
                CONF_TIER_3_DURATION: DEFAULT_TIER_3_DURATION,
                CONF_TIER_4_COUNT: 0,
                CONF_TIER_4_DURATION: DEFAULT_TIER_4_DURATION,
            },
            HUB_SECTION_ADVANCED: {CONF_DEBOUNCE_SECONDS: DEFAULT_DEBOUNCE_SECONDS},
        },
        entry_id="hub_id",
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def bus_subentry(hass: HomeAssistant, hub_entry: MockConfigEntry) -> ConfigSubentry:
    """Attach a DC Bus node so battery connections resolve."""
    sub = ConfigSubentry(
        data=MappingProxyType({
            CONF_ELEMENT_TYPE: ElementType.NODE,
            CONF_NAME: "DC Bus",
            SECTION_ROLE: {CONF_IS_SOURCE: True, CONF_IS_SINK: True},
        }),
        subentry_type=ElementType.NODE,
        title="DC Bus",
        unique_id=None,
    )
    hass.config_entries.async_add_subentry(hub_entry, sub)
    return sub


@pytest.fixture
def battery_subentry(
    hass: HomeAssistant,
    hub_entry: MockConfigEntry,
    bus_subentry: ConfigSubentry,
) -> ConfigSubentry:
    """Attach a battery subentry with initial min_charge=50%."""
    hass.states.async_set("sensor.battery_capacity", "10000", {"unit_of_measurement": "Wh"})
    hass.states.async_set("sensor.battery_soc", "50.0")
    sub = ConfigSubentry(
        data=MappingProxyType({
            CONF_ELEMENT_TYPE: ElementType.BATTERY,
            CONF_NAME: "Battery",
            CONF_CONNECTION: as_connection_target("DC Bus"),
            SECTION_STORAGE: {
                CONF_CAPACITY: as_constant_value(10.0),
                CONF_INITIAL_CHARGE_PERCENTAGE: as_constant_value(50.0),
            },
            SECTION_LIMITS: {
                CONF_MIN_CHARGE_PERCENTAGE: as_constant_value(50.0),
                CONF_MAX_CHARGE_PERCENTAGE: as_constant_value(100.0),
            },
            SECTION_POWER_LIMITS: {
                CONF_MAX_POWER_TARGET_SOURCE: as_constant_value(5.0),
                CONF_MAX_POWER_SOURCE_TARGET: as_constant_value(5.0),
            },
            SECTION_PRICING: {
                CONF_SALVAGE_VALUE: as_constant_value(0.0),
            },
            SECTION_EFFICIENCY: {
                CONF_EFFICIENCY_SOURCE_TARGET: as_constant_value(95.0),
                CONF_EFFICIENCY_TARGET_SOURCE: as_constant_value(95.0),
            },
            SECTION_PARTITIONING: {},
        }),
        subentry_type=ElementType.BATTERY,
        title="Battery",
        unique_id=None,
    )
    hass.config_entries.async_add_subentry(hub_entry, sub)
    return sub


@pytest.fixture(autouse=True)
def patch_state_change() -> Any:
    """Stub out async_track_state_change_event so we don't need real entities."""
    with patch(
        "custom_components.haeo.coordinator.coordinator.async_track_state_change_event",
        return_value=lambda: None,
    ):
        yield


@pytest.fixture
def runtime_data(hass: HomeAssistant, hub_entry: MockConfigEntry) -> HaeoRuntimeData:
    """Attach a runtime_data object with a stubbed horizon manager."""
    horizon: Any = MagicMock(spec=HorizonManager)
    horizon.get_forecast_timestamps.return_value = (1000.0, 2000.0, 3000.0)
    horizon.subscribe.return_value = MagicMock()
    switch: Any = MagicMock(spec=AutoOptimizeSwitch)
    switch.is_on = True
    switch.entity_id = "switch.haeo_auto_optimize"
    rt = HaeoRuntimeData(horizon_manager=horizon, auto_optimize_switch=switch)
    hub_entry.runtime_data = rt
    return rt


async def test_store_change_queues_pending_update_with_correct_min_charge(
    hass: HomeAssistant,
    hub_entry: MockConfigEntry,
    battery_subentry: ConfigSubentry,
    runtime_data: HaeoRuntimeData,
) -> None:
    """Steps 3-6: store mutation queues a pending update with the new value.

    Setting ``min_charge_percentage`` via the input store should synchronously
    fire the coordinator's listener, which loads the new element config and
    queues it in ``_pending_element_updates``. Critically, the queued config
    must carry the new value, not the snapshot value captured at startup.
    """
    runtime_data.input_stores = build_input_stores(hass, hub_entry, runtime_data.horizon_manager)

    coordinator = HaeoDataUpdateCoordinator(hass, hub_entry)
    coordinator.network = MagicMock()  # avoid building a real network

    key = ("Battery", (SECTION_LIMITS, CONF_MIN_CHARGE_PERCENTAGE))
    assert key in runtime_data.input_stores, f"store missing for {key}"
    store = runtime_data.input_stores[key]

    unsub = store.add_listener(coordinator._create_store_listener("Battery"))
    try:
        with patch.object(coordinator, "signal_optimization_stale") as trigger_mock:
            store.set_value(20.0)

        assert "Battery" in coordinator._pending_element_updates, (
            f"No pending update queued! keys={list(coordinator._pending_element_updates)}"
        )
        queued = cast("Any", coordinator._pending_element_updates["Battery"])
        queued_min = queued[SECTION_LIMITS][CONF_MIN_CHARGE_PERCENTAGE]
        queued_min_arr = np.asarray(queued_min, dtype=float).ravel()
        # min_charge_percentage is stored as a ratio (0.2) after store resolution.
        assert np.allclose(queued_min_arr, 0.2), (
            f"Pending update has stale min_charge={queued_min_arr}, expected 0.2"
        )
        trigger_mock.assert_called_once()
    finally:
        unsub()


async def test_optimization_context_reflects_live_subentry_data(
    hass: HomeAssistant,
    hub_entry: MockConfigEntry,
    battery_subentry: ConfigSubentry,
    runtime_data: HaeoRuntimeData,
) -> None:
    """Step 11 (issue #467): diagnostics carry the live subentry data.

    ``OptimizationContext.participants`` is built from a separate participant
    schemas view from the one driving the actual optimization. Before #467
    that view was the structural snapshot captured at coordinator construction,
    which never updates after a user edit, so the resulting diagnostics dump
    misleadingly suggested the optimizer was using the old value.

    The fix introduces ``_get_live_participant_configs`` which re-reads from
    ``subentry.data`` at optimization time, while ``_get_participant_configs``
    continues to return the immutable structural snapshot (so structural
    changes still require a reload).
    """
    runtime_data.input_stores = build_input_stores(hass, hub_entry, runtime_data.horizon_manager)
    coordinator = HaeoDataUpdateCoordinator(hass, hub_entry)

    def _min_charge(configs: Any) -> Any:
        return configs["Battery"][SECTION_LIMITS][CONF_MIN_CHARGE_PERCENTAGE]

    # Snapshot captured at __init__ has min_charge=50%.
    assert _min_charge(coordinator._get_participant_configs()) == as_constant_value(50.0)

    # Simulate the user editing the subentry (as async_update_subentry_value does):
    # HA replaces subentry.data with a fresh MappingProxy.
    new_data = dict(battery_subentry.data)
    new_data[SECTION_LIMITS] = {
        **new_data[SECTION_LIMITS],
        CONF_MIN_CHARGE_PERCENTAGE: as_constant_value(20.0),
    }
    hass.config_entries.async_update_subentry(hub_entry, battery_subentry, data=new_data)

    # The captured snapshot must NOT auto-update (structure stability invariant).
    assert _min_charge(coordinator._get_participant_configs()) == as_constant_value(50.0), (
        "Structural snapshot leaked live values - reload invariant broken"
    )

    # But the live view MUST reflect the edit, so diagnostics carry the value
    # actually in effect for this run.
    assert _min_charge(coordinator._get_live_participant_configs()) == as_constant_value(20.0), (
        "Live participant-configs still stale"
    )


async def test_apply_pending_updates_writes_to_network(
    hass: HomeAssistant,
    hub_entry: MockConfigEntry,
    battery_subentry: ConfigSubentry,
    runtime_data: HaeoRuntimeData,
) -> None:
    """Integrated steps 3-9: store change propagates to the live model element.

    Verifies the full coordinator-level reactive chain end-to-end:
    user edits the entity, the store notifies, the listener queues a pending
    update, ``_apply_pending_element_updates`` runs the updater, and the
    Battery model element's ``capacity`` TrackedParam reflects the new
    min_charge bound.
    """
    runtime_data.input_stores = build_input_stores(hass, hub_entry, runtime_data.horizon_manager)
    coordinator = HaeoDataUpdateCoordinator(hass, hub_entry)

    # Build the initial real network for this test.
    participant_configs = coordinator._get_participant_configs()
    initial_loaded = {
        name: load_element_config_from_values(
            name,
            cfg,
            coordinator._field_values_for_element(name),
            (1000.0, 2000.0, 3000.0),
        )
        for name, cfg in participant_configs.items()
    }

    network, updaters = await create_network(
        hub_entry, periods_seconds=[3600, 3600], participants=initial_loaded
    )
    coordinator.network = network
    coordinator._element_updaters = updaters

    battery = network.elements["Battery"]
    assert isinstance(battery, Battery)
    # capacity_first = 10, range = (1.0 - 0.5) * 10 = 5.0
    cap_initial = np.asarray(battery.capacity, dtype=float)
    assert np.allclose(cap_initial, 5.0), f"expected 5.0, got {cap_initial}"

    # User edits the entity, triggering the store -> listener -> pending chain.
    key = ("Battery", (SECTION_LIMITS, CONF_MIN_CHARGE_PERCENTAGE))
    store = runtime_data.input_stores[key]
    store.add_listener(coordinator._create_store_listener("Battery"))
    with patch.object(coordinator, "signal_optimization_stale"):
        store.set_value(20.0)

    # This is what _async_update_data calls before network.optimize().
    coordinator._apply_pending_element_updates()

    cap_after = np.asarray(battery.capacity, dtype=float)
    # New range = (1.0 - 0.2) * 10 = 8.0
    assert np.allclose(cap_after, 8.0), f"capacity didn't update; expected 8.0, got {cap_after}"
    assert coordinator._pending_element_updates == {}
