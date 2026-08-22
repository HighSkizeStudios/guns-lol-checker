"""
main.py - Clean entry point
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import TextIO

import aiohttp

import checker
import config
import generator
import notifier as notifier_mod
import preflight
import stats as stats_mod

log = logging.getLogger("main")

def parse_args(argv: list[str] | None = None) -> tuple[config.Settings, bool]:
    d = config.Settings()

    p = argparse.ArgumentParser(
        description="Check guns.lol username availability (read-only).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    p.add_argument("--length", type=int, choices=(2, 3, 4), default=d.length,
                   help="Username length to generate.")
    p.add_argument("--charset", choices=("letters", "alnum"), default=d.charset,
                   help="Character set: 'letters' (a-z) or 'alnum' (a-z0-9).")
    p.add_argument("--wordlist", default=d.wordlist_path,
                   help="Path to a file of usernames (one per line). Overrides generation.")
    p.add_argument("--no-loop", action="store_true",
                   help="Run a single pass instead of looping forever.")
    p.add_argument("--loop-pause", type=float, default=d.loop_pause,
                   help="Seconds to wait between passes when looping.")
    p.add_argument("--no-shuffle", action="store_true",
                   help="Check names in alphabetical order instead of random.")
    p.add_argument("--threads", type=int, default=d.concurrency, dest="concurrency",
                   help="Number of concurrent workers.")
    p.add_argument("--delay", type=float, default=d.delay,
                   help="Seconds to wait per worker between requests.")
    p.add_argument("--jitter", type=float, default=d.jitter,
                   help="Random jitter fraction added to the delay (0..1).")
    p.add_argument("--timeout", type=float, default=d.timeout,
                   help="Per-request timeout in seconds.")
    p.add_argument("--max-retries", type=int, default=d.max_retries,
                   help="Retry attempts for transient network errors.")
    p.add_argument("--webhook", default=None,
                   help="Discord webhook URL (overrides config).")
    p.add_argument("--no-discord", action="store_true",
                   help="Disable Discord notifications.")
    p.add_argument("--self-test", action="store_true",
                   help="Run connectivity + sample request, then exit.")
    p.add_argument("--no-preflight", action="store_true",
                   help="Skip the pre-run connectivity check.")
    p.add_argument("--output", default=None, dest="output_file",
                   help="File to append available usernames to.")
    p.add_argument("--log-file", default=d.log_file,
                   help="Log file path (use '' to disable).")
    p.add_argument("--log-level", default=d.log_level,
                   choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                   help="Log verbosity.")

    args = p.parse_args(argv)

    settings = config.Settings(
        length=args.length,
        charset=args.charset,
        wordlist_path=args.wordlist,
        concurrency=args.concurrency,
        delay=args.delay,
        jitter=args.jitter,
        timeout=args.timeout,
        max_retries=args.max_retries,
        output_file=os.path.join(
            config.app_dir(), args.output_file or config.default_output_for(args.length)
        ),
        log_file=(os.path.join(config.app_dir(), args.log_file) if args.log_file else None),
        log_level=args.log_level,
        skip_preflight=args.no_preflight,
        loop=(not args.no_loop),
        loop_pause=args.loop_pause,
        shuffle=(not args.no_shuffle),
        discord_webhook=(args.webhook or None),
        discord_disabled=args.no_discord,
    )
    return settings, args.self_test

def setup_logging(settings: config.Settings) -> None:
    handlers: list[logging.Handler] = []
    
    # Only show WARNING+ on console
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    handlers.append(stream)

    if settings.log_file:
        fileh = logging.FileHandler(settings.log_file, encoding="utf-8")
        fileh.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        handlers.append(fileh)

    logging.basicConfig(level=settings.log_level, handlers=handlers)

def open_output(settings: config.Settings) -> tuple[TextIO, set[str]]:
    seen: set[str] = set()
    if os.path.exists(settings.output_file):
        with open(settings.output_file, "r", encoding="utf-8") as fh:
            for line in fh:
                name = line.strip()
                if name:
                    seen.add(name)
        print(f"Loaded {len(seen)} previously-found names from {settings.output_file}")
    fh = open(settings.output_file, "a", encoding="utf-8")
    return fh, seen


def _taken_cache_json_path() -> str:
    return os.path.join(config.app_dir(), "taken_cache.json")


def _taken_cache_mode_key(settings: config.Settings) -> str:
    """Key identifying a length+charset combo, e.g. '3l' or '4a'."""
    return f"{settings.length}{settings.charset[:1]}"


class TakenCache:
    """Single-file (JSON) cache of confirmed-taken names, keyed by mode.

    Consolidates what used to be one .txt file per length/charset combo
    (taken_2l.txt, taken_3l.txt, taken_3a.txt, ...) into one taken_cache.json
    so the folder doesn't accumulate lots of small files over time.
    """

    def __init__(self, path: str):
        self.path = path
        self._data: dict[str, list[str]] = {}
        if os.path.exists(path):
            import json
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self._data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def get(self, mode_key: str) -> set[str]:
        return set(self._data.get(mode_key, []))

    def add(self, mode_key: str, username: str) -> None:
        self._data.setdefault(mode_key, [])
        if username not in self._data[mode_key]:
            self._data[mode_key].append(username)

    def save(self) -> None:
        import json
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
        except OSError as exc:
            print(f"(couldn't write {self.path}: {exc})")


def load_taken_cache(settings: config.Settings) -> tuple[TakenCache, set[str]]:
    """Load the shared taken_cache.json and return (cache, names-for-this-mode)."""
    cache = TakenCache(_taken_cache_json_path())
    mode_key = _taken_cache_mode_key(settings)
    taken = cache.get(mode_key)
    if taken:
        print(f"Loaded {len(taken)} known-taken names for mode '{mode_key}' from {cache.path} (will skip these)")
    return cache, taken

async def stats_ticker(stats: stats_mod.Stats, stop: asyncio.Event) -> None:
    while not stop.is_set():
        stats_mod.print_live(stats)
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.25)
        except asyncio.TimeoutError:
            pass
    stats_mod.print_live(stats)

async def run(settings: config.Settings) -> None:
    stats = stats_mod.Stats(
        total=generator.count_source(
            wordlist_path=settings.wordlist_path,
            length=settings.length,
            charset=settings.charset,
        )
    )

    out_fh, seen = open_output(settings)
    taken_cache_store, taken_cache = load_taken_cache(settings)
    taken_mode_key = _taken_cache_mode_key(settings)
    notifier: notifier_mod.DiscordNotifier | None = None

    def on_result(result: checker.CheckResult) -> None:
        stats.record(result.status)
        if result.status == "available" and result.username not in seen:
            seen.add(result.username)
            out_fh.write(result.username + "\n")
            out_fh.flush()
            os.fsync(out_fh.fileno())
            # Only print the important stuff
            print(f"\n✓ AVAILABLE: {result.username}")
            if notifier is not None:
                notifier.enqueue(result.username)
        elif result.status == "taken" and result.username not in taken_cache:
            taken_cache.add(result.username)
            taken_cache_store.add(taken_mode_key, result.username)

    timeout = aiohttp.ClientTimeout(total=settings.timeout)
    connector = aiohttp.TCPConnector(limit=settings.concurrency)

    stop = asyncio.Event()
    ticker = asyncio.create_task(stats_ticker(stats, stop))

    webhook = settings.resolved_webhook()

    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            if webhook:
                notifier = notifier_mod.DiscordNotifier(
                    session,
                    webhook,
                    batch_size=settings.discord_batch_size,
                    flush_interval=settings.discord_flush_interval,
                )
                notifier.start()
                print("Discord notifications: ENABLED")

            pool = checker.AvailabilityWorkerPool(session, settings)

            pass_num = 0
            while True:
                pass_num += 1
                usernames = generator.build_source(
                    wordlist_path=settings.wordlist_path,
                    length=settings.length,
                    charset=settings.charset,
                    shuffle=settings.shuffle,
                )
                if taken_cache:
                    usernames = (u for u in usernames if u not in taken_cache)
                await pool.run(usernames, on_result)

                if not settings.loop:
                    break
                await asyncio.sleep(settings.loop_pause)

            if notifier is not None:
                await notifier.close()
    finally:
        stop.set()
        await ticker
        out_fh.close()
        taken_cache_store.save()

    stats_mod.print_final(stats)
    print(f"Done. Available names saved to {settings.output_file}")
    _append_run_log(settings, stats)


def _append_run_log(settings: config.Settings, stats: stats_mod.Stats) -> None:
    """Append a one-line summary of this run to runs.log next to the exe.

    Keeps a lightweight history across sessions so a crash or closed terminal
    doesn't lose the final tally - useful for tracking progress on a keyspace
    over multiple runs/days.
    """
    import datetime

    log_path = os.path.join(config.app_dir(), "runs.log")
    line = (
        f"{datetime.datetime.now().isoformat(timespec='seconds')} "
        f"length={settings.length} charset={settings.charset} "
        f"checked={stats.checked} available={stats.available} "
        f"taken={stats.taken} errors={stats.errors} "
        f"elapsed={stats.elapsed:.0f}s rate={stats.per_second:.2f}/s\n"
    )
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        print(f"(couldn't write runs.log: {exc})")

def main(argv: list[str] | None = None) -> None:
    settings, self_test = parse_args(argv)
    setup_logging(settings)

    if self_test:
        print("Running self-test (connectivity + one sample request)...")
        report = preflight.run_preflight(timeout=settings.timeout)
        print(report.message)
        if not report.ok:
            raise SystemExit(1)
        try:
            result = asyncio.run(preflight.sample_check(settings))
            print(f"Sample check for 'abc' -> status={result.status!r} detail={result.detail!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"Sample check failed: {type(exc).__name__}: {exc}")
            raise SystemExit(1)
        raise SystemExit(0)

    mode = f"{settings.length}-char"
    if settings.length == 3 and settings.charset == "letters":
        mode = "3-LETTER (a-z)"
    elif settings.length == 3 and settings.charset == "alnum":
        mode = "3-CHAR (a-z0-9)"
    elif settings.length == 4 and settings.charset == "alnum":
        mode = "4-CHAR (a-z0-9)"

    print("=" * 60)
    print(f"  guns.lol checker - {mode}")
    print(f"  Saving to: {settings.output_file}")
    print(f"  Discord: {'ON' if settings.resolved_webhook() else 'OFF'}")
    print("=" * 60)
    print()

    if not settings.skip_preflight:
        report = preflight.run_preflight(timeout=settings.timeout)
        if not report.ok:
            print(f"Preflight failed: {report.message}")
            raise SystemExit(1)

    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        print("\n\nStopped by user. Results saved.")
        print("(Note: partial-run summary was not added to runs.log since the run didn't finish normally.)")

if __name__ == "__main__":
    main()