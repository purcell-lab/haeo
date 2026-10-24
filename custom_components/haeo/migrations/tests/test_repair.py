"""Tests for the fork patch that repairs entries a pre-release build saved at version 1.4."""

from __future__ import annotations

from types import MappingProxyType

from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haeo.const import CONF_INTEGRATION_TYPE, DOMAIN, ELEMENT_TYPE_NETWORK, INTEGRATION_TYPE_HUB
from custom_components.haeo.core.adapters.elements.junction import JUNCTION_DEVICE_JUNCTION, JUNCTION_POWER_BALANCE
from custom_components.haeo.core.adapters.elements.node import NODE_DEVICE_NODE, NODE_POWER_BALANCE
from custom_components.haeo.core.const import (
    CONF_ADVANCED_MODE,
    CONF_DEBOUNCE_SECONDS,
    CONF_ELEMENT_TYPE,
    CONF_HORIZON,
    CONF_HORIZON_PRESET,
    CONF_NAME,
    HORIZON_PRESET_7_DAYS,
    HUB_SECTION_ADVANCED,
    HUB_SECTION_COMMON,
    HUB_SECTION_TIERS,
)
from custom_components.haeo.core.schema import as_constant_value
from custom_components.haeo.core.schema.elements import battery, buffer, connection, grid, junction, node
from custom_components.haeo.core.schema.elements.conformance import is_element_config_schema
from custom_components.haeo.core.schema.horizon_value import as_horizon_preset_value
from custom_components.haeo.core.schema.migrations import battery_partitions as partitions
from custom_components.haeo.core.schema.sections import (
    CONF_EFFICIENCY_SOURCE_TARGET,
    CONF_MAX_POWER_SOURCE_TARGET,
    CONF_PRICE_SOURCE_TARGET,
    SECTION_EFFICIENCY,
    SECTION_POWER_LIMITS,
    SECTION_PRICING,
)
from custom_components.haeo.migrations import MIGRATION_MINOR_VERSION, v1_4
from custom_components.haeo.unique_id import element_output_unique_id

from .test_upgrade import CUSTOM_TIERS, LEGACY_ELEMENTS, SOURCE_NODE, _legacy_hub

FORWARD_LINK = "Generator link"
REVERSE_LINK = "Generator link (Switchboard to Generator bus)"


def _early_v1_4_elements() -> tuple[dict[str, object], ...]:
    """Return the legacy elements as the pre-release v1.4 migration left them: connections split, nothing else."""
    elements: list[dict[str, object]] = []
    for data in LEGACY_ELEMENTS:
        if data[CONF_NAME] != FORWARD_LINK:
            elements.append(data)
            continue
        endpoints = data[connection.SECTION_ENDPOINTS]
        assert isinstance(endpoints, dict)
        elements.append(
            {
                CONF_ELEMENT_TYPE: connection.ELEMENT_TYPE,
                CONF_NAME: FORWARD_LINK,
                connection.SECTION_ENDPOINTS: endpoints,
                SECTION_POWER_LIMITS: {CONF_MAX_POWER_SOURCE_TARGET: as_constant_value(5.0)},
                SECTION_PRICING: {},
                SECTION_EFFICIENCY: {CONF_EFFICIENCY_SOURCE_TARGET: as_constant_value(98.0)},
            }
        )
        elements.append(
            {
                CONF_ELEMENT_TYPE: connection.ELEMENT_TYPE,
                CONF_NAME: REVERSE_LINK,
                connection.SECTION_ENDPOINTS: {
                    connection.CONF_SOURCE: endpoints[connection.CONF_TARGET],
                    connection.CONF_TARGET: endpoints[connection.CONF_SOURCE],
                },
                SECTION_POWER_LIMITS: {CONF_MAX_POWER_SOURCE_TARGET: as_constant_value(3.0)},
                SECTION_PRICING: {CONF_PRICE_SOURCE_TARGET: as_constant_value(0.01)},
                SECTION_EFFICIENCY: {CONF_EFFICIENCY_SOURCE_TARGET: as_constant_value(95.0)},
            }
        )
    return tuple(elements)


def _early_v1_4_hub(hass: HomeAssistant, *, advanced_mode: bool) -> MockConfigEntry:
    """Add a hub as a pre-release build saved it at version 1.4."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_INTEGRATION_TYPE: INTEGRATION_TYPE_HUB,
            HUB_SECTION_COMMON: {CONF_NAME: "Home", CONF_HORIZON_PRESET: HORIZON_PRESET_7_DAYS},
            HUB_SECTION_TIERS: CUSTOM_TIERS,
            HUB_SECTION_ADVANCED: {CONF_DEBOUNCE_SECONDS: 2, CONF_ADVANCED_MODE: advanced_mode},
        },
        version=1,
        minor_version=v1_4.MINOR_VERSION,
    )
    entry.add_to_hass(hass)
    for data in _early_v1_4_elements():
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType(data),
                subentry_type=str(data[CONF_ELEMENT_TYPE]),
                title=str(data[CONF_NAME]),
                unique_id=None,
            ),
        )
    return entry


def _subentry_id(entry: MockConfigEntry, title: str) -> str:
    return next(subentry.subentry_id for subentry in entry.subentries.values() if subentry.title == title)


async def test_early_v1_4_hub_is_repaired_and_sets_up(hass: HomeAssistant) -> None:
    """A hub saved at 1.4 by a pre-release build gets the junction, buffer and horizon steps and sets up."""
    entry = _early_v1_4_hub(hass, advanced_mode=True)
    assert v1_4.needs_repair(entry)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.minor_version == MIGRATION_MINOR_VERSION
    assert entry.data[HUB_SECTION_COMMON][CONF_HORIZON] == as_horizon_preset_value(HORIZON_PRESET_7_DAYS)
    assert HUB_SECTION_TIERS not in entry.data
    subentries = {subentry.title: subentry for subentry in entry.subentries.values()}
    assert all(
        is_element_config_schema(dict(subentry.data))
        for subentry in subentries.values()
        if subentry.subentry_type != ELEMENT_TYPE_NETWORK
    )
    assert subentries["Switchboard"].subentry_type == junction.ELEMENT_TYPE
    assert subentries["Battery reserve"].subentry_type == buffer.ELEMENT_TYPE
    assert subentries["Battery headroom"].subentry_type == buffer.ELEMENT_TYPE
    # The connections the pre-release build split are kept as they were, with no third connection
    connections = sorted(title for title, s in subentries.items() if s.subentry_type == connection.ELEMENT_TYPE)
    assert connections == [FORWARD_LINK, REVERSE_LINK]
    assert not v1_4.needs_repair(entry)


async def test_switchboard_shadow_price_keeps_its_entity_id(hass: HomeAssistant) -> None:
    """The Switchboard's power balance sensor is re-keyed to the junction and keeps its entity ID."""
    entry = _early_v1_4_hub(hass, advanced_mode=False)
    switchboard_id = _subentry_id(entry, "Switchboard")
    registry = er.async_get(hass)
    sensor = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        element_output_unique_id(entry.entry_id, switchboard_id, NODE_DEVICE_NODE, NODE_POWER_BALANCE),
        config_entry=entry,
        suggested_object_id="switchboard_power_balance_shadow_price",
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    migrated = registry.async_get(sensor.entity_id)
    assert migrated is not None
    assert migrated.entity_id == "sensor.switchboard_power_balance_shadow_price"
    assert migrated.unique_id == element_output_unique_id(
        entry.entry_id, switchboard_id, JUNCTION_DEVICE_JUNCTION, JUNCTION_POWER_BALANCE
    )
    assert migrated.config_subentry_id == switchboard_id


async def test_current_hub_is_not_repaired(hass: HomeAssistant) -> None:
    """A hub the released migration brought up to date is left unchanged."""
    entry = _legacy_hub(hass, preset=HORIZON_PRESET_7_DAYS, advanced_mode=True)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    data = dict(entry.data)
    subentries = {subentry_id: dict(subentry.data) for subentry_id, subentry in entry.subentries.items()}

    assert not v1_4.needs_repair(entry)
    assert await v1_4.async_repair_entry(hass, entry)

    assert dict(entry.data) == data
    assert {subentry_id: dict(subentry.data) for subentry_id, subentry in entry.subentries.items()} == subentries


async def test_current_source_node_outside_advanced_mode_is_kept(hass: HomeAssistant) -> None:
    """A current hub outside advanced mode keeps a node that sources power, as the released migration does."""
    entry = _legacy_hub(hass, preset=HORIZON_PRESET_7_DAYS, advanced_mode=True)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    advanced = {**entry.data[HUB_SECTION_ADVANCED], CONF_ADVANCED_MODE: False}
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, HUB_SECTION_ADVANCED: advanced}, minor_version=v1_4.MINOR_VERSION
    )

    assert not v1_4.needs_repair(entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentries = {subentry.title: subentry for subentry in entry.subentries.values()}
    assert subentries[SOURCE_NODE].subentry_type == node.ELEMENT_TYPE


async def test_entry_below_v1_4_is_left_to_the_migration(hass: HomeAssistant) -> None:
    """An entry below 1.4 is never treated as needing repair."""
    entry = _legacy_hub(hass, preset=HORIZON_PRESET_7_DAYS, advanced_mode=False)

    assert not v1_4.needs_repair(entry)


async def test_grid_and_battery_survive_repair(hass: HomeAssistant) -> None:
    """The repair keeps the grid and battery, with the battery's partitions moved to buffers."""
    entry = _early_v1_4_hub(hass, advanced_mode=False)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentries = {subentry.title: subentry for subentry in entry.subentries.values()}
    assert subentries["Grid"].subentry_type == grid.ELEMENT_TYPE
    assert subentries["Battery"].subentry_type == battery.ELEMENT_TYPE
    assert partitions.SECTION_PARTITIONING not in subentries["Battery"].data
