"""
Step4: website verification + goji ingredient analysis (merged).

Reads 03_xiaoman.xlsx top-1 rows → fetches company websites → calls LLM judge
→ writes 04_verified.xlsx.
"""
from __future__ import annotations

import html
import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from openpyxl import load_workbook

from llm_judge import JudgeInput, judge
from schema import write_verified_xlsx

log = logging.getLogger(__name__)

PAGE_PATHS = ["", "/about", "/products"]
MAX_PAGE_CHARS = 5000
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)


def _cell_text(value: object) -> str:
    return str(value or "").strip()


def _as_int(value: object) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _read_xlsx_rows(path: Path) -> list[dict]:
    wb = load_workbook(path, read_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header_row = next(rows, None)
    if not header_row:
        return []
    header = [_cell_text(h) for h in header_row]
    out = []
    for raw in rows:
        row = {}
        for idx, key in enumerate(header):
            row[key] = raw[idx] if idx < len(raw) and raw[idx] is not None else ""
        if any(_cell_text(value) for value in row.values()):
            out.append(row)
    return out


def _buyer_key(row: dict) -> tuple[str, str, str]:
    return (
        _cell_text(row.get("Input Company Name")),
        _cell_text(row.get("Input Country")),
        _cell_text(row.get("Input Lead Type")),
    )


def _unique_join(values: list[object]) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        for part in _cell_text(value).split(";"):
            cleaned = part.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                out.append(cleaned)
    return "; ".join(out)


def _top1_company_rows(xiaoman_xlsx: Path) -> list[dict]:
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for row in _read_xlsx_rows(xiaoman_xlsx):
        if _as_int(row.get("Match Rank")) != 1:
            continue
        groups.setdefault(_buyer_key(row), []).append(row)

    top_rows = []
    for rows in groups.values():
        first = dict(rows[0])
        first["Contact Name"] = _unique_join([row.get("Contact Name") for row in rows])
        first["Email"] = _unique_join([row.get("Email") for row in rows])
        first["Position"] = _unique_join([row.get("Position") for row in rows])
        top_rows.append(first)
    return top_rows


def _base_url(domain: str) -> str:
    value = _cell_text(domain)
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        return f"{parsed.scheme}://{parsed.netloc}"
    return f"https://{value.strip('/')}"


def _html_to_text(content: str) -> str:
    content = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", content)
    content = re.sub(r"(?s)<[^>]+>", " ", content)
    content = html.unescape(content)
    return re.sub(r"\s+", " ", content).strip()


def _fetch_website_pages(domain: str) -> tuple[str, list[str]]:
    import httpx

    base = _base_url(domain)
    if not base:
        return "", []

    chunks: list[str] = []
    fetched_urls: list[str] = []
    with httpx.Client(
        follow_redirects=True,
        timeout=10.0,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for path in PAGE_PATHS:
            url = urljoin(base + "/", path.lstrip("/"))
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except Exception as exc:
                log.info("website fetch failed %s: %s", url, exc)
                continue
            text = _html_to_text(resp.text)
            if not text:
                continue
            fetched_urls.append(str(resp.url))
            chunks.append(f"SOURCE_URL: {resp.url}\n{text[:MAX_PAGE_CHARS]}")
    return "\n\n".join(chunks), fetched_urls


def fetch_website_text(domain: str) -> str:
    """Fetch website home + /about + /products and return concatenated text."""
    text, _ = _fetch_website_pages(domain)
    return text


def run(xiaoman_xlsx: Path, output_path: Path, *, sleep_s: float = 2.0) -> Path:
    """Step4 main entry."""
    if output_path.exists():
        log.info("step4: %s already exists, skipping", output_path.name)
        return output_path
    if not xiaoman_xlsx.is_file():
        raise FileNotFoundError(f"step4: missing {xiaoman_xlsx}")

    top_rows = _top1_company_rows(xiaoman_xlsx)
    log.info("step4: %d top-1 companies to verify", len(top_rows))

    rows_out: list[dict] = []
    for idx, row in enumerate(top_rows, start=1):
        company_name = _cell_text(row.get("Xiaoman Company Name")) or _cell_text(row.get("Input Company Name"))
        website = _cell_text(row.get("Website"))
        domain = website or _cell_text(row.get("Domain"))
        website_text, fetched_urls = _fetch_website_pages(domain)
        evidence_fallback = fetched_urls[0] if fetched_urls else domain
        log.info("[%d/%d] step4 verify %r (%d chars)", idx, len(top_rows), company_name, len(website_text))

        verdict = judge(
            JudgeInput(
                company_name=company_name,
                country=_cell_text(row.get("Xiaoman Country")) or _cell_text(row.get("Input Country")),
                domain=_cell_text(row.get("Domain")) or domain,
                lead_type=_cell_text(row.get("Input Lead Type")),
                website_text=website_text,
            )
        )

        rows_out.append(
            {
                "Input Company Name": _cell_text(row.get("Input Company Name")),
                "Input Country": _cell_text(row.get("Input Country")),
                "Lead Type": _cell_text(row.get("Input Lead Type")),
                "Xiaoman Company Name": company_name,
                "Domain": _cell_text(row.get("Domain")),
                "Website Country": verdict.website_country,
                "Website": website,
                "B2B/B2C": verdict.b2b_or_b2c,
                "Verified Target": verdict.is_target,
                "Customer Type": verdict.customer_type,
                "Is Competitor": verdict.is_competitor,
                "Goji Presence": verdict.goji_presence,
                "Rating": verdict.rating,
                "P Priority": verdict.p_priority,
                "Track Match": verdict.track_match,
                "Matched Track": verdict.matched_track,
                "Evidence URL": verdict.evidence_url or evidence_fallback,
                "Rating Reason": verdict.rating_reason,
                "Outreach Angle": verdict.outreach_angle,
                "Contact Count": row.get("Contact Count", ""),
                "Email Count": row.get("Email Count", ""),
                "Contact Name": _cell_text(row.get("Contact Name")),
                "Email": _cell_text(row.get("Email")),
                "Position": _cell_text(row.get("Position")),
            }
        )

        if sleep_s > 0 and idx < len(top_rows):
            time.sleep(sleep_s)

    write_verified_xlsx(rows_out, output_path)
    log.info("step4: wrote %d verified rows to %s", len(rows_out), output_path.name)
    return output_path
