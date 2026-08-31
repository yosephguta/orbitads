from __future__ import annotations

"""
Photo-proxy policy — which image hosts block Shotstack (so the pipeline re-hosts
their photos through S3). Backed by the `blocked_photo_hosts` table, cached
in-process to avoid a DB hit per job.

Two population paths:
  - runtime:    the pipeline hit an access-denied render and learned the host.
  - config_gen: config generation detected a Cloudflare-fronted photo host.
"""

from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx

_CACHE: set = set()
_LOADED = False

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def host_of(url) -> str:
    try:
        return (urlparse(str(url)).hostname or "").lower()
    except Exception:
        return ""


async def ensure_loaded() -> None:
    """Lazy-load the blocked-host set from the DB once per process."""
    global _LOADED
    if _LOADED:
        return
    try:
        from sqlmodel import select
        from app.core.database import AsyncSessionLocal
        from app.models.blocked_photo_host import BlockedPhotoHost
        async with AsyncSessionLocal() as session:
            rows = (await session.exec(select(BlockedPhotoHost.hostname))).all()
            _CACHE.update(h for h in rows if h)
        _LOADED = True
    except Exception as e:
        # Never block a render on this — fall back to the retry-on-failure net.
        print(f"[photo_proxy] load failed: {e}")


def is_blocked(hostname: str) -> bool:
    return bool(hostname) and hostname in _CACHE


def any_blocked(photo_urls) -> bool:
    return any(is_blocked(host_of(u)) for u in photo_urls)


async def add_blocked_hosts(hostnames, source: str = "runtime") -> None:
    """Persist newly-discovered blocked hosts (idempotent) and update the cache."""
    hosts = {h for h in (hostnames or []) if h and h not in _CACHE}
    _CACHE.update(h for h in (hostnames or []) if h)  # cache immediately
    if not hosts:
        return
    try:
        from app.core.database import AsyncSessionLocal
        from app.models.blocked_photo_host import BlockedPhotoHost
        async with AsyncSessionLocal() as session:
            for h in hosts:
                session.add(BlockedPhotoHost(hostname=h, source=source, created_at=datetime.utcnow()))
                try:
                    await session.commit()
                except Exception:
                    await session.rollback()  # unique conflict — already stored
    except Exception as e:
        print(f"[photo_proxy] persist failed: {e}")


async def detect_cloudflare(url: str) -> bool:
    """True if the URL's host is Cloudflare-fronted (cf-ray / server: cloudflare).
    Our server isn't blocked, so the CF headers are visible even though Shotstack
    would be denied. Best-effort — returns False on any error."""
    if not url or not str(url).startswith("http"):
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                     headers={"User-Agent": _UA}) as client:
            try:
                resp = await client.head(url)
            except Exception:
                resp = await client.get(url)  # some hosts reject HEAD
            h = resp.headers
            server = (h.get("server") or "").lower()
            return "cf-ray" in h or "cloudflare" in server
    except Exception:
        return False


async def detect_and_flag_from_html(photos_html: Optional[str]) -> Optional[str]:
    """
    From a pasted photos fragment, find the first real image URL, check if its
    host is Cloudflare-fronted, and if so persist it as a blocked host
    (source=config_gen). Returns the flagged hostname or None. Fire-and-forget.
    """
    if not photos_html:
        return None
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(photos_html, "html.parser")
        url = None
        for img in soup.find_all("img"):
            cand = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if cand and str(cand).startswith("http"):
                url = cand
                break
        if not url:
            return None
        if await detect_cloudflare(url):
            host = host_of(url)
            if host:
                await add_blocked_hosts([host], source="config_gen")
                return host
    except Exception as e:
        print(f"[photo_proxy] detect_and_flag failed: {e}")
    return None
