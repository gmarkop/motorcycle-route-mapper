"""One door to Overpass, because it is a shared free service with a slot budget.

Three parts of this app ask OpenStreetMap questions: closures, points of
interest, and the motorway-ref lookup the German incident provider uses. The
browser fires all three layers at once, so all three queries used to leave
together — and the public Overpass instance grants roughly **two** concurrent
slots per IP. The third request came back 429, and which of the three lost the
race was luck. On a long route the symptom was a sidebar with "unavailable" in
two panels and no clue why.

So every Overpass query now goes through here, where a semaphore keeps the app
inside the budget, a retry handles the throttling and timeouts that happen
anyway, and failures are described in terms of what actually went wrong rather
than the name of a Python exception.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

#: Statuses worth trying again: throttling and the gateway timeouts a busy
#: Overpass returns under load. Everything else is a bad query or a dead
#: service, and repeating it just adds load.
RETRYABLE = frozenset({429, 502, 503, 504})

#: Seconds before the first retry, doubling after that. A module constant so
#: tests can set it to zero rather than sleeping through the backoff.
RETRY_BASE_DELAY = 1.0


#: Seconds allowed on top of the query's own budget, for connecting and for
#: streaming back what can be a few megabytes of JSON.
TRANSFER_MARGIN_S = 15.0


def query_header(settings: Settings) -> str:
    """The `[out:json][timeout:N];` prelude every query shares.

    Kept here so the declared budget and the HTTP timeout cannot drift apart —
    they were 90 and 20 once, which made every slow query fail client-side.
    """
    return f"[out:json][timeout:{int(settings.overpass_timeout_s)}];"


class OverpassError(RuntimeError):
    """A failed Overpass query, with a message fit to show a rider."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


_semaphores: dict[int, asyncio.Semaphore] = {}


def _semaphore(limit: int) -> asyncio.Semaphore:
    """One semaphore per configured limit, created on first use.

    Keyed by limit rather than made a module constant so a self-hosted Overpass
    can be given a higher budget without restarting anything.
    """
    if limit not in _semaphores:
        _semaphores[limit] = asyncio.Semaphore(max(1, limit))
    return _semaphores[limit]


def describe(status: int) -> str:
    """Plain English for an Overpass status code.

    The status is the single most useful fact when this goes wrong, and
    `HTTPStatusError` — which is all the user used to see — throws it away.
    """
    if status == 429:
        return ("Overpass is rate-limiting this address (429). It allows about "
                "two queries at a time; try again in a moment.")
    if status == 504:
        return ("Overpass timed out (504) — the query was too expensive for the "
                "public server. A shorter route, or your own Overpass, will work.")
    if status in (502, 503):
        return f"Overpass is overloaded or down ({status}). Try again shortly."
    if status == 400:
        return "Overpass rejected the query as malformed (400). This is a bug here."
    return f"Overpass returned HTTP {status}."


async def run_query(
    query: str,
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    attempts: int = 3,
) -> dict[str, Any]:
    """POST an Overpass QL query and return the parsed JSON.

    Raises :class:`OverpassError` with a readable message on failure.
    """
    limit = max(1, settings.overpass_concurrency)
    delay = RETRY_BASE_DELAY
    last: OverpassError | None = None

    for attempt in range(1, attempts + 1):
        async with _semaphore(limit):
            try:
                response = await client.post(
                    settings.overpass_url,
                    data={"data": query},
                    headers={"User-Agent": settings.user_agent},
                    # Overriding the shared client's timeout: an Overpass query
                    # is allowed far longer than an ordinary API call, and must
                    # outlast the budget the query itself declares.
                    timeout=settings.overpass_timeout_s + TRANSFER_MARGIN_S,
                )
            except httpx.TimeoutException:
                last = OverpassError(
                    f"Overpass did not answer within "
                    f"{int(settings.overpass_timeout_s + TRANSFER_MARGIN_S)}s. Long "
                    "routes make expensive queries; a shorter route, or your own "
                    "Overpass server, will work."
                )
                response = None
            except httpx.HTTPError as exc:
                last = OverpassError(f"Could not reach Overpass ({type(exc).__name__}).")
                response = None

            if response is not None:
                if response.status_code < 400:
                    try:
                        return response.json()
                    except ValueError:
                        raise OverpassError(
                            "Overpass returned something that is not JSON."
                        ) from None

                last = OverpassError(describe(response.status_code), response.status_code)
                if response.status_code not in RETRYABLE:
                    raise last
                # Overpass tells us how long to wait when it throttles; believe it.
                delay = _retry_after(response) or delay

        if attempt < attempts:
            log.info("Overpass attempt %d failed (%s); retrying in %.1fs",
                     attempt, last, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8.0)

    raise last or OverpassError("Overpass failed for an unknown reason.")


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        # Only the seconds form; the HTTP-date form is rare and not worth parsing.
        return max(0.5, min(float(raw), 30.0))
    except ValueError:
        return None
