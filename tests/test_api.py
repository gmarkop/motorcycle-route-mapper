"""End-to-end API tests.

These run the real FastAPI app with ``offline=True``, so the live layers report
themselves unavailable instead of reaching for the network. That is exactly the
behaviour a rider gets in a valley with no signal, which makes it worth testing
directly rather than mocking away.
"""

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from moto_route.api import create_app
from moto_route.config import Settings


@pytest.fixture
def client(tmp_path):
    settings = Settings(cache_dir=tmp_path, offline=True, max_upload_bytes=1024 * 1024)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def uploaded(client, read_fixture):
    response = client.post(
        "/api/routes",
        files={"file": ("garmin_route.gpx", read_fixture("garmin_route.gpx"), "application/gpx+xml")},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------- basics

def test_health_reports_the_supported_formats(client):
    payload = client.get("/api/health").json()

    assert payload["status"] == "ok"
    assert set(payload["supported_formats"]) == {".gpx", ".kml", ".kmz"}
    assert payload["offline"] is True


def test_the_index_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Route Mapper" in response.text


# -------------------------------------------------------------------- uploads

def test_uploading_a_gpx_returns_a_drawable_route(uploaded):
    route = uploaded["route"]

    assert uploaded["id"]
    assert route["name"] == "Stelvio Loop"
    assert route["source_format"] == "gpx"
    assert route["lines"] and len(route["lines"][0]) >= 2
    assert route["stats"]["distance_m"] > 0
    assert len(route["bounds"]) == 4


def test_waypoint_kinds_survive_the_round_trip(uploaded):
    kinds = {w["kind"] for w in uploaded["route"]["waypoints"]}
    assert {"via", "shaping", "waypoint"} <= kinds


def test_uploading_a_kmz_works(client, read_fixture):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("doc.kml", read_fixture("google_earth.kml"))

    response = client.post(
        "/api/routes",
        files={"file": ("ride.kmz", buffer.getvalue(), "application/vnd.google-earth.kmz")},
    )

    assert response.status_code == 200
    assert response.json()["route"]["source_format"] == "kmz"


def test_a_bad_file_returns_a_readable_message(client):
    response = client.post(
        "/api/routes", files={"file": ("notes.txt", b"not a route at all", "text/plain")}
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "GPX" in detail and "KML" in detail


def test_an_oversized_file_is_refused(client):
    huge = b"<gpx>" + b"x" * (2 * 1024 * 1024)
    response = client.post("/api/routes", files={"file": ("big.gpx", huge, "application/gpx+xml")})

    assert response.status_code == 413
    assert "limit" in response.json()["detail"]


def test_an_xml_bomb_is_refused_with_a_400(client):
    bomb = b"""<?xml version="1.0"?>
    <!DOCTYPE gpx [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>
    <gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><name>&b;</name></trk></gpx>"""
    response = client.post("/api/routes", files={"file": ("bomb.gpx", bomb, "application/gpx+xml")})

    assert response.status_code == 400
    assert "entities" in response.json()["detail"]


# ------------------------------------------------------------ enrichment paths

def test_an_unknown_route_id_returns_404(client):
    response = client.get("/api/routes/deadbeef/weather")

    assert response.status_code == 404
    assert "upload the file again" in response.json()["detail"]


@pytest.mark.parametrize("layer", ["weather", "hazards", "alternates"])
def test_live_layers_degrade_cleanly_when_offline(client, uploaded, layer):
    payload = client.get(f"/api/routes/{uploaded['id']}/{layer}").json()

    assert payload["available"] is False
    assert "Offline" in payload["reason"]


def test_a_bad_departure_time_is_rejected(client, uploaded):
    response = client.get(f"/api/routes/{uploaded['id']}/weather?departure=next+tuesday")

    assert response.status_code == 400
    assert "departure" in response.json()["detail"].lower()


def test_an_out_of_range_speed_is_rejected(client, uploaded):
    assert client.get(f"/api/routes/{uploaded['id']}/weather?speed_kmh=500").status_code == 422


# ------------------------------------------------------------------- elevation

def test_elevation_profile_is_served_when_the_file_has_altitudes(client, read_fixture):
    upload = client.post(
        "/api/routes", files={"file": ("track.gpx", read_fixture("track.gpx"), "application/gpx+xml")}
    ).json()

    payload = client.get(f"/api/routes/{upload['id']}/elevation").json()

    assert payload["available"] is True
    assert len(payload["samples"]) == 6
    assert payload["samples"][0] == {"distance_m": 0, "ele": 500.0}
    distances = [s["distance_m"] for s in payload["samples"]]
    assert distances == sorted(distances)


def test_elevation_reports_unavailable_without_altitude_data(client):
    flat = b"""<?xml version="1.0"?>
    <gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1"><trk><trkseg>
      <trkpt lat="48.0" lon="11.0"/><trkpt lat="48.1" lon="11.1"/>
    </trkseg></trk></gpx>"""
    upload = client.post("/api/routes", files={"file": ("flat.gpx", flat, "application/gpx+xml")}).json()

    payload = client.get(f"/api/routes/{upload['id']}/elevation").json()

    assert payload["available"] is False
    assert "no elevation" in payload["reason"].lower()


# ----------------------------------------------------------------- route store

def test_the_oldest_route_is_evicted_once_the_store_is_full(client, read_fixture):
    from moto_route.api import MAX_ROUTES_IN_MEMORY

    data = read_fixture("track.gpx")
    first = client.post("/api/routes", files={"file": ("a.gpx", data, "application/gpx+xml")}).json()

    for _ in range(MAX_ROUTES_IN_MEMORY):
        client.post("/api/routes", files={"file": ("b.gpx", data, "application/gpx+xml")})

    assert client.get(f"/api/routes/{first['id']}/elevation").status_code == 404


# ------------------------------------------------------- touring feature paths

@pytest.mark.parametrize("layer", ["pois", "incidents"])
def test_new_live_layers_degrade_cleanly_when_offline(client, uploaded, layer):
    payload = client.get(f"/api/routes/{uploaded['id']}/{layer}").json()

    assert payload["available"] is False
    assert "Offline" in payload["reason"]


def test_health_publishes_the_defaults_the_frontend_needs(client):
    defaults = client.get("/api/health").json()["defaults"]

    assert defaults["speed_kmh"] > 0
    assert defaults["tank_range_km"] > 0
    # OSM's tile policy names 250 tiles as the limit for an area.
    assert defaults["max_cached_tiles"] <= 250
    assert defaults["tile_prefetch_delay_ms"] > 0


def test_curviness_endpoint_returns_positioned_samples(client, read_fixture):
    upload = client.post(
        "/api/routes",
        files={"file": ("track.gpx", read_fixture("track.gpx"), "application/gpx+xml")},
    ).json()

    payload = client.get(f"/api/routes/{upload['id']}/curviness").json()

    if payload["available"]:
        assert payload["samples"]
        first = payload["samples"][0]
        assert {"lat", "lon", "distance_m", "curviness"} <= set(first)
        distances = [s["distance_m"] for s in payload["samples"]]
        assert distances == sorted(distances)


def test_curviness_window_is_validated(client, uploaded):
    assert client.get(f"/api/routes/{uploaded['id']}/curviness?window_m=5").status_code == 422
    assert client.get(f"/api/routes/{uploaded['id']}/curviness?window_m=600").status_code == 200


def test_a_short_route_reports_curviness_unavailable(client):
    stub = b"""<?xml version="1.0"?>
    <gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1"><trk><trkseg>
      <trkpt lat="48.0" lon="11.0"/><trkpt lat="48.0001" lon="11.0"/>
    </trkseg></trk></gpx>"""
    upload = client.post(
        "/api/routes", files={"file": ("tiny.gpx", stub, "application/gpx+xml")}).json()

    payload = client.get(f"/api/routes/{upload['id']}/curviness").json()
    assert payload["available"] is False


def test_export_returns_a_downloadable_gpx(client, uploaded):
    response = client.get(f"/api/routes/{uploaded['id']}/export.gpx")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/gpx+xml")
    assert "attachment" in response.headers["content-disposition"]
    assert ".gpx" in response.headers["content-disposition"]
    assert response.content.startswith(b'<?xml')


def test_exported_file_can_be_re_uploaded(client, uploaded):
    """The round trip has to survive the HTTP layer too, not just the writer."""
    exported = client.get(f"/api/routes/{uploaded['id']}/export.gpx").content

    again = client.post(
        "/api/routes",
        files={"file": ("enriched.gpx", exported, "application/gpx+xml")},
    )

    assert again.status_code == 200
    assert again.json()["route"]["stats"]["point_count"] > 0


def test_export_works_offline_with_every_live_layer_down(client, uploaded):
    """Offline mode means no enrichment, but the file must still come out."""
    response = client.get(f"/api/routes/{uploaded['id']}/export.gpx")

    assert response.status_code == 200
    assert b"<trk>" in response.content


def test_export_of_an_unknown_route_is_404(client):
    assert client.get("/api/routes/deadbeef/export.gpx").status_code == 404


def test_tank_range_is_range_checked(client, uploaded):
    assert client.get(f"/api/routes/{uploaded['id']}/pois?tank_range_km=5000").status_code == 422
    assert client.get(f"/api/routes/{uploaded['id']}/pois?tank_range_km=200").status_code == 200


def test_the_service_worker_and_modules_are_served(client):
    for path in ["/static/sw.js", "/static/js/app.js", "/static/js/tiles.js",
                 "/static/js/format.js", "/static/js/mapview.js", "/static/js/panels.js"]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.content, path
