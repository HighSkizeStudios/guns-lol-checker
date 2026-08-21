"""
notifier.py
===========

Optional Discord webhook notifier: posts available usernames to a channel as
they are discovered.

Why batching?
-------------
Discord webhooks are rate-limited (roughly 30 requests/minute per webhook, with
short bursts allowed). Firing one HTTP POST per available name would quickly hit
`429 Too Many Requests`. So instead we:

  * collect available names into an in-memory queue,
  * flush them as a *single* message either when a batch fills up
    (`batch_size`) or on a timer (`flush_interval` seconds),
  * and, if Discord still returns 429, honour the `retry_after` it sends back.

The notifier runs as a background asyncio task, so posting to Discord never
blocks the username checking itself.

Security note
-------------
A webhook URL is a secret: anyone who has it can post to your channel. Don't
commit it to a public repo or share logs that contain it. Prefer setting it via
the GUNS_DISCORD_WEBHOOK environment variable over hardcoding it in config.py.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

log = logging.getLogger("notifier")

# Discord's hard limit on a webhook message's "content" field.
_DISCORD_CONTENT_LIMIT = 2000


class DiscordNotifier:
    """Batches available usernames and posts them to a Discord webhook."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        webhook_url: str,
        *,
        batch_size: int = 1,
        flush_interval: float = 1.0,
        message_prefix: str = "New user available, claim it whilst you can..!",
        register_url: str = "https://guns.lol/register",
    ):
        self.session = session
        self.webhook_url = webhook_url
        self.batch_size = max(1, batch_size)
        self.flush_interval = max(0.2, flush_interval)
        self.message_prefix = message_prefix
        self.register_url = register_url

        self._queue: list[str] = []
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Set whenever a name is enqueued, so the flush loop wakes immediately
        # instead of waiting out the interval — critical for beating snipers.
        self._wake = asyncio.Event()
        # Serialise sends so we never have two POSTs in flight (keeps us well
        # inside Discord's rate limits and makes 429 handling simple).
        self._lock = asyncio.Lock()

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        """Launch the background flush loop."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())
            log.info("Discord notifier started (batch=%d, interval=%.1fs)",
                     self.batch_size, self.flush_interval)

    async def close(self) -> None:
        """Flush anything remaining and stop the background loop."""
        self._stop.set()
        self._wake.set()  # wake the loop so it notices the stop immediately
        if self._task is not None:
            await self._task
            self._task = None
        # Final flush in case names arrived after the loop's last tick.
        await self._flush()

    # -- producer side (called from the result callback) ----------------
    def enqueue(self, username: str) -> None:
        """Add an available username to the outgoing queue (non-blocking).

        Also wakes the flush loop right away so the alert goes out with minimal
        delay — for rare 3-letter names, every second counts.
        """
        self._queue.append(username)
        self._wake.set()

    # -- background loop -------------------------------------------------
    async def _run(self) -> None:
        """Flush as soon as a name is enqueued, or on the interval as a backstop."""
        while not self._stop.is_set():
            try:
                # Wake instantly when a name arrives; otherwise tick on interval.
                await asyncio.wait_for(self._wake.wait(), timeout=self.flush_interval)
            except asyncio.TimeoutError:
                pass  # interval elapsed with nothing new — harmless
            self._wake.clear()

            # Send in batch-sized chunks so a big backlog doesn't exceed the
            # Discord content limit in one message.
            while len(self._queue) >= self.batch_size:
                await self._flush(limit=self.batch_size)
            # Also flush a partial batch so names never sit waiting.
            await self._flush(limit=self.batch_size)

    async def _flush(self, limit: int | None = None) -> None:
        """Post up to `limit` queued names as one message."""
        if not self._queue:
            return
        take = len(self._queue) if limit is None else min(limit, len(self._queue))
        batch = self._queue[:take]

        content = self._format(batch)
        ok = await self._send(content)
        if ok:
            # Only drop the names we actually sent successfully.
            del self._queue[:take]

    def _format(self, names: list[str]) -> str:
        """Render a batch of names into a Discord message body.

        Names are wrapped in backticks and truncated to stay under Discord's
        2000-character content limit (rare, but be safe with big batches).
        """
        body = " ".join(f"`{n}`" for n in names)
        # Include a direct register link so you can go claim it in one click.
        message = f"{self.message_prefix}\n{body}\n{self.register_url}"
        if len(message) > _DISCORD_CONTENT_LIMIT:
            message = message[: _DISCORD_CONTENT_LIMIT - 1] + "…"
        return message

    async def _send(self, content: str) -> bool:
        """POST one message, honouring Discord rate limits. Returns success."""
        payload = {"content": content}
        async with self._lock:
            for attempt in range(1, 4):  # a few tries for transient issues
                try:
                    async with self.session.post(
                        self.webhook_url, json=payload
                    ) as resp:
                        # 204 No Content is the normal webhook success response.
                        if resp.status in (200, 204):
                            log.debug("Posted %d chars to Discord", len(content))
                            return True
                        if resp.status == 429:
                            # Rate limited — Discord tells us how long to wait.
                            try:
                                data = await resp.json()
                                retry_after = float(data.get("retry_after", 1.0))
                            except Exception:  # noqa: BLE001
                                retry_after = 1.0
                            log.warning(
                                "Discord rate limited; retrying in %.2fs", retry_after
                            )
                            await asyncio.sleep(retry_after + 0.1)
                            continue
                        # Other statuses (e.g. 401/404 = bad/deleted webhook).
                        text = await resp.text()
                        log.error(
                            "Discord webhook error HTTP %d: %s",
                            resp.status, text[:200],
                        )
                        return False
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    log.warning("Discord POST failed (%s); attempt %d/3",
                                type(exc).__name__, attempt)
                    await asyncio.sleep(1.0 * attempt)
            log.error("Giving up on a Discord batch after retries")
            return False
