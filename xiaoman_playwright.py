"""
Xiaoman mining-v2 via Playwright (path-2 of step3 rewrite).

The direct-requests path (old xiaoman_client.py) is blocked because the
`searchListV2` signature is body-bound (verified 2026-04-13). Instead of
reversing the JS signer, we drive a real browser via Playwright:
  - launch_persistent_context() so login persists across runs
  - fill the search box + press Enter so the frontend computes signatures
  - capture searchListV2 responses via page.expect_response

Two CLI modes:
  harvest  — dump every xiaoman API response to disk (debug aid)
  search   — programmatic search for a keyword, print parsed companies

Usage:
    # One-shot keyword test (stage 2a):
    python xiaoman_playwright.py search --keyword goji --max-pages 2

    # Debug / exploration:
    python xiaoman_playwright.py harvest
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from playwright.sync_api import (
    BrowserContext,
    Page,
    Response,
    sync_playwright,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

try:
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
except Exception:
    pass

PROJECT_ROOT = Path(__file__).parent
PROFILE_DIR = Path.home() / ".xiaoman_playwright_profile"
DUMP_DIR = PROJECT_ROOT / "runs" / "spike_xiaoman_api" / "raw"
MINING_URL = "https://crm.xiaoman.cn/new_discovery/mining-v2/list"
SEARCH_API_FRAGMENT = "searchListV2"
PROFILE_EMAILS_API_FRAGMENT = "profileEmails"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

# Empirically observed on 2026-04-14 from the mining-v2 page source.
# input is the only okki-input on the page with maxlength=50.
SEARCH_INPUT_SELECTOR = 'input.okki-input[maxlength="50"]'
MANUAL_LOGIN_TIMEOUT_S = 600.0
# Scope to the table body and the row's title/header blocks. These classes are
# much shorter and more stable than the full `#client-search-v2 ...` DOM chain,
# while still targeting the part of the row that actually opens the detail view.
SEARCH_RESULTS_ROW_SELECTOR = ".main-table tbody tr:not(.okki-table-measure-row)"
SEARCH_RESULT_CLICKABLE_SELECTOR = ".item-title span.truncate.min-w-0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
)
log = logging.getLogger("xiaoman_playwright")


COMPANY_SUFFIX_RE = re.compile(
    r"\b("
    r"inc|incorporated|ltd|limited|llc|gmbh|co|company|corp|corporation|"
    r"international|intl|group|holdings?|bv|srl|s\.r\.l|sarl|sa|sas|ag|kg|plc"
    r")\b",
    re.IGNORECASE,
)

COUNTRY_ALIASES_TO_ISO2 = {
    "ARGENTINA": "AR",
    "AUSTRALIA": "AU",
    "AUSTRIA": "AT",
    "BELGIUM": "BE",
    "BRAZIL": "BR",
    "CANADA": "CA",
    "CHINA": "CN",
    "FRANCE": "FR",
    "GABON": "GA",
    "GERMANY": "DE",
    "DEUTSCHLAND": "DE",
    "ISRAEL": "IL",
    "ITALY": "IT",
    "NETHERLANDS": "NL",
    "THE NETHERLANDS": "NL",
    "UNITED KINGDOM": "GB",
    "UK": "GB",
    "GREAT BRITAIN": "GB",
    "UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND": "GB",
    "UNITED STATES": "US",
    "USA": "US",
    "U.S.": "US",
    "U.S.A.": "US",
    "UNITED STATES OF AMERICA": "US",
}

COUNTRY_MATCH_WHITELIST = {
    # Keep explicit hook for future accepted cross-border HQ cases.
    # Values are normalized ISO2 codes.
}

MEDIUM_TOP1_NAME_SIMILARITY = 0.65
STRONG_TOP1_NAME_SIMILARITY = 0.88
DOMAIN_TOP1_NAME_SIMILARITY = 0.65


class XiaomanRateLimitError(RuntimeError):
    """Raised when Xiaoman shows its access-frequency captcha."""


# ----------------------------------------------------------------------
# Schema (matches xiaoman_stage2/src/discovery-export.js output)
# ----------------------------------------------------------------------


@dataclass
class XiaomanContact:
    company_name: str = ""
    contact_name: str = ""
    first_name: str = ""
    last_name: str = ""
    position: str = ""
    emails: list[str] = field(default_factory=list)
    email_quality: dict[str, bool] = field(default_factory=dict)
    phone_numbers: list[str] = field(default_factory=list)
    linkedin: str = ""
    confidence: int = 0


@dataclass
class XiaomanCompany:
    company_name: str = ""
    website: str = ""
    domain: str = ""
    description: str = ""
    country_name: str = ""
    country_code: str = ""
    country_cn_name: str = ""
    company_hash_id: str = ""  # used to fetch contacts for the top-1 match
    contact_count: int = 0
    email_count: int = 0
    contacts: list[XiaomanContact] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def parse_company(item: dict[str, Any]) -> XiaomanCompany:
    """Convert one searchListV2 list item into XiaomanCompany.

    Field paths verified against runs/spike_xiaoman_api/raw/response_20260414_*.json.
    """
    return XiaomanCompany(
        company_name=item.get("companyName") or item.get("customsName", ""),
        website=item.get("homepage", ""),
        domain=item.get("domain", ""),
        description=item.get("description", ""),
        country_name=item.get("countryName", ""),
        country_code=item.get("countryCode", ""),
        country_cn_name=item.get("countryCnName", ""),
        company_hash_id=item.get("companyHashId", ""),
        contact_count=item.get("contactCount", 0) or 0,
        email_count=item.get("emailCount", 0) or 0,
        raw=item,
    )


def _normalize_string_list(values: Any) -> list[str]:
    out: list[str] = []
    for value in values or []:
        if isinstance(value, str):
            cleaned = value.strip()
        elif isinstance(value, dict):
            cleaned = str(
                value.get("value")
                or value.get("email")
                or value.get("phone")
                or value.get("number")
                or ""
            ).strip()
        else:
            cleaned = str(value).strip()
        if cleaned:
            out.append(cleaned)
    return out


def _normalize_bool_dict(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, bool] = {}
    for key, raw in value.items():
        if isinstance(raw, bool):
            out[str(key)] = raw
        elif isinstance(raw, (int, float)):
            out[str(key)] = bool(raw)
        elif isinstance(raw, str):
            out[str(key)] = raw.strip().lower() in {"1", "true", "yes", "y"}
    return out


def parse_contact(item: dict[str, Any]) -> XiaomanContact:
    first_name = str(item.get("first_name") or "").strip()
    last_name = str(item.get("last_name") or "").strip()
    contact_name = " ".join(part for part in [first_name, last_name] if part).strip()
    if not contact_name:
        contact_name = str(item.get("name") or item.get("full_name") or "").strip()

    confidence_raw = item.get("confidence", 0)
    try:
        confidence = int(confidence_raw or 0)
    except Exception:
        confidence = 0

    return XiaomanContact(
        company_name=str(item.get("company_name") or "").strip(),
        contact_name=contact_name,
        first_name=first_name,
        last_name=last_name,
        position=str(item.get("position") or "").strip(),
        emails=_normalize_string_list(item.get("emails")),
        email_quality=_normalize_bool_dict(item.get("email_quality")),
        phone_numbers=_normalize_string_list(item.get("phone_numbers")),
        linkedin=str(item.get("linkedin") or "").strip(),
        confidence=confidence,
    )


def normalize_country_to_iso2(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    code = text.upper()
    if len(code) == 2 and code.isalpha():
        return code
    return COUNTRY_ALIASES_TO_ISO2.get(code, "")


def normalize_company_name(value: object) -> str:
    text = str(value or "").lower()
    text = COMPANY_SUFFIX_RE.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def company_name_similarity(input_name: str, xiaoman_name: str) -> float:
    left = normalize_company_name(input_name)
    right = normalize_company_name(xiaoman_name)
    if not left or not right:
        return 0.0

    char_score = difflib.SequenceMatcher(None, left, right).ratio()
    compact_score = difflib.SequenceMatcher(None, left.replace(" ", ""), right.replace(" ", "")).ratio()
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    token_score = len(left_tokens & right_tokens) / len(left_tokens | right_tokens) if left_tokens and right_tokens else 0.0
    return round(max(char_score, compact_score, token_score), 3)


def _compact_company_name(value: object) -> str:
    return normalize_company_name(value).replace(" ", "")


def _domain_host(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = text.split("/", 1)[0]
    if text.startswith("www."):
        text = text[4:]
    return text


def domain_contains_company_name(input_name: str, company: XiaomanCompany) -> bool:
    compact_name = _compact_company_name(input_name)
    if not compact_name or len(compact_name) < 4:
        return False
    for value in (company.domain, company.website):
        host = _domain_host(value)
        if not host:
            continue
        host_compact = re.sub(r"[^a-z0-9]+", "", host)
        if compact_name in host_compact:
            return True
    return False


def exact_company_name_match(input_name: str, xiaoman_name: str) -> bool:
    left = normalize_company_name(input_name)
    right = normalize_company_name(xiaoman_name)
    if not left or not right:
        return False
    return left == right or left.replace(" ", "") == right.replace(" ", "")


def country_matches(input_country: str, company: XiaomanCompany) -> bool:
    input_code = normalize_country_to_iso2(input_country)
    candidate_code = normalize_country_to_iso2(company.country_code) or normalize_country_to_iso2(
        company.country_name or company.country_cn_name
    )
    if not input_code or not candidate_code:
        return False
    if input_code == candidate_code:
        return True
    return candidate_code in COUNTRY_MATCH_WHITELIST.get(input_code, set())


def country_conflicts(input_country: str, company: XiaomanCompany) -> bool:
    input_code = normalize_country_to_iso2(input_country)
    candidate_code = normalize_country_to_iso2(company.country_code) or normalize_country_to_iso2(
        company.country_name or company.country_cn_name
    )
    if not input_code or not candidate_code:
        return False
    if input_code == candidate_code:
        return False
    return candidate_code not in COUNTRY_MATCH_WHITELIST.get(input_code, set())


def annotate_and_rank_companies(
    input_name: str,
    input_country: str,
    companies: list[XiaomanCompany],
    *,
    medium_similarity: float = MEDIUM_TOP1_NAME_SIMILARITY,
    strong_similarity: float = STRONG_TOP1_NAME_SIMILARITY,
    domain_similarity: float = DOMAIN_TOP1_NAME_SIMILARITY,
) -> list[tuple[int, XiaomanCompany, float, bool, str, bool, bool]]:
    """Return ranked candidates after top-1 quality gating.

    Strong name evidence can become rank 1 even when the search-country signal
    disagrees. Domain evidence can rescue a medium name match, but low-name-sim
    domain hits stay in review. Failed candidates are retained for manual
    review, but cannot become rank 1.
    """
    annotated = []
    for company in companies:
        similarity = company_name_similarity(input_name, company.company_name)
        country_match = country_matches(input_country, company)
        exact_match = exact_company_name_match(input_name, company.company_name)
        domain_match = domain_contains_company_name(input_name, company)
        strong_match = exact_match or similarity >= strong_similarity
        domain_name_match = domain_match and similarity >= domain_similarity
        medium_country_match = similarity >= medium_similarity and country_match
        eligible = strong_match or domain_name_match or medium_country_match
        if strong_match:
            match_quality = "strong"
        elif domain_name_match:
            match_quality = "domain_name"
        elif medium_country_match:
            match_quality = "medium_country"
        else:
            match_quality = "review"
        country_conflict = eligible and country_conflicts(input_country, company)
        annotated.append(
            (
                company,
                similarity,
                country_match,
                match_quality,
                eligible,
                country_conflict,
            )
        )

    eligible = [item for item in annotated if item[4]]
    ineligible = [item for item in annotated if item not in eligible]

    ranked: list[tuple[int, XiaomanCompany, float, bool, str, bool, bool]] = []
    next_rank = 1 if eligible else 2
    for company, similarity, country_match, match_quality, top1_eligible, country_conflict in eligible + ineligible:
        ranked.append(
            (
                next_rank,
                company,
                similarity,
                country_match,
                match_quality,
                top1_eligible,
                country_conflict,
            )
        )
        next_rank += 1
    return ranked


# ----------------------------------------------------------------------
# Response-dump helper (shared by both modes)
# ----------------------------------------------------------------------


def dump_response(resp: Response) -> Path | None:
    """Persist a searchListV2 response body to DUMP_DIR. Returns file path."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    try:
        body = resp.json()
    except Exception:
        body = {"_raw_text": resp.text()}
    try:
        post_data = resp.request.post_data
    except Exception:
        post_data = None
    payload = {
        "url": resp.url,
        "status": resp.status,
        "request_post_data": post_data,
        "response": body,
    }
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    out = DUMP_DIR / f"response_{ts}.json"
    i = 1
    while out.exists():
        out = DUMP_DIR / f"response_{ts}_{i}.json"
        i += 1
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ----------------------------------------------------------------------
# Client (stage 2a)
# ----------------------------------------------------------------------


class XiaomanPlaywrightClient:
    """Drives mining-v2 via a persistent-context Chromium.

    Lifecycle:
        with XiaomanPlaywrightClient() as client:
            for company in client.search("goji", max_pages=3):
                ...
    """

    def __init__(
        self,
        *,
        headless: bool = False,
        nav_timeout_ms: int = 30_000,
        captcha_action: str = "wait",
    ):
        self.headless = headless
        self.nav_timeout_ms = nav_timeout_ms
        self.captcha_action = captcha_action
        self._pw = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> "XiaomanPlaywrightClient":
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        first_run = not any(PROFILE_DIR.iterdir())
        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=self.headless,
            viewport={"width": 1440, "height": 900},
            user_agent=USER_AGENT,
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(self.nav_timeout_ms)
        log.info("opening %s%s", MINING_URL, " (FIRST RUN — log in manually)" if first_run else "")
        self._page.goto(MINING_URL, wait_until="domcontentloaded")
        self._wait_until_search_ready()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            if self._pw is not None:
                self._pw.stop()

    # ------------------------------------------------------------------

    def _wait_until_search_ready(self, manual_login_timeout_s: float = MANUAL_LOGIN_TIMEOUT_S) -> None:
        """Wait for the mining search box, allowing manual login when expired.

        The search input is the proxy for "logged in and ready". If Xiaoman
        redirects to a login page or the existing profile has expired, keep the
        real Chromium window open and let the user log in manually. Once the
        mining page loads and the search input appears, the caller continues.
        """
        assert self._page is not None
        input_loc = self._page.locator(SEARCH_INPUT_SELECTOR).first
        try:
            input_loc.wait_for(state="visible", timeout=self.nav_timeout_ms)
            log.info("xiaoman login ok")
            return
        except PlaywrightTimeoutError:
            if self.headless:
                raise RuntimeError(
                    "xiaoman login appears expired, but browser is headless. "
                    "Run `python xiaoman_playwright.py check-login` with a visible browser first."
                )

        log.warning(
            "xiaoman login appears expired or not ready. "
            "Log in manually in the opened Chromium window; I'll continue once the search page loads "
            "(waiting up to %.0f min).",
            manual_login_timeout_s / 60,
        )
        try:
            input_loc.wait_for(state="visible", timeout=manual_login_timeout_s * 1000)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("xiaoman manual login did not complete within timeout") from exc
        log.info("xiaoman login refreshed; search page ready")

    def search(
        self,
        keyword: str,
        *,
        max_pages: int = 1,
        sleep_between_pages_s: float = 1.0,
    ) -> Iterator[XiaomanCompany]:
        """Yield XiaomanCompany for each result across pages.

        Implementation: fill the search input, press Enter, then wait for the
        `searchListV2` POST response. For page 2+ we re-fire the same search;
        xiaoman's pagination clicks also re-issue searchListV2 with `page=N`,
        but clicking the next-page button requires a brittle selector.
        Instead we observe that pressing Enter re-runs the search from page 1
        — so for pagination we need the next-page button. TODO: wire up once
        the button's selector is confirmed; for now max_pages=1 is the
        reliable path.
        """
        assert self._page is not None
        page = self._page

        # If xiaoman threw up the "access too frequent" captcha, pause until
        # the user dismisses it before we try to interact with the page.
        self._wait_if_captcha()

        # Clear + type the keyword. Using fill() instead of type() skips
        # per-keystroke autocomplete calls (associateSearch) that pollute
        # the response stream.
        input_loc = page.locator(SEARCH_INPUT_SELECTOR).first
        input_loc.click()
        input_loc.fill("")
        input_loc.fill(keyword)

        current_page = 1
        while current_page <= max_pages:
            log.info("searching %r page=%d", keyword, current_page)
            with page.expect_response(
                lambda r: SEARCH_API_FRAGMENT in r.url and r.request.method == "POST",
                timeout=self.nav_timeout_ms,
            ) as resp_info:
                if current_page == 1:
                    input_loc.press("Enter")
                else:
                    # Pagination: click the "next page" control. We try
                    # several candidate selectors because we haven't locked
                    # down the exact one yet. Remove the fallbacks once
                    # stable.
                    self._click_next_page()

            resp = resp_info.value
            out_path = dump_response(resp)
            try:
                body = resp.json()
            except Exception as exc:
                log.error("page %d response not JSON: %s", current_page, exc)
                return

            if body.get("code") != 0:
                log.error(
                    "xiaoman returned non-zero code=%s msg=%s — stopping",
                    body.get("code"),
                    body.get("msg"),
                )
                return

            items = body.get("data", {}).get("list", []) or []
            total = body.get("data", {}).get("total_count")
            log.info(
                "page %d: %d items (total=%s)  dumped to %s",
                current_page,
                len(items),
                total,
                out_path.name if out_path else "?",
            )
            if not items:
                return
            for item in items:
                yield parse_company(item)

            if current_page >= max_pages:
                return
            current_page += 1
            if sleep_between_pages_s > 0:
                time.sleep(sleep_between_pages_s)

    def _wait_if_captcha(self, timeout_s: float = 600.0) -> None:
        """Pause if the 'access too frequent' captcha modal is visible.

        Xiaoman throws a modal with id `http-over-limit-captcha-dialog-<ts>`
        after ~20 rapid searches. We can't solve it programmatically (that's
        the whole point of a captcha), so we block until the user dismisses
        it, then resume.
        """
        assert self._page is not None
        sel = '[id^="http-over-limit-captcha-dialog"]'
        loc = self._page.locator(sel).first
        if loc.count() == 0:
            return
        log.warning("⚠️  xiaoman rate-limit captcha is up — solve it in the browser, then I'll resume (up to %.0f min)", timeout_s / 60)
        if self.captcha_action == "abort":
            raise XiaomanRateLimitError("xiaoman rate-limit captcha is visible")
        try:
            loc.wait_for(state="detached", timeout=timeout_s * 1000)
        except Exception:
            raise XiaomanRateLimitError("captcha not dismissed within timeout — aborting")
        log.info("captcha gone, resuming")
        # Give the page a beat to settle after the modal close.
        self._page.wait_for_timeout(1000)

    def _click_next_page(self) -> None:
        """Click the pagination 'next page' control.

        We try a few selectors because we haven't nailed the exact one from
        DOM inspection yet. If all fail, raises so the caller can surface
        the error rather than silently hanging.
        """
        assert self._page is not None
        candidates = [
            "li.okki-pagination-next:not(.okki-pagination-disabled)",
            "li.ant-pagination-next:not(.ant-pagination-disabled)",
            'button[aria-label="Next Page"]',
            'button[aria-label="下一页"]',
            'button[title="下一页"]',
            'button[title="下一頁"]',
        ]
        for sel in candidates:
            loc = self._page.locator(sel).first
            if loc.count() > 0:
                loc.click()
                return
        raise RuntimeError(
            "no next-page button matched any known selector. "
            "Run `python xiaoman_playwright.py harvest`, click next-page manually, "
            "and inspect the button's HTML to add its selector."
        )

    def fetch_contacts(
        self,
        keyword: str,
        *,
        max_contacts_pages: int = 1,
    ) -> list[XiaomanContact]:
        """Search one company, open the top-1 card, and return its contacts.

        We intentionally reuse the same input fill + Enter pattern as `search()`
        so the frontend keeps computing signatures for us. Contact pagination is
        optional; the pipeline currently calls this with `max_contacts_pages=1`.
        """
        assert self._page is not None
        page = self._page

        self._wait_if_captcha()

        input_loc = page.locator(SEARCH_INPUT_SELECTOR).first
        input_loc.click()
        input_loc.fill("")
        input_loc.fill(keyword)

        with page.expect_response(
            lambda r: SEARCH_API_FRAGMENT in r.url and r.request.method == "POST",
            timeout=self.nav_timeout_ms,
        ) as resp_info:
            input_loc.press("Enter")

        resp = resp_info.value
        try:
            body = resp.json()
        except Exception as exc:
            raise RuntimeError(f"search response not JSON for {keyword!r}: {exc}") from exc

        if body.get("code") != 0:
            raise RuntimeError(
                f"xiaoman search returned code={body.get('code')} msg={body.get('msg')}"
            )

        items = body.get("data", {}).get("list", []) or []
        if not items:
            return []

        self._wait_if_captcha()
        click_target = self._first_company_click_target()
        contacts: list[XiaomanContact] = []
        try:
            with page.expect_response(
                lambda r: PROFILE_EMAILS_API_FRAGMENT in r.url,
                timeout=self.nav_timeout_ms,
            ) as contact_resp_info:
                click_target.click()
            contacts.extend(self._parse_contacts_response(contact_resp_info.value))

            current_page = 1
            while current_page < max_contacts_pages:
                next_button = self._find_contacts_next_page_button()
                if next_button is None:
                    break
                with page.expect_response(
                    lambda r: PROFILE_EMAILS_API_FRAGMENT in r.url,
                    timeout=self.nav_timeout_ms,
                ) as contact_resp_info:
                    next_button.click()
                new_contacts = self._parse_contacts_response(contact_resp_info.value)
                if not new_contacts:
                    break
                contacts.extend(new_contacts)
                current_page += 1
        finally:
            self._return_to_search_results()

        return contacts

    def _first_company_click_target(self):
        assert self._page is not None
        row = self._page.locator(SEARCH_RESULTS_ROW_SELECTOR).first
        row.wait_for(state="visible")
        target = row.locator(SEARCH_RESULT_CLICKABLE_SELECTOR).first
        if target.count() > 0:
            return target
        return row

    def _parse_contacts_response(self, resp: Response) -> list[XiaomanContact]:
        try:
            body = resp.json()
        except Exception as exc:
            raise RuntimeError(f"profileEmails response not JSON: {exc}") from exc

        if body.get("code") not in (0, None):
            log.warning(
                "profileEmails returned non-zero code=%s msg=%s",
                body.get("code"),
                body.get("msg"),
            )
            return []

        items = body.get("data", {}).get("emails", []) or []
        return [parse_contact(item) for item in items if isinstance(item, dict)]

    def _find_contacts_next_page_button(self):
        assert self._page is not None
        candidates = [
            ".drawer .okki-pagination-next:not(.okki-pagination-disabled)",
            ".drawer .ant-pagination-next:not(.ant-pagination-disabled)",
            ".ant-drawer .okki-pagination-next:not(.okki-pagination-disabled)",
            ".ant-drawer .ant-pagination-next:not(.ant-pagination-disabled)",
        ]
        for sel in candidates:
            loc = self._page.locator(sel).first
            if loc.count() > 0:
                return loc
        return None

    def _return_to_search_results(self) -> None:
        """Best-effort close detail view so the next search starts cleanly."""
        assert self._page is not None
        page = self._page

        close_selectors = [
            ".ant-drawer-close",
            ".okki-dialog-close",
            'button[aria-label="Close"]',
            'button[aria-label="关闭"]',
            'button[title="Close"]',
            'button[title="关闭"]',
        ]
        for sel in close_selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() == 0:
                    continue
                loc.click(timeout=1000)
                page.wait_for_timeout(500)
                page.locator(SEARCH_INPUT_SELECTOR).first.wait_for(state="visible", timeout=2000)
                return
            except Exception:
                continue

        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            page.locator(SEARCH_INPUT_SELECTOR).first.wait_for(state="visible", timeout=2000)
            return
        except Exception:
            pass

        if page.url != MINING_URL:
            try:
                page.go_back(wait_until="domcontentloaded", timeout=5000)
            except Exception:
                pass

        page.locator(SEARCH_INPUT_SELECTOR).first.wait_for(state="visible")


# ----------------------------------------------------------------------
# Harvest mode (debug helper, kept from the earlier iteration)
# ----------------------------------------------------------------------


def run_harvest() -> None:
    """Open the browser; dump every xiaoman API response while user drives."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    first_run = not any(PROFILE_DIR.iterdir())
    log.info("profile dir: %s%s", PROFILE_DIR, " (FIRST RUN)" if first_run else "")
    log.info("dump dir:    %s", DUMP_DIR)

    static_exts = (".js", ".css", ".png", ".jpg", ".jpeg", ".svg", ".woff", ".woff2", ".ico", ".gif")

    def on_response(resp: Response) -> None:
        if "xiaoman.cn" not in resp.url:
            return
        if any(resp.url.endswith(ext) for ext in static_exts):
            return
        log.info("[api] %s %s", resp.status, resp.url)
        if SEARCH_API_FRAGMENT not in resp.url:
            return
        path = dump_response(resp)
        log.info("captured searchListV2 → %s", path.name if path else "?")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1440, "height": 900},
            user_agent=USER_AGENT,
        )
        context.on("response", on_response)
        context.on(
            "request",
            lambda req: (
                log.info("[req] %s %s", req.method, req.url)
                if "xiaoman.cn" in req.url and not any(req.url.endswith(ext) for ext in static_exts)
                else None
            ),
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(MINING_URL, wait_until="domcontentloaded")
        log.info("browser open. Ctrl+C or close window to exit.")

        closed = {"flag": False}
        context.on("close", lambda *_: closed.update(flag=True))
        signal.signal(signal.SIGINT, lambda *_: closed.update(flag=True))
        try:
            while not closed["flag"]:
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    break
        finally:
            try:
                context.close()
            except Exception:
                pass
            log.info("done. dumps in %s", DUMP_DIR)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=False)

    sub.add_parser("harvest", help="Open browser and dump every API response (debug).")
    sub.add_parser("check-login", help="Open Xiaoman and wait until the mining search page is ready.")

    s = sub.add_parser("search", help="Run a keyword search and print parsed companies.")
    s.add_argument("--keyword", required=True)
    s.add_argument("--max-pages", type=int, default=1)

    args = parser.parse_args()
    if args.mode is None:
        # Default to harvest so old invocations still work.
        args.mode = "harvest"
    return args


def main() -> None:
    args = parse_args()
    if args.mode == "harvest":
        run_harvest()
        return
    if args.mode == "check-login":
        with XiaomanPlaywrightClient():
            log.info("xiaoman check-login complete")
        return
    if args.mode == "search":
        with XiaomanPlaywrightClient() as client:
            companies = list(client.search(args.keyword, max_pages=args.max_pages))
        log.info("got %d companies for %r", len(companies), args.keyword)
        for i, c in enumerate(companies, 1):
            print(
                f"  {i:3d}. {c.company_name!r:40s}  "
                f"{c.country_code:3s}  {c.domain:30s}  "
                f"contacts={c.contact_count}  emails={c.email_count}  "
                f"hash={c.company_hash_id}"
            )
        return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
