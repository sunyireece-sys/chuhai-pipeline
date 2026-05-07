"""
Minimal sales feedback webUI.

Reads profile JSONs from runs/<run_id>/05_profiles/ and serves a list page
with an inline feedback form per company. Feedback and dry-run send tracking
are appended to a SQLite database (feedback.db).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from send_outreach import (
    ConfigError,
    SEND_LOG_JSONL,
    _build_message,
    _send_message,
    load_send_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "runs"
DEFAULT_RUN_ID = "2026-04-30"
DB_PATH = Path(os.environ.get("FEEDBACK_DB_PATH") or (Path(__file__).resolve().parent / "feedback.db"))
def _is_dry_run() -> bool:
    return os.environ.get("SEND_MODE", "live").strip().lower() == "dry-run"
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

STATUS_OPTIONS = [
    "未发",
    "已发待回",
    "已回",
    "不相关",
    "国家错",
    "邮箱无效",
    "联系人不对",
    "已成交",
]
TAG_OPTIONS = [
    "邮件太硬",
    "需要再跟进",
    "对方推荐其他人",
    "客户已有供应商",
    "报价偏高",
    "其他",
]
SUBMITTER_OPTIONS = ["Nicky", "Andrew", "Reece", "Other"]


app = FastAPI(
    title="Sales Feedback Demo",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
security = HTTPBasic(auto_error=False)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _webui_password() -> str:
    return (os.environ.get("WEBUI_PASSWORD") or "").strip()


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> str:
    password = _webui_password()
    if not password:
        return ""
    authenticated = (
        credentials is not None
        and secrets.compare_digest(credentials.username, "sales")
        and secrets.compare_digest(credentials.password, password)
    )
    if authenticated:
        return credentials.username
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Basic"},
    )


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_slug TEXT NOT NULL,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                note TEXT NOT NULL DEFAULT '',
                submitted_by TEXT NOT NULL,
                submitted_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_slug_run "
            "ON feedback(profile_slug, run_id, submitted_at DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS send_tracking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_slug TEXT NOT NULL,
                run_id TEXT NOT NULL,
                original_subject TEXT NOT NULL,
                original_body TEXT NOT NULL,
                original_whatsapp TEXT NOT NULL DEFAULT '',
                original_follow_up TEXT NOT NULL DEFAULT '',
                sent_subject TEXT NOT NULL,
                sent_body TEXT NOT NULL,
                sent_whatsapp TEXT NOT NULL DEFAULT '',
                sent_follow_up TEXT NOT NULL DEFAULT '',
                subject_edited INTEGER NOT NULL DEFAULT 0,
                body_edited INTEGER NOT NULL DEFAULT 0,
                whatsapp_edited INTEGER NOT NULL DEFAULT 0,
                follow_up_edited INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL,
                actual_to TEXT NOT NULL,
                original_to TEXT NOT NULL,
                send_status TEXT NOT NULL,
                smtp_response TEXT,
                send_error TEXT,
                submitted_by TEXT NOT NULL,
                submitted_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_send_slug_run "
            "ON send_tracking(profile_slug, run_id, submitted_at DESC)"
        )


_init_db()


def _read_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _safe_path_component(value: str) -> bool:
    return bool(_SAFE_COMPONENT_RE.match(value or ""))


def _run_output_dir(run_id: str) -> Path | None:
    if not _safe_path_component(run_id):
        return None
    return RUNS_DIR / run_id / "05_profiles"


def _profile_json_path(run_id: str, slug: str) -> Path | None:
    output_dir = _run_output_dir(run_id)
    if output_dir is None or not _safe_path_component(slug):
        return None
    return output_dir / "profiles" / f"{slug}.json"


def _contacts_json_path(run_id: str, slug: str) -> Path | None:
    output_dir = _run_output_dir(run_id)
    if output_dir is None or not _safe_path_component(slug):
        return None
    return output_dir / "contacts" / f"{slug}.json"


def _send_log_path(run_id: str) -> Path | None:
    output_dir = _run_output_dir(run_id)
    if output_dir is None:
        return None
    return output_dir / SEND_LOG_JSONL


def _truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"



def load_sales_leads(run_id: str) -> list[dict]:
    """Iterate profiles/*.json, return sales-mode entries with contact info."""
    output_dir = _run_output_dir(run_id)
    if output_dir is None:
        return []
    profiles_dir = output_dir / "profiles"
    contacts_dir = output_dir / "contacts"
    if not profiles_dir.is_dir():
        return []

    rows: list[dict] = []
    for json_path in sorted(profiles_dir.glob("*.json")):
        profile = _read_json(json_path)
        if profile.get("outreach_mode") != "sales":
            continue
        contacts = _read_json(contacts_dir / json_path.name)
        emails = contacts.get("emails") or []
        phones = contacts.get("phones") or []
        contact_pages = contacts.get("contact_pages") or []
        if not (emails or phones or contact_pages):
            continue

        company = profile.get("company") or {}
        body = profile.get("profile") or {}
        outreach = profile.get("outreach") or {}
        rows.append(
            {
                "slug": json_path.stem,
                "display_name": company.get("display_name", ""),
                "country": company.get("country", ""),
                "rating": company.get("step4_rating", ""),
                "website": company.get("website", ""),
                "email": emails[0] if emails else "",
                "phone": phones[0] if phones else "",
                "has_contact_page": "Yes" if contact_pages else "",
                "subject": outreach.get("cold_email_subject", ""),
                "body": outreach.get("cold_email_body", ""),
                "whatsapp": outreach.get("whatsapp_or_linkedin_message", ""),
                "follow_up": outreach.get("follow_up_email", ""),
                "bio": body.get("bio_cn", ""),
                "business_relevance": body.get("business_relevance_cn", ""),
            }
        )
    return rows


def load_latest_feedback(slug: str, run_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM feedback WHERE profile_slug=? AND run_id=? "
            "ORDER BY submitted_at DESC LIMIT 1",
            (slug, run_id),
        ).fetchone()
    if not row:
        return None
    return {
        "status": row["status"],
        "tags": json.loads(row["tags"]),
        "note": row["note"],
        "submitted_by": row["submitted_by"],
        "submitted_at": row["submitted_at"],
    }


def load_history(slug: str, run_id: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM feedback WHERE profile_slug=? AND run_id=? "
            "ORDER BY submitted_at DESC",
            (slug, run_id),
        ).fetchall()
    return [
        {
            "status": r["status"],
            "tags": json.loads(r["tags"]),
            "note": r["note"],
            "submitted_by": r["submitted_by"],
            "submitted_at": r["submitted_at"],
        }
        for r in rows
    ]


def load_latest_send(slug: str, run_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM send_tracking WHERE profile_slug=? AND run_id=? "
            "ORDER BY submitted_at DESC, id DESC LIMIT 1",
            (slug, run_id),
        ).fetchone()
    if not row:
        return None
    return {
        "original_subject": row["original_subject"],
        "original_body": row["original_body"],
        "original_whatsapp": row["original_whatsapp"],
        "original_follow_up": row["original_follow_up"],
        "sent_subject": row["sent_subject"],
        "sent_body": row["sent_body"],
        "sent_whatsapp": row["sent_whatsapp"],
        "sent_follow_up": row["sent_follow_up"],
        "subject_edited": bool(row["subject_edited"]),
        "body_edited": bool(row["body_edited"]),
        "whatsapp_edited": bool(row["whatsapp_edited"]),
        "follow_up_edited": bool(row["follow_up_edited"]),
        "mode": row["mode"],
        "actual_to": row["actual_to"],
        "original_to": row["original_to"],
        "send_status": row["send_status"],
        "smtp_response": row["smtp_response"],
        "send_error": row["send_error"],
        "submitted_by": row["submitted_by"],
        "submitted_at": row["submitted_at"],
    }


def load_original_send_source(slug: str, run_id: str) -> dict | None:
    profile_path = _profile_json_path(run_id, slug)
    contacts_path = _contacts_json_path(run_id, slug)
    if profile_path is None or contacts_path is None:
        return None
    profile = _read_json(profile_path)
    contacts = _read_json(contacts_path)
    outreach = profile.get("outreach") if isinstance(profile.get("outreach"), dict) else {}
    emails = contacts.get("emails") if isinstance(contacts.get("emails"), list) else []
    first_email = str(emails[0]).strip() if emails else ""
    return {
        "original_to": first_email,
        "subject": str(outreach.get("cold_email_subject") or ""),
        "body": str(outreach.get("cold_email_body") or ""),
        "whatsapp": str(outreach.get("whatsapp_or_linkedin_message") or ""),
        "follow_up": str(outreach.get("follow_up_email") or ""),
    }


def _redact_secret(text: object, secret: str) -> str:
    out = str(text or "")
    return out.replace(secret, "***") if secret else out


def _append_send_log(run_id: str, record: dict) -> None:
    log_path = _send_log_path(run_id)
    if log_path is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _build_row_context(slug: str, run_id: str, leads: list[dict] | None = None) -> dict:
    leads = leads if leads is not None else load_sales_leads(run_id)
    lead = next((l for l in leads if l["slug"] == slug), None)
    if lead is None:
        return {}
    latest = load_latest_feedback(slug, run_id)
    history = load_history(slug, run_id)
    latest_send = load_latest_send(slug, run_id)
    return {
        "r": {
            "lead": lead,
            "latest": latest,
            "history": history,
            "latest_send": latest_send,
        },
        "run_id": run_id,
        "status_options": STATUS_OPTIONS,
        "tag_options": TAG_OPTIONS,
        "submitter_options": SUBMITTER_OPTIONS,
        "send_mode": "dry-run" if _is_dry_run() else "live",
        "truncate": _truncate,
    }


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    run: str = DEFAULT_RUN_ID,
    current_user: str = Depends(require_auth),
) -> HTMLResponse:
    leads = load_sales_leads(run)
    enriched = []
    for lead in leads:
        latest = load_latest_feedback(lead["slug"], run)
        history = load_history(lead["slug"], run)
        latest_send = load_latest_send(lead["slug"], run)
        enriched.append(
            {
                "lead": lead,
                "latest": latest,
                "history": history,
                "latest_send": latest_send,
            }
        )
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "rows": enriched,
            "run_id": run,
            "current_user": current_user,
            "status_options": STATUS_OPTIONS,
            "tag_options": TAG_OPTIONS,
            "submitter_options": SUBMITTER_OPTIONS,
            "send_mode": "dry-run" if _is_dry_run() else "live",
            "truncate": _truncate,
        },
    )


@app.post("/feedback", response_class=HTMLResponse)
async def submit_feedback(
    request: Request,
    current_user: str = Depends(require_auth),
) -> HTMLResponse:
    form = await request.form()
    slug = form.get("slug", "").strip()
    run_id = form.get("run_id", DEFAULT_RUN_ID).strip() or DEFAULT_RUN_ID
    status = form.get("status", "").strip()
    tags = form.getlist("tags")
    note = form.get("note", "").strip()
    submitted_by = form.get("submitted_by", "").strip()

    if not slug or not status or not submitted_by:
        return HTMLResponse("missing required field", status_code=400)

    submitted_at = dt.datetime.now().isoformat(timespec="seconds")
    with _db() as conn:
        conn.execute(
            "INSERT INTO feedback (profile_slug, run_id, status, tags, note, "
            "submitted_by, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (slug, run_id, status, json.dumps(tags, ensure_ascii=False),
             note, submitted_by, submitted_at),
        )

    ctx = _build_row_context(slug, run_id)
    if not ctx:
        return HTMLResponse("profile not found", status_code=404)
    ctx["request"] = request
    ctx["current_user"] = current_user
    return templates.TemplateResponse("_row.html", ctx)


@app.post("/send", response_class=HTMLResponse)
async def submit_send(
    request: Request,
    current_user: str = Depends(require_auth),
) -> HTMLResponse:
    form = await request.form()
    slug = str(form.get("slug") or "").strip()
    run_id = str(form.get("run_id") or DEFAULT_RUN_ID).strip() or DEFAULT_RUN_ID
    submitted_by = str(form.get("submitted_by") or "").strip()
    form_actual_to = str(form.get("form_actual_to") or "").strip()
    subject = str(form.get("subject") or "").strip()
    body = str(form.get("body") or "")
    whatsapp = str(form.get("whatsapp") or "")
    follow_up = str(form.get("follow_up") or "")

    if not slug or not submitted_by or not subject or not body.strip():
        return HTMLResponse("missing required field", status_code=400)

    original = load_original_send_source(slug, run_id)
    if not original:
        return HTMLResponse("profile not found", status_code=404)
    original_to = original["original_to"]
    send_to = form_actual_to if form_actual_to else original_to
    if not send_to:
        return HTMLResponse("recipient email not found", status_code=422)
    if not original["subject"] or not original["body"]:
        return HTMLResponse("original outreach content not found", status_code=422)

    dry_run = _is_dry_run()
    test_recipient = (os.environ.get("SMTP_TEST_RECIPIENT") or "").strip() if dry_run else ""
    try:
        config = load_send_config(live=not dry_run, test_recipient=test_recipient, sleep_s=0)
    except ConfigError as exc:
        return HTMLResponse(str(exc), status_code=422)

    actual_to, final_subject, final_body = _build_message(
        live=not dry_run,
        test_recipient=test_recipient,
        original_to=send_to,
        subject=subject,
        body=body,
    )

    subject_edited = int(subject != original["subject"])
    body_edited = int(body != original["body"])
    whatsapp_edited = int(whatsapp != original["whatsapp"])
    follow_up_edited = int(follow_up != original["follow_up"])
    submitted_at = dt.datetime.now().isoformat(timespec="seconds")
    send_status = "sent"
    smtp_response = None
    send_error = None

    try:
        smtp_response = _redact_secret(
            _send_message(
                config=config,
                original_to=send_to,
                actual_to=actual_to,
                subject=final_subject,
                body=final_body,
            ),
            config.smtp_pass,
        )
    except Exception as exc:  # Record the failed attempt for audit and UI feedback.
        send_status = "error"
        send_error = _redact_secret(f"{type(exc).__name__}: {exc}", config.smtp_pass)

    with _db() as conn:
        conn.execute(
            """
            INSERT INTO send_tracking (
                profile_slug, run_id,
                original_subject, original_body, original_whatsapp, original_follow_up,
                sent_subject, sent_body, sent_whatsapp, sent_follow_up,
                subject_edited, body_edited, whatsapp_edited, follow_up_edited,
                mode, actual_to, original_to, send_status, smtp_response, send_error,
                submitted_by, submitted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slug,
                run_id,
                original["subject"],
                original["body"],
                original["whatsapp"],
                original["follow_up"],
                subject,
                body,
                whatsapp,
                follow_up,
                subject_edited,
                body_edited,
                whatsapp_edited,
                follow_up_edited,
                "dry-run" if dry_run else "live",
                actual_to,
                original_to,
                send_status,
                smtp_response,
                send_error,
                submitted_by,
                submitted_at,
            ),
        )

    _append_send_log(
        run_id,
        {
            "timestamp": submitted_at,
            "slug": slug,
            "mode": "dry-run" if dry_run else "live",
            "original_to": original_to,
            "actual_to": actual_to,
            "subject": final_subject,
            "status": send_status,
            "smtp_response": smtp_response,
            "error": send_error,
            "submitted_by": submitted_by,
            "source": "webui",
            "edited_fields": {
                "subject": bool(subject_edited),
                "body": bool(body_edited),
                "whatsapp": bool(whatsapp_edited),
                "follow_up": bool(follow_up_edited),
            },
        },
    )

    ctx = _build_row_context(slug, run_id)
    if not ctx:
        return HTMLResponse("profile not found", status_code=404)
    ctx["request"] = request
    ctx["current_user"] = current_user
    return templates.TemplateResponse("_row.html", ctx)
