"""
stats.py
========

Live run statistics and a tiny single-line console dashboard.

Kept separate from main.py so the counting/formatting logic is easy to test and
reuse. The :class:`Stats` object is updated from the result callback and rendered
periodically by a background task in main.py.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field


@dataclass
class Stats:
    """Mutable counters for a checking run."""

    total: int | None = None        # total to check (None if unknown/wordlist)
    checked: int = 0
    available: int = 0
    taken: int = 0
    errors: int = 0
    start_time: float = field(default_factory=time.monotonic)

    def record(self, status: str) -> None:
        """Update counters for one completed check."""
        self.checked += 1
        if status == "available":
            self.available += 1
        elif status == "taken":
            self.taken += 1
        else:  # "error"
            self.errors += 1

    @property
    def elapsed(self) -> float:
        return max(1e-9, time.monotonic() - self.start_time)

    @property
    def per_second(self) -> float:
        """Checks per second since the run started."""
        return self.checked / self.elapsed

    def render_line(self) -> str:
        """Build a compact one-line status string."""
        parts = [
            f"Checked: {self.checked}",
            f"Available: {self.available}",
            f"Taken: {self.taken}",
            f"Errors: {self.errors}",
            f"{self.per_second:.1f}/s",
        ]
        if self.total:
            pct = 100.0 * self.checked / self.total
            parts.insert(0, f"{pct:5.1f}%")
            parts.append(f"({self.checked}/{self.total})")
        return " | ".join(parts)


def print_live(stats: Stats) -> None:
    """Overwrite the current console line with the latest stats.

    Uses a carriage return so the dashboard stays on a single line instead of
    scrolling. The trailing spaces clear any leftover characters from a longer
    previous line.
    """
    line = stats.render_line()
    sys.stdout.write("\r" + line + "   ")
    sys.stdout.flush()


def print_final(stats: Stats) -> None:
    """Print a tidy summary once the run finishes."""
    # Newline first to move off the live line.
    print()
    print("=" * 60)
    print("Run complete")
    print(f"  Checked   : {stats.checked}")
    print(f"  Available : {stats.available}")
    print(f"  Taken     : {stats.taken}")
    print(f"  Errors    : {stats.errors}")
    print(f"  Elapsed   : {stats.elapsed:.1f}s")
    print(f"  Rate      : {stats.per_second:.2f} checks/sec")
    print("=" * 60)
