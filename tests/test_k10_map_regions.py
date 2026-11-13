"""K10+ regional hosts and map rendering.

Synthetic maps only: no account, network or robot is used.
"""

import base64
import json
import math
import struct
import sys
import unittest
import zlib
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.switchbot_vacuum import coordinator as coordinator_module
from custom_components.switchbot_vacuum import image, sensor
from custom_components.switchbot_vacuum import k10_api as api
from custom_components.switchbot_vacuum import k10_map
from custom_components.switchbot_vacuum.const import (
    DEVICE_TYPE_K10,
    DEVICE_TYPE_K10PRO,
    DEVICE_TYPE_S10,
    DOMAIN,
)

US = "https://wonderlabs.us.api.switchbot.net"


class RegionTests(unittest.TestCase):
    def test_known_regions(self):
        self.assertEqual(api.botslab_region("https://wonderlabs.eu.api.switchbot.net"), "eu1")
        self.assertEqual(api.botslab_region(US + "/"), "na1")
        self.assertEqual(api.botslab_region("https://wonderlabs.ap.api.switchbot.net"), "ap1")

    def test_unknown_or_foreign_hosts(self):
        for host in (None, "", "https://attacker.example", "http://wonderlabs.us.api.switchbot.net",
                     "https://wonderlabs.us.api.switchbot.net.attacker.example",
                     "https://wonderlabs.zz.api.switchbot.net"):
            self.assertIsNone(api.botslab_region(host), host)

    def test_storage_url_must_match_region(self):
        self.assertTrue(api.trusted_storage_url(
            "https://na1-ali-1days.oss-us-east-1.aliyuncs.com/x?sig=1", "na1"))
        self.assertTrue(api.trusted_storage_url(
            "https://eu1-ali-180days.oss-eu-central-1.aliyuncs.com/x", "eu1"))
        for url in (
            "https://eu1-ali-1days.oss-eu-central-1.aliyuncs.com/x",
            "https://na1-x.attacker.example/x",
            "https://na1-x.aliyuncs.com/x",
            "https://na1-ali-1days.attacker.aliyuncs.com/x",
            "https://na1-ali-1days.oss-us-east-1.aliyuncs.com.attacker.example/x",
            "http://na1-ali-1days.oss-us-east-1.aliyuncs.com/x",
        ):
            self.assertFalse(api.trusted_storage_url(url, "na1"), url)

    def test_upload_accepts_regional_bucket(self):
        info = {
            "provider": "aliyun", "bucket": "na1-ali-1days",
            "endpoint": "oss-us-east-1.aliyuncs.com", "path": "a/b.txt",
            "sts": {"accesskey_id": "k", "accesskey_secret": "s", "security_token": "t"},
        }
        url, _, robot_url = api.oss_request(info, b"{}", region="na1")
        self.assertTrue(url.startswith("https://na1-ali-1days.oss-us-east-1.aliyuncs.com/"))
        self.assertEqual(robot_url, "aliyun://na1-ali-1days.oss-us-east-1.aliyuncs.com/a/b.txt")
        with self.assertRaises(api.K10ProtocolError):
            api.oss_request(info, b"{}", region="eu1")
        with self.assertRaisesRegex(api.K10ProtocolError, "'aws'"):
            api.oss_request(info | {"provider": "aws"}, b"{}", region="na1")
        with self.assertRaises(api.K10ProtocolError):
            api.oss_request(info | {"endpoint": "attacker.example"}, b"{}", region="na1")


class Response:
    def __init__(self, *, data=None, status=200):
        self.data, self.status = data, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return self.data

    def raise_for_status(self):
        pass


class Session:
    def __init__(self, replies=()):
        self.replies = iter(replies)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return Response(data=next(self.replies))

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return Response(data=next(self.replies))


class RegionalSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_us_account_uses_us_hosts(self):
        client = api.K10Api("example-device", "example-product", "installation")
        session = Session([
            {"statusCode": 100, "body": {"token": "delegated", "valid_time": 9999999999}},
            {"code": 0},
        ])
        await client.authorize(session, US, "account-token", "family")
        await client.request(session, "/v1/iot/device/get_property")
        self.assertEqual(session.calls[0][1], US + "/wonder/sweeper360/v1/auth/applyToken")
        self.assertEqual(session.calls[1][1],
                         "https://na1-sapp-api.botslab.com/v1/iot/device/get_property")

    async def test_region_change_discards_token(self):
        client = api.K10Api("example-device", "example-product", "installation")
        client.token, client.token_expiry = "eu-token", 9999999999
        session = Session([{"statusCode": 100, "body": {"token": "us", "valid_time": 9999999999}}])
        await client.authorize(session, US, "account-token", "family")
        self.assertEqual((client.region, client.token), ("na1", "us"))

    async def test_unknown_region_is_rejected_before_any_request(self):
        client = api.K10Api("example-device", "example-product", "installation")
        session = Session()
        with self.assertRaises(api.K10ProtocolError):
            await client.authorize(session, "https://wonderlabs.zz.api.switchbot.net",
                                   "account-token", "family")
        self.assertEqual(session.calls, [])


def lz4_literals(data: bytes) -> bytes:
    """Valid LZ4 block made of a single literal run."""
    n = len(data)
    out = bytearray([min(n, 15) << 4])
    if n >= 15:
        rest = n - 15
        while rest >= 255:
            out.append(255)
            rest -= 255
        out.append(rest)
    return bytes(out) + data


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# 40 x 30 cells at 5 cm, origin (-1.0 m, -0.5 m). Grid row 0 is the bottom.
W, H, RES, X0, Y0 = 40, 30, 0.05, -1.0, -0.5
UNKNOWN, FLOOR, WALL = 127, 255, 0  # As observed on a K10+.


def grid_bytes() -> bytes:
    cells = bytearray([UNKNOWN]) * (W * H)
    for r in range(5, 25):
        for c in range(5, 35):
            edge = r in (5, 24) or c in (5, 34)
            cells[r * W + c] = WALL if edge else FLOOR
    cells[12 * W + 25] = WALL  # An obstacle speck inside a room.
    return bytes(cells)


def mm(col: float, row: float) -> list[float]:
    """Robot millimetres for a continuous (col, row-from-bottom) grid position."""
    return [round((X0 + col * RES) * 1000, 3), round((Y0 + row * RES) * 1000, 3)]


def k10_map_record() -> dict:
    return {
        "mapId": 101,
        "cleanId": "example-clean",
        "width": W,
        "height": H,
        "resolution": RES,
        "x_min": X0,
        "y_min": Y0,
        "map": base64.b64encode(lz4_literals(grid_bytes())).decode(),
        "chargeHandlePos": mm(30, 8),
        "smartArea": {
            "mapId": 101,
            "value": [
                {"id": 1, "name": b64("Kitchen"),
                 "vertexs": [mm(6, 6), mm(20, 6), mm(20, 24), mm(6, 24)]},
                {"id": 2, "name": b64("Den"),
                 "vertexs": [mm(20, 6), mm(34, 6), mm(34, 24), mm(20, 24)]},
            ],
        },
    }


def rgba_rows(png: bytes) -> list[bytes]:
    """Decode our own unfiltered RGBA PNGs back into rows."""
    width, height = struct.unpack(">2I", png[16:24])
    idat_len = struct.unpack(">I", png[33:37])[0]
    raw = zlib.decompress(png[41 : 41 + idat_len])
    stride = 1 + width * 4
    return [raw[r * stride + 1 : (r + 1) * stride] for r in range(height)]


def pixels_of(png: bytes, rgba: tuple) -> list[tuple[int, int]]:
    """(x, y) of every pixel with exactly this colour."""
    target = bytes(rgba)
    return [
        (x, y)
        for y, row in enumerate(rgba_rows(png))
        for x in range(len(row) // 4)
        if row[x * 4 : x * 4 + 4] == target
    ]


def pixel(png: bytes, x: float, y: float) -> tuple:
    row = rgba_rows(png)[int(y)]
    return tuple(row[int(x) * 4 : int(x) * 4 + 4])


class Lz4Tests(unittest.TestCase):
    def test_overlapping_match(self):
        self.assertEqual(k10_map.lz4_block_decompress(b"\x35abc\x03\x00", 12), b"abc" * 4)

    def test_long_literal(self):
        data = bytes(range(256)) * 3
        self.assertEqual(k10_map.lz4_block_decompress(lz4_literals(data), len(data)), data)

    def test_rejects_bad_offsets_and_overruns(self):
        for bad in (b"\x35abc\x09\x00", b"\x35abc\x00\x00", b"\x35abc\x03"):
            with self.assertRaises(ValueError):
                k10_map.lz4_block_decompress(bad, 12)
        with self.assertRaises(ValueError):
            k10_map.lz4_block_decompress(lz4_literals(b"x" * 20), 10)


class RenderTests(unittest.TestCase):
    def test_renders_png_with_rooms_walls_and_dock(self):
        rendered = k10_map.decode_k10_map(k10_map_record())
        png, attrs = rendered["image"], rendered["attributes"]
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">2I", png[16:24]), (attrs["width"], attrs["height"]))
        self.assertEqual(attrs["map_id"], "101")
        self.assertEqual(attrs["source"], "SwitchBot K10+ cloud map")
        self.assertEqual({k: v["name"] for k, v in attrs["rooms"].items()},
                         {"room1": "Kitchen", "room2": "Den"})
        self.assertTrue(pixels_of(png, (*k10_map.ROOM_COLORS[0], 255)))
        self.assertTrue(pixels_of(png, (*k10_map.ROOM_COLORS[1], 255)))
        self.assertTrue(pixels_of(png, k10_map.C_WALL))
        self.assertIsNotNone(attrs["charger"])
        self.assertEqual(pixel(png, attrs["charger"]["x"], attrs["charger"]["y"]),
                         (*k10_map.C_DOCK, 255))
        self.assertEqual(pixel(png, 0, 0)[3], 0)  # Unexplored space is transparent.
        self.assertFalse(attrs["robot_position_available"])
        self.assertFalse(attrs["route_available"])

    def test_map_is_rotated_like_the_app(self):
        rendered = k10_map.decode_k10_map(k10_map_record())
        attrs = rendered["attributes"]
        kitchen = pixels_of(rendered["image"], (*k10_map.ROOM_COLORS[0], 255))
        den = pixels_of(rendered["image"], (*k10_map.ROOM_COLORS[1], 255))
        # The west room (kitchen) is drawn on the right, as in the SwitchBot app.
        self.assertGreater(min(x for x, _ in kitchen), max(x for x, _ in den))
        self.assertGreater(attrs["rooms"]["room1"]["x"], attrs["rooms"]["room2"]["x"])
        # The dock (east, near the bottom of the grid) is drawn top-left.
        self.assertLess(attrs["charger"]["x"], attrs["width"] / 2)
        self.assertLess(attrs["charger"]["y"], attrs["height"] / 2)

    def test_room_labels_are_drawn_at_room_centres(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow is not installed")
        rendered = k10_map.decode_k10_map(k10_map_record())
        kitchen = rendered["attributes"]["rooms"]["room1"]
        self.assertNotEqual(pixel(rendered["image"], kitchen["x"], kitchen["y"]),
                            (*k10_map.ROOM_COLORS[0], 255))

    def test_nogo_zone_is_visible_over_unexplored_space(self):
        record = k10_map_record()
        record["area"] = [{"vertexs": [mm(1, 1), mm(4, 1), mm(4, 4), mm(1, 4)]}]
        rendered = k10_map.decode_k10_map(record)
        self.assertTrue(pixels_of(rendered["image"], (*k10_map.C_NOGO, 90)))
        self.assertTrue(pixels_of(rendered["image"], (*k10_map.C_NOGO, 255)))
        # The crop grows to keep the whole zone (it starts outside the explored area).
        plain = k10_map.decode_k10_map(k10_map_record())
        self.assertGreater(rendered["attributes"]["width"], plain["attributes"]["width"])

    def test_room_without_outline_is_not_drawn(self):
        record = k10_map_record()
        record["smartArea"]["value"][1]["vertexs"] = [mm(20, 6)]
        rendered = k10_map.decode_k10_map(record)
        self.assertEqual(list(rendered["attributes"]["rooms"]), ["room1"])
        self.assertFalse(pixels_of(rendered["image"], (*k10_map.ROOM_COLORS[1], 255)))

    def test_invalid_grid_fails_cleanly(self):
        for key, value in (("map", "not base64!!"), ("width", 41), ("resolution", 0)):
            record = k10_map_record()
            record[key] = value
            with self.assertRaises(ValueError, msg=key):
                k10_map.decode_k10_map(record)


def fake_coordinator(**extra):
    hass = MagicMock()

    async def run(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=run)
    coordinator = object.__new__(coordinator_module.SwitchBotS10Coordinator)
    coordinator.hass = hass
    coordinator.entry = SimpleNamespace(data={"device_type": DEVICE_TYPE_K10}, options={})
    coordinator.device_mac = "FAKEDEVICE"
    coordinator.wonderlab_endpoint = US
    coordinator.map_data = None
    coordinator.map_updated = None
    coordinator.map_refreshed_at = None
    coordinator.k10_last_error = None
    coordinator._rooms = {}
    coordinator._last_room_refresh = 0
    coordinator._k10_last_reported_map_package = None
    coordinator.update_interval = timedelta(seconds=30)
    coordinator.path_updated_at = None
    coordinator.k10_track_error = None
    coordinator._k10_api = None
    coordinator._k10_path = []
    coordinator._k10_track_index = 0
    coordinator._k10_track_loaded = False
    coordinator._k10_overlay_drawn = None
    coordinator.data = {}
    for key, value in extra.items():
        setattr(coordinator, key, value)
    return coordinator


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.object(coordinator_module, "async_get_clientsession",
                               return_value=MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, record):
        client = MagicMock()
        client.get_map = AsyncMock(return_value=(record, {"MapID": 101}))
        return client

    async def test_room_refresh_renders_map(self):
        coordinator = fake_coordinator(k10_last_error="earlier failure")
        coordinator._authorize_k10_api = AsyncMock(return_value=self.client(k10_map_record()))
        await coordinator.async_refresh_k10_rooms()
        self.assertEqual(coordinator.rooms, {"room1": "Kitchen", "room2": "Den"})
        self.assertEqual(coordinator.map_data["attributes"]["source"], "SwitchBot K10+ cloud map")
        self.assertIsNotNone(coordinator.map_refreshed_at)
        self.assertEqual(coordinator.map_updated, coordinator.map_refreshed_at)
        self.assertIsNone(coordinator.k10_last_error)

    async def test_render_failure_keeps_rooms_and_is_explained(self):
        record = k10_map_record()
        record["width"] = W + 1  # The grid no longer matches its dimensions.
        coordinator = fake_coordinator()
        coordinator._authorize_k10_api = AsyncMock(return_value=self.client(record))
        await coordinator.async_refresh_k10_rooms()
        self.assertEqual(coordinator.rooms, {"room1": "Kitchen", "room2": "Den"})
        self.assertIsNone(coordinator.map_data)
        self.assertIsNone(coordinator.map_refreshed_at)
        self.assertEqual(coordinator.k10_last_error, "Map could not be rendered (ValueError)")

    async def test_protocol_error_is_recorded(self):
        coordinator = fake_coordinator()
        coordinator._authorize_k10_api = AsyncMock(
            side_effect=api.K10ProtocolError("Map storage returned HTTP 403"))
        await coordinator.async_refresh_k10_rooms()
        self.assertEqual(coordinator.k10_last_error, "Map storage returned HTTP 403")
        self.assertIsNone(coordinator.map_data)

    async def test_network_error_records_only_its_type(self):
        coordinator = fake_coordinator()
        coordinator._authorize_k10_api = AsyncMock(
            side_effect=TimeoutError("https://signed.example/?Signature=secret"))
        await coordinator.async_refresh_k10_rooms()
        self.assertEqual(coordinator.k10_last_error, "TimeoutError")

    async def test_status_poll_triggers_map_refresh_on_new_upload(self):
        coordinator = fake_coordinator(_last_room_refresh=10**12)
        coordinator.async_get_k10_status = AsyncMock(return_value={"WorkingStatus": 6})
        client = MagicMock()
        client.get_properties = AsyncMock(return_value={"MapPkgOssInfo": "a"})
        coordinator._authorize_k10_api = AsyncMock(return_value=client)
        await coordinator._async_update_data_k10()
        self.assertEqual(coordinator._last_room_refresh, 10**12)
        client.get_properties.return_value = {}  # A failed read keeps the marker.
        await coordinator._async_update_data_k10()
        self.assertEqual(coordinator._last_room_refresh, 10**12)
        client.get_properties.return_value = {"MapPkgOssInfo": "b"}
        await coordinator._async_update_data_k10()
        self.assertEqual(coordinator._last_room_refresh, 0)


class MapPlatformTests(unittest.IsolatedAsyncioTestCase):
    async def test_map_entity_for_k10_and_s10_not_k10_pro(self):
        for device_type, expected in ((DEVICE_TYPE_K10, 1), (DEVICE_TYPE_S10, 1),
                                      (DEVICE_TYPE_K10PRO, 0)):
            add = MagicMock()
            coordinator = MagicMock(device_mac="FAKEDEVICE", map_updated=None)
            hass = MagicMock()
            hass.data = {DOMAIN: {"e": coordinator}}
            entry = SimpleNamespace(entry_id="e", data={"device_type": device_type})
            await image.async_setup_entry(hass, entry, add)
            self.assertEqual(sum(len(c.args[0]) for c in add.call_args_list), expected)

    async def test_map_refresh_sensor_only_for_k10(self):
        for device_type, expected in ((DEVICE_TYPE_K10, 1), (DEVICE_TYPE_S10, 0),
                                      (DEVICE_TYPE_K10PRO, 0)):
            coordinator = MagicMock(device_mac="FAKEDEVICE", rooms={})
            coordinator.data = {"rooms": {}}
            entry = MagicMock(entry_id="e", data={"device_type": device_type})
            hass = MagicMock()
            hass.data = {DOMAIN: {"e": coordinator}}
            added = []
            await sensor.async_setup_entry(hass, entry, added.extend)
            found = [e for e in added if isinstance(e, sensor.SwitchBotMapRefreshSensor)]
            self.assertEqual(len(found), expected, device_type)

    def test_map_refresh_sensor_reports_time_and_last_error(self):
        coordinator = MagicMock(device_mac="FAKEDEVICE", map_refreshed_at=None,
                                k10_last_error="Map storage returned HTTP 403")
        entity = sensor.SwitchBotMapRefreshSensor(coordinator)
        self.assertEqual(entity.unique_id, "FAKEDEVICE_last_map_refresh")
        self.assertIsNone(entity.native_value)
        self.assertEqual(entity.extra_state_attributes,
                         {"last_error": "Map storage returned HTTP 403"})


def track_reply(points, total, *, as_string=True):
    track = {"pathID": 1, "pointCounts": len(points), "posArray": points,
             "startPos": 0, "totalPoints": total}
    outer = {"ErrorNO": 0, "Track": json.dumps(track) if as_string else track}
    return {"code": 0, "data": {"result": json.dumps(outer)}}


class TrackApiTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        client = api.K10Api("example-device", "example-product", "installation")
        client.token, client.token_expiry = "delegated", 9999999999
        return client

    @staticmethod
    def start_indexes(session):
        return [json.loads(c[2]["data"]["input"])["StartPosIndex"] for c in session.calls]

    async def test_pages_until_total(self):
        session = Session([track_reply([[1, 2], [3, 4]], 3), track_reply([[5, 6]], 3)])
        points, total = await self.client().get_clean_track(session)
        self.assertEqual((points, total), ([[1, 2], [3, 4], [5, 6]], 3))
        self.assertEqual(self.start_indexes(session), [0, 2])
        self.assertEqual(session.calls[0][2]["data"]["identifier"], "GetCleanTrackWithPosIdx")

    async def test_reads_only_new_points(self):
        session = Session([track_reply([[5, 6]], 3, as_string=False)])
        self.assertEqual(await self.client().get_clean_track(session, 2), ([[5, 6]], 3))
        self.assertEqual(self.start_indexes(session), [2])

    async def test_empty_or_restarted_track(self):
        session = Session([track_reply([], 1)])
        self.assertEqual(await self.client().get_clean_track(session, 5), ([], 1))

    async def test_rejected_or_invalid_track(self):
        for reply in ({"code": 1004}, {"code": 0, "data": {"result": "{}"}}):
            with self.assertRaises(api.K10ProtocolError):
                await self.client().get_clean_track(Session([reply]))


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.rendered = k10_map.decode_k10_map(k10_map_record())

    def test_without_path_or_robot_the_image_is_unchanged(self):
        overlay = k10_map.render_k10_overlay(self.rendered, None, [])
        self.assertEqual(rgba_rows(overlay["image"]), rgba_rows(self.rendered["image"]))
        self.assertEqual((overlay["pixel"], overlay["segments"]), (None, 0))

    def test_path_is_drawn_and_jumps_are_skipped(self):
        path = [tuple(mm(8, 10)), tuple(mm(12, 10)), tuple(mm(30, 20))]  # Last step > 1 m.
        overlay = k10_map.render_k10_overlay(self.rendered, None, path)
        self.assertEqual(overlay["segments"], 1)
        self.assertTrue(pixels_of(overlay["image"], (*k10_map.C_PATH, 255)))

    def test_robot_is_drawn_at_its_position(self):
        pose = (*mm(10, 10), None)
        overlay = k10_map.render_k10_overlay(self.rendered, pose, [])
        self.assertEqual(pixel(overlay["image"], overlay["pixel"]["x"], overlay["pixel"]["y"]),
                         (*k10_map.C_ROBOT, 255))
        self.assertFalse(pixels_of(overlay["image"], (*k10_map.C_HEADING, 255)))

    def heading_direction(self, heading):
        """Mean offset of the heading line's pixels from the robot's centre."""
        overlay = k10_map.render_k10_overlay(self.rendered, (*mm(15, 15), heading), [])
        line = pixels_of(overlay["image"], (*k10_map.C_HEADING, 255))
        self.assertTrue(line, heading)
        cx, cy = overlay["pixel"]["x"], overlay["pixel"]["y"]
        return (sum(x for x, _ in line) / len(line) - cx, sum(y for _, y in line) / len(line) - cy)

    def test_heading_follows_the_rotated_map(self):
        # Milliradians counter-clockwise from +x; the map is rotated 180°, so robot
        # +x is drawn to the left and robot +y downwards.
        for heading, (want_x, want_y) in ((0, (-1, 0)), (1571, (0, 1)),
                                          (3142, (1, 0)), (-1571, (0, -1))):
            dx, dy = self.heading_direction(heading)
            norm = math.hypot(dx, dy)
            self.assertGreater((dx * want_x + dy * want_y) / norm, 0.95, heading)

    def test_robot_off_the_map_is_not_drawn(self):
        overlay = k10_map.render_k10_overlay(self.rendered, (10**7, 10**7, 0), [])
        self.assertIsNone(overlay["pixel"])

    def test_parse_pose(self):
        self.assertEqual(k10_map.parse_pose("[100, -200, 45]"), (100.0, -200.0, 45))
        self.assertEqual(k10_map.parse_pose("[100, -200]"), (100.0, -200.0, None))
        for bad in (None, "", "not json", "[1]", "{}", '["a", "b"]'):
            self.assertIsNone(k10_map.parse_pose(bad), bad)


class PathCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.object(coordinator_module, "async_get_clientsession",
                               return_value=MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def coordinator(self, **extra):
        coordinator = fake_coordinator(map_data=k10_map.decode_k10_map(k10_map_record()), **extra)
        coordinator._k10_api = MagicMock(get_clean_track=AsyncMock())
        return coordinator

    async def test_whole_track_once_then_new_points_then_restart(self):
        coordinator = self.coordinator()
        track = coordinator._k10_api.get_clean_track
        a, b, c = mm(8, 10), mm(9, 10), mm(10, 10)
        track.return_value = ([a, b], 2)
        await coordinator._async_update_k10_track(7)  # Docked: read the whole last track.
        self.assertEqual(coordinator._k10_path, [tuple(a), tuple(b)])
        first_update = coordinator.path_updated_at
        self.assertIsNotNone(first_update)
        await coordinator._async_update_k10_track(7)  # Still docked: nothing new to read.
        self.assertEqual(track.await_count, 1)
        track.return_value = ([c], 3)
        await coordinator._async_update_k10_track(1)  # Cleaning: only new points.
        self.assertEqual(track.await_args.args[1], 2)
        self.assertEqual(len(coordinator._k10_path), 3)
        track.side_effect = [([], 1), ([a], 1)]
        await coordinator._async_update_k10_track(1)  # A new task restarted the track.
        self.assertEqual(coordinator._k10_path, [tuple(a)])
        self.assertEqual(coordinator._k10_track_index, 1)

    async def test_unchanged_track_keeps_its_timestamp(self):
        coordinator = self.coordinator()
        coordinator._k10_api.get_clean_track = AsyncMock(return_value=([mm(8, 10)], 1))
        await coordinator._async_update_k10_track(1)
        first_update = coordinator.path_updated_at
        coordinator._k10_api.get_clean_track.return_value = ([], 1)
        await coordinator._async_update_k10_track(1)
        self.assertEqual(coordinator.path_updated_at, first_update)

    async def test_track_errors(self):
        coordinator = self.coordinator()
        track = coordinator._k10_api.get_clean_track
        track.side_effect = TimeoutError("https://signed.example/?Signature=secret")
        await coordinator._async_update_k10_track(7)
        self.assertFalse(coordinator._k10_track_loaded)  # Network errors retry next poll.
        self.assertEqual(coordinator.k10_track_error, "TimeoutError")
        track.side_effect = api.K10ProtocolError("Cannot read cleaning track (code 1004)")
        await coordinator._async_update_k10_track(7)
        await coordinator._async_update_k10_track(7)
        self.assertEqual(track.await_count, 2)  # Rejections don't retry every idle poll.
        self.assertEqual(coordinator.k10_track_error, "Cannot read cleaning track (code 1004)")
        track.side_effect, track.return_value = None, ([mm(8, 10)], 1)
        await coordinator._async_update_k10_track(1)  # A task retries; success clears it.
        self.assertIsNone(coordinator.k10_track_error)

    async def test_overlay_is_redrawn_only_on_change(self):
        coordinator = self.coordinator()
        pose = (*mm(12, 12), 45)
        await coordinator._async_update_k10_overlay(pose)
        attrs = coordinator.map_data["attributes"]
        self.assertTrue(attrs["robot_position_available"])
        self.assertEqual({k: attrs["robot_position"][k] for k in ("kind", "x", "y", "heading")},
                         {"kind": "reported", "x": pose[0], "y": pose[1], "heading": 45})
        self.assertFalse(attrs["route_available"])
        renders = coordinator.hass.async_add_executor_job.await_count
        await coordinator._async_update_k10_overlay(pose)
        self.assertEqual(coordinator.hass.async_add_executor_job.await_count, renders)
        coordinator._k10_path = [tuple(mm(8, 10)), tuple(mm(9, 10))]
        await coordinator._async_update_k10_overlay(pose)
        self.assertTrue(coordinator.map_data["attributes"]["route_available"])
        self.assertEqual(coordinator.map_data["attributes"]["route_points"], 2)
        self.assertIn("_frame", coordinator.map_data)  # Later redraws still have the base.

    async def test_map_refresh_rereads_track_and_redraws(self):
        coordinator = self.coordinator(_k10_track_loaded=True, _k10_overlay_drawn=(None, 0))
        client = MagicMock()
        client.get_map = AsyncMock(return_value=(k10_map_record(), {"MapID": 101}))
        coordinator._authorize_k10_api = AsyncMock(return_value=client)
        await coordinator.async_refresh_k10_rooms()
        self.assertFalse(coordinator._k10_track_loaded)
        self.assertIsNone(coordinator._k10_overlay_drawn)

    async def test_polls_faster_while_moving(self):
        coordinator = self.coordinator(_last_room_refresh=10**12)
        client = MagicMock()
        client.get_properties = AsyncMock(return_value={"CurrentLocation": "[0, 0, 0]"})
        client.get_clean_track = AsyncMock(return_value=([], 0))
        coordinator._k10_api = client
        coordinator._authorize_k10_api = AsyncMock(return_value=client)
        for status, seconds in ((1, 15), (4, 30), (5, 15), (7, 30)):
            coordinator.async_get_k10_status = AsyncMock(return_value={"WorkingStatus": status})
            await coordinator._async_update_data_k10()
            self.assertEqual(coordinator.update_interval, timedelta(seconds=seconds), status)


class PathSensorTests(unittest.IsolatedAsyncioTestCase):
    async def test_path_update_sensor_only_for_k10(self):
        for device_type, expected in ((DEVICE_TYPE_K10, 1), (DEVICE_TYPE_S10, 0)):
            coordinator = MagicMock(device_mac="FAKEDEVICE", rooms={})
            coordinator.data = {"rooms": {}}
            entry = MagicMock(entry_id="e", data={"device_type": device_type})
            hass = MagicMock()
            hass.data = {DOMAIN: {"e": coordinator}}
            added = []
            await sensor.async_setup_entry(hass, entry, added.extend)
            found = [e for e in added if isinstance(e, sensor.SwitchBotPathUpdateSensor)]
            self.assertEqual(len(found), expected, device_type)

    def test_path_update_sensor_reports_time(self):
        coordinator = MagicMock(device_mac="FAKEDEVICE", path_updated_at="then",
                                k10_track_error="TimeoutError")
        entity = sensor.SwitchBotPathUpdateSensor(coordinator)
        self.assertEqual(entity.unique_id, "FAKEDEVICE_last_path_update")
        self.assertEqual(entity.native_value, "then")
        self.assertEqual(entity.extra_state_attributes, {"last_error": "TimeoutError"})


if __name__ == "__main__":
    unittest.main()
