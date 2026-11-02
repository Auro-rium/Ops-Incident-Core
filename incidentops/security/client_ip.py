from __future__ import annotations

import ipaddress
import logging
from functools import lru_cache
from typing import Any

from incidentops.config.settings import Settings

logger = logging.getLogger("incidentops.security.client_ip")

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=32)
def parse_trusted_proxies(value: str) -> tuple[_Network, ...]:
    networks: list[_Network] = []
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid TRUSTED_PROXIES entry %r", item)
    return tuple(networks)


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _is_trusted(address: ipaddress.IPv4Address | ipaddress.IPv6Address, networks: tuple[_Network, ...]) -> bool:
    return any(address in network for network in networks)


def client_ip(request: Any, settings: Settings) -> str | None:
    """Return the caller's IP address.

    ``X-Forwarded-For`` is honoured only when the direct peer is in TRUSTED_PROXIES.
    The header is walked right to left and the first hop that is not a trusted
    proxy is the client; entries to its left are client-controlled and ignored.
    """
    if request is None or not getattr(request, "client", None):
        return None
    peer = request.client.host
    networks = parse_trusted_proxies(settings.trusted_proxies)
    peer_address = _parse_ip(peer or "")
    if not networks or peer_address is None or not _is_trusted(peer_address, networks):
        return peer
    forwarded_for = request.headers.get("x-forwarded-for") or ""
    candidate = peer
    for hop in reversed([item for item in forwarded_for.split(",") if item.strip()]):
        address = _parse_ip(hop)
        if address is None:
            break
        candidate = str(address)
        if not _is_trusted(address, networks):
            break
    return candidate
