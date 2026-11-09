"""The mission card's rules, run in the real module rather than read as text.

`mission.js` is pure -- payloads in, rows and markup out -- so it can be run
under Node exactly as the browser runs it. The rest of the frontend can only
be checked by reading its source (see test_frontend.py); this part can be
checked by what it does, which is the stronger test.

Node is not a dependency of the app, so without it these skip rather than
fail. The two modules are copied next to a ``package.json`` that says they are
ES modules, which works on any Node from 14 up without relying on newer
versions guessing the module type from the syntax.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from moto_route.services import weather

REPO = Path(__file__).resolve().parent.parent
JS = REPO / "moto_route" / "static" / "js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="needs Node to run mission.js")

DEPARTURE = "2026-10-23T06:00:00.000Z"


@pytest.fixture(scope="module")
def module_dir(tmp_path_factory) -> Path:
    where = tmp_path_factory.mktemp("mission")
    for name in ("mission.js", "format.js"):
        shutil.copy(JS / name, where / name)
    (where / "package.json").write_text('{"type": "module"}')
    return where


def run(module_dir: Path, body: str, data=None):
    """Run ``body`` with the module imported as ``m`` and ``data`` as the input.

    The body ends by assigning ``out``, which comes back as JSON.
    """
    script = ("import * as m from './mission.js';\n"
              f"const data = {json.dumps(data)};\n"
              f"let out;\n{body}\n"
              "process.stdout.write(JSON.stringify(out));\n")
    (module_dir / "case.mjs").write_text(script)
    done = subprocess.run([NODE, "case.mjs"], cwd=module_dir, capture_output=True,
                          text=True, timeout=30, env={"TZ": "UTC", "PATH": ""})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def rows_for(module_dir, **payloads):
    args = {"weather": None, "hazards": None, "curviness": None,
            "departure": DEPARTURE, "speedKmh": 60, **payloads}
    return run(module_dir, "out = m.missionRows(data);", args)


def point(km, warnings=(), rideability=90, **extra):
    return {"distance_m": km * 1000, "eta": f"2026-10-23T{6 + km // 60:02d}:00:00Z",
            "warnings": list(warnings), "rideability": rideability, **extra}


WEATHER_OK = {"available": True, "points": []}
HAZARDS_OK = {"available": True, "hazards": []}
CURVINESS_OK = {"elevation": {"available": True}, "demanding": []}


# ------------------------------------------------- the weather service's words

def _every_warning_the_weather_service_writes() -> dict[str, str]:
    """One example of each warning, keyed by what it should be on the card."""
    def warnings(**conditions):
        base = dict(temperature_c=15.0, precipitation_mm=0.0, precipitation_probability=0,
                    gust_kmh=10.0, visibility_m=20000.0, weather_code=0)
        return weather.rideability(**{**base, **conditions})[1]

    wx = (warnings(temperature_c=1.0, precipitation_mm=1.0)              # black ice
          + warnings(temperature_c=2.0)                                  # near freezing
          + warnings(weather_code=71)                                    # snow
          + warnings(weather_code=95)                                    # thunder
          + warnings(visibility_m=500.0)                                 # fog
          + warnings(precipitation_mm=3.0)                               # heavy rain
          + warnings(precipitation_mm=1.0)                               # rain
          + warnings(precipitation_probability=80))                      # chance of rain
    wind = (warnings(gust_kmh=60.0, wind_relative="crosswind from the left",
                     crosswind_kmh=50.0)
            + warnings(gust_kmh=60.0, wind_relative="headwind")
            + warnings(gust_kmh=60.0)
            + warnings(gust_kmh=48.0, wind_relative="crosswind from the right",
                       crosswind_kmh=40.0)
            + warnings(gust_kmh=48.0))
    comfort = warnings(temperature_c=7.0) + warnings(temperature_c=36.0)

    expected = {w: "wx" for w in wx}
    expected.update({w: "wind" for w in wind})
    expected.update({w: None for w in comfort})
    return expected


def test_every_warning_the_weather_service_writes_lands_in_the_right_place(module_dir):
    """The card sorts warnings by their wording, so the wording is a contract.

    Run every warning `rideability` can produce through the card's own
    classifier. Reword one in weather.py and this fails, rather than the
    warning quietly falling off the card -- or a cold morning turning up as
    "bad weather".
    """
    expected = _every_warning_the_weather_service_writes()
    assert len(expected) >= 13, "each case should produce its own warning"

    got = run(module_dir, "out = data.map((w) => m.warningKind(w));", list(expected))

    assert dict(zip(expected, got)) == expected


# ----------------------------------------------------------------- the rows

def test_only_the_four_kinds_reach_the_card_in_route_order(module_dir):
    weather_payload = {"available": True, "points": [
        point(10, ["Cold (7 °C) — heated grips weather"]),
        point(40, ["Thunderstorms forecast"], rideability=40),
        point(70, ["Strong gusts (60 km/h)"], gust_kmh=60, push_deg=90),
    ]}
    hazards = {"available": True, "hazards": [
        {"label": "Closed gate", "severity": "closed", "detail": "",
         "distance_along_route_m": 55_000}]}
    curviness = {"elevation": {"available": True}, "demanding": [
        {"from_m": 20_000, "to_m": 21_500, "gradient_pct": -9.0, "descending": True,
         "curviness": 200}]}

    result = rows_for(module_dir, weather=weather_payload, hazards=hazards,
                      curviness=curviness)

    assert [(r["kind"], r["from_m"]) for r in result["rows"]] == [
        ("ts", 20_000), ("wx", 40_000), ("road", 55_000), ("wind", 70_000)]
    assert result["unchecked"] == []
    assert "Cold" not in json.dumps(result)


def test_a_shut_road_is_listed_before_anything_else_at_the_same_km(module_dir):
    """Nothing after a closure on that kilometre applies, so it reads first."""
    result = rows_for(
        module_dir,
        weather={"available": True, "points": [point(30, ["Heavy rain (3.0 mm/h)"])]},
        hazards={"available": True, "hazards": [
            {"label": "Access forbidden", "severity": "closed", "detail": "",
             "distance_along_route_m": 30_000}]},
        curviness=CURVINESS_OK)

    assert [r["kind"] for r in result["rows"]] == ["road", "wx"]


def test_a_wet_stretch_is_one_row_spoken_for_by_its_worst_point(module_dir):
    points = [point(25, ["Rain (0.8 mm/h)"], rideability=70),
              point(50, ["Heavy rain (3.2 mm/h)"], rideability=45),
              point(75, ["Rain (1.0 mm/h)"], rideability=68),
              point(100),                                           # dry
              point(125, ["Rain (0.6 mm/h)"], rideability=72)]

    rows = rows_for(module_dir, weather={"available": True, "points": points},
                    hazards=HAZARDS_OK, curviness=CURVINESS_OK)["rows"]

    assert [(r["from_m"], r["to_m"]) for r in rows] == [(25_000, 75_000), (125_000, 125_000)]
    assert "Heavy rain (3.2 mm/h)" in rows[0]["html"]
    assert "worst of 3" in rows[0]["html"]
    assert "worst of" not in rows[1]["html"]


def test_wind_runs_are_kept_apart_from_weather_and_keep_their_direction(module_dir):
    """Rain and a crosswind at the same place are two things to know."""
    gusty = "Strong gusts (62 km/h) — crosswind from the left, 55 km/h across"
    points = [point(30, ["Rain (1.0 mm/h)", "Gusty (47 km/h)"], gust_kmh=47, push_deg=80),
              point(60, ["Rain (1.0 mm/h)", gusty], gust_kmh=62, push_deg=95)]

    rows = rows_for(module_dir, weather={"available": True, "points": points},
                    hazards=HAZARDS_OK, curviness=CURVINESS_OK)["rows"]
    wind = [r for r in rows if r["kind"] == "wind"]

    assert len(rows) == 2 and len(wind) == 1
    assert "crosswind from the left" in wind[0]["html"]          # the strongest gust
    assert "rotate(95deg)" in wind[0]["html"]                    # and its arrow


def test_close_twisty_and_steep_stretches_become_one_row(module_dir):
    stretches = [
        {"from_m": 10_000, "to_m": 10_600, "gradient_pct": 7.0, "descending": False},
        {"from_m": 11_500, "to_m": 12_200, "gradient_pct": -10.5, "descending": True},
        {"from_m": 13_000, "to_m": 13_400, "gradient_pct": 6.0, "descending": False},
        {"from_m": 30_000, "to_m": 33_200, "gradient_pct": -8.75, "descending": True},
    ]
    rows = rows_for(module_dir, weather=WEATHER_OK, hazards=HAZARDS_OK,
                    curviness={"elevation": {"available": True},
                               "demanding": stretches})["rows"]

    assert [(r["from_m"], r["to_m"]) for r in rows] == [(10_000, 13_400), (30_000, 33_200)]
    # 10.5% is 6 degrees, the way the GPX marker says it.
    assert rows[0]["html"] == "3 stretches over 3.4 km, steepest down 6°"
    assert rows[1]["html"] == "3.2 km, down 5°"


def test_a_closure_mapped_in_pieces_is_one_row_but_different_ones_are_not(module_dir):
    hazards = [
        {"label": "Under construction (road)", "severity": "closed", "detail": "",
         "distance_along_route_m": m}
        for m in (40_000, 40_800, 41_900)
    ] + [{"label": "Lift gate", "severity": "restricted", "detail": "name: Passo Giau",
          "distance_along_route_m": 41_000}]

    rows = rows_for(module_dir, weather=WEATHER_OK, curviness=CURVINESS_OK,
                    hazards={"available": True, "hazards": hazards})["rows"]

    assert [(r["from_m"], r["to_m"]) for r in rows] == [(40_000, 41_900), (41_000, 41_000)]
    assert "×3" in rows[0]["html"] and "<strong>" in rows[0]["html"]
    assert "Passo Giau" in rows[1]["html"] and "<strong>" not in rows[1]["html"]


def test_times_follow_the_plan_with_no_stops(module_dir):
    """60 km/h from 06:00: the closure at km 90 is reached at 07:30."""
    rows = rows_for(module_dir, weather=WEATHER_OK, curviness=CURVINESS_OK,
                    hazards={"available": True, "hazards": [
                        {"label": "Closed gate", "severity": "closed", "detail": "",
                         "distance_along_route_m": 90_000}]})["rows"]

    assert rows[0]["eta"] == "2026-10-23T07:30:00.000Z"


# ------------------------------------------------- what was not checked

def test_a_category_nobody_checked_is_said_out_loud(module_dir):
    """Silence about the weather must not read as a dry day."""
    result = rows_for(
        module_dir,
        weather={"available": False,
                 "reason": "Beyond the 16-day forecast horizon. Departure is 40 days away."},
        hazards=None,
        curviness={"elevation": {"available": False, "reason": "No heights."}})

    assert result["rows"] == []
    text = " ".join(result["unchecked"])
    assert "16-day forecast horizon" in text
    assert "Closures not loaded" in text
    assert "No heights." in text


def test_a_clean_check_says_nothing_is_unchecked(module_dir):
    result = rows_for(module_dir, weather=WEATHER_OK, hazards=HAZARDS_OK,
                      curviness=CURVINESS_OK)
    assert result == {"rows": [], "unchecked": []}


# ----------------------------------------------------------------- the card

def test_the_card_has_the_f16_on_the_left_and_the_f5_on_the_right(module_dir):
    html = run(module_dir, "out = m.missionCard(data);", {
        "name": "Bolzano <to> Cortina", "departure": DEPARTURE, "speedKmh": 60,
        "distance": "76.6 km", "rows": [], "unchecked": []})

    assert html.index('aria-label="F-16') < html.index("<h1>") < html.index('aria-label="F-5')
    assert "Bolzano &lt;to&gt; Cortina" in html
    assert "Nothing to report" in html


def test_an_empty_card_with_gaps_does_not_claim_an_all_clear(module_dir):
    html = run(module_dir, "out = m.missionCard(data);", {
        "name": "R", "departure": DEPARTURE, "speedKmh": 60, "distance": "",
        "rows": [], "unchecked": ["Weather and wind not checked."]})

    assert "Nothing to report" not in html
    assert "Nothing found in what was checked" in html
    assert "Weather and wind not checked." in html


def test_the_card_tells_time_on_a_24_hour_clock(module_dir):
    assert run(module_dir, "out = m.clock(data);", "2026-10-23T18:23:00Z") == "18:23"


def test_the_silhouettes_are_symmetric(module_dir):
    """Drawn as a right half and mirrored, so the two wings cannot disagree."""
    xs = run(module_dir, """
        const path = m.mirroredPath([[35, 0], [50, 40], [40, 100], [35, 100]]);
        out = path.slice(1, -1).split('L').map((p) => Number(p.split(',')[0]));
    """)
    assert sorted(x - 35 for x in xs) == sorted(35 - x for x in xs)


def test_the_card_lists_its_rows_with_km_time_and_kind(module_dir):
    """A range of km where a row spans one, a single km where it does not."""
    html = run(module_dir, "out = m.missionCard(data);", {
        "name": "R", "departure": DEPARTURE, "speedKmh": 60, "distance": "", "unchecked": [],
        "rows": [
            {"kind": "wx", "from_m": 25_000, "to_m": 77_000,
             "eta": "2026-10-23T06:25:00Z", "html": "Heavy rain (4.0 mm/h)"},
            {"kind": "road", "from_m": 62_000, "to_m": 62_000,
             "eta": "2026-10-23T07:02:00Z", "html": "<strong>Closed gate</strong>"},
        ]})

    assert "Nothing" not in html
    assert '<td class="mc-km">25–77</td><td class="mc-time">06:25</td>' in html
    assert '<td class="mc-km">62</td><td class="mc-time">07:02</td>' in html
    assert '<b class="mc-tag">WX</b> Heavy rain' in html
    assert '<b class="mc-tag">ROAD</b> <strong>Closed gate</strong>' in html
