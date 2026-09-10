"""The diagnostics are code too, and pytest does not otherwise import them.

A signature change in the app once broke `check_services.py` and nothing
noticed: the suite never touches `tools/`, so the wrong call sat green through
several merges and surfaced as a TypeError on the owner's server, mid-setup.
These run each tool far enough to catch that, without any network.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"


def _import_tag_census():
    """Import the tool as a module, to test its logic rather than its output."""
    sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location(
        "tag_census", TOOLS / "tag_census.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=REPO,
                          capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("tool", [
    "check_services.py", "check_coverage.py", "verify_autobahn.py",
    "browser_test.py", "stub_apis.py", "tag_census.py",
    "check_extract.py",
])
def test_every_tool_at_least_imports(tool):
    """A syntax error or a bad import should not wait for a live server."""
    result = run("-c", f"import ast, pathlib; "
                       f"ast.parse(pathlib.Path('tools/{tool}').read_text())")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("tool", ["check_services.py", "check_coverage.py",
                                  "verify_autobahn.py", "browser_test.py",
                                  "tag_census.py", "check_extract.py"])
def test_every_tool_parses_its_arguments(tool):
    result = run(str(TOOLS / tool), "--help")
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_check_services_builds_its_queries_without_a_network():
    """The regression this file exists for.

    --dry-run runs everything up to the HTTP call: route parsing, the app's own
    coordinate thinning, chunking and query building. Calling into the app with
    the wrong arguments fails here rather than on a server three merges later.
    """
    result = run(str(TOOLS / "check_services.py"), "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Nothing was sent." in result.stdout
    assert "closures" in result.stdout and "points of interest" in result.stdout


def test_the_dry_run_reports_a_corridor_without_gaps():
    """The coverage invariant, checked through the tool the owner actually runs."""
    result = run(str(TOOLS / "check_services.py"), "--dry-run")

    gaps = [int(line.split(":")[1].strip().rstrip(" m"))
            for line in result.stdout.splitlines()
            if "widest gap between coordinates" in line]
    assert gaps, result.stdout
    # The closure corridor is 150 m, so its coordinates must be within 300 m.
    assert gaps[0] <= 300, f"closure coordinates {gaps[0]} m apart"


def test_check_coverage_reports_a_polygon_file(tmp_path):
    poly = tmp_path / "box.poly"
    poly.write_text("test\n1\n  22.0  37.0\n  24.0  37.0\n  24.0  39.0\n"
                    "  22.0  39.0\nEND\nEND\n")
    result = run(str(TOOLS / "check_coverage.py"), str(poly))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Athens" in result.stdout


def test_tag_census_builds_its_query_without_a_network():
    result = run(str(TOOLS / "tag_census.py"), "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Nothing was sent." in result.stdout


def test_every_candidate_tag_is_actually_queried():
    """The grouping must not lose a candidate on its way into the query.

    Candidates sharing a key collapse into one regex to keep the query small,
    and that is the step that can quietly drop one -- a candidate queried by
    nobody counts zero, and zero is a legitimate answer here ("nobody maps
    this"), so the bug would read as a result rather than a failure. Verified
    by mutation: keeping only the first value per key fails this.
    """
    census = _import_tag_census()
    # Take each selector apart rather than substring-matching it: "motorcycle"
    # is a substring of "motorcycle_repair", so a dropped candidate would
    # otherwise pass on its neighbour's name.
    queried: dict[str, set[str] | None] = {}
    for selector in census.selectors():
        if '"~"^(' in selector:                     # ["key"~"^(a|b)$"]
            key, alternation = selector.split('"~"^(')
            queried[key.strip('["')] = set(alternation.rstrip(')$"]').split("|"))
        else:                                       # ["key"]
            queried[selector.strip('["]')] = None

    for label, key, value in census.CANDIDATES:
        assert key in queried, f"{label}: key {key} never queried"
        values = queried[key]
        assert values is None or value in values, \
            f"{label}: {value} missing from the {key} selector"


def test_the_census_counts_and_cross_tabs(capsys):
    """The classification, which is the part that decides the feature."""
    census = _import_tag_census()
    elements = [
        {"tags": {"amenity": "fuel", "name": "EKO"}},
        {"tags": {"amenity": "fuel", "name": "Shell"}},
        {"tags": {"amenity": "cafe", "name": "Plain Cafe"}},
        {"tags": {"amenity": "cafe", "name": "Biker Stop",
                  "motorcycle_friendly": "yes"}},
        {"tags": {"shop": "motorcycle", "name": "Moto Shop"}},
        {"tags": {"tourism": "hotel", "name": "Hotel", "motorcycle:theme": "yes"}},
    ]

    route = SimpleNamespace(name="Test", distance_m=100_000.0)
    census.report(elements, route, 1000.0, partial=False)
    out = capsys.readouterr().out

    def counted(label: str) -> int:
        line = next(l for l in out.splitlines() if l.strip().startswith(label))
        return int(line.split()[-2])

    assert counted("fuel (baseline)") == 2
    assert counted("cafe (baseline)") == 2
    assert counted("motorcycle shop") == 1
    assert counted("viewpoint (baseline)") == 0
    assert counted("motorcycle_friendly") == 1
    # Only the two motorcycle-tagged stops reach the cross-tab, by name.
    assert "Biker Stop" in out and "Hotel" in out
    assert "Plain Cafe" not in out and "EKO" not in out


def test_the_census_says_when_the_cap_truncated_it(capsys):
    """A truncated union understates every count at once, silently."""
    census = _import_tag_census()
    elements = [{"tags": {"amenity": "fuel"}}] * census.CAP

    census.report(elements, SimpleNamespace(name="T", distance_m=100_000.0),
                  1000.0, partial=False)

    assert "CAP REACHED" in capsys.readouterr().out


def test_the_census_flags_features_it_could_not_classify(capsys):
    """The query and the classifier come from one list, so this should be empty.

    A feature matching no candidate means a selector is wider than the
    candidate it was written for -- the counts would then not add up to the
    features returned, which is the check that validated the Athens-Volos run.
    """
    census = _import_tag_census()
    elements = [{"tags": {"amenity": "fuel"}},
                {"tags": {"leisure": "pitch", "name": "Not Asked For"}}]

    census.report(elements, SimpleNamespace(name="T", distance_m=100_000.0),
                  1000.0, partial=False)
    out = capsys.readouterr().out

    assert "1 feature(s) matched no candidate" in out
    assert "Not Asked For" in out


def test_a_clean_census_says_nothing_about_classification(capsys):
    census = _import_tag_census()

    census.report([{"tags": {"amenity": "fuel"}}],
                  SimpleNamespace(name="T", distance_m=100_000.0),
                  1000.0, partial=False)

    assert "matched no candidate" not in capsys.readouterr().out


def test_the_census_is_not_given_local_timeouts_for_a_public_server(monkeypatch):
    """The Dolomites regression.

    On a box with a local extract the environment sets the coverage, and
    `_is_own_server` then only has to see the primary endpoint equal
    `overpass_url` -- which a one-endpoint Settings satisfies by construction.
    The census was handed the 120 s local deadline for a public server.
    """
    monkeypatch.setenv("MOTO_OVERPASS_COVERAGE", "34.8,6.3,47.2,29.6")
    census = _import_tag_census()
    configured = census.Settings()
    assert configured.overpass_coverage, "the box's environment, reproduced"

    single = census.public_settings("https://overpass.kumi.systems/api/interpreter",
                                    configured)
    endpoints = single.overpass_endpoints

    assert single.deadline_for(endpoints) == 900.0
    assert single.concurrency_for(endpoints) == configured.overpass_concurrency
    # A client deadline buys nothing if the header tells Overpass to stop first.
    assert single.overpass_timeout_s >= 300


def test_keeping_the_coverage_would_reproduce_the_bug(monkeypatch):
    """Why public_settings clears the coverage, rather than only the fallbacks."""
    monkeypatch.setenv("MOTO_OVERPASS_COVERAGE", "34.8,6.3,47.2,29.6")
    census = _import_tag_census()

    naive = census.Settings(overpass_url="https://overpass.kumi.systems/api/interpreter",
                            overpass_fallback_urls=[])

    assert naive.deadline_for(naive.overpass_endpoints) == naive.overpass_deadline_s


def test_smaller_chunks_make_more_and_cheaper_queries():
    """The lever for a region too dense to answer in one query."""
    wide = run(str(TOOLS / "tag_census.py"), "--dry-run")
    narrow = run(str(TOOLS / "tag_census.py"), "--dry-run", "--max-points", "20")

    assert wide.returncode == 0 and narrow.returncode == 0, narrow.stderr

    def chunks(out: str) -> int:
        line = next(l for l in out.splitlines() if "chunk(s) of at most" in l)
        return int(line.split("->")[1].split()[0])

    def kilobytes(out: str) -> float:
        line = next(l for l in out.splitlines() if "query size:" in l)
        return float(line.split(":")[1].split()[0])

    assert chunks(narrow.stdout) > chunks(wide.stdout)
    assert kilobytes(narrow.stdout) < kilobytes(wide.stdout)


def test_the_chunk_size_reaches_the_query_not_just_the_plan():
    """--max-points must change what is sent, not only what is printed."""
    census = _import_tag_census()
    single = census.public_settings("https://overpass.kumi.systems/api/interpreter",
                                    census.Settings(), 20)

    assert single.overpass_max_points == 20


def test_the_extract_check_covers_every_tag_the_app_queries():
    """It must not drift from what the app actually asks its server for."""
    sys.path.insert(0, str(TOOLS))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "check_extract", TOOLS / "check_extract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    from moto_route.services.pois import CATEGORY_TAGS

    checked = {(key, value) for _, key, value in module.wanted()}
    for key, values in CATEGORY_TAGS.values():
        for value in values:
            assert (key, value) in checked, f"{key}={value} is never verified"


def test_the_extract_check_refuses_a_public_server():
    """A public server holds every tag, so checking one proves nothing."""
    result = run(str(TOOLS / "check_extract.py"),
                 "--url", "https://overpass.kumi.systems/api/interpreter")

    assert result.returncode == 2
    assert "not your own server" in result.stderr


def _import_check_extract():
    sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location(
        "check_extract", TOOLS / "check_extract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("total,peak,expected", [
    # The counts the owner's server actually returned after the failed rebuild.
    (17, 17_839, True),        # hotels, against viewpoints on the same key
    (23, 17_839, True),        # guest houses
    (2, 51_931, True),         # motorcycle parking, against cafes
    (29_035, 51_931, False),   # fuel, healthy
    (17_839, 51_931, False),   # viewpoints, healthy
    # The closest real data comes to the cutoff: construction is 1/58 of
    # barrier across Greece and Italy, and must not be flagged. That gap
    # is why the threshold is 1/100 and not 1/10.
    (10_280, 593_512, False),
])
def test_a_tag_too_rare_to_have_been_filtered_for_is_caught(total, peak, expected):
    check = _import_check_extract()
    assert check.implausible(total, peak, floor=100) is expected
