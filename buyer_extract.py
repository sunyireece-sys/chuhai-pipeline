"""
Extract candidate B2B buyer companies from Serper organic search results.

Pure heuristic, no LLM. Goal is "good enough first pass" — the user can hand-clean
the resulting xlsx before running step3.

Pipeline (per organic result):
  1. Drop blacklisted domains (amazon, alibaba, linkedin, ...)
  2. Try to find a brand/company-like fragment in the SERP title
  3. If the title mostly looks like a product page, fall back to the root domain
  4. Deduplicate across all results by root domain
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlparse

# Domains we never want as B2B buyer leads.
BLACKLIST_DOMAINS = {
    "amazon", "ebay", "alibaba", "aliexpress", "etsy", "walmart", "target",
    "shopee", "made-in-china", "tradewheel", "go4worldbusiness", "exporthub",
    "tradeford", "ecplaza", "dhgate", "globalsources", "indiamart",
    "europages", "news-medical", "pmc",
    "linkedin", "facebook", "twitter", "instagram", "youtube", "tiktok",
    "wikipedia", "wikidata", "crunchbase", "pinterest", "reddit",
    "indeed", "glassdoor", "yelp", "tripadvisor",
    "google", "bing", "duckduckgo",
    "youtube", "vimeo",
    "medium", "substack", "wordpress", "blogspot",
    "iherb", "vitacost",  # consumer retailers
}

# If the title fragment matches one of these (case-insensitive), drop the row.
TITLE_BLACKLIST_PHRASES = {
    "home", "about", "about us", "products", "product", "contact",
    "contact us", "wholesale", "shop", "store", "blog", "news",
    "login", "sign in", "register", "cart", "search results",
    "page not found", "404",
}

# Splitters used to chop a SERP title into candidate name + tail.
TITLE_SPLITTERS = re.compile(r"\s+[-|–—:·•·]\s+")

# Suffixes we strip from names.
NAME_SUFFIX_NOISE = re.compile(
    r"\s*[-|–—].*$",  # any trailing tagline
    re.IGNORECASE,
)

COMPANY_SUFFIX_HINTS = {
    "co", "company", "inc", "inc.", "corp", "corp.", "corporation", "llc",
    "ltd", "ltd.", "limited", "gmbh", "bv", "b.v.", "ag", "sa", "srl",
    "plc", "group", "foods", "foods.", "herb", "herbs", "trading",
}

PRODUCT_TITLE_HINTS = {
    "organic", "bulk", "berries", "berry", "powder", "extract", "wholesale",
    "private", "label", "certified", "premium", "conventional", "dried",
    "raw", "whole", "supplier", "distributor", "buy", "shop", "sale",
    "antioxidant", "vitamin", "packed",
}

GENERIC_SUBDOMAIN_LABELS = {
    "www", "m", "en", "de", "fr", "uk", "us", "store", "shop", "tl",
}

GENERIC_BAD_NAMES = {
    "store", "shop", "us", "uk", "de", "fr", "pmc",
}


@dataclass
class CompanyCandidate:
    name: str
    domain: str
    url: str
    country_display: str
    score: int = 0
    queries: list[str] = field(default_factory=list)
    modifiers: list[str] = field(default_factory=list)


def extract_root_domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if not netloc:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def domain_is_blacklisted(domain: str) -> bool:
    if not domain:
        return True
    parts = domain.split(".")
    for part in parts:
        if part in BLACKLIST_DOMAINS:
            return True
    return False


def extract_company_name(title: str, domain: str) -> str:
    if not title:
        return ""

    domain_tokens = _domain_tokens(domain)
    candidates = [_clean_title_fragment(part) for part in TITLE_SPLITTERS.split(title)]
    candidates.extend(_extract_brand_phrases(title))
    candidates = [part for part in candidates if part]
    if not candidates:
        return ""

    scored = sorted(
        ((_score_title_fragment(part, domain_tokens), part) for part in candidates),
        reverse=True,
    )
    best_score, best = scored[0]
    if best_score < 2:
        return ""
    return best


def domain_to_fallback_name(domain: str) -> str:
    """When title parsing fails, use the second-level domain as a name."""
    if not domain:
        return ""
    base = _preferred_domain_label(domain)
    if not base:
        return ""
    pretty = base.replace("-", " ").replace("_", " ").strip()
    if pretty.lower().startswith("shop") and len(pretty) > 8:
        pretty = pretty[4:].strip()
    if pretty.lower().startswith("store") and len(pretty) > 9:
        pretty = pretty[5:].strip()
    return pretty.title()


def _clean_title_fragment(fragment: str) -> str:
    fragment = NAME_SUFFIX_NOISE.sub("", fragment).strip()
    fragment = fragment.strip("·•|- ").strip()
    if not fragment:
        return ""
    if fragment.lower() in TITLE_BLACKLIST_PHRASES:
        return ""
    if len(fragment) < 3 or len(fragment) > 80:
        return ""
    return fragment


def _normalize_token(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _preferred_domain_label(domain: str) -> str:
    labels = [label for label in domain.split(".") if label]
    for label in labels:
        if label not in GENERIC_SUBDOMAIN_LABELS:
            return label
    return labels[0] if labels else ""


def _domain_tokens(domain: str) -> set[str]:
    if not domain:
        return set()
    base = _preferred_domain_label(domain)
    raw_tokens = re.split(r"[^a-z0-9]+", base.lower())
    tokens = {_normalize_token(token) for token in raw_tokens if token}
    compact = _normalize_token(base)
    if compact:
        tokens.add(compact)
    return {token for token in tokens if len(token) >= 3}


def _extract_brand_phrases(title: str) -> list[str]:
    candidates: list[str] = []

    from_match = re.search(r"\bfrom\s+([A-Z][A-Za-z'&.\s]+)$", title)
    if from_match:
        candidates.append(from_match.group(1).strip())

    trailing_match = re.search(r"\s[-|–—:]\s([A-Z][A-Za-z'&.\s]+)$", title)
    if trailing_match:
        candidates.append(trailing_match.group(1).strip())

    return [_clean_title_fragment(candidate) for candidate in candidates]


def _score_title_fragment(fragment: str, domain_tokens: set[str]) -> int:
    words = re.findall(r"[a-z0-9]+", fragment.lower())
    normalized_words = {_normalize_token(word) for word in words if word}
    normalized_fragment = _normalize_token(fragment)
    score = 0

    if fragment.lower() in TITLE_BLACKLIST_PHRASES:
        return -10

    if domain_tokens and normalized_fragment in domain_tokens:
        score += 8

    domain_matches = sum(1 for token in normalized_words if token in domain_tokens)
    score += domain_matches * 3

    if any(word in COMPANY_SUFFIX_HINTS for word in words):
        score += 3

    product_hits = sum(1 for word in words if word in PRODUCT_TITLE_HINTS)
    score -= product_hits * 2

    title_case_words = re.findall(r"[A-Z][A-Za-z'&.]*", fragment)
    if 1 < len(title_case_words) <= 4 and product_hits == 0:
        score += 2

    if len(words) <= 3:
        score += 1
    if len(words) >= 6:
        score -= 2

    return score


@dataclass
class ExtractionContext:
    query: str
    country_display: str
    modifier_dim: str  # "A" / "B" / "C" / "D"


def extract_from_serper(
    organic_results: Iterable[dict],
    ctx: ExtractionContext,
) -> list[CompanyCandidate]:
    candidates: list[CompanyCandidate] = []
    for entry in organic_results:
        url = entry.get("link", "")
        domain = extract_root_domain(url)
        if domain_is_blacklisted(domain):
            continue

        name = extract_company_name(entry.get("title", ""), domain)
        if not name:
            name = domain_to_fallback_name(domain)
        if _normalize_token(name) in GENERIC_BAD_NAMES:
            continue
        if not name:
            continue

        candidates.append(
            CompanyCandidate(
                name=name,
                domain=domain,
                url=url,
                country_display=ctx.country_display,
                score=10,  # base; merge step adds more
                queries=[ctx.query],
                modifiers=[ctx.modifier_dim],
            )
        )
    return candidates


def merge_by_domain(candidates: Iterable[CompanyCandidate]) -> list[CompanyCandidate]:
    """
    Deduplicate by domain. When the same domain appears multiple times,
    keep the longest/most-specific name and merge the query/modifier history.
    """
    by_domain: dict[str, CompanyCandidate] = {}
    for c in candidates:
        key = c.domain or c.name.lower()
        if key not in by_domain:
            by_domain[key] = c
            continue
        existing = by_domain[key]
        # Prefer shorter, cleaner names; the old longest-name heuristic tended
        # to upgrade domains like "Nuts" into product-page titles like
        # "Bulk Goji Berries".
        if _is_better_company_name(c.name, existing.name, c.domain):
            existing.name = c.name
        existing.queries.extend(c.queries)
        existing.modifiers.extend(c.modifiers)
        existing.score += 5  # repeated hits = stronger signal
    return list(by_domain.values())


def _is_better_company_name(candidate: str, current: str, domain: str) -> bool:
    candidate_score = _score_title_fragment(candidate, _domain_tokens(domain))
    current_score = _score_title_fragment(current, _domain_tokens(domain))
    if candidate_score != current_score:
        return candidate_score > current_score
    return len(candidate) < len(current)


def assign_lead_type(candidate: CompanyCandidate) -> str:
    """
    MODIFIER-D = functional segment (potential brand buyers)
    Otherwise (A/B/C from supply-chain anchors) = direct ingredient buyers
    """
    if "D" in candidate.modifiers:
        return "Potential Buyer"
    return "Direct Buyer"


def to_xlsx_rows(candidates: list[CompanyCandidate]) -> list[dict]:
    rows = []
    for c in candidates:
        rows.append(
            {
                "Company Name": c.name,
                "Country": c.country_display,
                "Lead Type": assign_lead_type(c),
                "Keywords Used": " | ".join(sorted(set(c.queries))),
                "Source Modifier": ",".join(sorted(set(c.modifiers))),
            }
        )
    # Stable sort: by country, then by name
    rows.sort(key=lambda r: (r["Country"], r["Company Name"].lower()))
    return rows
