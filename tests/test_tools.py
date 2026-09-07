"""The diagnostics are code too, and pytest does not otherwise import them.

A signature change in the app once broke `check_services.py` and nothing
noticed: the suite never touches `tools/`, so the wrong call sat green through
several merges and surfaced as a TypeError on the owner's server, mid-setup.
These run each tool far enough to catch that, without any network.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=REPO,
                          capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("tool", [
    "check_services.py", "check_coverage.py", "verify_autobahn.py",
    "browser_test.py", "stub_apis.py",
])
def test_every_tool_at_least_imports(tool):
    """A syntax error or a bad import should not wait for a live server."""
    result = run("-c", f"import ast, pathlib; "
                       f"ast.parse(pathlib.Path('tools/{tool}').read_text())")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("tool", ["check_services.py", "check_coverage.py",
                                  "verify_autobahn.py", "browser_test.py"])
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
