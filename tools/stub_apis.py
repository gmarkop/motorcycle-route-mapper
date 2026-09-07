"""Stand-ins for the four external services, for verifying the app offline.

Every external source this app uses is a live third-party service, which makes
the interesting states — a thunderstorm at the far end of the route, a fuel
desert, a closed pass — impossible to reproduce on demand. This impersonates all
four with data shaped to exercise them.

It is a development tool, not part of the app, and nothing imports it.

    python -m uvicorn tools.stub_apis:app --port 8940 &

    MOTO_WEATHER_URL=http://127.0.0.1:8940/v1/forecast \
    MOTO_OVERPASS_URL=http://127.0.0.1:8940/api/interpreter \
    MOTO_OSRM_URL=http://127.0.0.1:8940 \
    MOTO_AUTOBAHN_URL=http://127.0.0.1:8940/o/autobahn \
    python run.py --no-browser

Then load `examples/dolomites_demo.gpx`. The weather deteriorates along the
route on purpose — clear at the start, thunderstorms and 69 km/h gusts by the
end — so the rideability colours, the warnings and the "do not ride" verdict all
have something to show.
"""

from __future__ import annotations

import asyncio
import math
import os
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request

app = FastAPI(title="Route mapper stub services")


@app.get("/v1/forecast")
async def open_meteo(request: Request):
    """Open-Meteo. Returns one hourly series per requested coordinate."""
    latitudes = request.query_params["latitude"].split(",")
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(72)]

    series = []
    for index, _ in enumerate(latitudes):
        # Severity ramps from 0 at the first sample to 1 at the last.
        severity = index / max(len(latitudes) - 1, 1)
        series.append({"hourly": {
            "time": times,
            "temperature_2m": [round(22 - 14 * severity, 1)] * 72,
            "apparent_temperature": [round(21 - 15 * severity, 1)] * 72,
            "precipitation": [round(4.0 * severity, 1)] * 72,
            "precipitation_probability": [int(10 + 85 * severity)] * 72,
            "weather_code": [0 if severity < 0.3 else (61 if severity < 0.7 else 95)] * 72,
            "wind_speed_10m": [round(8 + 30 * severity, 1)] * 72,
            "wind_gusts_10m": [round(14 + 55 * severity, 1)] * 72,
            "visibility": [20000 if severity < 0.8 else 600] * 72,
        }})
    return series


def _node(lat: float, lon: float, tags: dict, node_id: int) -> dict:
    return {"type": "node", "id": node_id, "lat": lat, "lon": lon, "tags": tags}


@app.post("/api/interpreter")
async def overpass(request: Request):
    """Overpass. Answers POI, motorway-ref and hazard queries from one endpoint.

    The body arrives form-encoded as `data=<query>`, so it must be read through
    `request.form()`. Matching against the raw body instead silently fails every
    query — the percent-encoding hides the quotes the checks look for.
    """
    # STUB_OVERPASS_DELAY holds each answer back, so the browser check can see
    # what the panels look like while a real long-route query is still running.
    delay = float(os.environ.get("STUB_OVERPASS_DELAY", "0"))
    if delay:
        await asyncio.sleep(delay)

    form = await request.form()
    query = form.get("data", "")

    if query.rstrip().endswith("out count;"):
        # Used by tools/check_services.py to work out whether `around:` follows
        # the line through its coordinates or only searches near each one. Set
        # STUB_AROUND=circles to make this stub behave the second way, which is
        # how that probe is tested without a real Overpass.
        coord_count = query.count(",") // 2
        if os.environ.get("STUB_AROUND") == "circles":
            total = coord_count * 3          # scales with the number of points
        else:
            total = 240                      # depends on the line, not the points
        return {"elements": [{"type": "count", "id": 0,
                              "tags": {"total": str(total), "ways": str(total)}}]}

    # Matches however the POI query spells the amenity filter — it moved from
    # three exact-match sub-queries to one regex when the corridor walks were
    # reduced, and a stub keyed to the old spelling silently returns nothing.
    if "amenity" in query and "viewpoint" in query:
        return {"elements": [
            _node(46.4983, 11.3548, {"amenity": "fuel", "name": "Agip Bolzano",
                                     "brand": "Agip", "opening_hours": "24/7"}, 201),
            _node(46.5122, 11.7594, {"amenity": "fuel",
                                     "name": "Passo Sella Tankstelle"}, 202),
            _node(46.5405, 12.1357, {"amenity": "fuel", "name": "Q8 Cortina"}, 203),
            _node(46.5000, 11.5000, {"amenity": "cafe", "name": "Bar Ciampedie",
                                     "cuisine": "coffee_shop"}, 204),
            _node(46.4879, 11.8129, {"amenity": "cafe", "name": "Rifugio Pordoi"}, 205),
            _node(46.5192, 12.0093, {"tourism": "viewpoint",
                                     "name": "Falzarego Panorama", "ele": "2105"}, 206),
            _node(46.5122, 11.7594, {"tourism": "viewpoint", "name": "Sella Towers"}, 207),
        ]}

    if '"highway"="motorway"' in query:
        return {"elements": [{"type": "way", "tags": {"ref": "A22"}}]}

    return {"elements": [
        {"type": "way", "id": 101,
         "tags": {"highway": "construction", "construction": "secondary",
                  "name": "SS242 Passo Sella", "note": "resurfacing until October"},
         "geometry": [{"lat": 46.5122, "lon": 11.7594},
                      {"lat": 46.5130, "lon": 11.7610}]},
        {"type": "node", "id": 102, "lat": 46.5192, "lon": 12.0093,
         "tags": {"barrier": "gate", "access": "no", "seasonal": "yes",
                  "name": "Passo Falzarego winter gate"}},
    ]}


@app.get("/o/autobahn")
async def autobahn_roads():
    """The road index. The provider does not use it, but tools/verify_autobahn.py
    checks it, so the stub answers it too."""
    return {"roads": ["A22"]}


@app.get("/o/autobahn/{road}/services/{service}")
async def autobahn(road: str, service: str):
    """Germany's Autobahn API. Note `long`, not `lon` — as the real one does."""
    if service == "roadworks":
        return {"roadworks": [{
            "coordinate": {"lat": "46.5000", "long": "11.5000"},
            "title": f"{road} | Bolzano",
            "description": ["Right lane closed", "Until 30.10.2026"],
            "startTimestamp": "2026-08-01T06:00:00.000Z",
        }]}
    if service == "warning":
        return {"warning": [{
            "coordinate": {"lat": "46.4879", "long": "11.8129"},
            "title": f"{road} | Pordoi",
            "description": ["High wind warning"],
        }]}
    return {service: []}


@app.get("/route/v1/driving/{coords}")
async def osrm(coords: str):
    """OSRM. Two alternates: one straight and fast, one longer and twistier."""
    def line(offset: float, steps: int = 60, swing: float = 0.0) -> dict:
        return {"coordinates": [
            [11.3548 + (12.1357 - 11.3548) * i / steps + offset + swing * math.sin(i / 2),
             46.4983 + (46.5405 - 46.4983) * i / steps + swing * math.cos(i / 2)]
            for i in range(steps + 1)
        ]}

    return {"code": "Ok", "routes": [
        {"distance": 71000, "duration": 5400, "geometry": line(0.02)},
        {"distance": 88000, "duration": 7800, "geometry": line(-0.03, swing=0.02)},
    ]}
