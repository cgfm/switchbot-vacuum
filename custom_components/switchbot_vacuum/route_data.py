"""Read and render historical S10 tracks entirely in an executor worker."""

from __future__ import annotations

import asyncio
import io
import json
import math
import zipfile
from datetime import datetime, timezone
from typing import Any

from .map_data import SCALE, _png


def download_route(creds, bucket, resource, region, map_data, record):
    """Keep SDK loading, SSL, ZIP parsing and rendering outside HA's loop."""
    import aiobotocore.session
    from aiobotocore.config import AioConfig

    async def download():
        async with aiobotocore.session.get_session().create_client(
            "s3",
            region_name=region,
            aws_access_key_id=creds["accessKeyId"],
            aws_secret_access_key=creds["secretAccessKey"],
            aws_session_token=creds["sessionToken"],
            config=AioConfig(
                connect_timeout=10,
                read_timeout=30,
                retries={"total_max_attempts": 2, "mode": "standard"},
            ),
        ) as client:
            response = await client.get_object(Bucket=bucket, Key=resource)
            async with response["Body"] as body:
                raw = await body.read()
                if len(raw) > 16 * 1024 * 1024:
                    raise ValueError("Report is too large")
                return raw

    return render_report(map_data, asyncio.run(download()), record)


def render_report(map_data: dict[str, Any], raw: bytes, record: dict[str, Any]) -> dict[str, Any]:
    """Match the report to this map, respecting the app's track segment IDs."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if sum(f.file_size for f in archive.infolist()) > 32 * 1024 * 1024:
            raise ValueError("Report archive is too large")
        info = json.loads(archive.read("mapinfo.json"))
        if info.get("uuid") != map_data["attributes"]["map_id"]:
            return {"status": "different_map"}
        points = json.loads(archive.read("track.json"))
    if not isinstance(points, list) or len(points) > 300_000:
        raise ValueError("Invalid track data")
    valid = [
        p
        for p in points
        if isinstance(p, list)
        and len(p) >= 6
        and all(type(v) in (int, float) and math.isfinite(v) for v in p[:6])
    ]
    if not valid:
        return {"status": "empty"}
    # Field 4 separates continuous paths; field 5 identifies cleaning/movement.
    # Verified against the S10 app's getPathList and PathType definitions.
    groups = {}
    for p in sorted(valid, key=lambda p: p[3]):
        groups.setdefault(p[4], []).append(p)
    width, height = map_data["attributes"]["width"], map_data["attributes"]["height"]
    rows = [bytearray(row) for row in map_data["_render_rows"]]
    origin, resolution, grid_height = map_data["_transform"]
    ox, oy, yaw = origin

    def pixel(p):
        dx, dy = p[0] - ox, p[1] - oy
        return (
            (dx * math.cos(yaw) + dy * math.sin(yaw)) / resolution * SCALE,
            (grid_height - 1 - (-dx * math.sin(yaw) + dy * math.cos(yaw)) / resolution) * SCALE,
        )

    segments = 0
    for group in groups.values():
        for a, b in zip(group, group[1:]):
            x0, y0 = pixel(a)
            x1, y1 = pixel(b)
            # Never draw jumps or connect unrelated continuous segments.
            if math.hypot(b[0] - a[0], b[1] - a[1]) > 2:
                continue
            if not (0 <= x0 < width and 0 <= y0 < height and 0 <= x1 < width and 0 <= y1 < height):
                continue
            color = bytes((25, 130, 83) if b[5] in (1, 2) else (110, 123, 143))
            steps = max(1, math.ceil(max(abs(x1 - x0), abs(y1 - y0))))
            for step in range(steps + 1):
                x = round(x0 + (x1 - x0) * step / steps)
                y = round(y0 + (y1 - y0) * step / steps)
                for xx, yy in ((x, y), (x + 1, y)):
                    if 0 <= xx < width and 0 <= yy < height:
                        rows[yy][3 * xx : 3 * xx + 3] = color
            segments += 1
    start = datetime.fromtimestamp(record["timestamp"] / 1000, timezone.utc)
    return {
        "status": "available",
        "image": _png(width, height, rows),
        "_history_rows": tuple(bytes(row) for row in rows),
        "metadata": {
            "kind": "historical",
            "task_id": record["taskID"],
            "started_at": start.isoformat(),
            "point_count": len(valid),
            "segments": len(groups),
            "rendered_lines": segments,
            "clean_area": record.get("taskArea"),
            "duration_seconds": round(record.get("duration", 0) / 1000, 1),
        },
    }


def render_position(map_data, pose):
    """Draw the most recently received pose without modifying cached pixels."""
    width, height = map_data["attributes"]["width"], map_data["attributes"]["height"]
    rows = [bytearray(row) for row in map_data.get("_history_rows", map_data["_render_rows"])]
    origin, resolution, grid_height = map_data["_transform"]
    ox, oy, yaw = origin
    dx, dy = pose[0] - ox, pose[1] - oy
    x = (dx * math.cos(yaw) + dy * math.sin(yaw)) / resolution * SCALE
    y = (grid_height - 1 - (-dx * math.sin(yaw) + dy * math.cos(yaw)) / resolution) * SCALE
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError("Pose outside map")
    for yy in range(max(0, round(y) - 11), min(height, round(y) + 12)):
        for xx in range(max(0, round(x) - 11), min(width, round(x) + 12)):
            distance = math.hypot(xx - x, yy - y)
            if distance <= 11:
                rows[yy][xx * 3 : xx * 3 + 3] = bytes(
                    (235, 126, 24) if distance <= 8 else (255, 255, 255)
                )
    # Heading in the map frame; image y increases downwards.
    angle = pose[2] - yaw
    for i in range(17):
        xx, yy = round(x + math.cos(angle) * i), round(y - math.sin(angle) * i)
        if 0 <= xx < width and 0 <= yy < height:
            rows[yy][xx * 3 : xx * 3 + 3] = bytes((120, 54, 5))
    return {"image": _png(width, height, rows), "pixel": {"x": round(x, 2), "y": round(y, 2)}}
