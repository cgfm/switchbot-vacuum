"""Rooms exposed as Home Assistant vacuum segments for vacuum.clean_area."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeassistant.components.vacuum import Segment, VacuumEntityFeature
from homeassistant.components.vacuum.services import _async_clean_area
from homeassistant.core import ServiceCall
from homeassistant.exceptions import HomeAssistantError

from custom_components.switchbot_vacuum import vacuum
from custom_components.switchbot_vacuum.const import (
    DEVICE_TYPE_K10,
    DEVICE_TYPE_K10PRO,
    DEVICE_TYPE_S10,
)


def coordinator(device_type=DEVICE_TYPE_K10, rooms=None):
    c = MagicMock()
    c.entry.data = {"device_type": device_type}
    c.device_mac = "FAKEDEVICE"
    c.device_name = "Robovac"
    c.k10_room_map_id = "101"
    c.rooms = {"room1": "Kitchen", "room2": "Den"} if rooms is None else rooms
    c.data = {"firmware": "", "work_status": 7, "rooms": c.rooms}
    c.async_clean_k10_rooms = AsyncMock()
    c.async_send_command = AsyncMock()
    c.async_request_refresh = AsyncMock()
    return c


def mapped(entity, segment_ids):
    entity.registry_entry = SimpleNamespace(id="reg", options={"vacuum": {
        "last_seen_segments": [{"id": s, "name": s, "group": None} for s in segment_ids],
    }})


class SegmentTests(unittest.IsolatedAsyncioTestCase):
    def test_clean_area_feature_except_k10_pro(self):
        for device_type, expected in ((DEVICE_TYPE_K10, True), (DEVICE_TYPE_S10, True),
                                      (DEVICE_TYPE_K10PRO, False)):
            entity = vacuum.SwitchBotS10Vacuum(coordinator(device_type))
            self.assertEqual(
                VacuumEntityFeature.CLEAN_AREA in entity.supported_features, expected, device_type
            )
            self.assertIn(VacuumEntityFeature.START, entity.supported_features)

    async def test_segments_are_rooms(self):
        entity = vacuum.SwitchBotS10Vacuum(coordinator())
        self.assertEqual(await entity.async_get_segments(),
                         [Segment(id="101:room2", name="Den"), Segment(id="101:room1", name="Kitchen")])

    async def test_clean_segments_uses_k10_room_protocol_in_order(self):
        c = coordinator()
        entity = vacuum.SwitchBotS10Vacuum(c)
        entity.entity_id = "vacuum.robovac"
        entity.registry_entry = SimpleNamespace(options={"vacuum": {"area_mapping": {
            "den": ["101:room2"], "kitchen": ["101:room1"],
        }}})
        call = ServiceCall(MagicMock(), "vacuum", "clean_area", {
            "cleaning_area_id": ["den", "kitchen"],
        })
        await _async_clean_area([entity], call)
        c.async_clean_k10_rooms.assert_awaited_once_with(
            ["room2", "room1"], 1, True, None, None, expected_map_id="101"
        )

    async def test_clean_segments_rejects_old_map_or_unscoped_ids(self):
        c = coordinator()
        entity = vacuum.SwitchBotS10Vacuum(c)
        for segment_ids in (["100:room1"], ["room1"], ["101:room1", "101:room3"]):
            with self.subTest(segment_ids=segment_ids):
                with self.assertRaisesRegex(HomeAssistantError, "update the Home Assistant area mapping"):
                    await entity.async_clean_segments(segment_ids)
        c.async_clean_k10_rooms.assert_not_awaited()

    async def test_cached_rooms_without_map_id_are_not_segments(self):
        c = coordinator()
        c.k10_room_map_id = None
        entity = vacuum.SwitchBotS10Vacuum(c)
        self.assertEqual(await entity.async_get_segments(), [])

    async def test_clean_segments_uses_s10_room_ids(self):
        c = coordinator(DEVICE_TYPE_S10, {"ROOM_001": "Kitchen"})
        entity = vacuum.SwitchBotS10Vacuum(c)
        entity.entity_id = "vacuum.robovac"
        entity.registry_entry = SimpleNamespace(options={"vacuum": {"area_mapping": {
            "kitchen": ["ROOM_001"],
        }}})
        call = ServiceCall(MagicMock(), "vacuum", "clean_area", {"cleaning_area_id": ["kitchen"]})
        await _async_clean_area([entity], call)
        payload = c.async_send_command.await_args.args[1]
        self.assertEqual(payload["0"], "clean_rooms")
        self.assertEqual([r["room_id"] for r in payload["1"]["rooms"]], ["ROOM_001"])


class RoomChangeTests(unittest.TestCase):
    def entity(self, rooms=None):
        entity = vacuum.SwitchBotS10Vacuum(coordinator(rooms=rooms))
        entity.async_create_segments_issue = MagicMock()
        return entity

    def test_no_issue_before_areas_are_mapped(self):
        entity = self.entity()
        entity.registry_entry = SimpleNamespace(id="reg", options={})
        entity._async_check_room_changes()
        entity.async_create_segments_issue.assert_not_called()

    def test_no_issue_when_rooms_match(self):
        entity = self.entity()
        mapped(entity, ["101:room1", "101:room2"])
        entity._async_check_room_changes()
        entity.async_create_segments_issue.assert_not_called()

    def test_issue_once_when_rooms_change(self):
        entity = self.entity({"room1": "Kitchen", "room3": "Office"})
        mapped(entity, ["101:room1", "101:room2"])
        entity._async_check_room_changes()
        entity._async_check_room_changes()
        entity.async_create_segments_issue.assert_called_once()

    def test_issue_when_new_map_reuses_room_ids(self):
        entity = self.entity()
        mapped(entity, ["101:room1", "101:room2"])
        entity.coordinator.k10_room_map_id = "102"
        entity._async_check_room_changes()
        entity.async_create_segments_issue.assert_called_once()

    def test_issue_when_current_map_has_no_rooms(self):
        entity = self.entity({})
        mapped(entity, ["101:room1"])
        entity._async_check_room_changes()
        entity.async_create_segments_issue.assert_called_once()

    def test_no_issue_while_rooms_unknown(self):
        entity = self.entity({})
        entity.coordinator.k10_room_map_id = None
        mapped(entity, ["101:room1"])
        entity._async_check_room_changes()
        entity.async_create_segments_issue.assert_not_called()

    def test_renamed_room_keeps_mapping(self):
        entity = self.entity({"room1": "Galley", "room2": "Den"})
        mapped(entity, ["101:room1", "101:room2"])
        entity._async_check_room_changes()
        entity.async_create_segments_issue.assert_not_called()


class RoomSensorVisibilityTests(unittest.TestCase):
    def test_new_room_sensors_are_hidden_diagnostics(self):
        from homeassistant.const import EntityCategory

        from custom_components.switchbot_vacuum import sensor

        entity = sensor.SwitchBotRoomSensor(coordinator(), "room1", "Kitchen")
        self.assertFalse(entity.entity_registry_enabled_default)
        self.assertEqual(entity.entity_category, EntityCategory.DIAGNOSTIC)


if __name__ == "__main__":
    unittest.main()
