"""Phase 2 — website gaps. Fills the `website` section of the audit.

Three free signals:
  1. PageSpeed Insights API (free, keyless OK) — Lighthouse performance/SEO/accessibility/
     best-practices scores + Core Web Vitals + mobile-friendliness.
  2. extruct — structured-data (schema.org) detection. Schema massively affects whether AI
     assistants will cite a site, so its absence is a headline gap.
  3. requests + BeautifulSoup — title/meta/H1, robots.txt + sitemap.xml, and a heuristic scan for
     dedicated service / location ("city + service") pages. A plain GET is tried first; if the site
     blocks it (Cloudflare / bot wall / JS-only), we fall back to a headless Chromium render
     (Playwright) so the page reads like a real visitor. `website.fetch_method` records which path won.

Degrades gracefully: no website -> records the gap "no website" (a major finding itself); blocked
even by the browser -> marks analyzable False with an honest note (never fabricates "missing X"
gaps); PageSpeed failure -> leaves scores null but keeps on-page data.

CLI:  python -m tools.analyze_site --website https://example.com --city "Austin, TX"
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from tools import common

PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# schema.org LocalBusiness has dozens of subtypes; an SMB that marks up as AutomotiveBusiness,
# Plumber, Restaurant, etc. DOES have local-business schema. Match the family, not just the base type.
# (Most subtypes also literally end in "Business", which the helper catches generically.)
_LOCAL_BIZ_TYPES = {
    "localbusiness", "autorepair", "autobodyshop", "autodealer", "autopartsstore", "autowash",
    "gasstation", "motorcyclerepair", "electrician", "generalcontractor", "housepainter",
    "locksmith", "movingcompany", "plumber", "roofingcontractor", "professionalservice",
    "legalservice", "accountingservice", "dentist", "physician", "veterinarycare", "hospital",
    "restaurant", "bakery", "cafeorcoffeeshop", "barorpub", "store", "beautysalon", "hairsalon",
    "nailsalon", "dayspa", "emergencyservice", "childcare", "drycleaningorlaundry", "selfstorage",
    "realestateagent", "travelagency", "insuranceagency", "lodgingbusiness",
}


def _is_local_business_type(t: str) -> bool:
    tl = (t or "").lower()
    return tl in _LOCAL_BIZ_TYPES or tl.endswith("business")  # AutomotiveBusiness, HVACBusiness, …


def _pagespeed(url: str) -> dict[str, Any] | None:
    # Keyless PSI is heavily rate-limited (429). Use the dedicated key, or fall back to the
    # Google Maps key — the same Google Cloud project just needs the PageSpeed Insights API enabled.
    key = common.get_key("pagespeed") or common.get_key("google_maps")
    params = [("url", url), ("strategy", "mobile")]
    for cat in ("performance", "seo", "accessibility", "best-practices"):
        params.append(("category", cat))
    if key:
        params.append(("key", key))
    resp = requests.get(PSI_URL, params=params, timeout=90)
    resp.raise_for_status()
    data = resp.json()
    lh = data.get("lighthouseResult", {})
    cats = lh.get("categories", {})
    audits = lh.get("audits", {})

    def score(cat: str) -> int | None:
        s = (cats.get(cat) or {}).get("score")
        return round(s * 100) if isinstance(s, (int, float)) else None

    def disp(audit_key: str) -> str | None:
        return (audits.get(audit_key) or {}).get("displayValue")

    field = (data.get("loadingExperience") or {}).get("metrics") or {}
    inp_field = field.get("INTERACTION_TO_NEXT_PAINT") or {}

    # Lighthouse renamed the viewport audit ("viewport" -> "meta-viewport"); check both, and leave
    # mobile_friendly UNKNOWN (None) rather than False when the audit is absent — otherwise a moved
    # key makes us wrongly claim a site "isn't mobile-friendly".
    vp_score = (audits.get("meta-viewport") or audits.get("viewport") or {}).get("score")

    return {
        "performance": score("performance"),
        "seo": score("seo"),
        "accessibility": score("accessibility"),
        "best_practices": score("best-practices"),
        "mobile_friendly": (vp_score == 1) if vp_score is not None else None,
        "lcp": disp("largest-contentful-paint"),
        "cls": disp("cumulative-layout-shift"),
        "tbt": disp("total-blocking-time"),
        "inp": inp_field.get("percentile"),
    }


def _schema_types(html: str, base_url: str) -> list[str]:
    """Return a flat list of schema.org @type strings found on the page."""
    try:
        import extruct  # imported lazily so the tool loads even if extruct is absent
        data = extruct.extract(html, base_url=base_url, syntaxes=["json-ld", "microdata", "rdfa"])
    except Exception:
        return []

    types: list[str] = []

    def _norm(t: Any) -> None:
        if isinstance(t, str):
            types.append(t.rsplit("/", 1)[-1].rsplit("#", 1)[-1])
        elif isinstance(t, list):
            for x in t:
                _norm(x)

    for item in data.get("json-ld", []) or []:
        _norm(item.get("@type"))
        for g in item.get("@graph", []) or []:
            _norm(g.get("@type"))
    for item in data.get("microdata", []) or []:
        _norm(item.get("type"))
    for item in data.get("rdfa", []) or []:
        _norm(item.get("@type"))

    # de-dupe preserving order
    seen, out = set(), []
    for t in types:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _url_ok(url: str) -> bool:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
        return r.status_code == 200
    except Exception:
        return False


def _fetch_robots_text(url: str) -> str | None:
    """The raw robots.txt body (None when missing / non-200 / unreachable)."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
        return r.text if r.status_code == 200 else None
    except Exception:
        return None


# AI-assistant crawlers that matter for being *cited*. If robots.txt blocks one, that assistant
# literally can't read the site, so it can never quote or recommend the business — a silent ceiling
# on AI visibility that the rest of the audit would never reveal. The search/live-fetch agents
# (OAI-SearchBot, ChatGPT-User, PerplexityBot) matter most; GPTBot/ClaudeBot feed the training index.
_AI_CRAWLERS = [
    ("GPTBot", "ChatGPT (OpenAI index)"),
    ("OAI-SearchBot", "ChatGPT Search"),
    ("ChatGPT-User", "ChatGPT live browsing"),
    ("PerplexityBot", "Perplexity"),
    ("ClaudeBot", "Claude"),
    ("Google-Extended", "Google AI Overviews / Gemini"),
]


def _ai_crawler_access(robots_txt: str | None, root: str) -> dict[str, Any]:
    """Which AI crawlers robots.txt lets read the homepage. No robots.txt = nothing disallowed."""
    names = [b for b, _ in _AI_CRAWLERS]
    if not robots_txt:
        return {"checked": False, "blocked": [], "allowed": names}
    from urllib.robotparser import RobotFileParser
    home = root.rstrip("/") + "/"
    rp = RobotFileParser()
    rp.parse(robots_txt.splitlines())
    blocked, allowed = [], []
    for bot in names:
        (allowed if rp.can_fetch(bot, home) else blocked).append(bot)
    return {"checked": True, "blocked": blocked, "allowed": allowed}


def _looks_blocked(status: int | None, html: str) -> bool:
    """True if the response is a bot wall / JS challenge rather than the real page."""
    if status != 200:
        return True
    body = (html or "").strip()
    if len(body) < 200:
        return True
    low = body[:4000].lower()
    return any(s in low for s in (
        "just a moment", "checking your browser", "cf-browser-verification",
        "enable javascript and cookies", "attention required! | cloudflare"))


def _requests_fetch(url: str, ua: str = UA) -> tuple[int | None, str, str]:
    r = requests.get(url, headers={"User-Agent": ua}, timeout=20, allow_redirects=True)
    return r.status_code, (r.text or ""), r.url


def _browser_fetch(url: str) -> dict[str, Any] | None:
    """Render the page with headless Chromium so JS-gated / Cloudflare-protected sites are readable
    like a real visitor. Returns {status, html, final_url, robots_ok, sitemap_ok}, or None when
    Playwright / its Chromium binary isn't available (caller then degrades gracefully).

    robots.txt and sitemap.xml are fetched through the SAME browser context, so they reuse the
    `cf_clearance` cookie earned by passing the challenge instead of getting blocked again.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception:
                return None  # Chromium binary not installed (`python -m playwright install chromium`)
            try:
                ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
                page = ctx.new_page()
                resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                status = resp.status if resp else None
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass  # long-polling sites never go idle — that's fine, we already have the DOM
                for _ in range(4):  # give a Cloudflare interstitial a few seconds to resolve
                    if "just a moment" not in (page.title() or "").lower():
                        break
                    page.wait_for_timeout(1500)
                html = page.content() or ""
                final_url = page.url
                root = f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}"

                def _sub(path: str) -> tuple[bool, str]:
                    try:
                        resp = ctx.request.get(urljoin(root, path), timeout=10000)
                        return resp.ok, (resp.text() if resp.ok else "")
                    except Exception:
                        return False, ""

                robots_ok, robots_txt = _sub("/robots.txt")
                sitemap_ok, _sm = _sub("/sitemap.xml")
                return {"status": status, "html": html, "final_url": final_url, "robots_ok": robots_ok,
                        "robots_txt": robots_txt, "sitemap_ok": sitemap_ok}
            finally:
                browser.close()
    except Exception:
        return None


def enrich(audit: dict[str, Any]) -> dict[str, Any]:
    site = audit["website"]
    url = site.get("url")
    city = (audit["meta"]["query"].get("city") or "")

    if not url:
        site["reachable"] = False
        site["gaps"] = ["No website found — there's no home base for Google or AI assistants to read about you."]
        return audit

    if not urlparse(url).scheme:
        url = "https://" + url
        site["url"] = url

    # Fetch the homepage. Fast path: a plain HTTP GET. If that's blocked (bot wall / Cloudflare
    # challenge / empty body), fall back to a real headless browser, which runs JS and reads the
    # site like an ordinary visitor. Only the fallback is heavy, so most sites never pay for it.
    method = "requests"
    status: int | None = None
    html = ""
    base = url
    browser_subs: dict[str, Any] | None = None

    try:
        status, html, base = _requests_fetch(url)
    except Exception as e:  # noqa: BLE001
        common.record_error(audit, "analyze_site", f"HTTP fetch failed for {url}: {e}")

    ok = bool(html) and not _looks_blocked(status, html)
    if not ok:
        # Transient bot walls often clear on a second attempt (observed: same site 403'd then
        # served 200 later the same evening). One cheap delayed retry with a fresher UA before
        # paying for the headless browser.
        try:
            time.sleep(3)
            retry_ua = UA.replace("10_15_7", "14_5") if "10_15_7" in UA else UA
            status, html, base = _requests_fetch(url, ua=retry_ua)
            ok = bool(html) and not _looks_blocked(status, html)
        except Exception:  # noqa: BLE001 - retry is best-effort; browser fallback is next
            pass
    if not ok:
        b = _browser_fetch(base or url)
        if b and b.get("html") and not _looks_blocked(b.get("status") or 200, b["html"]):
            method, html, base = "browser", b["html"], b["final_url"]
            status = b.get("status") or 200
            browser_subs = b
            ok = True

    site["http_status"] = status
    site["fetch_method"] = method

    if not ok:
        # Both the plain fetch AND a full headless browser failed (or Playwright is unavailable).
        # We learned NOTHING about the site's schema/pages, so never fabricate "missing X" gaps.
        site["reachable"] = False
        site["analyzable"] = False
        note = (f"We couldn't read your website automatically (HTTP {status}) — it blocked even a "
                f"full headless browser, which usually means an aggressive bot/Cloudflare rule. This "
                f"needs a manual review; it does NOT mean the pages or markup are missing.")
        common.record_error(audit, "analyze_site", note)
        site["gaps"] = [note]
        return audit

    site["reachable"] = True
    site["analyzable"] = True

    soup = BeautifulSoup(html, "lxml")

    # On-page basics.
    site["title"] = (soup.title.string.strip() if soup.title and soup.title.string else None)
    md = soup.find("meta", attrs={"name": "description"})
    site["meta_description"] = md.get("content", "").strip() if md else None
    site["h1"] = [h.get_text(strip=True) for h in soup.find_all("h1")][:5]

    # Structured data.
    schema_types = _schema_types(html, base)
    site["schema_types"] = schema_types
    site["has_localbusiness_schema"] = any(_is_local_business_type(t) for t in schema_types)
    site["has_faq_schema"] = any("FAQPage" in t or t == "Question" for t in schema_types)

    # robots.txt + sitemap.xml. If we rendered via the browser, reuse its results (same Cloudflare
    # clearance) instead of a plain GET that would just get blocked again.
    root = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
    if browser_subs is not None:
        robots_txt = browser_subs.get("robots_txt") or None
        site["has_robots"] = bool(browser_subs.get("robots_ok"))
        site["has_sitemap"] = bool(browser_subs.get("sitemap_ok"))
    else:
        robots_txt = _fetch_robots_text(urljoin(root, "/robots.txt"))
        site["has_robots"] = robots_txt is not None
        site["has_sitemap"] = _url_ok(urljoin(root, "/sitemap.xml"))
    # Does robots.txt lock out the AI assistants' crawlers? A blocked crawler can never cite you —
    # an invisible ceiling on AI visibility, so we surface it as a headline gap.
    site["ai_crawlers"] = _ai_crawler_access(robots_txt, root)

    # Service / location page heuristic over internal links.
    netloc = urlparse(base).netloc
    service_pages, location_pages = set(), set()
    city_token = city.split(",")[0].strip().lower()
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        p = urlparse(href)
        if p.netloc and p.netloc != netloc:
            continue
        path = p.path.lower().rstrip("/")
        if not path or path == "":
            continue
        if "service" in path:
            service_pages.add(path)
        if any(k in path for k in ("location", "areas", "service-area", "cities")) or (city_token and city_token in path):
            location_pages.add(path)
    site["service_pages_found"] = sorted(service_pages)[:15]
    site["location_pages_found"] = sorted(location_pages)[:15]

    # PageSpeed (slowest call last so the rest is captured even if it times out).
    try:
        site["pagespeed"] = _pagespeed(base)
        common.add_cost(audit, "pagespeed", 0.0, "free")
    except Exception as e:  # noqa: BLE001
        common.record_error(audit, "analyze_site", f"PageSpeed failed: {e}")
        site["pagespeed"] = None

    site["gaps"] = _derive_gaps(site)
    return audit


def _derive_gaps(site: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    ps = site.get("pagespeed") or {}
    if not site.get("schema_types"):
        gaps.append("No structured data (schema.org) — AI assistants and Google can't reliably understand your business.")
    elif not site.get("has_localbusiness_schema"):
        gaps.append("Missing LocalBusiness schema — the markup that tells Google/AI your hours, location and services.")
    if not site.get("has_faq_schema"):
        gaps.append("No FAQ schema — FAQ markup is one of the most direct ways to get quoted by ChatGPT/Perplexity.")
    if not site.get("meta_description"):
        gaps.append("Missing meta description — Google and AI snippets fall back to scraping random text.")
    if not site.get("h1"):
        gaps.append("No H1 heading — search engines and AI can't tell what the page is primarily about.")
    if not site.get("service_pages_found"):
        gaps.append("No dedicated service pages — you're not ranking for individual services people search for.")
    if not site.get("location_pages_found"):
        gaps.append("No location / 'service area' pages — you're invisible for 'service near [town]' searches.")
    if not site.get("has_sitemap"):
        gaps.append("No sitemap.xml — search engines have to guess at your pages.")
    blocked = (site.get("ai_crawlers") or {}).get("blocked")
    if blocked:
        gaps.append("Your robots.txt blocks AI assistant crawlers (" + ", ".join(blocked) +
                    ") — these assistants can't read your site, so they can never recommend or cite you.")
    if ps:
        if isinstance(ps.get("performance"), int) and ps["performance"] < 50:
            gaps.append(f"Slow mobile site (PageSpeed {ps['performance']}/100) — most local searches are on phones; slow pages lose customers.")
        if ps.get("mobile_friendly") is False:
            gaps.append("Not mobile-friendly — fails Google's mobile viewport check.")
        if isinstance(ps.get("seo"), int) and ps["seo"] < 80:
            gaps.append(f"Weak technical SEO (Lighthouse SEO {ps['seo']}/100).")
    return gaps


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyze a business website for SEO/AIO gaps.")
    ap.add_argument("--website", required=True)
    ap.add_argument("--city", default="")
    args = ap.parse_args()

    audit = common.new_audit("(site-only)", args.city, args.website)
    enrich(audit)
    print(json.dumps(audit["website"], indent=2))
    if audit["errors"]:
        print("Errors:", audit["errors"])


if __name__ == "__main__":
    main()
