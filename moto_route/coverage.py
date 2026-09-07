"""What ground a self-hosted Overpass actually holds.

A bounding box was the first answer here, and it is wrong in a way that only
shows up on the road. The box around Greece and Italy also contains Albania,
Croatia, Slovenia, Bosnia, Montenegro, Serbia, Bulgaria, western Turkey and a
strip of Tunisia — eight countries with no data behind them, and precisely the
ones you ride through going overland from Italy to Greece. Inside the box the
local server is asked; with no data it answers HTTP 200 and an empty list; the
rider is told the road is clear by a database that has never heard of it.

Geofabrik publishes the clipping polygon for every extract next to the extract
itself, as a `.poly` file. That is the exact shape of what was imported — in
fact slightly generous, since the polygons are buffered a little beyond the
border — so testing against it answers the real question rather than a
rectangular approximation of it.

The format is plain text: a name line, then one or more rings, each a ring name
followed by "lon lat" lines and END. A ring whose name starts with "!" is a
hole. The file ends with a final END.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

LatLon = tuple[float, float]


class CoverageError(ValueError):
    """A coverage definition that could not be understood."""


@dataclass(frozen=True)
class Ring:
    """One closed ring, with its bounding box kept for a cheap rejection."""

    hole: bool
    points: tuple[LatLon, ...]          # (lat, lon), closed or not
    south: float
    west: float
    north: float
    east: float

    @classmethod
    def of(cls, hole: bool, points: Sequence[LatLon]) -> "Ring":
        lats = [lat for lat, _ in points]
        lons = [lon for _, lon in points]
        return cls(hole, tuple(points),
                   min(lats), min(lons), max(lats), max(lons))

    def may_contain(self, lat: float, lon: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lon <= self.east

    def contains(self, lat: float, lon: float) -> bool:
        """Ray casting, counting crossings of the horizontal line at ``lat``.

        Points exactly on an edge are not worth special-casing: the polygons
        are buffered beyond the real border, so the boundary is already
        approximate by design.
        """
        if not self.may_contain(lat, lon):
            return False

        inside = False
        pts = self.points
        j = len(pts) - 1
        for i in range(len(pts)):
            lat_i, lon_i = pts[i]
            lat_j, lon_j = pts[j]
            # Half-open comparison, so a vertex at exactly `lat` is counted
            # once rather than zero or two times.
            if (lat_i > lat) != (lat_j > lat):
                span = lat_j - lat_i
                if span != 0 and lon < lon_i + (lat - lat_i) / span * (lon_j - lon_i):
                    inside = not inside
            j = i
        return inside


@dataclass(frozen=True)
class Coverage:
    """The union of every extract's clipping polygon."""

    rings: tuple[Ring, ...]

    def contains(self, lat: float, lon: float) -> bool:
        # Inside any solid ring, and not inside a hole of the region that
        # accepted it. Holes are rare in country extracts (an enclave such as
        # Lesotho or the Vatican), but cost nothing to honour.
        if any(ring.contains(lat, lon) for ring in self.rings if ring.hole):
            return False
        return any(ring.contains(lat, lon) for ring in self.rings if not ring.hole)

    def contains_all(self, points: Iterable[LatLon]) -> bool:
        """True only when every point is covered.

        Every point, not the route's bounding box: a ride from Italy to Greece
        has both ends inside the data and its middle in Albania, and a box test
        would wave it through.
        """
        seen = False
        for lat, lon in points:
            seen = True
            if not self.contains(lat, lon):
                return False
        return seen


def parse_poly(text: str) -> list[Ring]:
    """Parse one Osmosis/Geofabrik `.poly` file into rings."""
    rings: list[Ring] = []
    lines = [line.strip() for line in text.splitlines()]
    i = 0
    if i < len(lines) and lines[i]:
        i += 1                                    # the file's name line

    while i < len(lines):
        name = lines[i]
        i += 1
        if not name:
            continue
        if name == "END":
            break

        hole = name.startswith("!")
        points: list[LatLon] = []
        while i < len(lines) and lines[i] != "END":
            parts = lines[i].split()
            if len(parts) >= 2:
                try:
                    # .poly is longitude first, which is the opposite of every
                    # other coordinate in this codebase. Swapping it here means
                    # nothing downstream has to remember that.
                    lon, lat = float(parts[0]), float(parts[1])
                except ValueError as exc:
                    raise CoverageError(f"Bad coordinate line: {lines[i]!r}") from exc
                points.append((lat, lon))
            i += 1
        i += 1                                    # the ring's END

        if len(points) >= 3:
            rings.append(Ring.of(hole, points))

    if not rings:
        raise CoverageError("No rings found; is this an Osmosis .poly file?")
    return rings


def load(paths: Sequence[str]) -> Coverage:
    """Read one or more `.poly` files into a single coverage area."""
    rings: list[Ring] = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                rings.extend(parse_poly(handle.read()))
        except OSError as exc:
            raise CoverageError(f"Could not read coverage file {path}: {exc}") from exc
    return Coverage(tuple(rings))
