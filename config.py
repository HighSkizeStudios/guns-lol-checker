"""
config.py - Clean configuration
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

# Discord webhook - YOUR URL HERE
DISCORD_WEBHOOK_URL: str | None = "https://discord.com/api/webhooks/1538942630841417769/uwoH0vK_cVAGxnTwewjUK8RUFUwjxjwgboxg0vpvTkaSH959NZHBN0nThQuquJe5KABO"

# Endpoint configuration
AVAILABILITY_ENDPOINT: str = "https://guns.lol/api/auth/username/{username}/availability"
RESPONSE_MODE: Literal["GET_JSON", "GET_PROFILE"] = "GET_JSON"
JSON_KEY_EXISTS: str | None = None
JSON_KEY_AVAILABLE: str | None = "available"
PROFILE_AVAILABLE_STATUS: int = 404
PROFILE_TAKEN_STATUS: int = 200

@dataclass
class Settings:
    length: Literal[2, 3, 4] = 3
    charset: Literal["letters", "alnum"] = "letters"
    wordlist_path: str | None = None
    loop: bool = True
    loop_pause: float = 5.0
    shuffle: bool = True
    concurrency: int = 5
    delay: float = 1.0
    jitter: float = 0.25
    timeout: float = 15.0
    max_retries: int = 4
    backoff_base: float = 0.5
    backoff_factor: float = 2.0
    backoff_max: float = 30.0
    user_agent: str = "guns-lol-availability-checker/1.0"
    output_file: str = "available.txt"
    log_file: str | None = None  # Disabled by default
    log_level: str = "WARNING"   # Only show warnings and errors
    extra_headers: dict[str, str] = field(default_factory=dict)
    discord_webhook: str | None = None
    discord_batch_size: int = 1
    discord_flush_interval: float = 1.0
    skip_preflight: bool = False

    def resolved_webhook(self) -> str | None:
        return (
            self.discord_webhook
            or os.environ.get("GUNS_DISCORD_WEBHOOK")
            or DISCORD_WEBHOOK_URL
        )

    def request_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
        }
        headers.update(self.extra_headers)
        return headers

_LENGTH_OUTPUT = {
    2: "2available.txt",
    3: "3lavailable.txt",
    4: "4cavailable.txt",
}

def default_output_for(length: int) -> str:
    return _LENGTH_OUTPUT.get(length, "available.txt")

DEFAULT_SETTINGS = Settings()