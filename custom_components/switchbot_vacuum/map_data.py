"""Decode the S10 occupancy map in an executor, without optional libraries."""

from __future__ import annotations

import io
import json
import math
import re
import struct
import zipfile
import zlib
from typing import Any

SCALE = 4
COLORS = (
    (192, 217, 246),
    (225, 206, 241),
    (180, 225, 224),
    (249, 218, 181),
    (211, 225, 177),
    (242, 201, 213),
    (215, 213, 247),
)


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    """Encode RGB pixels as PNG; all compression happens in the worker."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(b"\0" + row for row in rows)))
        + chunk(b"IEND", b"")
    )


def decode_map(raw: bytes) -> dict[str, Any] | None:
    """Return rooms, pixel geometry and a rendered image from the actual archive."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if "labels.json" not in archive.namelist():
            return None
        if sum(f.file_size for f in archive.infolist()) > 32 * 1024 * 1024:
            raise ValueError("Map archive is too large")
        labels = json.loads(archive.read("labels.json")).get("data", [])
        info = json.loads(archive.read("mapinfo.json"))
        metadata = json.loads(archive.read("map.json"))
        markers = json.loads(archive.read("markers.json")).get("data", [])
        pgm = archive.read("map.pgm")
    # S10 maps use binary 8-bit PGM. Comments are permitted between header tokens.
    header = re.match(rb"P5\s+(?:#[^\n]*\n\s*)*(\d+)\s+(\d+)\s+(\d+)\s", pgm)
    if not header:
        raise ValueError("Unsupported occupancy image")
    width, height, maximum = map(int, header.groups())
    if maximum != 255 or not 0 < width * height <= 4_000_000:
        raise ValueError("Invalid occupancy image dimensions")
    pixels = pgm[header.end() :]
    if len(pixels) != width * height:
        raise ValueError("Incomplete occupancy image")
    resolution = float(metadata["resolution"])
    ox, oy, yaw = metadata["origin"]
    if resolution <= 0:
        raise ValueError("Invalid map resolution")

    def point(x: float, y: float) -> list[float]:
        dx, dy = x - ox, y - oy
        return [
            (dx * math.cos(yaw) + dy * math.sin(yaw)) / resolution,
            height - 1 - (-dx * math.sin(yaw) + dy * math.cos(yaw)) / resolution,
        ]

    rows = []
    for y in range(height):
        rows.append(
            [
                ((239, 243, 247) if p == 205 else (45, 57, 72) if p < 100 else (250, 251, 253))
                for p in pixels[y * width : (y + 1) * width]
            ]
        )
    rooms = {}
    geometry = {}
    for room in labels:
        room_id = room.get("id", "")
        if not room_id.startswith("ROOM_") or room.get("type") != "polygon":
            continue
        coords = room.get("geometry", [])
        if len(coords) < 6 or len(coords) % 2:
            continue
        polygon = [point(float(coords[i]), float(coords[i + 1])) for i in range(0, len(coords), 2)]
        color = COLORS[len(rooms) % len(COLORS)]
        interior = []
        for y in range(
            max(0, int(min(p[1] for p in polygon))),
            min(height, math.ceil(max(p[1] for p in polygon)) + 1),
        ):
            scan = y + 0.5
            intersections = sorted(
                a[0] + (scan - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
                for a, b in zip(polygon, polygon[1:] + polygon[:1])
                if (a[1] <= scan < b[1]) or (b[1] <= scan < a[1])
            )
            for left, right in zip(intersections[::2], intersections[1::2]):
                for x in range(max(0, math.ceil(left)), min(width, math.ceil(right))):
                    if pixels[y * width + x] >= 250:
                        rows[y][x] = color
                        interior.append((x, y))
        cx = sum(p[0] for p in polygon) / len(polygon)
        cy = sum(p[1] for p in polygon) / len(polygon)
        if interior:
            cx, cy = min(interior, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
        rooms[room_id] = room.get("name") or room_id
        geometry[room_id] = {
            "name": rooms[room_id],
            "outline": [[round(x * SCALE, 2), round(y * SCALE, 2)] for x, y in polygon],
            "x": round(cx * SCALE, 2),
            "y": round(cy * SCALE, 2),
        }
    charger = None
    for marker in markers:
        coords = marker.get("geometry", [])
        if str(marker.get("id", "")).startswith("CHARGE_") and len(coords) >= 2:
            x, y = point(float(coords[0]), float(coords[1]))
            charger = {"x": round(x * SCALE, 2), "y": round(y * SCALE, 2)}
            for yy in range(max(0, int(y) - 3), min(height, int(y) + 4)):
                for xx in range(max(0, int(x) - 3), min(width, int(x) + 4)):
                    if (xx - x) ** 2 + (yy - y) ** 2 <= 9:
                        rows[yy][xx] = (30, 120, 210)
    expanded = []
    for row in rows:
        rgb = b"".join(bytes(color) * SCALE for color in row)
        expanded.extend([rgb] * SCALE)
    base_image = _png(width * SCALE, height * SCALE, expanded)
    return {
        "_base_image": base_image,
        "_render_rows": expanded,
        "_transform": (metadata["origin"], resolution, height),
        "rooms": rooms,
        "image": base_image,
        "attributes": {
            "map_id": info["uuid"],
            "rooms": geometry,
            "charger": charger,
            "width": width * SCALE,
            "height": height * SCALE,
            "resolution": resolution,
            "robot_position_available": False,
            "route_available": False,
            "source": "SwitchBot S10 saved cloud map",
        },
    }
