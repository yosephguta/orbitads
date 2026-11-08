from __future__ import annotations

"""
Config access resolution — `allowed_config_domains` (+ the dealership's required
taglines) for a user, computed on read.

Extracted verbatim from auth.py's /auth/me block so the same logic can be reused
by the dealership-inventory sync domain check (and anywhere else). Output is
identical to the previous inline computation — /auth/me is unchanged.

DEALER TAKES PRECEDENCE: if the user's dealership has an ACTIVE config
(Dealership.platform_id → active DealerPlatform), they inherit ONLY that config's
domain(s) (source_url + dealer_platform_domains), ignoring their own
dealership_url; otherwise they fall back to their own dealership_url. cars.com /
cargurus are universal (handled in the extension, not gated by this list).
"""

from sqlmodel import select

from app.models.dealership import Dealership
from app.models.dealer_platform import DealerPlatform
from app.models.dealer_platform_domain import DealerPlatformDomain


def _bare_domain(url):
    """Lowercase bare host from a URL/domain (no scheme/www/path/query)."""
    if not url:
        return None
    d = str(url).replace("https://", "").replace("http://", "")
    if d.startswith("www."):
        d = d[4:]
    d = d.split("/")[0].split("?")[0].strip().lower()
    return d or None


async def get_config_access(user, session) -> dict:
    """
    Returns {allowed_config_domains, required_tagline, required_tagline_es}.

    Mirrors the former inline logic in /auth/me exactly (same ordering, same
    fallback), so moving it here keeps that endpoint byte-for-byte identical.
    """
    allowed_config_domains = []
    required_tagline = None
    required_tagline_es = None

    if user.dealership_id:
        dealership = await session.get(Dealership, user.dealership_id)
        if dealership:
            required_tagline = dealership.required_tagline
            required_tagline_es = dealership.required_tagline_es
            if dealership.platform_id:
                platform = await session.get(DealerPlatform, dealership.platform_id)
                if platform and platform.status == "active":
                    doms = set()
                    d = _bare_domain(platform.source_url)
                    if d:
                        doms.add(d)
                    rows = (await session.exec(
                        select(DealerPlatformDomain.domain).where(
                            DealerPlatformDomain.platform_id == platform.id
                        )
                    )).all()
                    doms.update(x for x in rows if x)
                    allowed_config_domains = sorted(doms)

    if not allowed_config_domains and user.dealership_url:
        d = _bare_domain(user.dealership_url)
        if d:
            allowed_config_domains = [d]

    return {
        "allowed_config_domains": allowed_config_domains,
        "required_tagline": required_tagline,
        "required_tagline_es": required_tagline_es,
    }
