"""
checker.py
==========

The network layer: given a username, ask guns.lol whether it is available.

Design goals
------------
* **Respectful** — bounded concurrency (an asyncio.Semaphore), a configurable
  per-request delay with jitter, and an honest User-Agent.
* **Robust** — transient failures (timeouts, connection resets, HTTP 429/5xx)
  are retried with exponential backoff. Permanent/unexpected responses are
  reported as errors rather than silently guessed.
* **Endpoint-agnostic** — the single function ``interpret_response`` turns a raw
  HTTP response into one of three verdicts. If guns.lol changes its API you
  should only need to touch ``config.py`` and, at most, this one function.

Verdicts
--------
Every check resolves to a :class:`CheckResult` whose ``status`` is one of:
    "available"  -> the username is free
    "taken"      -> the username is in use
    "error"      -> we could not determine availability (network/parse failure)
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Literal

import aiohttp

import config

log = logging.getLogger("checker")

Verdict = Literal["available", "taken", "error"]


@dataclass
class CheckResult:
    """Outcome of checking a single username."""

    username: str
    status: Verdict
    detail: str = ""     # human-readable note (e.g. HTTP status, error message)


# HTTP status codes that indicate a *transient* problem worth retrying.
# 429 = rate limited, 5xx = server side hiccups.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def interpret_response(
    settings: config.Settings,
    status_code: int,
    body_json: dict | None,
    body_text: str,
) -> CheckResult | None:
    """Translate a raw HTTP response into a verdict.

    Returns
    -------
    CheckResult
        A concrete "available"/"taken"/"error" verdict, OR
    None
        Meaning "this looks transient, please retry" (the caller handles
        backoff). Returning None keeps retry logic in one place.

    This is the ONE place that encodes assumptions about the endpoint's shape.
    If guns.lol changes its response format, adjust the logic here to match what
    you observe in developer tools (see README).
    """
    # A retryable status → signal the caller to back off and try again.
    if status_code in _RETRYABLE_STATUS:
        return None

    # ---- Profile-page mode: status code is the whole story --------------
    # The response mode is a module-level setting in config.py (it describes the
    # endpoint's shape, not a per-run knob), so we read it from there.
    mode = config.RESPONSE_MODE
    if mode == "GET_PROFILE":
        if status_code == config.PROFILE_AVAILABLE_STATUS:
            return CheckResult("", "available", f"HTTP {status_code}")
        if status_code == config.PROFILE_TAKEN_STATUS:
            return CheckResult("", "taken", f"HTTP {status_code}")
        # Anything else is unexpected for this mode.
        return CheckResult("", "error", f"unexpected HTTP {status_code}")

    # ---- JSON API mode --------------------------------------------------
    # We expect a 2xx with a JSON body. Non-2xx that isn't retryable is an error.
    if not (200 <= status_code < 300):
        return CheckResult("", "error", f"unexpected HTTP {status_code}")

    if body_json is None:
        # Couldn't parse JSON — surface a snippet to aid debugging.
        snippet = body_text[:120].replace("\n", " ")
        return CheckResult("", "error", f"non-JSON body: {snippet!r}")

    # Interpret whichever key the API uses. Exactly one of these is configured.
    if config.JSON_KEY_EXISTS is not None:
        exists = _extract_bool(body_json, config.JSON_KEY_EXISTS)
        if exists is None:
            return CheckResult(
                "", "error", f"missing key {config.JSON_KEY_EXISTS!r} in {body_json}"
            )
        return CheckResult("", "taken" if exists else "available", "json:exists")

    if config.JSON_KEY_AVAILABLE is not None:
        available = _extract_bool(body_json, config.JSON_KEY_AVAILABLE)
        if available is None:
            return CheckResult(
                "", "error", f"missing key {config.JSON_KEY_AVAILABLE!r} in {body_json}"
            )
        return CheckResult("", "available" if available else "taken", "json:available")

    # Neither key configured — misconfiguration, not a per-username error.
    return CheckResult("", "error", "no JSON key configured in config.py")


def _extract_bool(body: dict, key: str) -> bool | None:
    """Pull a boolean out of a (possibly nested) JSON body.

    Supports a dotted key path like "data.exists" for APIs that wrap their
    payload. Returns None if the key is absent or not boolean-ish.
    """
    node: object = body
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if isinstance(node, bool):
        return node
    # Some APIs use "true"/"false" strings or 0/1.
    if isinstance(node, str):
        return node.strip().lower() in ("true", "1", "yes")
    if isinstance(node, int):
        return bool(node)
    return None


async def check_username(
    session: aiohttp.ClientSession,
    settings: config.Settings,
    username: str,
) -> CheckResult:
    """Check a single username, with retries and exponential backoff.

    This function is safe to call concurrently; it does not touch shared state.
    Concurrency limiting and inter-request delay are handled by the caller
    (see :class:`AvailabilityWorkerPool`).
    """
    url = config.AVAILABILITY_ENDPOINT.format(username=username)
    attempt = 0

    while True:
        attempt += 1
        try:
            async with session.get(url, headers=settings.request_headers()) as resp:
                status_code = resp.status
                text = await resp.text()
                body_json = None
                # Try to parse JSON regardless of Content-Type; some servers
                # mislabel it. Failure is fine — interpret_response handles it.
                try:
                    body_json = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001 - JSON parse is best-effort
                    body_json = None

            verdict = interpret_response(settings, status_code, body_json, text)

            if verdict is not None:
                verdict.username = username  # fill in the name
                log.debug("%s -> %s (%s)", username, verdict.status, verdict.detail)
                return verdict

            # verdict is None => retryable HTTP status (e.g. 429/5xx).
            reason = f"HTTP {status_code}"

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # Network-level transient error — eligible for retry.
            reason = f"{type(exc).__name__}: {exc}"

        # ---- We got here because the attempt failed transiently ----------
        if attempt > settings.max_retries:
            log.warning(
                "%s: giving up after %d attempts (%s)", username, attempt, reason
            )
            return CheckResult(username, "error", f"retries exhausted: {reason}")

        backoff = _backoff_delay(settings, attempt)
        log.debug(
            "%s: transient failure (%s); retry %d/%d in %.2fs",
            username,
            reason,
            attempt,
            settings.max_retries,
            backoff,
        )
        await asyncio.sleep(backoff)


def _backoff_delay(settings: config.Settings, attempt: int) -> float:
    """Compute an exponential backoff delay with full jitter.

    delay = min(backoff_max, backoff_base * factor**(attempt-1)) * random(0.5..1)

    Full jitter avoids the "thundering herd" where many workers retry in sync.
    """
    raw = settings.backoff_base * (settings.backoff_factor ** (attempt - 1))
    capped = min(settings.backoff_max, raw)
    # Jitter between 50% and 100% of the capped value.
    return capped * (0.5 + 0.5 * random.random())


class AvailabilityWorkerPool:
    """Runs many username checks concurrently while staying polite.

    Responsibilities:
      * cap simultaneous in-flight requests with a semaphore,
      * enforce a per-worker delay (+jitter) between requests,
      * hand each finished :class:`CheckResult` to a callback so the caller can
        update statistics and persist available names immediately.
    """

    def __init__(self, session: aiohttp.ClientSession, settings: config.Settings):
        self.session = session
        self.settings = settings
        # The semaphore is the hard cap on concurrency.
        self._sem = asyncio.Semaphore(settings.concurrency)

    async def _worker(self, username: str, on_result) -> None:
        """Check one username under the concurrency limit, then delay."""
        async with self._sem:
            result = await check_username(self.session, self.settings, username)
            # Deliver the result (callback may be async or sync).
            maybe_coro = on_result(result)
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro

            # Politeness delay AFTER the request, still holding the semaphore so
            # the effective request rate per worker respects `delay`.
            if self.settings.delay > 0:
                jitter = self.settings.delay * self.settings.jitter * random.random()
                await asyncio.sleep(self.settings.delay + jitter)

    async def run(self, usernames, on_result) -> None:
        """Schedule checks for an iterable/iterator of usernames.

        We create tasks lazily in batches so an enormous keyspace doesn't
        create millions of pending tasks at once. `on_result(result)` is invoked
        for every completed check.
        """
        # A bounded number of outstanding tasks keeps memory flat even for the
        # full 4-char alphanumeric keyspace.
        max_outstanding = self.settings.concurrency * 4
        pending: set[asyncio.Task] = set()

        for username in usernames:
            if len(pending) >= max_outstanding:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                # Surface any unexpected exceptions from finished tasks.
                for task in done:
                    task.result()

            pending.add(asyncio.create_task(self._worker(username, on_result)))

        # Drain the remaining tasks.
        if pending:
            done, _ = await asyncio.wait(pending)
            for task in done:
                task.result()
