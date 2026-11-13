"""Decode and render the K10+ (Qihoo 360 platform) cloud map in an executor.

The map record holds a base64, LZ4-compressed occupancy grid (``map``) described
by ``width``/``height`` cells of ``resolution`` metres from ``x_min``/``y_min``
metres. Rooms (``smartArea.value[].vertexs``), no-go zones (``area[].vertexs``)
and the dock (``chargeHandlePos``) are in millimetres. The SwitchBot app shows
the map rotated 180° from the raw grid; this renderer does the same.

The cleaning path (``GetCleanTrackWithPosIdx``) and robot position
(``CurrentLocation``) are also in millimetres. They are drawn over a cached copy
of the rendered map, so the grid is decoded only when a new map arrives.

Pure standard library apart from optional Pillow labels (a Home Assistant core
dependency).
"""

from __future__ import annotations

import base64
import json
import math
import struct
import zlib
from typing import Any

from .k10_api import room_names, smart_area

MAX_CELLS = 4_000_000
MAX_IMAGE_EDGE = 1600
MARGIN_CELLS = 3
MM_PER_M = 1000
# K10+ grid cell values; anything else is a wall or obstacle.
GRID_UNKNOWN = 127
GRID_FLOOR = 255

C_FLOOR = (200, 204, 212, 255)  # Floor not assigned to any room.
C_WALL = (66, 60, 100, 255)  # Walls and obstacles.
C_UNKNOWN = (0, 0, 0, 0)  # Unexplored: transparent.
C_NOGO = (224, 74, 74)
C_DOCK = (30, 120, 210)
C_ROBOT = (235, 126, 24)
C_HEADING = (120, 54, 5)
C_PATH = (255, 255, 255)
C_LABEL_BG = (44, 46, 62, 165)
# Track points further apart than this are a relocalisation jump, not a path.
MAX_PATH_STEP_MM = 1000
# Saturated, app-like room colours; red is left for no-go zones.
ROOM_COLORS = (
    (126, 186, 240),
    (246, 168, 92),
    (150, 146, 232),
    (244, 196, 112),
    (110, 212, 178),
    (236, 136, 112),
    (100, 160, 228),
    (196, 150, 220),
)


def lz4_block_decompress(src: bytes, out_size: int) -> bytes:
    """Bounds-checked LZ4 block decompression."""
    out = bytearray()
    si, n = 0, len(src)
    while si < n:
        token = src[si]
        si += 1
        literal = token >> 4
        if literal == 15:
            while True:
                if si >= n:
                    raise ValueError("Truncated LZ4 literal length")
                b = src[si]
                si += 1
                literal += b
                if b != 255:
                    break
        if si + literal > n or len(out) + literal > out_size:
            raise ValueError("LZ4 literal overruns buffer")
        out += src[si : si + literal]
        si += literal
        if si >= n:
            break
        if si + 2 > n:
            raise ValueError("Truncated LZ4 offset")
        offset = src[si] | (src[si + 1] << 8)
        si += 2
        match = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while True:
                if si >= n:
                    raise ValueError("Truncated LZ4 match length")
                b = src[si]
                si += 1
                match += b
                if b != 255:
                    break
        start = len(out) - offset
        if offset == 0 or start < 0 or len(out) + match > out_size:
            raise ValueError("Invalid LZ4 match")
        for k in range(match):  # Overlapping copies must be byte-by-byte.
            out.append(out[start + k])
    return bytes(out)


def decode_grid(map_data: dict[str, Any]) -> tuple[int, int, bytes]:
    width, height = int(map_data["width"]), int(map_data["height"])
    if not 0 < width * height <= MAX_CELLS:
        raise ValueError("Invalid map dimensions")
    packed = base64.b64decode(map_data["map"], validate=True)
    grid = lz4_block_decompress(packed, width * height)
    if len(grid) != width * height:
        raise ValueError("Occupancy grid size does not match its dimensions")
    return width, height, grid


def _png_rgba(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(b"\0" + bytes(row) for row in rows), 6))
        + chunk(b"IEND", b"")
    )


def _points(value: Any) -> list[tuple[float, float]]:
    """Finite ``[[x, y], ...]`` points; anything else is skipped."""
    if not isinstance(value, list):
        return []
    points = []
    for p in value:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            try:
                x, y = float(p[0]), float(p[1])
            except (TypeError, ValueError):
                continue
            if math.isfinite(x) and math.isfinite(y):
                points.append((x, y))
    return points


class _Frame:
    """Robot millimetres -> cropped grid cells -> scaled image pixels, rotated 180°."""

    def __init__(self, x_min, y_min, res, height, crop, scale):
        self.x_min, self.y_min, self.res, self.height = x_min, y_min, res, height
        self.left, self.top, self.right, self.bottom = crop  # Rows counted from the top.
        self.scale = scale
        self.image_w = (self.right - self.left + 1) * scale
        self.image_h = (self.bottom - self.top + 1) * scale

    def crop(self, x: float, y: float) -> tuple[float, float]:
        """Continuous crop-space coordinates (columns, rows from the top)."""
        col = (x / MM_PER_M - self.x_min) / self.res
        row = (y / MM_PER_M - self.y_min) / self.res  # Grid rows count from the bottom.
        return col - self.left, self.height - row - self.top

    def to_pixel(self, col: float, row: float) -> tuple[float, float]:
        return self.image_w - col * self.scale, self.image_h - row * self.scale

    def pixel(self, x: float, y: float) -> tuple[float, float]:
        return self.to_pixel(*self.crop(x, y))


def _fill_polygon(cells: list[tuple[float, float]], cw: int, ch: int):
    """Yield (col, row) cells whose centres are inside a polygon in crop space."""
    top = max(0, int(min(p[1] for p in cells)))
    bottom = min(ch, math.ceil(max(p[1] for p in cells)) + 1)
    for row in range(top, bottom):
        scan = row + 0.5
        xs = sorted(
            a[0] + (scan - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            for a, b in zip(cells, cells[1:] + cells[:1])
            if (a[1] <= scan < b[1]) or (b[1] <= scan < a[1])
        )
        for left, right in zip(xs[::2], xs[1::2]):
            for col in range(max(0, math.ceil(left - 0.5)), min(cw, math.ceil(right - 0.5))):
                yield col, row


def _disc(rows, width, height, x, y, radius, color, ring=None):
    for yy in range(max(0, int(y - radius - 1)), min(height, int(y + radius + 2))):
        row = rows[yy]
        for xx in range(max(0, int(x - radius - 1)), min(width, int(x + radius + 2))):
            d = math.hypot(xx + 0.5 - x, yy + 0.5 - y)
            if d <= radius:
                rgb = color if ring is None or d <= radius - 2 else ring
                row[xx * 4 : xx * 4 + 4] = bytes((*rgb, 255))


def _stroke(rows, width, height, segments, rgb, half) -> None:
    """Paint line segments (pixel coordinates) of half-width ``half`` onto rows."""
    color = bytes((*rgb, 255))
    for (x0, y0), (x1, y1) in segments:
        steps = max(1, int(math.hypot(x1 - x0, y1 - y0) * 2))
        for i in range(steps + 1):
            x = x0 + (x1 - x0) * i / steps
            y = y0 + (y1 - y0) * i / steps
            for yy in range(max(0, int(y - half)), min(height, int(y + half) + 1)):
                for xx in range(max(0, int(x - half)), min(width, int(x + half) + 1)):
                    rows[yy][xx * 4 : xx * 4 + 4] = color


def _draw_labels(rows, width, height, labels, scale) -> list:
    """Room names in rounded pills, like the app; skipped if Pillow is unavailable."""
    if not labels:
        return rows
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return rows
    image = Image.frombytes("RGBA", (width, height), b"".join(bytes(r) for r in rows))
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    try:
        font = ImageFont.load_default(size=max(10, scale * 5))
    except TypeError:  # Pillow < 10.1 has no scalable default font.
        font = ImageFont.load_default()
    pad = max(2, scale)
    for text, x, y in labels:
        left, top, right, bottom = draw.textbbox((x, y), text, font=font, anchor="mm")
        draw.rounded_rectangle(
            (left - pad * 2, top - pad, right + pad * 2, bottom + pad),
            radius=(bottom - top) // 2 + pad, fill=C_LABEL_BG,
        )
        draw.text((x, y), text, font=font, fill=(255, 255, 255, 255), anchor="mm")
    data = Image.alpha_composite(image, overlay).tobytes()
    stride = width * 4
    return [bytearray(data[i * stride : (i + 1) * stride]) for i in range(height)]


def decode_k10_map(map_data: dict[str, Any]) -> dict[str, Any]:
    """Return the rendered PNG and image attributes for a K10+ map record."""
    names = room_names(map_data)
    rooms = [
        (f"room{room['id']}", names[str(room["id"])], _points(room.get("vertexs")))
        for room in smart_area(map_data).get("value") or []
    ]
    zones = [
        _points(zone.get("vertexs"))
        for zone in map_data.get("area") or []
        if isinstance(zone, dict)
    ]
    dock = _points([map_data.get("chargeHandlePos")])

    width, height, grid = decode_grid(map_data)
    res = float(map_data["resolution"])
    x_min, y_min = float(map_data["x_min"]), float(map_data["y_min"])
    if not (res > 0 and math.isfinite(x_min) and math.isfinite(y_min)):
        raise ValueError("Invalid map transform")

    # Crop to explored cells (image rows count from the top; grid row 0 is the bottom).
    cols, rows_known = [], []
    for r in range(height):
        line = grid[r * width : (r + 1) * width]
        if line.count(GRID_UNKNOWN) != width:
            rows_known.append(height - 1 - r)
            cols.append(next(i for i, v in enumerate(line) if v != GRID_UNKNOWN))
            cols.append(width - 1 - next(
                i for i, v in enumerate(reversed(line)) if v != GRID_UNKNOWN
            ))
    # No-go zones may extend over unexplored space; keep them whole, as the app does.
    for x, y in (p for zone in zones for p in zone):
        cx, cy = (x / MM_PER_M - x_min) / res, (y / MM_PER_M - y_min) / res
        if 0 <= cx < width and 0 <= cy < height:
            cols.append(int(cx))
            rows_known.append(height - 1 - int(cy))
    if cols:
        left = max(0, min(cols) - MARGIN_CELLS)
        right = min(width - 1, max(cols) + MARGIN_CELLS)
        top = max(0, min(rows_known) - MARGIN_CELLS)
        bottom = min(height - 1, max(rows_known) + MARGIN_CELLS)
    else:
        left, top, right, bottom = 0, 0, width - 1, height - 1
    cw, ch = right - left + 1, bottom - top + 1
    scale = max(1, min(4, MAX_IMAGE_EDGE // max(cw, ch)))
    frame = _Frame(x_min, y_min, res, height, (left, top, right, bottom), scale)

    # cells[row][col] in unrotated crop space.
    cells = [[C_UNKNOWN] * cw for _ in range(ch)]
    floor = [[False] * cw for _ in range(ch)]
    for row in range(ch):
        source = (height - 1 - (top + row)) * width + left
        for col, value in enumerate(grid[source : source + cw]):
            if value == GRID_FLOOR:
                cells[row][col] = C_FLOOR
                floor[row][col] = True
            elif value != GRID_UNKNOWN:
                cells[row][col] = C_WALL

    def rounded(col: float, row: float) -> list[float]:
        return [round(v, 1) for v in frame.to_pixel(col, row)]

    geometry = {}
    labels: list[tuple[str, float, float]] = []
    for index, (room_id, name, vertices) in enumerate(rooms):
        if len(vertices) < 3:
            continue
        polygon = [frame.crop(x, y) for x, y in vertices]
        color = (*ROOM_COLORS[index % len(ROOM_COLORS)], 255)
        interior = []
        for col, row in _fill_polygon(polygon, cw, ch):
            if floor[row][col]:
                cells[row][col] = color
                interior.append((col + 0.5, row + 0.5))
        cx = sum(p[0] for p in polygon) / len(polygon)
        cy = sum(p[1] for p in polygon) / len(polygon)
        if interior:  # Snap the label onto the room's floor for L/U shapes.
            cx, cy = min(interior, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
        labels.append((name, *frame.to_pixel(cx, cy)))
        label_x, label_y = rounded(cx, cy)
        geometry[room_id] = {
            "name": name,
            "outline": [rounded(col, row) for col, row in polygon],
            "x": label_x,
            "y": label_y,
        }

    zone_outlines = []
    for vertices in zones:
        if len(vertices) < 3:
            continue
        for col, row in _fill_polygon([frame.crop(x, y) for x, y in vertices], cw, ch):
            base = cells[row][col]
            if base[3]:
                cells[row][col] = (
                    *((b * 3 + n * 2) // 5 for b, n in zip(base[:3], C_NOGO)), 255
                )
            else:  # Over unexplored space, as in the app.
                cells[row][col] = (*C_NOGO, 90)
        outline = [frame.pixel(x, y) for x, y in vertices]
        zone_outlines += list(zip(outline, outline[1:] + outline[:1]))

    image_w, image_h = frame.image_w, frame.image_h
    pixel_rows = []
    for row in reversed(cells):  # Rotate 180°: last row first, each row reversed.
        expanded = b"".join(bytes(c) * scale for c in reversed(row))
        pixel_rows.extend(bytearray(expanded) for _ in range(scale))
    _stroke(pixel_rows, image_w, image_h, zone_outlines, C_NOGO, max(0.5, scale / 4))

    charger = None
    if dock:
        px, py = frame.pixel(*dock[0])
        if 0 <= px < image_w and 0 <= py < image_h:
            _disc(pixel_rows, image_w, image_h, px, py, max(5, scale * 2), C_DOCK, (255, 255, 255))
            charger = {"x": round(px, 1), "y": round(py, 1)}

    # The path and robot are drawn between these pixels and the labels.
    base_rows = [bytes(row) for row in pixel_rows]
    return {
        "image": _png_rgba(
            image_w, image_h, _draw_labels(pixel_rows, image_w, image_h, labels, scale)
        ),
        "_base_rows": base_rows,
        "_frame": frame,
        "_labels": labels,
        "attributes": {
            "map_id": str(map_data.get("mapId", "")),
            "rooms": geometry,
            "charger": charger,
            "width": image_w,
            "height": image_h,
            "resolution": res,
            "robot_position_available": False,
            "route_available": False,
            "source": "SwitchBot K10+ cloud map",
        },
    }


def parse_pose(value: Any) -> tuple[float, float, float | None] | None:
    """Parse ``CurrentLocation``, a JSON ``[x, y, heading]`` string, into a pose.

    The heading is in milliradians (±π), counter-clockwise from the robot's +x
    axis; it was matched against the direction of travel on a K10+.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    point = _points([value])
    if not point:
        return None
    heading = value[2] if len(value) >= 3 and isinstance(value[2], (int, float)) else None
    return (*point[0], heading)


def parse_track(points: Any) -> list[tuple[float, float]]:
    """Parse ``posArray`` track points (``[[x, y], ...]``)."""
    return _points(points)


def render_k10_overlay(
    map_data: dict[str, Any],
    pose: tuple[float, float, float | None] | None,
    path: list[tuple[float, float]],
) -> dict[str, Any]:
    """Draw the cleaning path, room labels and robot over the cached map pixels."""
    frame: _Frame = map_data["_frame"]
    width, height = frame.image_w, frame.image_h
    rows = [bytearray(row) for row in map_data["_base_rows"]]
    segments = [
        (frame.pixel(*a), frame.pixel(*b))
        for a, b in zip(path, path[1:])
        if math.dist(a, b) <= MAX_PATH_STEP_MM
    ]
    _stroke(rows, width, height, segments, C_PATH, max(0.5, frame.scale / 4))
    rows = _draw_labels(rows, width, height, map_data["_labels"], frame.scale)
    pixel = None
    if pose is not None:
        x, y = frame.pixel(pose[0], pose[1])
        if 0 <= x < width and 0 <= y < height:
            radius = max(6, frame.scale * 2.5)
            _disc(rows, width, height, x, y, radius, C_ROBOT, (255, 255, 255))
            if pose[2] is not None:
                _draw_heading(rows, frame, pose, radius)
            pixel = {"x": round(x, 1), "y": round(y, 1)}
    return {"image": _png_rgba(width, height, rows), "pixel": pixel, "segments": len(segments)}


def _draw_heading(rows, frame: _Frame, pose: tuple[float, float, float], radius: float) -> None:
    """Draw a line from the robot's centre in the direction it faces."""
    angle = pose[2] / 1000  # Milliradians, counter-clockwise from +x.
    x, y = frame.pixel(pose[0], pose[1])
    # Map a point ahead of the robot, so the frame applies the map's rotation.
    ahead_x, ahead_y = frame.pixel(
        pose[0] + math.cos(angle) * MM_PER_M, pose[1] + math.sin(angle) * MM_PER_M
    )
    length = math.hypot(ahead_x - x, ahead_y - y)
    tip = (x + (ahead_x - x) / length * radius * 1.6, y + (ahead_y - y) / length * radius * 1.6)
    _stroke(rows, frame.image_w, frame.image_h, [((x, y), tip)], C_HEADING, max(1, frame.scale / 3))
