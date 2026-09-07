"""End-to-end browser checks for the offline behaviour.

The Python suite cannot reach any of this: IndexedDB, service workers and the
real offline state only exist in a browser. Every bug this file has caught was
invisible to `pytest` — lost writes from concurrent IndexedDB transactions,
stale DOM left in a hidden panel, a service worker that could not claim its
scope.

Run it against a live app with the stub services behind it:

    python -m uvicorn tools.stub_apis:app --port 8960 &
    MOTO_WEATHER_URL=http://127.0.0.1:8960/v1/forecast \
    MOTO_OVERPASS_URL=http://127.0.0.1:8960/api/interpreter \
    MOTO_OSRM_URL=http://127.0.0.1:8960 \
    MOTO_AUTOBAHN_URL=http://127.0.0.1:8960/o/autobahn \
    python -m uvicorn moto_route.api:app --port 8961 &

    python tools/browser_test.py                     # defaults to :8961
    python tools/browser_test.py --url http://127.0.0.1:8000

Needs `pip install playwright` and a Chromium. If the environment already
provides one (as CI images often do), point at it with --chromium.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:
    _env.require("playwright")

REPO = Path(__file__).resolve().parent.parent
DEMO = REPO / "examples" / "dolomites_demo.gpx"
SHORT = REPO / "tests" / "data" / "track.gpx"


def snapshot(page) -> dict:
    return page.evaluate("""() => ({
        routeName: document.getElementById('route-name').textContent,
        routePaths: document.querySelectorAll('#map path').length,
        weatherRows: document.querySelectorAll('#weather-list li').length,
        poiRows: document.querySelectorAll('#poi-list li:not(.pending)').length,
        hazardRows: document.querySelectorAll('#hazard-list li:not(.pending)').length,
        incidentRows: document.querySelectorAll('#incident-list li').length,
        profileVisible: !document.getElementById('profile-wrap').hidden,
        savedRides: document.querySelectorAll('#saved-list li').length,
        bannerClass: document.getElementById('cache-banner').className,
    })""")


def load_route(page, path: Path,
               wait_for: str = "#poi-list li:not(.pending)") -> None:
    page.set_input_files("#file-input", str(path))
    page.wait_for_selector(wait_for, timeout=30000)
    page.wait_for_timeout(2500)


def check(failures: list[str], condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def run(url: str, chromium: str | None) -> list[str]:
    failures: list[str] = []
    console: list[str] = []

    with sync_playwright() as pw:
        launch = {"args": ["--no-sandbox"]}
        if chromium:
            launch["executable_path"] = chromium
        browser = pw.chromium.launch(**launch)

        context = browser.new_context(viewport={"width": 1400, "height": 980})
        page = context.new_page()
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)

        # 0. Twisty-and-steep stretches reach the panel.
        #
        # The whole chain is server-side except this last step, so a working
        # endpoint and an empty panel look identical from pytest.
        load_route(page, DEMO)
        page.wait_for_selector("#demanding-list li", timeout=45000)
        demanding = page.evaluate("""() => {
            const rows = [...document.querySelectorAll('#demanding-list li')];
            return {
                count: rows.length,
                first: rows.length ? rows[0].innerText.replace(/\\n/g, ' ') : '',
                note: document.getElementById('demanding-note').innerText,
                hidden: document.getElementById('demanding-panel').hidden,
            };
        }""")
        check(failures, demanding["count"] > 0,
              "demanding stretches: the panel lists none")
        check(failures, not demanding["hidden"],
              "demanding stretches: the panel stayed hidden")
        check(failures, "%" in demanding["first"],
              f"demanding stretches: no gradient in {demanding['first']!r}")

        # 1. A loaded ride is written to IndexedDB, file bytes and all.
        load_route(page, DEMO)
        online = snapshot(page)
        stored = page.evaluate("""async () => {
            const store = await import('/static/js/store.js');
            const ride = (await store.allRides())[0];
            return {
                hasFileBlob: ride.file instanceof Blob,
                fileSize: ride.file ? ride.file.size : 0,
                layers: Object.keys(ride.layers).sort(),
            };
        }""")
        print("1. online:", {k: online[k] for k in ("routeName", "weatherRows", "poiRows")})
        print("   stored:", stored)
        check(failures, stored["hasFileBlob"] and stored["fileSize"] > 1000,
              "the original file must be kept, for silent re-upload later")
        # All seven layers, not just whichever transaction happened to win.
        check(failures, len(stored["layers"]) >= 7,
              f"expected every layer persisted, got {stored['layers']}")

        # 2. The whole point: a reload with the network genuinely off.
        context.set_offline(True)
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        offline = snapshot(page)
        print("2. offline reload:", {k: offline[k] for k in
                                     ("routeName", "weatherRows", "bannerClass")})
        check(failures, offline["routeName"] == online["routeName"], "route lost offline")
        check(failures, offline["routePaths"] >= 2, "route not drawn offline")
        check(failures, offline["weatherRows"] == online["weatherRows"],
              "cached layers not restored offline")
        check(failures, offline["profileVisible"], "elevation profile lost offline")
        check(failures, "offline" in offline["bannerClass"],
              f"banner must say offline, got {offline['bannerClass']!r}")

        # 3. Regaining signal reconciles without being asked.
        context.set_offline(False)
        page.evaluate("window.dispatchEvent(new Event('online'))")
        page.wait_for_timeout(4000)
        back = snapshot(page)
        print("3. back online:", back["bannerClass"])
        check(failures, "fresh" in back["bannerClass"] or back["weatherRows"] > 0,
              "layers did not refresh after coming back online")

        # 4. A forgotten route id (server restart) recovers by re-uploading.
        page.evaluate("""async () => {
            const store = await import('/static/js/store.js');
            const ride = (await store.allRides())[0];
            await store.patchRide(ride.key, { serverRouteId: 'deadbeefdead' });
        }""")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        recovered_id = page.evaluate("""async () => {
            const store = await import('/static/js/store.js');
            return (await store.allRides())[0].serverRouteId;
        }""")
        print("4. recovered route id:", recovered_id)
        check(failures, recovered_id != "deadbeefdead",
              "a stale route id was not recovered by re-upload")
        check(failures, snapshot(page)["weatherRows"] > 0,
              "layers did not reload after route-id recovery")

        # 5. Forgetting a ride clears both the store and the DOM.
        page.click("#saved-list [data-delete]")
        page.wait_for_timeout(1200)
        after = page.evaluate("""async () => {
            const store = await import('/static/js/store.js');
            return {
                inDb: (await store.allRides()).length,
                rows: document.querySelectorAll('#saved-list li').length,
                panelHidden: document.getElementById('saved-panel').hidden,
            };
        }""")
        print("5. after delete:", after)
        check(failures, after["inDb"] == 0, "ride not removed from IndexedDB")
        check(failures, after["rows"] == 0 and after["panelHidden"],
              "stale rows left in the hidden panel")

        # 6. The store is capped, so a tablet's quota is never eaten.
        for index in range(7):
            load_route(page, DEMO if index % 2 else SHORT,
                       "#poi-list li:not(.pending)" if index % 2 else "#summary")
        kept = page.evaluate(
            "async () => (await (await import('/static/js/store.js')).allRides()).length")
        print("6. rides kept after loading 7:", kept)
        check(failures, kept <= 5, f"ride cap not enforced: {kept} kept")
        context.close()

        # 7. Without IndexedDB at all, the app still plans a ride.
        locked = browser.new_context(viewport={"width": 1200, "height": 900})
        page2 = locked.new_page()
        page_errors: list[str] = []
        page2.on("pageerror", lambda e: page_errors.append(str(e)))
        page2.add_init_script("delete window.indexedDB;")
        page2.goto(url, wait_until="domcontentloaded")
        page2.wait_for_timeout(1000)
        load_route(page2, DEMO)
        degraded = page2.evaluate("""() => ({
            weatherRows: document.querySelectorAll('#weather-list li').length,
            savedPanelHidden: document.getElementById('saved-panel').hidden,
        })""")
        print("7. without IndexedDB:", degraded)
        check(failures, degraded["weatherRows"] > 0, "app broke without IndexedDB")
        check(failures, degraded["savedPanelHidden"],
              "saved panel shown despite having nowhere to save")
        check(failures, not page_errors, f"page errors without IndexedDB: {page_errors[:2]}")

        browser.close()

    # Step 4 deliberately aims at a dead route id, so one 404 per layer is the
    # recovery path working. Tile and network errors come from the sandbox.
    noise = ("404", "ERR_", "net::", "Failed to fetch", "tile")
    real = [line for line in console if not any(token in line for token in noise)]
    if real:
        failures.append(f"console errors: {real}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8961/")
    parser.add_argument("--chromium", default=None,
                        help="Path to a Chromium binary, if not the bundled one")
    args = parser.parse_args()

    failures = run(args.url, args.chromium)
    print("\nFAILURES:" if failures else "\nAll browser checks passed.")
    for failure in failures:
        print(f"  - {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
