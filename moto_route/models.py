"""Domain model.

A parsed file becomes exactly one :class:`Route`, whatever the source format.
Keeping the rest of the app behind this shape means the weather, hazard and
alternate-route services never need to know whether the ride came from a Garmin
GPX, a Google Earth KML or something else entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

from . import geo


@dataclass(slots=True)
class GeoPoint:
    """One position on the ride."""

    lat: float
    lon: float
    ele: float | None = None
    time: datetime | None = None

    def as_latlon(self) -> geo.LatLon:
        return (self.lat, self.lon)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"lat": round(self.lat, 6), "lon": round(self.lon, 6)}
        if self.ele is not None:
            d["ele"] = round(self.ele, 1)
        if self.time is not None:
            d["time"] = self.time.isoformat()
        return d


#: ``via`` points are the ones you actually chose ("stop for coffee here");
#: ``shaping`` points only exist to drag the line onto a nicer road and should
#: not be shown as stops; ``waypoint`` is a standalone POI from the file.
WaypointKind = str


@dataclass(slots=True)
class Waypoint:
    lat: float
    lon: float
    name: str = ""
    description: str = ""
    kind: WaypointKind = "waypoint"
    symbol: str = ""
    #: Filled in later: how far along the route this point sits, in metres.
    distance_m: float | None = None

    def as_latlon(self) -> geo.LatLon:
        return (self.lat, self.lon)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "name": self.name,
            "kind": self.kind,
        }
        if self.description:
            d["description"] = self.description
        if self.symbol:
            d["symbol"] = self.symbol
        if self.distance_m is not None:
            d["distance_m"] = round(self.distance_m, 1)
        return d


@dataclass(slots=True)
class Route:
    """A ride: one or more drawable lines plus the points of interest on it.

    ``lines`` is a list of lists because a GPX file can hold several tracks, or
    one track split into segments where the GPS lost its fix. Drawing them as
    separate polylines avoids a bogus straight line across the gap.
    """

    name: str = ""
    source_format: str = ""
    lines: list[list[GeoPoint]] = field(default_factory=list)
    waypoints: list[Waypoint] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def iter_points(self) -> Iterator[GeoPoint]:
        for line in self.lines:
            yield from line

    @property
    def all_latlon(self) -> list[geo.LatLon]:
        """Every point of every line, flattened — the ride as one polyline."""
        return [p.as_latlon() for p in self.iter_points()]

    @property
    def point_count(self) -> int:
        return sum(len(line) for line in self.lines)

    @property
    def distance_m(self) -> float:
        """Total length, summed per line so segment gaps are not counted."""
        return sum(geo.total_distance_m([p.as_latlon() for p in line]) for line in self.lines)

    def elevation_gain_m(self) -> tuple[float, float]:
        """Cumulative (ascent, descent) in metres.

        Raw barometric or SRTM elevation is noisy, so changes below a small
        threshold are ignored — otherwise a flat motorway "climbs" hundreds of
        metres of sensor jitter.
        """
        threshold = 3.0
        ascent = descent = 0.0
        for line in self.lines:
            anchor: float | None = None
            for p in line:
                if p.ele is None:
                    continue
                if anchor is None:
                    anchor = p.ele
                    continue
                delta = p.ele - anchor
                if delta > threshold:
                    ascent += delta
                    anchor = p.ele
                elif delta < -threshold:
                    descent += -delta
                    anchor = p.ele
        return ascent, descent

    def bounds(self) -> tuple[float, float, float, float] | None:
        pts = self.all_latlon + [w.as_latlon() for w in self.waypoints]
        if not pts:
            return None
        return geo.bounding_box(pts)

    def annotate_waypoint_distances(self) -> None:
        """Record how far along the route each waypoint sits.

        Done by projecting the waypoint onto the route line, which works even
        when the waypoint is a few metres off the recorded track (a parking spot
        beside the road, say).
        """
        pts = self.all_latlon
        if len(pts) < 2:
            return
        cumulative = geo.cumulative_distances(pts)
        for wp in self.waypoints:
            best_d, best_dist = float("inf"), None
            target = wp.as_latlon()
            for i, (a, b) in enumerate(zip(pts, pts[1:])):
                d = geo.point_to_segment_m(target, a, b)
                if d < best_d:
                    best_d = d
                    # Good enough: snap to the nearer end of the winning segment.
                    seg_len = cumulative[i + 1] - cumulative[i]
                    along = min(seg_len, geo.haversine_m(a, target))
                    best_dist = cumulative[i] + along
            wp.distance_m = best_dist

    def simplified_lines(self, tolerance_m: float) -> list[list[GeoPoint]]:
        """Drop redundant points for drawing, keeping the visible shape."""
        if tolerance_m <= 0:
            return self.lines
        out = []
        for line in self.lines:
            if len(line) < 3:
                out.append(line)
                continue
            coords = [p.as_latlon() for p in line]
            keep = geo.simplify_indices(coords, tolerance_m)
            out.append([line[i] for i in keep])
        return out

    def to_dict(self, simplify_m: float = 0.0) -> dict[str, Any]:
        ascent, descent = self.elevation_gain_m()
        lines = self.simplified_lines(simplify_m)
        return {
            "name": self.name,
            "source_format": self.source_format,
            "lines": [[p.to_dict() for p in line] for line in lines],
            "waypoints": [w.to_dict() for w in self.waypoints],
            "stats": {
                "distance_m": round(self.distance_m, 1),
                "point_count": self.point_count,
                "drawn_point_count": sum(len(l) for l in lines),
                "waypoint_count": len(self.waypoints),
                "ascent_m": round(ascent),
                "descent_m": round(descent),
            },
            "bounds": self.bounds(),
            "metadata": self.metadata,
        }
