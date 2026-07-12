"""Web research tools for the enrichment agent.

ADK function tools: plain functions with type hints + a docstring, from which ADK
builds the function-calling schema. `fetch_page` returns not just text but the
page's internal links and images (src + alt) — so the agent can find client/case
sub-pages and read logo filenames. `identify_logos` reads company names off logo
images (multimodal) when the filename/alt don't reveal them.
"""

import asyncio
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from google import genai
from google.genai import types
from pydantic import BaseModel

from app import budget
from app.config import settings
from app.genai_retry import generate_with_retry
from app.graph import cache
from app.graph.driver import get_driver
from app.tools.encoding import response_text
from app.tools.social import find_social_links

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NebulaResearchBot/0.1)"}
_MAX_LINKS = 60
_MAX_IMAGES = 60
_MAX_LOGO_IMAGES = 16


def web_search(query: str) -> dict:
    """Search the web for a query and return the top results.

    Returns up to 6 results, each with a title, url, and snippet. Use this to find
    a company's HQ, headcount, founding year, funding, partners, clients, or
    leadership when they aren't on the company's own site.
    """
    budget.charge_search()  # charge the active per-run budget; no-op if unbudgeted
    with DDGS() as ddgs:
        hits = ddgs.text(query, max_results=6)
    return {
        "results": [
            {"title": h.get("title"), "url": h.get("href"), "snippet": h.get("body")} for h in hits
        ]
    }


def _fetch_page_live(url: str) -> dict:
    """Blocking fetch + parse of one page (run off-loop via a thread)."""
    try:
        resp = requests.get(url, timeout=15, headers=_HEADERS)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — hand any fetch error back to the model
        return {"url": url, "error": str(exc)}

    # Decode UTF-8-safely: a charset-less body must not fall back to ISO-8859-1 (#89).
    html = response_text(resp)
    soup = BeautifulSoup(html, "lxml")
    domain = urlparse(url).netloc

    links, seen_l = [], set()
    for a in soup.find_all("a", href=True):
        abs_url = urljoin(url, a["href"]).split("#")[0]
        if urlparse(abs_url).netloc == domain and abs_url not in seen_l:
            seen_l.add(abs_url)
            links.append({"url": abs_url, "text": a.get_text(" ", strip=True)[:80]})
            if len(links) >= _MAX_LINKS:
                break

    images, seen_i = [], set()
    for im in soup.find_all("img"):
        src = im.get("src") or im.get("data-src") or ""
        if not src:
            continue
        abs_src = urljoin(url, src)
        if abs_src not in seen_i:
            seen_i.add(abs_src)
            images.append({"src": abs_src, "alt": (im.get("alt") or "").strip()[:80]})
            if len(images) >= _MAX_IMAGES:
                break

    # The company's own social/profile links (LinkedIn etc.) — found from the raw
    # HTML because the same-domain filter above drops these external hrefs.
    social = find_social_links(html)

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ").split())
    return {"url": url, "text": text[:5000], "links": links, "images": images, "social": social}


async def fetch_page(url: str) -> dict:
    """Fetch a web page and return its readable text plus its internal links, images
    (src + alt), and the company's social/profile URLs. Use `links` to find relevant
    sub-pages, `images` when a page shows information as logos, and `social` for the
    company's own LinkedIn/Twitter/etc. (prefer social.linkedin over a web_search
    result). To gather a company's client list, prefer the find_clients tool over
    crawling by hand. Results are cached, so re-reading a page is cheap.

    Returns {url, text (~5000 chars), links:[{url,text}], images:[{src,alt}],
    social:{platform:url}} on success, or {url, error} on failure.
    """
    driver = get_driver()
    cached = await cache.get_cached_page(driver, url)
    if cached is not None:
        return cached
    # Charge the active per-run budget only for a real network fetch — a cache hit
    # is the cheap path the cache exists to provide, so it costs nothing. No-op
    # (unlimited) when no budget is installed on the context.
    budget.charge_page()
    page = await asyncio.to_thread(_fetch_page_live, url)
    if "error" not in page:
        await cache.store_page(driver, page)
    return page


class _LogoNames(BaseModel):
    companies: list[str]


# Gemini's image API accepts only these raster types; SVG/GIF/ICO give a 400.
_GEMINI_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}


def _gemini_image_mime(content_type: str, data: bytes) -> str | None:
    """The MIME to hand Gemini, or None if it can't accept this image (e.g. SVG)."""
    mime = content_type.split(";")[0].strip().lower()
    if mime not in _GEMINI_IMAGE_MIMES:
        return None
    if data[:1] == b"<":  # SVG/XML/HTML mislabeled as a raster image
        return None
    return mime


def _download_image(image_url: str) -> tuple[bytes, str] | None:
    try:
        resp = requests.get(image_url, timeout=12, headers=_HEADERS)
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        return None
    mime = _gemini_image_mime(resp.headers.get("content-type", ""), resp.content)
    if mime is None:
        return None
    return resp.content, mime


async def identify_logos(image_urls: list[str]) -> dict:
    """Look at logo images and identify the company/brand in each. Use for client
    logos whose company name isn't clear from the filename or alt text. Pass the
    image src URLs (up to ~16 are used). Returns {"companies": [names]}."""
    parts: list[types.Part] = [
        types.Part(
            text=(
                "Each image is a logo from a company's website. Return the names of "
                "the actual client/customer ORGANISATIONS shown. Skip: certification "
                "or compliance badges (e.g. Living Wage, GDPR, ISO, Cyber Essentials, "
                "Armed Forces Covenant, B-Corp, Disability Confident), award badges, "
                "social-media and generic icons, and any logo you cannot confidently "
                "identify. Return the list of organisation names."
            )
        )
    ]
    for image_url in image_urls[:_MAX_LOGO_IMAGES]:
        downloaded = await asyncio.to_thread(_download_image, image_url)
        if downloaded:
            data, mime = downloaded
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))

    if len(parts) == 1:  # nothing downloaded
        return {"companies": []}

    resp = await generate_with_retry(
        genai.Client(),
        model=settings.gemini_model,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_LogoNames,
            temperature=0,
        ),
    )
    parsed = resp.parsed
    return {"companies": parsed.companies if isinstance(parsed, _LogoNames) else []}


_CLIENT_PAGE_KEYWORDS = (
    "client",
    "customer",
    "who-we-have-helped",
    "case-stud",
    "case_stud",
    "our-work",
    "portfolio",
    "helped",
    "success-stor",
)


def _looks_like_client_page(link: dict) -> bool:
    hay = (link.get("url", "") + " " + link.get("text", "")).lower()
    return any(k in hay for k in _CLIENT_PAGE_KEYWORDS)


def _dedup_names(names: list[str]) -> list[str]:
    out, seen = [], set()
    for name in names:
        name = name.strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


# Certification / award / compliance badges that show up as logos but aren't clients.
_BADGE_TERMS = (
    "gdpr",
    "iso ",
    "iso9001",
    "iso 27001",
    "cyber essentials",
    "living wage",
    "armed forces covenant",
    "b-corp",
    "bcorp",
    "disability confident",
    "great place to work",
    "investors in people",
)


# Logo src fragments that indicate a partner/badge/award, not a client.
_NON_CLIENT_LOGO_SRC = (
    "partner",
    "award",
    "badge",
    "accredit",
    "covenant",
    "gdpr",
    "lwe-",
    "living-wage",
    "great-place",
    "cyber-essentials",
    "iso-",
)
_MAX_TOTAL_LOGOS = 32  # ~2 vision batches — bounds latency/rate-limit pressure


async def _extract_clients_from_text(text: str) -> list[str]:
    """Pull client/customer organisation names out of client-page text (case
    studies etc.) — complements the logo reading."""
    if not text.strip():
        return []
    prompt = (
        "The text below is from a company's client / case-study / 'who we've "
        "helped' pages. List the CLIENT or CUSTOMER organisations mentioned — the "
        "companies and public bodies this firm has worked for. Exclude the firm "
        "itself, technology vendors/partners, certifications, and generic terms.\n\n" + text
    )
    resp = await generate_with_retry(
        genai.Client(),
        model=settings.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_LogoNames,
            temperature=0,
        ),
    )
    parsed = resp.parsed
    return parsed.companies if isinstance(parsed, _LogoNames) else []


def _is_noise(name: str, brand: str) -> bool:
    low = name.lower().strip()
    if not low:
        return True
    if brand and brand in low.replace(" ", ""):  # the company's own logo
        return True
    return any(term in low for term in _BADGE_TERMS)


async def find_clients(website: str) -> dict:
    """Discover a company's CLIENTS/CUSTOMERS by crawling its site deterministically:
    it finds client / "who we've helped" / case-study pages and their sub-pages,
    collects the client LOGOS, and reads the company names off them with vision.
    Prefer this over fetching pages yourself when you need the client list — pass
    the company's website. Returns {"clients": [names], "pages_crawled": [...]}.
    """
    start = website if website.startswith("http") else "https://" + website
    domain = cache.domain_of(start)

    # Cached client list for this company? (refresh via POST /cache/refresh.)
    cached_clients = await cache.get_cached_clients(get_driver(), domain)
    if cached_clients is not None:
        return {"clients": cached_clients, "pages_crawled": [], "cached": True}

    home = await fetch_page(start)
    if "error" in home:
        return {"clients": [], "error": home["error"], "pages_crawled": []}

    # Homepage + candidate client pages (and one level of their sub-pages).
    client_pages = _dedup_names(
        link["url"] for link in home.get("links", []) if _looks_like_client_page(link)
    )[:5]
    to_crawl = [start] + client_pages
    crawled: list[str] = []
    logo_srcs: list[str] = []
    alt_names: list[str] = []
    page_texts: list[str] = []

    def collect(page: dict) -> None:
        for im in page.get("images", []):
            src = im.get("src", "")
            low = src.lower()
            # Logo-ish image, but not a partner/certification/award badge.
            if "logo" in low and not any(bad in low for bad in _NON_CLIENT_LOGO_SRC):
                logo_srcs.append(src)
                alt = im.get("alt", "").strip()
                if alt and len(alt.split()) <= 5:
                    alt_names.append(alt)

    collect(home)
    crawled.append(start)
    for url in to_crawl[1:]:
        page = await fetch_page(url)
        if "error" in page:
            continue
        crawled.append(url)
        collect(page)
        page_texts.append(page.get("text", ""))
        subs = [
            link["url"]
            for link in page.get("links", [])
            if link["url"].startswith(url.rstrip("/") + "/") and link["url"] != url
        ][:6]
        for sub in subs:
            sub_page = await fetch_page(sub)
            if "error" not in sub_page:
                crawled.append(sub)
                collect(sub_page)
                page_texts.append(sub_page.get("text", ""))

    # Read the logos with vision, in batches (cap total to bound cost).
    logo_srcs = _dedup_names(logo_srcs)[:_MAX_TOTAL_LOGOS]
    companies: list[str] = []
    for i in range(0, len(logo_srcs), _MAX_LOGO_IMAGES):
        result = await identify_logos(logo_srcs[i : i + _MAX_LOGO_IMAGES])
        companies.extend(result["companies"])

    # Also mine the page text for clients named in case studies (not just logos).
    text_clients = await _extract_clients_from_text(" ".join(page_texts)[:16000])

    brand = domain.split(".")[0]
    clients = [
        n for n in _dedup_names(companies + alt_names + text_clients) if not _is_noise(n, brand)
    ]
    await cache.store_clients(get_driver(), domain, clients)
    return {
        "clients": clients,
        "pages_crawled": crawled[:12],
        "logos_read": len(logo_srcs),
    }
