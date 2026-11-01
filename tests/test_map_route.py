"""Synthetic map, historical route and pose regressions for the fork release."""
from __future__ import annotations

import asyncio
import io
import json
import struct
import time
import zipfile
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import Platform

from custom_components.switchbot_vacuum import PLATFORMS, map_data, route_data
from custom_components.switchbot_vacuum import coordinator as module


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zipped:
        for name, value in files.items():
            zipped.writestr(name, value if isinstance(value, bytes) else json.dumps(value))
    return output.getvalue()


def base_map(**overrides):
    files = {
        "mapinfo.json": {"uuid": "test-map"},
        "map.json": {"origin": [0, 0, 0], "resolution": 1},
        "labels.json": {"data": []},
        "markers.json": {"data": [{"id": "CHARGE_TEST", "geometry": [0, 0, 0]}]},
        "map.pgm": b"P5\n10 10\n255\n" + bytes([254]) * 100,
    }
    files.update(overrides)
    return map_data.decode_map(archive(files))


def report(points, map_id="test-map"):
    return archive({"mapinfo.json": {"uuid": map_id}, "track.json": points})


RECORD = {"taskID": "test-task", "timestamp": 1000000, "taskArea": 10, "duration": 5000}


def test_image_platform_preserves_existing_platforms():
    assert {Platform.VACUUM, Platform.SENSOR, Platform.BINARY_SENSOR,
            Platform.BUTTON, Platform.SELECT, Platform.IMAGE} <= set(PLATFORMS)


def test_map_dimensions_station_and_missing_position():
    data = base_map()
    assert struct.unpack(">II", data["image"][16:24]) == (40, 40)
    assert data["attributes"]["charger"] == {"x": 0, "y": 36}
    assert data["attributes"]["robot_position_available"] is False
    assert data["attributes"]["route_available"] is False


@pytest.mark.parametrize("pgm", [b"invalid", b"P5\n10 10\n255\n" + bytes(99)])
def test_bad_occupancy_data_rejected(pgm):
    with pytest.raises(ValueError):
        base_map(**{"map.pgm": pgm})


def test_no_label_file_preserves_room_cache_contract():
    assert map_data.decode_map(archive({"other.json": {}})) is None


def test_route_uses_separate_continuous_segments():
    base = base_map()
    original = base["image"]
    result = route_data.render_report(base, report([
        [1, 1, 0, 0, 0, 1], [2, 1, 0, 100, 0, 1],
        [5, 5, 0, 200, 1, 2], [6, 5, 0, 300, 1, 2],
    ]), RECORD)
    assert result["metadata"]["segments"] == 2
    assert result["metadata"]["rendered_lines"] == 2
    assert result["metadata"]["kind"] == "historical"
    assert base["image"] == original


def test_route_for_other_map_is_not_drawn():
    assert route_data.render_report(base_map(), report([], "other-map"), RECORD) == {
        "status": "different_map"
    }


def test_long_coordinate_jump_is_not_connected():
    result = route_data.render_report(base_map(), report([
        [1, 1, 0, 0, 0, 1], [8, 8, 0, 100, 0, 1],
    ]), RECORD)
    assert result["metadata"]["rendered_lines"] == 0


def test_invalid_route_points_are_not_drawn():
    assert route_data.render_report(base_map(), report([
        [float("nan"), 1, 0, 0, 0, 1], ["invalid"],
    ]), RECORD) == {"status": "empty"}


def test_position_conversion_does_not_mutate_base():
    data = base_map()
    original = data["image"]
    result = route_data.render_position(data, [2, 3, 0, 100])
    assert result["pixel"] == {"x": 8, "y": 24}
    assert data["image"] == original
    assert result["image"] != original


def test_outside_position_rejected():
    with pytest.raises(ValueError):
        route_data.render_position(base_map(), [100, 100, 0, 100])


@pytest.fixture
def coordinator():
    coord = object.__new__(module.SwitchBotS10Coordinator)
    coord.map_data = base_map()
    coord.map_updated = None
    coord._map_stopping = False
    coord._map_monitor_open = False
    coord._position_task = None
    coord._position_index = 0
    coord._position_key = None
    coord._position_received_at = 0
    coord.hass = MagicMock()
    async def executor(function, *args):
        return function(*args)
    coord.hass.async_add_executor_job = executor
    coord.async_close_map_monitor = AsyncMock()
    coord._async_map_request = AsyncMock(side_effect=[{}, {
        "endIndex": 1, "data": [[2, 3, 0, 100, 0, 1]],
    }])
    return coord


async def test_idle_pose_does_not_start_monitor(coordinator):
    await coordinator.async_refresh_position({1010: 3, 1032: {"taskId": "test-task"}})
    coordinator._async_map_request.assert_not_awaited()
    coordinator.async_close_map_monitor.assert_awaited_once()


async def test_active_pose_is_expired_when_feed_stops(coordinator):
    props = {1010: 9, 1032: {"taskId": "test-task"}}
    await coordinator.async_refresh_position(props)
    assert coordinator.map_data["attributes"]["robot_position_available"]
    assert coordinator._position_index == 1
    coordinator._position_received_at = time.time() - 61
    coordinator._async_map_request = AsyncMock(return_value={
        "endIndex": 1, "data": [[2, 3, 0, 100, 0, 1]],
    })
    await coordinator.async_refresh_position(props)
    assert not coordinator.map_data["attributes"]["robot_position_available"]
    assert coordinator.map_data["image"] == coordinator.map_data["_base_image"]


async def test_position_from_other_map_is_not_displayed(coordinator):
    await coordinator.async_refresh_position({
        1010: 9, 1032: {"taskId": "test-task"}, 1055: {"mapID": "other-map"},
    })
    coordinator._async_map_request.assert_not_awaited()


async def test_return_to_idle_removes_pose(coordinator):
    await coordinator.async_refresh_position({1010: 9, 1032: {"taskId": "test-task"}})
    await coordinator.async_refresh_position({1010: 3})
    assert not coordinator.map_data["attributes"]["robot_position_available"]


async def test_map_shutdown_cancels_background_work_and_closes_monitor(coordinator):
    async def pending():
        await asyncio.Event().wait()
    coordinator._route_task = asyncio.create_task(pending())
    await asyncio.sleep(0)
    await coordinator.async_stop_maps()
    assert coordinator._route_task.cancelled()
    assert coordinator._map_stopping
    coordinator.async_close_map_monitor.assert_awaited_once()


async def test_historical_route_refresh_retains_previous_image_on_failure(coordinator, caplog):
    coordinator._route_refresh_lock = asyncio.Lock()
    coordinator._last_route_attempt = 0
    coordinator._map_bucket = "test-bucket"
    coordinator.async_get_latest_clean_record = AsyncMock(return_value=RECORD | {
        "data": {"resource": "test-report.zip"}
    })
    coordinator.async_get_map_policy = AsyncMock(return_value=({}, "us-west-2"))
    coordinator.hass.async_add_executor_job = AsyncMock(side_effect=OSError("fake-private-url"))
    original = coordinator.map_data["image"]
    await coordinator.async_refresh_last_route()
    assert coordinator.map_data["image"] == original
    assert "fake-private" not in caplog.text
