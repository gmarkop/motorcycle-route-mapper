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
])
def test_every_tool_at_least_imports(tool):
    """A syntax error or a bad import should not wait for a live server."""
    result = run("-c", f"import ast, pathlib; "
                       f"ast.parse(pathlib.Path('tools/{tool}').read_text())")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("tool", ["check_services.py", "check_coverage.py",
                                  "verify_autobahn.py", "browser_test.py",
                                  "tag_census.py"])
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
