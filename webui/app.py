"""
Minimal sales feedback webUI.

Reads profile JSONs from runs/<run_id>/05_profiles/ and serves a list page
with an inline feedback form per company. Feedback and dry-run send tracking
are appended to a SQLite database (feedback.db).
"""
from __future__ import annotations

import datetime as dt
import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
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
DB_PATH = Path(os.environ.get("FEEDBACK_DB_PATH") or (Path(__file__).resolve().parent / "feedback.db"))


def list_available_runs() -> list[str]:
    """Return run IDs that have profiles, sorted newest first."""
    runs = []
    if RUNS_DIR.is_dir():
        for d in RUNS_DIR.iterdir():
            if d.is_dir() and (d / "05_profiles" / "profiles").is_dir():
                profiles = list((d / "05_profiles" / "profiles").glob("*.json"))
                if profiles:
                    runs.append(d.name)
    runs.sort(reverse=True)
    return runs


def _default_run_id() -> str:
    runs = list_available_runs()
    return runs[0] if runs else "2026-04-30"
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
EXCLUDED_STATUS = "excluded"
INITIAL_USERS = [
    ("nicky", "Nicky", 0),
    ("clement", "Clement", 0),
    ("jeff", "Jeff", 0),
    ("admin", "Admin", 1),
]
EDIT_CLASSIFICATIONS = {"content", "tone", "unclear"}


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


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _initial_user_password() -> str:
    return (os.environ.get("WEBUI_INITIAL_PASSWORD") or os.environ.get("WEBUI_PASSWORD") or "").strip()


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return base64.b64encode(salt + dk).decode()


def _verify_password(password: str, stored: str) -> bool:
    try:
        raw = base64.b64decode(stored)
    except Exception:
        return False
    if len(raw) < 17:
        return False
    salt, dk = raw[:16], raw[16:]
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return secrets.compare_digest(dk, check)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, ddl: str) -> None:
    if column_name not in _table_columns(conn, table_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")


def _map_feedback_status_to_manual(status_value: str) -> str | None:
    status_value = str(status_value or "").strip()
    if status_value in {"", "未发", EXCLUDED_STATUS}:
        return None
    if status_value == "已发待回":
        return "已发"
    if status_value == "已回":
        return "已收到有效回复"
    return status_value


def _migrate_lead_status(conn: sqlite3.Connection) -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    keys = {
        (row["profile_slug"], row["run_id"])
        for row in conn.execute("SELECT DISTINCT profile_slug, run_id FROM feedback")
    }
    keys.update(
        (row["profile_slug"], row["run_id"])
        for row in conn.execute("SELECT DISTINCT profile_slug, run_id FROM send_tracking")
    )

    for slug, run_id in sorted(keys):
        sent_row = conn.execute(
            """
            SELECT submitted_at FROM send_tracking
            WHERE profile_slug = ? AND run_id = ? AND send_status = 'sent'
            ORDER BY submitted_at DESC, id DESC LIMIT 1
            """,
            (slug, run_id),
        ).fetchone()
        auto_status = "已发" if sent_row else "未发"
        latest_feedback = conn.execute(
            """
            SELECT status, submitted_at FROM feedback
            WHERE profile_slug = ? AND run_id = ?
            ORDER BY submitted_at DESC, id DESC LIMIT 1
            """,
            (slug, run_id),
        ).fetchone()
        manual_status = (
            _map_feedback_status_to_manual(latest_feedback["status"])
            if latest_feedback
            else None
        )
        effective_status = manual_status or auto_status
        updated_at = (
            latest_feedback["submitted_at"]
            if latest_feedback
            else (sent_row["submitted_at"] if sent_row else now)
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO lead_status (
                profile_slug, run_id, auto_status, manual_status, effective_status, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (slug, run_id, auto_status, manual_status, effective_status, updated_at),
        )


def _seed_initial_users(conn: sqlite3.Connection) -> None:
    initial_password = _initial_user_password()
    if not initial_password:
        return
    created_at = dt.datetime.now().isoformat(timespec="seconds")
    for username, display_name, is_admin in INITIAL_USERS:
        conn.execute(
            """
            INSERT OR IGNORE INTO users (
                username, display_name, password_hash, is_admin, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, display_name, _hash_password(initial_password), is_admin, created_at),
        )


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
                message_id TEXT,
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
        _ensure_column(conn, "send_tracking", "message_id", "message_id TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS received_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imap_uid TEXT NOT NULL,
                message_id TEXT,
                in_reply_to TEXT,
                profile_slug TEXT,
                run_id TEXT,
                received_at TEXT NOT NULL,
                from_email TEXT NOT NULL,
                subject TEXT NOT NULL,
                body_text TEXT NOT NULL DEFAULT '',
                match_method TEXT NOT NULL DEFAULT 'unmatched',
                llm_verdict TEXT,
                llm_reasoning TEXT,
                judged_at TEXT,
                UNIQUE(imap_uid)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_replies_slug "
            "ON received_replies(profile_slug, run_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_replies_from "
            "ON received_replies(from_email)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lead_status (
                profile_slug TEXT NOT NULL,
                run_id TEXT NOT NULL,
                auto_status TEXT NOT NULL DEFAULT '未发',
                manual_status TEXT,
                effective_status TEXT NOT NULL DEFAULT '未发',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (profile_slug, run_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS status_change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_slug TEXT NOT NULL,
                run_id TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                changed_by TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT NOT NULL,
                source TEXT NOT NULL,
                reason TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_type (
                profile_slug TEXT NOT NULL,
                run_id TEXT NOT NULL,
                type TEXT,
                source TEXT NOT NULL DEFAULT 'manual',
                assigned_by TEXT,
                assigned_at TEXT,
                PRIMARY KEY (profile_slug, run_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_type_change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_slug TEXT NOT NULL,
                run_id TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                changed_by TEXT NOT NULL,
                from_type TEXT,
                to_type TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS edit_classifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                send_id INTEGER NOT NULL,
                profile_slug TEXT NOT NULL,
                run_id TEXT NOT NULL,
                field TEXT NOT NULL,
                original_text TEXT NOT NULL,
                edited_text TEXT NOT NULL,
                classification TEXT NOT NULL,
                reasoning TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL,
                classified_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_edit_class_send "
            "ON edit_classifications(send_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS content_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                classification_id INTEGER NOT NULL,
                profile_slug TEXT NOT NULL,
                run_id TEXT NOT NULL,
                field TEXT NOT NULL,
                original_text TEXT NOT NULL,
                edited_text TEXT NOT NULL,
                reasoning TEXT NOT NULL,
                submitted_by TEXT NOT NULL,
                flagged_at TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                resolved_at TEXT,
                resolved_by TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_content_flags_unresolved "
            "ON content_flags(resolved, flagged_at DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tone_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                field TEXT NOT NULL,
                original_text TEXT NOT NULL,
                edited_text TEXT NOT NULL,
                profile_slug TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tone_examples_user "
            "ON tone_examples(username, field)"
        )
        _seed_initial_users(conn)
        _migrate_lead_status(conn)


_init_db()


def _auth_error(detail: str = "Authentication required") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Basic"},
    )


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> str:
    if credentials is None:
        raise _auth_error()
    username = credentials.username.strip().lower()
    if not username:
        raise _auth_error("Invalid credentials")
    with _db() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None or not _verify_password(credentials.password, row["password_hash"]):
        raise _auth_error("Invalid credentials")
    return username


def require_admin(current_user: str = Depends(require_auth)) -> str:
    with _db() as conn:
        row = conn.execute(
            "SELECT is_admin FROM users WHERE username = ?",
            (current_user,),
        ).fetchone()
    if row is None or not row["is_admin"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return current_user


def _user_display_name(username: str) -> str:
    with _db() as conn:
        row = conn.execute(
            "SELECT display_name FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    return row["display_name"] if row else username


def _user_is_admin(username: str) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT is_admin FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    return bool(row and row["is_admin"])


def _llm_classify_edit(field: str, original: str, edited: str) -> dict:
    api_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("GLM_API_KEY")
        or ""
    ).strip()
    if not api_key:
        return {"classification": "unclear", "reasoning": "LLM_API_KEY not set"}

    prompt = (
        "你是一个邮件编辑意图分类助手。\n\n"
        f"邮件字段：{field}\n\n"
        f"AI 原文：\n{original}\n\n"
        f"人工改后：\n{edited}\n\n"
        "请判断这次改动属于：\n"
        "- content：原文有事实错误、关联性不对、信息不准确，需要人工复查\n"
        "- tone：内容没有问题，只是措辞风格、语气或落款不同，是个人习惯\n"
        "- unclear：无法判断\n\n"
        '只返回 JSON：{"classification": "content 或 tone 或 unclear", "reasoning": "一句话说明"}'
    )

    try:
        from openai import OpenAI

        client_kwargs: dict = {"api_key": api_key}
        base_url = (os.environ.get("LLM_BASE_URL") or "").strip()
        if base_url:
            client_kwargs["base_url"] = base_url
        client_kwargs["timeout"] = float(os.environ.get("LLM_TIMEOUT_SECONDS", "30"))
        client = OpenAI(**client_kwargs)
        response = client.chat.completions.create(
            model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0,
        )
        payload = json.loads(response.choices[0].message.content or "{}")
    except Exception as exc:
        reason = str(exc).replace(api_key, "***")
        return {"classification": "unclear", "reasoning": _truncate(f"error: {reason}", 1000)}

    classification = str(payload.get("classification") or "unclear").strip().lower()
    if classification not in EDIT_CLASSIFICATIONS:
        classification = "unclear"
    reasoning = str(payload.get("reasoning") or "").strip()
    return {"classification": classification, "reasoning": _truncate(reasoning, 1000)}


def _classify_and_store_edits(
    send_id: int,
    profile_slug: str,
    run_id: str,
    username: str,
    submitted_by: str,
    edits: dict,
) -> None:
    for field, (original, edited, was_edited) in edits.items():
        original = str(original or "")
        edited = str(edited or "")
        if not was_edited or not original.strip() or not edited.strip():
            continue

        result = _llm_classify_edit(field, original, edited)
        classification = str(result.get("classification") or "unclear").strip().lower()
        if classification not in EDIT_CLASSIFICATIONS:
            classification = "unclear"
        reasoning = str(result.get("reasoning") or "").strip()
        now = dt.datetime.now().isoformat(timespec="seconds")

        with _db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO edit_classifications (
                    send_id, profile_slug, run_id, field, original_text, edited_text,
                    classification, reasoning, username, classified_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    send_id,
                    profile_slug,
                    run_id,
                    field,
                    original,
                    edited,
                    classification,
                    reasoning,
                    username,
                    now,
                ),
            )
            classification_id = cursor.lastrowid
            if classification == "content":
                conn.execute(
                    """
                    INSERT INTO content_flags (
                        classification_id, profile_slug, run_id, field, original_text,
                        edited_text, reasoning, submitted_by, flagged_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        classification_id,
                        profile_slug,
                        run_id,
                        field,
                        original,
                        edited,
                        reasoning,
                        submitted_by,
                        now,
                    ),
                )
            elif classification == "tone":
                conn.execute(
                    """
                    INSERT INTO tone_examples (
                        username, field, original_text, edited_text, profile_slug, recorded_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (username, field, original, edited, profile_slug, now),
                )


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


def _sender_text(text: object, sender_name: str) -> str:
    value = str(text or "")
    return value.replace("Nicky", sender_name) if sender_name else value


def load_sales_leads(run_id: str, sender_name: str = "") -> list[dict]:
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
        generated_at = profile.get("generated_at", "")
        rows.append(
            {
                "slug": json_path.stem,
                "run_id": run_id,
                "generated_at": generated_at[:10] if generated_at else "",
                "display_name": company.get("display_name", ""),
                "country": company.get("country", ""),
                "rating": company.get("step4_rating", ""),
                "website": company.get("website", ""),
                "email": emails[0] if emails else "",
                "phone": phones[0] if phones else "",
                "has_contact_page": "Yes" if contact_pages else "",
                "subject": _sender_text(
                    outreach.get("cold_email_subject", ""),
                    sender_name,
                ),
                "body": _sender_text(
                    outreach.get("cold_email_body", ""),
                    sender_name,
                ),
                "whatsapp": _sender_text(
                    outreach.get("whatsapp_or_linkedin_message", ""),
                    sender_name,
                ),
                "follow_up": _sender_text(
                    outreach.get("follow_up_email", ""),
                    sender_name,
                ),
                "bio": body.get("bio_cn", ""),
                "business_relevance": body.get("business_relevance_cn", ""),
            }
        )
    return rows


def load_all_leads(sender_name: str = "") -> list[dict]:
    """Load leads from all available runs, newest run first."""
    all_rows: list[dict] = []
    for run_id in list_available_runs():
        all_rows.extend(load_sales_leads(run_id, sender_name=sender_name))
    return all_rows


def load_latest_feedback(slug: str, run_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM feedback WHERE profile_slug=? AND run_id=? "
            "ORDER BY submitted_at DESC, id DESC LIMIT 1",
            (slug, run_id),
        ).fetchone()
    if not row:
        return None
    if not row["status"]:
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
            "SELECT * FROM feedback WHERE profile_slug=? AND run_id=? AND status != '' "
            "ORDER BY submitted_at DESC, id DESC",
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


def load_latest_status(slug: str, run_id: str) -> str:
    with _db() as conn:
        row = conn.execute(
            "SELECT status FROM feedback WHERE profile_slug=? AND run_id=? "
            "ORDER BY submitted_at DESC, id DESC LIMIT 1",
            (slug, run_id),
        ).fetchone()
    return str(row["status"] or "") if row else ""


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


def _record_feedback_status(
    slug: str,
    run_id: str,
    status_value: str,
    submitted_by: str,
    tags: list[str] | None = None,
    note: str = "",
) -> None:
    submitted_at = dt.datetime.now().isoformat(timespec="seconds")
    with _db() as conn:
        conn.execute(
            "INSERT INTO feedback (profile_slug, run_id, status, tags, note, "
            "submitted_by, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                slug,
                run_id,
                status_value,
                json.dumps(tags or [], ensure_ascii=False),
                note,
                submitted_by,
                submitted_at,
            ),
        )


def _build_row_context(
    slug: str,
    run_id: str,
    leads: list[dict] | None = None,
    sender_name: str = "",
) -> dict:
    leads = leads if leads is not None else load_sales_leads(
        run_id,
        sender_name=sender_name,
    )
    lead = next((l for l in leads if l["slug"] == slug), None)
    if lead is None:
        return {}
    latest = load_latest_feedback(slug, run_id)
    history = load_history(slug, run_id)
    latest_send = load_latest_send(slug, run_id)
    latest_status = load_latest_status(slug, run_id)
    return {
        "r": {
            "lead": lead,
            "latest": latest,
            "history": history,
            "latest_send": latest_send,
            "status": latest_status,
        },
        "run_id": run_id,
        "status_options": STATUS_OPTIONS,
        "tag_options": TAG_OPTIONS,
        "send_mode": "dry-run" if _is_dry_run() else "live",
        "truncate": _truncate,
    }


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    show_excluded: int = 0,
    current_user: str = Depends(require_auth),
) -> HTMLResponse:
    current_user_display = _user_display_name(current_user)
    leads = load_all_leads(sender_name=current_user_display)
    enriched = []
    excluded_count = 0
    include_excluded = show_excluded == 1
    for lead in leads:
        run_id = lead["run_id"]
        latest = load_latest_feedback(lead["slug"], run_id)
        history = load_history(lead["slug"], run_id)
        latest_send = load_latest_send(lead["slug"], run_id)
        latest_status = load_latest_status(lead["slug"], run_id)
        if latest_status == EXCLUDED_STATUS:
            excluded_count += 1
            if not include_excluded:
                continue
        enriched.append(
            {
                "lead": lead,
                "latest": latest,
                "history": history,
                "latest_send": latest_send,
                "status": latest_status,
            }
        )
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "rows": enriched,
            "current_user": current_user,
            "current_user_display": current_user_display,
            "current_user_is_admin": _user_is_admin(current_user),
            "show_excluded": include_excluded,
            "excluded_count": excluded_count,
            "status_options": STATUS_OPTIONS,
            "tag_options": TAG_OPTIONS,
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
    run_id = form.get("run_id", _default_run_id()).strip() or _default_run_id()
    status = form.get("status", "").strip()
    tags = form.getlist("tags")
    note = form.get("note", "").strip()

    if not slug or not status:
        return HTMLResponse("missing required field", status_code=400)

    submitted_by = _user_display_name(current_user)
    _record_feedback_status(slug, run_id, status, submitted_by, tags=tags, note=note)

    ctx = _build_row_context(slug, run_id, sender_name=submitted_by)
    if not ctx:
        return HTMLResponse("profile not found", status_code=404)
    ctx["request"] = request
    ctx["current_user"] = current_user
    ctx["current_user_display"] = submitted_by
    return templates.TemplateResponse("_row.html", ctx)


@app.post("/exclude", response_class=HTMLResponse)
async def exclude_lead(
    request: Request,
    current_user: str = Depends(require_auth),
) -> HTMLResponse:
    form = await request.form()
    slug = str(form.get("slug") or "").strip()
    run_id = str(form.get("run_id") or _default_run_id()).strip() or _default_run_id()
    if not slug:
        return HTMLResponse("missing required field", status_code=400)

    submitted_by = _user_display_name(current_user)
    _record_feedback_status(slug, run_id, EXCLUDED_STATUS, submitted_by)
    ctx = _build_row_context(slug, run_id, sender_name=submitted_by)
    if not ctx:
        return HTMLResponse("profile not found", status_code=404)
    ctx["request"] = request
    ctx["current_user"] = current_user
    ctx["current_user_display"] = submitted_by
    return templates.TemplateResponse("_row.html", ctx)


@app.post("/restore", response_class=HTMLResponse)
async def restore_lead(
    request: Request,
    current_user: str = Depends(require_auth),
) -> HTMLResponse:
    form = await request.form()
    slug = str(form.get("slug") or "").strip()
    run_id = str(form.get("run_id") or _default_run_id()).strip() or _default_run_id()
    if not slug:
        return HTMLResponse("missing required field", status_code=400)

    submitted_by = _user_display_name(current_user)
    _record_feedback_status(slug, run_id, "", submitted_by)
    ctx = _build_row_context(slug, run_id, sender_name=submitted_by)
    if not ctx:
        return HTMLResponse("profile not found", status_code=404)
    ctx["request"] = request
    ctx["current_user"] = current_user
    ctx["current_user_display"] = submitted_by
    return templates.TemplateResponse("_row.html", ctx)


@app.post("/send", response_class=HTMLResponse)
async def submit_send(
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: str = Depends(require_auth),
) -> HTMLResponse:
    form = await request.form()
    slug = str(form.get("slug") or "").strip()
    run_id = str(form.get("run_id") or _default_run_id()).strip() or _default_run_id()
    form_actual_to = str(form.get("form_actual_to") or "").strip()
    subject = str(form.get("subject") or "").strip()
    body = str(form.get("body") or "")
    whatsapp = str(form.get("whatsapp") or "")
    follow_up = str(form.get("follow_up") or "")

    if not slug or not subject or not body.strip():
        return HTMLResponse("missing required field", status_code=400)

    submitted_by = _user_display_name(current_user)
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
        cursor = conn.execute(
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
        send_id = int(cursor.lastrowid)

    edited_fields = {
        "subject": (original["subject"], subject, subject_edited),
        "body": (original["body"], body, body_edited),
        "whatsapp": (original["whatsapp"], whatsapp, whatsapp_edited),
        "follow_up": (original["follow_up"], follow_up, follow_up_edited),
    }
    if any(was_edited for _, _, was_edited in edited_fields.values()):
        background_tasks.add_task(
            _classify_and_store_edits,
            send_id=send_id,
            profile_slug=slug,
            run_id=run_id,
            username=current_user,
            submitted_by=submitted_by,
            edits=edited_fields,
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

    ctx = _build_row_context(slug, run_id, sender_name=submitted_by)
    if not ctx:
        return HTMLResponse("profile not found", status_code=404)
    ctx["request"] = request
    ctx["current_user"] = current_user
    ctx["current_user_display"] = submitted_by
    return templates.TemplateResponse("_row.html", ctx)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(
    request: Request,
    current_user: str = Depends(require_admin),
) -> HTMLResponse:
    with _db() as conn:
        users = conn.execute(
            "SELECT username, display_name, is_admin FROM users ORDER BY id"
        ).fetchall()
        content_flags = conn.execute(
            "SELECT * FROM content_flags WHERE resolved = 0 ORDER BY flagged_at DESC"
        ).fetchall()
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "users": users,
            "content_flags": content_flags,
            "current_user": current_user,
            "current_user_display": _user_display_name(current_user),
        },
    )


@app.post("/admin/change-password")
async def change_password(
    request: Request,
    _: str = Depends(require_admin),
) -> RedirectResponse:
    form = await request.form()
    username = str(form.get("username") or "").strip().lower()
    new_password = str(form.get("new_password") or "").strip()
    if not username or not new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="username and new_password required",
        )
    with _db() as conn:
        cursor = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (_hash_password(new_password), username),
        )
    if cursor.rowcount < 1:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/resolve-flag")
async def resolve_flag(
    request: Request,
    admin: str = Depends(require_admin),
) -> RedirectResponse:
    form = await request.form()
    try:
        flag_id = int(form.get("flag_id") or 0)
    except ValueError:
        flag_id = 0
    if flag_id < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="flag_id required")

    now = dt.datetime.now().isoformat(timespec="seconds")
    with _db() as conn:
        cursor = conn.execute(
            """
            UPDATE content_flags
            SET resolved = 1, resolved_at = ?, resolved_by = ?
            WHERE id = ?
            """,
            (now, admin, flag_id),
        )
    if cursor.rowcount < 1:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flag not found")
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
