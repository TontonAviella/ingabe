"""Trust policy for unauthenticated remote COG URLs."""

from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import urlsplit, urlunsplit


REMOTE_COG_ALLOWED_HOSTS_ENV = "REMOTE_COG_ALLOWED_HOSTS"

DEFAULT_REMOTE_COG_ALLOWED_HOSTS = frozenset(
    {
        "deafrica-input-datasets.s3.af-south-1.amazonaws.com",
        "eodata.dataspace.copernicus.eu",
        "io-10m-annual-lulc.s3.us-west-2.amazonaws.com",
        "isdasoil.s3.amazonaws.com",
        "sentinel2l2a01.blob.core.windows.net",
        "storage.googleapis.com",
        "sentinel-cogs.s3.us-west-2.amazonaws.com",
    }
)
_LOCAL_LAYER_REF_RE = re.compile(r"^mundi-layer:[A-Za-z0-9_-]{1,80}$")

_HOST_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_UNSAFE_URL_CHARACTER_RE = re.compile(r"[\x00-\x20\\]")


class RemoteCogUrlError(ValueError):
    """Raised when a URL is outside the remote COG trust policy."""


def remote_cog_allowed_hosts() -> frozenset[str]:
    """Return built-in hosts plus exact operator-configured host additions."""
    configured_hosts: set[str] = set()
    raw_hosts = os.environ.get(REMOTE_COG_ALLOWED_HOSTS_ENV, "")
    for raw_host in raw_hosts.split(","):
        host = raw_host.strip().lower()
        if not host:
            continue
        if not _is_public_hostname(host):
            raise RemoteCogUrlError(
                f"Invalid {REMOTE_COG_ALLOWED_HOSTS_ENV} entry: {raw_host.strip()}"
            )
        configured_hosts.add(host)
    return DEFAULT_REMOTE_COG_ALLOWED_HOSTS | configured_hosts


def validate_remote_cog_url(url: str) -> str:
    """Validate and canonicalize an HTTPS URL against the exact COG allowlist.

    This deliberately does not make a one-time DNS trust decision. Only hostnames
    explicitly trusted by code or deployment configuration can reach GDAL.
    """
    if not isinstance(url, str) or not url or url != url.strip():
        raise RemoteCogUrlError("Remote COG URL must be a non-empty string")
    if _LOCAL_LAYER_REF_RE.fullmatch(url):
        return url
    if _UNSAFE_URL_CHARACTER_RE.search(url):
        raise RemoteCogUrlError("Remote COG URL contains unsafe characters")

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RemoteCogUrlError("Remote COG URL is malformed") from exc

    if parsed.scheme.lower() != "https":
        raise RemoteCogUrlError("Remote COG URL must use HTTPS")
    if not hostname:
        raise RemoteCogUrlError("Remote COG URL is missing a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise RemoteCogUrlError("Remote COG URL must not contain credentials")
    if parsed.fragment:
        raise RemoteCogUrlError("Remote COG URL must not contain a fragment")
    if port not in (None, 443):
        raise RemoteCogUrlError("Remote COG URL must use the default HTTPS port")

    hostname = hostname.lower()
    if not _is_public_hostname(hostname):
        raise RemoteCogUrlError(f"Remote COG hostname is invalid: {hostname}")
    if not _is_trusted_provider_host(hostname):
        raise RemoteCogUrlError(f"Remote COG host is not trusted: {hostname}")

    return urlunsplit(("https", hostname, parsed.path, parsed.query, ""))


def _is_trusted_provider_host(hostname: str) -> bool:
    return hostname in remote_cog_allowed_hosts()


def _is_public_hostname(hostname: str) -> bool:
    if not _HOST_RE.fullmatch(hostname):
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return False
