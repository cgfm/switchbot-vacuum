"""Optional repair offering to disable room sensors created by earlier versions."""

from __future__ import annotations

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import CONF_DEVICE_MAC, DOMAIN

ISSUE_HIDE_ROOM_SENSORS = "hide_room_sensors"


def _issue_id(entry: ConfigEntry) -> str:
    return f"{ISSUE_HIDE_ROOM_SENSORS}_{entry.entry_id}"


@callback
def _enabled_room_sensors(hass: HomeAssistant, entry: ConfigEntry) -> list[er.RegistryEntry]:
    prefix = f"{entry.data.get(CONF_DEVICE_MAC)}_room_"
    return [
        entity
        for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if entity.platform == DOMAIN
        and entity.domain == "sensor"
        and entity.unique_id.startswith(prefix)
        and entity.disabled_by is None
    ]


@callback
def async_offer_to_hide_room_sensors(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Raise a dismissible repair if this entry has enabled room sensors."""
    if sensors := _enabled_room_sensors(hass, entry):
        ir.async_create_issue(
            hass,
            DOMAIN,
            _issue_id(entry),
            data={"entry_id": entry.entry_id},
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_HIDE_ROOM_SENSORS,
            translation_placeholders={"name": entry.title, "count": str(len(sensors))},
        )


@callback
def async_remove_room_sensors_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))


class HideRoomSensorsFlow(ConfirmRepairFlow):
    """Disable the entry's enabled room sensors when the user confirms."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        if user_input is not None and (
            entry := self.hass.config_entries.async_get_entry(self.entry_id)
        ):
            registry = er.async_get(self.hass)
            for entity in _enabled_room_sensors(self.hass, entry):
                registry.async_update_entity(
                    entity.entity_id, disabled_by=er.RegistryEntryDisabler.USER
                )
        return await super().async_step_confirm(user_input)


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    return HideRoomSensorsFlow(str((data or {})["entry_id"]))
