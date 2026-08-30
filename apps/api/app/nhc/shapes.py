"""Read NHC's GIS bundles without unpacking them.

Each advisory ships a zip of ESRI shapefiles — cone polygon, track line,
forecast points, watch and warning segments — and the best track bundle adds the
wind swath, which is the area that actually experienced each threshold once the
season was reanalysed.

Shapes come back as GeoJSON rather than WKT so PostGIS can take them straight
through ``ST_GeomFromGeoJSON``. Writing our own WKT serialiser would be a second
place for a ring to close incorrectly, and a polygon that is subtly wrong is
worse here than one that fails outright: it would still render.

Nothing is extracted to disk. The zips are the cache, the manifest pins them,
and unpacking would create a second copy that nothing checksums.
"""

from __future__ import annotations

import io
import logging
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import shapefile

_PYSHP_LOG = logging.getLogger(shapefile.__name__)


@contextmanager
def _quiet_ring_warning():
    """Silence pyshp's ring-orientation warning for NHC's cone polygons.

    NHC winds the cone's rings counter-clockwise, which the shapefile spec reads
    as "this is a hole". pyshp notices, says so, and recovers by treating them as
    exterior rings — which is the right recovery, and produces the cone we want.

    Suppressed rather than tolerated because it fires on every advisory: forty
    lines of a known, verified condition in a worker log is how a genuinely new
    warning goes unread. The claim that the recovery is correct is not taken on
    faith — test_the_cone_is_a_cone_and_not_its_inverse measures the resulting
    area, because the failure mode is a polygon covering the whole globe except
    the cone.
    """
    previous = _PYSHP_LOG.level
    _PYSHP_LOG.setLevel(logging.ERROR)
    try:
        yield
    finally:
        _PYSHP_LOG.setLevel(previous)

#: NHC's "not available" sentinel in the GIS attribute tables. It appears as a
#: float in fields like MSLP on forecast points, where a naive read produces a
#: 9999-millibar hurricane.
MISSING = 9999.0


@dataclass(frozen=True)
class Feature:
    """One shapefile record: its geometry as GeoJSON, and its attributes."""

    geometry: dict[str, Any] | None
    attributes: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        value = self.attributes.get(key, default)
        return default if value == MISSING else value


def layers(archive: Path) -> tuple[str, ...]:
    """Names of the shapefile layers inside a bundle, without the extension."""
    with zipfile.ZipFile(archive) as z:
        return tuple(sorted({n[:-4] for n in z.namelist() if n.lower().endswith(".shp")}))


def read_layer(
    archive: Path | io.BytesIO,
    suffix: str,
    *,
    required: bool = True,
    contains: bool = False,
) -> list[Feature]:
    """Read the layer whose name ends with — or, with ``contains``, carries —
    ``suffix``.

    Matching on a suffix rather than a full name because NHC embeds the advisory
    number in the layer name — ``al132025-025_5day_pgn`` — so the caller would
    otherwise have to rebuild the filename it just derived the path from. The
    graphical outlook bundle inverts that convention (``gtwo_areas_<stamp>``),
    which is what ``contains`` is for. ``archive`` may be an in-memory zip: the
    outlook is fetched live rather than pinned in a cache directory.

    Some layers are genuinely absent rather than missing. Melissa's advisory 40
    ships no watch/warning layer because by then every watch had been
    discontinued, and "no warnings in effect" is a real state that should read as
    an empty list. Geometry layers stay ``required``, because a 5day bundle with
    no cone is a broken download.
    """
    with zipfile.ZipFile(archive) as z:
        names = [n[:-4] for n in z.namelist() if n.lower().endswith(".shp")]
        match = next(
            (n for n in names if (suffix in n if contains else n.endswith(suffix))),
            None,
        )
        if match is None:
            if not required:
                return []
            raise KeyError(
                f"no layer matching {suffix!r} in "
                f"{getattr(archive, 'name', 'in-memory archive')}: {sorted(names)}"
            )

        parts = {}
        for ext in ("shp", "dbf", "shx"):
            try:
                parts[ext] = io.BytesIO(z.read(f"{match}.{ext}"))
            except KeyError:
                # shx is an index and pyshp can rebuild what it needs without
                # one; shp and dbf are geometry and attributes, so their absence
                # is a corrupt bundle rather than a degraded one.
                if ext != "shx":
                    raise

        reader = shapefile.Reader(**parts)
        try:
            fields = [f[0] for f in reader.fields[1:]]  # [0] is the deletion flag
            out = []
            with _quiet_ring_warning():
                for shape_record in reader.iterShapeRecords():
                    shape = shape_record.shape
                    # A null shape is a legitimate record with no geometry — a
                    # watch/warning row for a segment that has been cancelled,
                    # for instance. Keep the attributes, say geometry is absent.
                    geometry = (
                        None if shape.shapeType == shapefile.NULL else shape.__geo_interface__
                    )
                    out.append(
                        Feature(
                            geometry=geometry,
                            attributes=dict(zip(fields, shape_record.record, strict=True)),
                        )
                    )
            return out
        finally:
            reader.close()
