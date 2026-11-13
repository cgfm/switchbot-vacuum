"""Optional repair that disables room sensors created by earlier versions."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.switchbot_vacuum import async_migrate_entry, repairs
from custom_components.switchbot_vacuum.const import DOMAIN


def reg(unique_id, *, disabled_by=None, domain="sensor", platform=DOMAIN):
    return SimpleNamespace(entity_id=f"{domain}.{unique_id}", unique_id=unique_id,
                           domain=domain, platform=platform, disabled_by=disabled_by)


ENTRIES = [
    reg("FAKEDEVICE_room_room1"),
    reg("FAKEDEVICE_room_room2"),
    reg("FAKEDEVICE_room_room3", disabled_by=er.RegistryEntryDisabler.USER),
    reg("FAKEDEVICE_battery"),
    reg("FAKEDEVICE_room_x", domain="binary_sensor"),
    reg("FAKEDEVICE_room_y", platform="other"),
]


def config_entry(version=1, minor_version=1):
    return SimpleNamespace(entry_id="e", title="Robovac", version=version,
                           minor_version=minor_version, data={"device_mac": "FAKEDEVICE"})


@pytest.fixture
def registry():
    registry = MagicMock()
    with (
        patch.object(repairs.er, "async_get", return_value=registry),
        patch.object(repairs.er, "async_entries_for_config_entry", return_value=ENTRIES),
    ):
        yield registry


async def test_upgrade_offers_repair_for_enabled_room_sensors(registry):
    hass, entry = MagicMock(), config_entry()
    with patch.object(repairs.ir, "async_create_issue") as create_issue:
        assert await async_migrate_entry(hass, entry)
    create_issue.assert_called_once()
    args, kwargs = create_issue.call_args
    assert args[1:] == (DOMAIN, "hide_room_sensors_e")
    assert kwargs["is_fixable"] and kwargs["is_persistent"]
    assert kwargs["severity"] is ir.IssueSeverity.WARNING
    assert kwargs["data"] == {"entry_id": "e"}
    assert kwargs["translation_placeholders"] == {"name": "Robovac", "count": "2"}
    hass.config_entries.async_update_entry.assert_called_once_with(entry, minor_version=2)


async def test_upgrade_without_enabled_room_sensors_raises_nothing():
    hass, entry = MagicMock(), config_entry()
    with (
        patch.object(repairs.er, "async_get"),
        patch.object(repairs.er, "async_entries_for_config_entry", return_value=ENTRIES[2:]),
        patch.object(repairs.ir, "async_create_issue") as create_issue,
    ):
        assert await async_migrate_entry(hass, entry)
    create_issue.assert_not_called()
    hass.config_entries.async_update_entry.assert_called_once_with(entry, minor_version=2)


@pytest.mark.parametrize(("version", "minor", "result"), [(1, 2, True), (2, 1, False)])
async def test_current_or_newer_entries_are_left_alone(registry, version, minor, result):
    hass = MagicMock()
    with patch.object(repairs.ir, "async_create_issue") as create_issue:
        assert await async_migrate_entry(hass, config_entry(version, minor)) is result
    create_issue.assert_not_called()
    hass.config_entries.async_update_entry.assert_not_called()


def flow_for(entry, entry_id="e"):
    flow = repairs.HideRoomSensorsFlow(entry_id)
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = entry
    flow.handler, flow.issue_id, flow.flow_id = DOMAIN, "hide_room_sensors_e", "f"
    return flow


async def test_fix_flow_creation():
    flow = await repairs.async_create_fix_flow(MagicMock(), "hide_room_sensors_e",
                                               {"entry_id": "e"})
    assert isinstance(flow, repairs.HideRoomSensorsFlow)
    assert flow.entry_id == "e"


async def test_confirm_form_disables_nothing(registry):
    flow = flow_for(config_entry())
    with patch("homeassistant.components.repairs.issue_handler.ir.async_get"):
        result = await flow.async_step_init()
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "confirm")
    registry.async_update_entity.assert_not_called()


async def test_confirming_disables_only_enabled_room_sensors(registry):
    flow = flow_for(config_entry())
    result = await flow.async_step_confirm({})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [c.args[0] for c in registry.async_update_entity.call_args_list] == [
        "sensor.FAKEDEVICE_room_room1", "sensor.FAKEDEVICE_room_room2",
    ]
    for call in registry.async_update_entity.call_args_list:
        assert call.kwargs == {"disabled_by": er.RegistryEntryDisabler.USER}


async def test_confirming_after_entry_removal_is_harmless(registry):
    flow = flow_for(None, "gone")
    result = await flow.async_step_confirm({})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    registry.async_update_entity.assert_not_called()


async def test_removing_entry_deletes_issue():
    from custom_components.switchbot_vacuum import async_remove_entry

    hass = MagicMock()
    with patch.object(repairs.ir, "async_delete_issue") as delete_issue:
        await async_remove_entry(hass, config_entry())
    delete_issue.assert_called_once_with(hass, DOMAIN, "hide_room_sensors_e")
