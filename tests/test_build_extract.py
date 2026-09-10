"""The extract build, run for real against a stand-in for Geofabrik.

These exist because of a silent failure that every other check passed: KEEP
gained the accommodation tags, the app and KEEP agreed, the unit tests were
green -- and the build reported "already filtered" for every country and merged
files produced by the *previous* tag set. It surfaced only on the running
server, as whole countries holding 17 hotels.
"""

import hashlib
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
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


def _sample(tmp_path: Path, extra: int = 0) -> Path:
    """The sample OSM XML, optionally with more nodes so the file grows."""
    nodes = "".join(
        f'<node id="{1000 + i}" lat="37.{500 + i}" lon="23.6" version="1">'
        f'<tag k="tourism" v="hotel"/></node>\n' for i in range(extra))
    path = tmp_path / f"sample-{extra}.osm"
    path.write_text(SAMPLE.replace("</osm>", nodes + "</osm>"))
    return path


def _http_script(tmp_path: Path, port: int) -> Path:
    """The real script against a Range-serving stand-in, resume left intact."""
    text = SCRIPT.read_text()
    text = text.replace('REGION_BASE="https://download.geofabrik.de"',
                        f'REGION_BASE="http://127.0.0.1:{port}"')
    text = text.replace("  nwr/tourism=viewpoint,hotel,guest_house,camp_site,motel",
                        "  nwr/tourism=viewpoint,hotel")
    out = tmp_path / "build-http.sh"
    out.write_text(text)
    return out


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
    _publish_md5(root / "greece-latest.osm.pbf")
    return tmp_path / "geofabrik"


def _publish_md5(pbf: Path) -> None:
    """Geofabrik publishes one of these beside every extract."""
    digest = hashlib.md5(pbf.read_bytes()).hexdigest()
    pbf.with_suffix(".pbf.md5").write_text(f"{digest}  {pbf.name}\n")


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


def test_a_damaged_download_is_replaced_rather_than_filtered(tmp_path):
    """The failure that reached the owner as "invalid BlobHeader size".

    A complete extract from an earlier build, then `curl -C -` resuming against
    a newer, larger file on the server: the old head keeps the new tail spliced
    onto it. Right size, unreadable. Here the damage is simulated directly --
    what matters is that a raw file which does not match Geofabrik's checksum
    is downloaded again instead of being handed to osmium.
    """
    geofabrik = _geofabrik(tmp_path)
    work = tmp_path / "work"
    script = _script(tmp_path, geofabrik, "viewpoint,hotel")

    subprocess.run(["bash", str(script), "--only", "greece", str(work)],
                   check=True, capture_output=True)

    raw = work / "raw" / "greece.osm.pbf"
    good = raw.read_bytes()
    raw.write_bytes(good[:len(good) // 2] + b"\xff" * 400)   # spliced, wrong
    shutil.rmtree(work / "filtered")

    result = subprocess.run(["bash", str(script), "--only", "greece", str(work)],
                            check=True, capture_output=True, text=True)

    assert "stale or damaged" in result.stdout, result.stdout
    assert raw.read_bytes() == good, "the damaged file was not replaced"
    assert "hotel" in _tags(work / "touring-europe.osm.pbf")


def test_an_intact_download_is_not_fetched_again(tmp_path):
    """Re-downloading 4 GB per build is the cost this avoids."""
    geofabrik = _geofabrik(tmp_path)
    work = tmp_path / "work"
    script = _script(tmp_path, geofabrik, "viewpoint,hotel")

    subprocess.run(["bash", str(script), "--only", "greece", str(work)],
                   check=True, capture_output=True)
    again = subprocess.run(["bash", str(script), "--only", "greece", str(work)],
                           check=True, capture_output=True, text=True)

    assert "already downloaded, checksum matches" in again.stdout, again.stdout


# ------------------------------------------- the resume splice, for real

class _RangeServer(threading.Thread):
    """Serves one file, honouring Range — as Geofabrik does.

    The corruption needs a server that answers a resume, so a file:// stand-in
    cannot show it. What the owner hit was: a complete extract from an earlier
    build, Geofabrik regenerating a larger one overnight, and `curl -C -`
    asking for "bytes N onward" of the new file and appending them to the old
    file's first N bytes. The result is the right size and unreadable.
    """

    def __init__(self, path: Path):
        super().__init__(daemon=True)
        self.path = path            # the extract, re-read on every request

        class H(BaseHTTPRequestHandler):
            def do_GET(handler):
                # Geofabrik publishes the checksum and the clipping polygon
                # beside the extract, and the script fetches all three.
                if handler.path.endswith(".md5"):
                    digest = hashlib.md5(self.path.read_bytes()).hexdigest()
                    return handler._send(
                        f"{digest}  greece-latest.osm.pbf\n".encode())
                if handler.path.endswith(".poly"):
                    return handler._send(
                        b"greece\n1\n  23.5  37.8\n  23.9  37.8\n"
                        b"  23.9  38.1\n  23.5  38.1\nEND\nEND\n")

                served = self.path.read_bytes()
                rng = handler.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    start = int(rng.split("=")[1].split("-")[0])
                    body = served[start:]
                    handler.send_response(206)
                    handler.send_header(
                        "Content-Range",
                        f"bytes {start}-{len(served) - 1}/{len(served)}")
                else:
                    body = served
                    handler.send_response(200)
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

            def _send(handler, body: bytes):
                handler.send_response(200)
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

            def log_message(handler, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_port

    def run(self):
        self.httpd.serve_forever()

    def stop(self):
        self.httpd.shutdown()


def test_a_resumed_download_of_a_regenerated_extract_is_caught(tmp_path):
    """End to end against the real mechanism, with `curl -C -` left in place."""
    day_one = tmp_path / "day1.osm.pbf"
    subprocess.run(["osmium", "cat", "--overwrite", "-o", str(day_one),
                    str(_sample(tmp_path))], check=True, capture_output=True)
    # "Today's" file: same shape, more in it, so it is larger.
    day_two = tmp_path / "day2.osm.pbf"
    subprocess.run(["osmium", "cat", "--overwrite", "-o", str(day_two),
                    str(_sample(tmp_path, extra=400))], check=True,
                   capture_output=True)
    assert day_two.stat().st_size > day_one.stat().st_size

    served = tmp_path / "served.osm.pbf"
    shutil.copy(day_one, served)
    server = _RangeServer(served)
    server.start()
    try:
        script = _http_script(tmp_path, server.port)
        work = tmp_path / "work"
        subprocess.run(["bash", str(script), "--only", "greece", str(work)],
                       check=True, capture_output=True)
        raw = work / "raw" / "greece.osm.pbf"
        assert raw.read_bytes() == day_one.read_bytes()

        shutil.copy(day_two, served)          # Geofabrik regenerates overnight
        shutil.rmtree(work / "filtered")
        result = subprocess.run(["bash", str(script), "--only", "greece",
                                 str(work)], check=True, capture_output=True,
                                text=True)
    finally:
        server.stop()

    assert raw.read_bytes() == day_two.read_bytes(), \
        "the resumed download was spliced and kept"
    assert "stale or damaged" in result.stdout, result.stdout
    subprocess.run(["osmium", "fileinfo", str(raw)], check=True,
                   capture_output=True)
