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
import math
import time
from typing import Any, Callable, Sequence

import httpx

from .. import geo
from ..config import Settings

LatLon = geo.LatLon

log = logging.getLogger(__name__)

#: Statuses worth trying again: throttling and the gateway timeouts a busy
#: Overpass returns under load. Everything else is a bad query or a dead
#: service, and repeating it just adds load.
RETRYABLE = frozenset({429, 502, 503, 504})

#: Seconds before the first retry, doubling after that. A module constant so
#: tests can set it to zero rather than sleeping through the backoff.
RETRY_BASE_DELAY = 1.0

#: Never start a query with less than this much of the deadline left. Beginning
#: one that cannot finish wastes the rider's time and the server's alike.
MIN_ATTEMPT_S = 20.0


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
    deadline: float | None = None,
    allowance: float | None = None,
    endpoints: Sequence[str] | None = None,
) -> dict[str, Any]:
    """POST an Overpass QL query and return the parsed JSON.

    ``deadline`` is a :func:`time.monotonic` instant after which no new attempt
    starts and the per-request timeout is trimmed to what is left. Without it a
    single stubborn chunk spent 311 seconds — three attempts at the full
    105-second timeout — which the rider experiences as the panel never filling
    in.

    ``allowance`` caps a single attempt, so that one chunk of a layer cannot
    spend the deadline its siblings needed. The deadline bounds the layer; the
    allowance shares it out.

    Attempts rotate through the configured endpoints. A refused connection is
    the one failure a second server reliably fixes — the main instance drops
    connections when it is busy, and it does so per-query, so the heavy closure
    query can fail while the lighter points-of-interest one alongside it
    succeeds. Rotating also spreads load across mirrors when one is throttling.

    Raises :class:`OverpassError` with a message describing what actually
    happened, not the name of a Python class.
    """
    endpoints = list(endpoints) if endpoints else settings.overpass_endpoints
    limit = max(1, settings.overpass_concurrency)
    delay = RETRY_BASE_DELAY
    failures: list[str] = []
    last: OverpassError | None = None

    # Set when this query first wins a slot. The allowance has to bound the
    # query as a whole, retries included: capping only one attempt lets three
    # retries spend three times the share, which is how one chunk of five ate a
    # whole 60-second budget while the other four waited for the semaphore.
    limit_at: float | None = None

    for attempt in range(attempts):
        endpoint = endpoints[attempt % len(endpoints)]

        async with _semaphore(limit):
            # The budget is measured *after* the slot is won, not before.
            # Several chunks of a route now queue on this semaphore at once, so
            # the wait for a slot is itself time off the deadline; a timeout
            # computed before queueing would overrun it by however long the
            # queue took. It is also why the allowance starts here: a query
            # should be charged for its own time, not for its turn in the queue.
            if allowance is not None and limit_at is None:
                limit_at = time.monotonic() + allowance

            # Overriding the shared client's timeout: an Overpass query is
            # allowed far longer than an ordinary API call, and must outlast
            # the budget the query itself declares — but never longer than the
            # deadline allows.
            wait = settings.overpass_timeout_s + TRANSFER_MARGIN_S

            # The layer's deadline decides whether to start at all.
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining < MIN_ATTEMPT_S:
                    raise last or OverpassError(
                        f"Ran out of time for Overpass after {attempt} attempt(s)."
                    )
                wait = min(wait, remaining)

            # The chunk's own share only trims the timeout, and ends the
            # retries once spent. It is deliberately not a reason to refuse the
            # first attempt: the share is floored at MIN_ATTEMPT_S, so testing
            # it against MIN_ATTEMPT_S on the pass that just created it fails
            # on nothing but the microseconds since.
            if limit_at is not None:
                share_left = limit_at - time.monotonic()
                if attempt > 0 and share_left < MIN_ATTEMPT_S:
                    raise last or OverpassError(
                        f"Ran out of time for Overpass after {attempt} attempt(s)."
                    )
                wait = min(wait, max(share_left, MIN_ATTEMPT_S))

            try:
                response = await client.post(
                    endpoint,
                    data={"data": query},
                    headers={"User-Agent": settings.user_agent},
                    timeout=wait,
                )
            except httpx.TimeoutException:
                last = OverpassError(
                    f"Overpass did not answer within {int(wait)}s. Long routes "
                    "make expensive queries; a shorter route, or your own "
                    "Overpass server, will work."
                )
                failures.append(f"{_host(endpoint)}: timed out")
                response = None
            except httpx.HTTPError as exc:
                # Name the host and the reason. "ConnectError" alone tells a
                # rider nothing about whether to retry, wait, or reconfigure.
                detail = str(exc).strip() or type(exc).__name__
                last = OverpassError(
                    f"Could not connect to {_host(endpoint)} — {detail}. "
                    "The public Overpass servers refuse connections when busy."
                )
                failures.append(f"{_host(endpoint)}: {type(exc).__name__}")
                response = None

            if response is not None:
                if response.status_code < 400:
                    try:
                        return response.json()
                    except ValueError:
                        raise OverpassError(
                            f"{_host(endpoint)} returned something that is not JSON."
                        ) from None

                last = OverpassError(
                    f"{_host(endpoint)}: {describe(response.status_code)}",
                    response.status_code,
                )
                failures.append(f"{_host(endpoint)}: HTTP {response.status_code}")
                if response.status_code not in RETRYABLE:
                    raise last
                # Overpass tells us how long to wait when it throttles; believe it.
                delay = _retry_after(response) or delay

        if attempt < attempts - 1:
            log.info("Overpass attempt %d via %s failed (%s); retrying in %.1fs",
                     attempt + 1, _host(endpoint), last, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8.0)

    if last is not None and len(set(failures)) > 1:
        # Several endpoints failed differently; list them rather than reporting
        # only whichever happened to be last.
        raise OverpassError(f"{last} (tried {', '.join(failures)})", last.status)
    raise last or OverpassError("Overpass failed for an unknown reason.")


def chunk_coordinates(
    coords: Sequence[LatLon],
    max_points: int,
) -> list[list[LatLon]]:
    """Split a corridor into pieces small enough for a public Overpass server.

    Cost grows with the number of coordinates in the `around:` filter, so a long
    enough route will always fail as one query — 169 points timed out where 44
    took fifteen seconds. Chunking makes the cost per query a constant and the
    number of queries linear, which is the right way round.

    Consecutive chunks share a point. Without that overlap the corridor has a
    gap at every seam, exactly where one query stops and the next begins.
    """
    if max_points < 2:
        max_points = 2
    if len(coords) <= max_points:
        return [list(coords)]

    chunks: list[list[LatLon]] = []
    start = 0
    while start < len(coords) - 1:
        end = min(start + max_points, len(coords))
        chunks.append(list(coords[start:end]))
        start = end - 1          # overlap by one point, closing the seam
    return chunks


async def run_chunked(
    chunks: Sequence[Sequence[LatLon]],
    build: Callable[[Sequence[LatLon]], str],
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    deadline_s: float | None = None,
    coverage_points: Sequence[LatLon] | None = None,
) -> dict[str, Any]:
    """Run one query per chunk, concurrently, and merge the results.

    Elements are de-duplicated by (type, id), because a feature spanning a seam
    is returned by both neighbouring queries.

    A chunk that fails does not sink the layer: whatever the others found is
    returned with ``partial`` set. Most of a route's closures beats none of
    them, as long as the caller says which it is.

    The chunks are launched together and bounded by the same semaphore every
    other Overpass query uses, so this asks no more of the public servers than
    running them one after another did — ``overpass_concurrency`` is still the
    only thing deciding how many queries are in flight. What it removes is the
    idle time: a layer no longer holds exactly one slot while another sits
    free.

    Concurrency alone does not make a slow chunk harmless. Where the semaphore
    is narrower than the number of chunks, they still take turns, and a chunk
    that hangs for the whole budget starves everything behind it — measured at
    ``overpass_concurrency=1``, two chunks of five consumed a 60-second budget
    and the other three never ran. So each chunk also gets an ``allowance``:
    the budget divided by the number of turns the semaphore forces, which is
    the old fair share generalised to any width. At a width of one it is the
    old behaviour exactly; at a width of four with four chunks it is the whole
    budget, because nothing is waiting behind them.
    """
    budget = settings.overpass_deadline_s if deadline_s is None else deadline_s
    deadline = time.monotonic() + budget if budget else None

    # How many turns the semaphore forces these chunks to take.
    rounds = math.ceil(len(chunks) / max(1, settings.overpass_concurrency))
    allowance = max(MIN_ATTEMPT_S, budget / rounds) if budget else None

    # Decided once for the layer from every point it will search, not per
    # chunk and not from a bounding box: a route that leaves a self-hosted
    # instance's coverage must not have half its chunks answered from a
    # database that has never heard of the other half.
    endpoints = settings.endpoints_for(coverage_points)

    results = await asyncio.gather(
        *(run_query(build(chunk), settings, client, deadline=deadline,
                    allowance=allowance, endpoints=endpoints)
          for chunk in chunks),
        return_exceptions=True,
    )

    elements: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    failed = 0
    last_error: OverpassError | None = None

    # Merged in chunk order, which `gather` preserves regardless of the order
    # they actually finished in, so the same route always yields the same list.
    for index, result in enumerate(results):
        if isinstance(result, OverpassError):
            failed += 1
            last_error = result
            log.warning("Overpass chunk %d/%d failed: %s",
                        index + 1, len(chunks), result)
            continue
        if isinstance(result, BaseException):
            # Not an Overpass failure — a bug here, or the request being
            # cancelled. Neither should be quietly folded into "partial".
            raise result

        for element in result.get("elements", []):
            key = (element.get("type"), element.get("id"))
            if key in seen:
                continue
            seen.add(key)
            elements.append(element)

    if failed == len(chunks):
        raise last_error or OverpassError("Every part of the route failed.")

    if failed:
        log.warning("Overpass answered %d of %d chunk(s) within %.0fs",
                    len(chunks) - failed, len(chunks), budget)

    return {
        "elements": elements,
        "partial": failed > 0,
        "failed_chunks": failed,
        "total_chunks": len(chunks),
    }


def _host(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[0] or url


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        # Only the seconds form; the HTTP-date form is rare and not worth parsing.
        return max(0.5, min(float(raw), 30.0))
    except ValueError:
        return None
