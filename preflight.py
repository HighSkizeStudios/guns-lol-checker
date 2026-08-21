"""
preflight.py
============

Connectivity sanity checks that run *before* a checking session so problems are
caught up front instead of after N failed requests.

Motivating case
---------------
On some networks a DNS/content filter (router parental controls, Pi-hole,
school/work policy) blackholes a domain by resolving it to a private/LAN address
such as ``192.168.x.x``. The TCP connection is then refused, and every check
turns into an "error". That is a *network* problem, not a bug in this tool — this
module detects exactly that situation and explains it clearly.

What it checks
--------------
1. **DNS** — does the endpoint's host resolve at all, and to what IP(s)?
2. **Sanity of the IP** — is it a *public* address, or a private/loopback/
   link-local/reserved one (a strong sign the domain is being blocked/hijacked)?
3. **TCP** — can we actually open a socket to host:port?
4. (Optional) **Live sample** — issue one real availability request and show how
   it was interpreted, so you can confirm the endpoint/parser before a big run.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp

import checker
import config

log = logging.getLogger("preflight")


@dataclass
class PreflightReport:
    """Structured result of the connectivity checks."""

    host: str
    port: int
    resolved_ips: list[str]
    tcp_ok: bool
    private_ip: bool          # any resolved IP is non-public?
    dns_ok: bool
    message: str              # human-readable summary of the main finding

    @property
    def ok(self) -> bool:
        """True only if we resolved to a public IP AND could open a socket."""
        return self.dns_ok and self.tcp_ok and not self.private_ip


def _endpoint_host_port() -> tuple[str, int]:
    """Extract (host, port) from the configured endpoint URL.

    We format the endpoint with a throwaway username first so a ``{username}``
    placeholder inside the host (unusual, but possible) doesn't break parsing.
    """
    url = config.AVAILABILITY_ENDPOINT.format(username="preflight")
    parts = urlsplit(url)
    host = parts.hostname or ""
    # Default port by scheme if not explicit.
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return host, port


def _is_public_ip(ip: str) -> bool:
    """Return True if `ip` is a normal, routable public address."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # Anything private/loopback/link-local/reserved/multicast is "not public"
    # and, for our purposes, a red flag that the domain is being redirected.
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def run_preflight(timeout: float = 6.0) -> PreflightReport:
    """Perform DNS + TCP checks against the configured endpoint host.

    This is synchronous on purpose — it's a quick one-shot check and keeping it
    plain makes it easy to reason about.
    """
    host, port = _endpoint_host_port()
    log.debug("Preflight target: %s:%d", host, port)

    # --- 1. DNS resolution ------------------------------------------------
    resolved: list[str] = []
    dns_ok = False
    try:
        # getaddrinfo returns all A/AAAA records; dedupe while preserving order.
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        for info in infos:
            ip = info[4][0]
            if ip not in resolved:
                resolved.append(ip)
        dns_ok = bool(resolved)
    except socket.gaierror as exc:
        return PreflightReport(
            host=host, port=port, resolved_ips=[], tcp_ok=False,
            private_ip=False, dns_ok=False,
            message=f"DNS resolution failed for {host!r}: {exc}",
        )

    # --- 2. Public vs private -------------------------------------------
    private_ip = any(not _is_public_ip(ip) for ip in resolved)

    # --- 3. TCP connect --------------------------------------------------
    tcp_ok = False
    tcp_error = ""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        tcp_ok = True
    except OSError as exc:
        tcp_error = f"{type(exc).__name__}: {exc}"

    # --- 4. Compose a helpful message -----------------------------------
    if private_ip:
        message = (
            f"{host} resolves to a NON-PUBLIC address ({', '.join(resolved)}). "
            "This almost always means the domain is being blocked/hijacked by a "
            "DNS or content filter on your network (router parental controls, "
            "Pi-hole/AdGuard, or a school/work policy) - traffic never reaches "
            "the real server. Fix the block on a network you control, or verify "
            "with:  nslookup " + host + " 1.1.1.1"
        )
    elif not tcp_ok:
        message = (
            f"{host} resolves to {', '.join(resolved)} but the TCP connection to "
            f"port {port} failed ({tcp_error}). The host may be down, firewalled, "
            "or the port/endpoint may be wrong."
        )
    else:
        message = (
            f"{host} resolves to {', '.join(resolved)} and port {port} is "
            "reachable. Connectivity looks good."
        )

    return PreflightReport(
        host=host, port=port, resolved_ips=resolved, tcp_ok=tcp_ok,
        private_ip=private_ip, dns_ok=dns_ok, message=message,
    )


async def sample_check(settings: config.Settings, username: str = "abc") -> checker.CheckResult:
    """Issue ONE real availability request and return how it was interpreted.

    Useful in --self-test to confirm the endpoint URL and response parser are
    correct before committing to a long run. Uses the same code path as a normal
    check, so what you see here is what you'll get at scale.
    """
    timeout = aiohttp.ClientTimeout(total=settings.timeout)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        return await checker.check_username(session, settings, username)
