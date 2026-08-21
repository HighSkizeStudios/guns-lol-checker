"""
generator.py
============

Produces the pool of candidate usernames to check.

Two sources are supported:

1. **Generated** — every combination of a given length over a chosen
   character set (letters only, or letters + digits).
2. **Loaded** — read from a wordlist file, one username per line. Anything that
   doesn't match the 2–4 character constraint is filtered out with a warning.

The generator yields lazily (it's a Python generator) so we never have to hold
the entire keyspace in memory. For 4-character alphanumeric names that keyspace
is 36**4 = 1,679,616 combinations — big, but streamed one at a time it costs
almost nothing.
"""

from __future__ import annotations

import itertools
import logging
import random
import string
from typing import Iterator

log = logging.getLogger("generator")

# Character pools we support.
_LETTERS = string.ascii_lowercase          # a-z
_DIGITS = string.digits                     # 0-9

# guns.lol usernames are case-insensitive for availability purposes, so we work
# entirely in lowercase to avoid checking "AB" and "ab" as if they differ.


def charset_pool(charset: str) -> str:
    """Return the pool of characters for a given charset name.

    Parameters
    ----------
    charset:
        "letters" for a-z, or "alnum" for a-z plus 0-9.
    """
    if charset == "letters":
        return _LETTERS
    if charset == "alnum":
        return _LETTERS + _DIGITS
    raise ValueError(f"Unknown charset {charset!r}; expected 'letters' or 'alnum'")


def generate_usernames(
    length: int, charset: str, *, shuffle: bool = False
) -> Iterator[str]:
    """Yield every username of `length` characters over `charset`.

    Parameters
    ----------
    shuffle:
        If False (default) names come out in alphabetical order (aaa, aab, …).
        If True, the whole keyspace is materialised and randomly shuffled first.

    Why shuffle? In alphabetical order the early names (aaa…) are the most
    heavily claimed, so a partial run wastes time in a region where nothing is
    free. Random order samples across the entire keyspace, so you start finding
    available names almost immediately instead of hours in. The trade-off is
    memory: the full list is held at once (fine up to a few million names).

    Example
    -------
    >>> next(generate_usernames(2, "letters"))
    'aa'
    """
    if length not in (2, 3, 4):
        raise ValueError(f"length must be 2, 3, or 4 (got {length})")

    pool = charset_pool(charset)
    total = len(pool) ** length
    log.info(
        "Generating %d candidate usernames (length=%d, charset=%s, shuffle=%s)",
        total, length, charset, shuffle,
    )

    if shuffle:
        # Materialise the full keyspace and shuffle it so we sample the whole
        # space evenly rather than crawling alphabetically from 'aaa'.
        names = ["".join(c) for c in itertools.product(pool, repeat=length)]
        random.shuffle(names)
        yield from names
        return

    # Streaming path (no shuffle): product yields lazily, near-zero memory.
    for combo in itertools.product(pool, repeat=length):
        yield "".join(combo)


def load_usernames(path: str) -> Iterator[str]:
    """Yield usernames from a file, one per line.

    Lines are stripped and lowercased. Blank lines and lines starting with '#'
    (comments) are ignored. Names outside the 2–4 character range are skipped
    with a warning so the wordlist can't silently include invalid candidates.
    """
    log.info("Loading usernames from %s", path)
    kept = 0
    skipped = 0
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            name = raw.strip().lower()
            if not name or name.startswith("#"):
                continue
            if not (2 <= len(name) <= 4):
                log.warning("Skipping %r: not 2-4 characters", name)
                skipped += 1
                continue
            kept += 1
            yield name
    log.info("Loaded %d usernames (%d skipped)", kept, skipped)


def build_source(
    *,
    wordlist_path: str | None,
    length: int,
    charset: str,
    shuffle: bool = False,
) -> Iterator[str]:
    """Choose the appropriate username source based on settings.

    If a wordlist path is provided it takes precedence; otherwise we generate
    the full keyspace for the requested length/charset (optionally shuffled).
    """
    if wordlist_path:
        return load_usernames(wordlist_path)
    return generate_usernames(length, charset, shuffle=shuffle)


def count_source(*, wordlist_path: str | None, length: int, charset: str) -> int | None:
    """Best-effort total count, used only to show progress percentages.

    For generated sets this is exact and cheap to compute. For wordlists we
    return None (unknown up front) rather than reading the file twice.
    """
    if wordlist_path:
        return None
    return len(charset_pool(charset)) ** length
