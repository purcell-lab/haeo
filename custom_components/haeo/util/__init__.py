"""Utility functions for HAEO."""

import copy
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant

from custom_components.haeo.elements import InputFieldPath, set_nested_config_value_by_path

if TYPE_CHECKING:
    from custom_components.haeo import HaeoConfigEntry


async def async_update_subentry_value(
    hass: HomeAssistant,
    entry: "HaeoConfigEntry",
    subentry: ConfigSubentry,
    field_path: InputFieldPath,
    value: Any,
) -> None:
    """Update a single field value in a subentry without triggering reload.

    This function sets a flag on runtime_data before updating the subentry,
    which signals async_update_listener to skip the full integration reload
    and just refresh the coordinator instead.

    It also records the changed ``subentry_id`` on ``runtime_data`` so the
    listener can ask the coordinator to push the new subentry data into the
    network's TrackedParams. Without this, the persisted value would only
    appear in the LP after the next full integration reload (issue #467).

    Args:
        hass: Home Assistant instance.
        entry: The hub config entry.
        subentry: The subentry to update.
        field_path: Path to the field to update.
        value: New value for the field.

    """
    # Set flag to prevent reload and record which subentry changed
    runtime_data = entry.runtime_data
    if runtime_data is not None:
        runtime_data.value_update_in_progress = True
        runtime_data.value_update_subentry_id = subentry.subentry_id

    # Update subentry data with new value
    new_data = copy.deepcopy(dict(subentry.data))
    set_nested_config_value_by_path(new_data, field_path, value)

    try:
        hass.config_entries.async_update_subentry(
            entry,
            subentry,
            data=new_data,
        )
    except Exception:
        if runtime_data is not None:
            runtime_data.value_update_in_progress = False
            runtime_data.value_update_subentry_id = None
        raise


__all__ = [
    "async_update_subentry_value",
]
