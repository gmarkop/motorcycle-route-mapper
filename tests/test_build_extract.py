"""The extract build, run for real against a stand-in for Geofabrik.

These exist because of a silent failure that every other check passed: KEEP
gained the accommodation tags, the app and KEEP agreed, the unit tests were
green -- and the build reported "already filtered" for every country and merged
files produced by the *previous* tag set. It surfaced only on the running
server, as whole countries holding 17 hotels.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy" / "overpass" / "build-extract.sh"

pytestmark = pytest.mark.skipif(shutil.which("osmium") is None,
                                reason="needs osmium-tool")

SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
  <node id="1" lat="37.98" lon="23.72" version="1"><tag k="amenity" v="fuel"/></node>
  <node id="3" lat="37.97" lon="23.71" version="1"><tag k="tourism" v="hotel"/></node>
  <node id="5" lat="37.95" lon="23.69" version="1"><tag k="amenity" v="motorcycle_parking"/></node>
  <node id="7" lat="37.93" lon="23.67" version="1"><tag k="amenity" v="bench"/></node>
</osm>
"""


def _geofabrik(tmp_path: Path) -> Path:
    """A local stand-in the script can fetch from with file:// URLs."""
    root = tmp_path / "geofabrik" / "europe"
    root.mkdir(parents=True)
    (tmp_path / "sample.osm").write_text(SAMPLE)
    subprocess.run(["osmium", "cat", "--overwrite", "-o",
                    str(root / "greece-latest.osm.pbf"),
                    str(tmp_path / "sample.osm")], check=True,
                   capture_output=True)
    (root / "greece.poly").write_text(
        "greece\n1\n  23.5  37.8\n  23.9  37.8\n  23.9  38.1\n  23.5  38.1\nEND\nEND\n")
    return tmp_path / "geofabrik"


def _script(tmp_path: Path, geofabrik: Path, keep_tourism: str) -> Path:
    """The real script, pointed at the stand-in, with KEEP set for the test."""
    text = SCRIPT.read_text()
    text = text.replace('REGION_BASE="https://download.geofabrik.de"',
                        f'REGION_BASE="file://{geofabrik}"')
    text = text.replace("curl -fL -C - -# --retry 3 --retry-delay 5",
                        "curl -fL --retry 1")
    text = text.replace("  nwr/tourism=viewpoint,hotel,guest_house,camp_site,motel",
                        f"  nwr/tourism={keep_tourism}")
    out = tmp_path / f"build-{keep_tourism.replace(',', '-')}.sh"
    out.write_text(text)
    return out


def _tags(extract: Path) -> set[str]:
    dumped = subprocess.run(
        ["osmium", "tags-filter", str(extract), "nwr/tourism", "nwr/amenity",
         "-f", "osm"], check=True, capture_output=True, text=True).stdout
    import re
    return set(re.findall(r'v="([a-z_]+)"', dumped))


def test_changing_keep_refilters_a_country_already_filtered(tmp_path):
    """The regression. Same workdir, KEEP gains a tag, the tag must appear."""
    geofabrik = _geofabrik(tmp_path)
    work = tmp_path / "work"

    old = _script(tmp_path, geofabrik, "viewpoint")
    subprocess.run(["bash", str(old), "--only", "greece", str(work)],
                   check=True, capture_output=True, text=True)
    first = _tags(work / "touring-europe.osm.pbf")
    assert "hotel" not in first, "the old tag set did not ask for hotels"

    new = _script(tmp_path, geofabrik, "viewpoint,hotel")
    result = subprocess.run(["bash", str(new), "--only", "greece", str(work)],
                            check=True, capture_output=True, text=True)

    assert "re-filtering" in result.stdout, result.stdout
    assert "hotel" in _tags(work / "touring-europe.osm.pbf"), \
        "KEEP changed and the country was not re-filtered"


def test_an_unchanged_keep_still_reuses_the_filtered_country(tmp_path):
    """The reuse is the point of the cache, and must survive the fix."""
    geofabrik = _geofabrik(tmp_path)
    work = tmp_path / "work"
    script = _script(tmp_path, geofabrik, "viewpoint,hotel")

    subprocess.run(["bash", str(script), "--only", "greece", str(work)],
                   check=True, capture_output=True)
    again = subprocess.run(["bash", str(script), "--only", "greece", str(work)],
                           check=True, capture_output=True, text=True)

    assert "already filtered with this tag set" in again.stdout, again.stdout
