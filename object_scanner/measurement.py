"""Pure helpers for scan naming, dimensions, and quality reporting."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


def slugify_scan_name(value: str) -> str:
    """Return a filesystem-safe scan label or raise for an unusable name."""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    slug = slug[:48].strip("-")
    if not slug:
        raise ValueError("Enter a scan name containing at least one letter or number.")
    return slug


def dimensions_from_extents(extents_m: Iterable[float]) -> dict[str, float]:
    """Convert three principal-axis extents in metres to descending millimetres."""
    values = [float(value) for value in extents_m]
    if len(values) != 3 or any(value <= 0 for value in values):
        raise ValueError("A valid 3D bounding box requires three positive extents.")

    length_m, width_m, height_m = sorted(values, reverse=True)
    return {
        "length_mm": round(length_m * 1000.0, 1),
        "width_mm": round(width_m * 1000.0, 1),
        "height_mm": round(height_m * 1000.0, 1),
    }


def quality_summary(accepted_frames: int, total_frames: int, object_points: int) -> dict[str, object]:
    """Return a text-first quality label with useful supporting metrics."""
    if total_frames <= 0:
        ratio = 0.0
    else:
        ratio = accepted_frames / total_frames

    if accepted_frames >= 80 and ratio >= 0.75 and object_points >= 10_000:
        label = "GOOD"
        border = "solid"
    elif accepted_frames >= 30 and ratio >= 0.5 and object_points >= 2_000:
        label = "CHECK"
        border = "dashed"
    else:
        label = "WEAK"
        border = "dotted"

    return {
        "label": label,
        "border": border,
        "accepted_frames": accepted_frames,
        "total_frames": total_frames,
        "tracking_ratio": round(ratio, 3),
        "object_points": object_points,
    }
