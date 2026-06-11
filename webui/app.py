"""
Minimal sales feedback webUI.

Reads profile JSONs from runs/<run_id>/05_profiles/ and serves a list page
with an inline feedback form per company. Feedback and dry-run send tracking
are appended to a SQLite database (feedback.db).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from send_outreach import (
    SEND_LOG_JSONL,
    ConfigError,
    _build_message,
    _send_message,
    load_send_config,
)
from webui.lead_priority import (
    DEFAULT_PRIORITY,
    compute_final_score,
    load_ranking_inputs,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "runs"
DB_PATH = Path(os.environ.get("FEEDBACK_DB_PATH") or (Path(__file__).resolve().parent / "feedback.db"))


def _imap_loop() -> None:
    while True:
        try:
            from webui.imap_poller import poll_once

            poll_once(DB_PATH)
            _check_synthesis_threshold()
        except Exception as exc:
            logging.warning("IMAP poll error: %s", exc)
        time.sleep(600)


def _check_synthesis_threshold() -> None:
    """Trigger synthesis if there are at least 3 new valid non-test replies."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            last = conn.execute(
                "SELECT run_at FROM synthesis_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            since = last["run_at"] if last else "1970-01-01T00:00:00"
            count = conn.execute(
                r"""
                SELECT COUNT(*)
                FROM received_replies
                WHERE llm_verdict = 'valid'
                  AND judged_at > ?
                  AND run_id NOT LIKE 'test\_%' ESCAPE '\'
                """,
                (since,),
            ).fetchone()[0]
        finally:
            conn.close()

        if count >= 3:
            from webui.synthesizer import run_synthesis

            threading.Thread(
                target=run_synthesis,
                kwargs={"db_path": DB_PATH, "trigger": "reply_threshold"},
                daemon=True,
            ).start()
            logging.info(
                "synthesis triggered: %d new valid replies since %s", count, since
            )
    except Exception as exc:
        logging.warning("synthesis threshold check error: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("IMAP_HOST") or os.environ.get("SMTP_HOST"):
        thread = threading.Thread(target=_imap_loop, daemon=True)
        thread.start()
    yield


def _list_runs(include_test: bool) -> list[str]:
    runs = []
    if RUNS_DIR.is_dir():
        for d in RUNS_DIR.iterdir():
            if d.is_dir() and (d / "05_profiles" / "profiles").is_dir():
                profiles = list((d / "05_profiles" / "profiles").glob("*.json"))
                if profiles:
                    is_test = d.name.startswith("test_")
                    if is_test != include_test:
                        continue
                    runs.append(d.name)
    runs.sort(reverse=True)
    return runs


def list_available_runs() -> list[str]:
    """Return non-test run IDs that have profiles, sorted newest first."""
    return _list_runs(include_test=False)


def list_test_runs() -> list[str]:
    """Return test run IDs that have profiles, sorted newest first."""
    return _list_runs(include_test=True)


def _default_run_id() -> str:
    runs = list_available_runs()
    return runs[0] if runs else "2026-04-30"


def _is_dry_run() -> bool:
    return os.environ.get("SEND_MODE", "live").strip().lower() == "dry-run"


def _is_test_run(run_id: str) -> bool:
    return str(run_id or "").startswith("test_")


_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

STATUS_OPTIONS = [
    "未发",
    "已发",
    "已收到有效回复",
    "已询价",
    "不相关",
    "国家错",
    "邮箱无效",
    "联系人不对",
    "已成交",
]
CUSTOMER_TYPE_OPTIONS = [
    "原料分销商",
    "OEM制造商",
    "品牌商",
    "不相关",
]
CUSTOMER_TYPE_AUTO_MAP = {
    "原料分销商": "原料分销商",
    "OEM制造商": "OEM制造商",
    "品牌商": "品牌商",
    "不相关": "不相关",
}
CUSTOMER_TYPE_MANUAL_REMAP = {
    "分销商": "原料分销商",
    "品牌方": "品牌商",
    "工厂客户": "OEM制造商",
}
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
    title="Sales Feedback",
    lifespan=lifespan,
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
templates.env.filters["fromjson"] = json.loads
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


def _profile_company_name(run_id: str, slug: str) -> str:
    if not (_safe_path_component(run_id) and _safe_path_component(slug)):
        return ""
    path = RUNS_DIR / run_id / "05_profiles" / "profiles" / f"{slug}.json"
    try:
        with path.open("r", encoding="utf-8") as handle:
            profile = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""
    company = profile.get("company") if isinstance(profile, dict) else {}
    if not isinstance(company, dict):
        return ""
    return str(company.get("display_name") or company.get("legal_name") or "").strip()


def _backfill_reply_company_names(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, profile_slug, run_id FROM received_replies
        WHERE company_name = '' AND profile_slug IS NOT NULL AND run_id IS NOT NULL
        """
    ).fetchall()
    for row in rows:
        company_name = _profile_company_name(row["run_id"], row["profile_slug"])
        if company_name:
            conn.execute(
                "UPDATE received_replies SET company_name = ? WHERE id = ?",
                (company_name, row["id"]),
            )


def _map_feedback_status_to_manual(status_value: str) -> str | None:
    status_value = str(status_value or "").strip()
    if status_value in {"", "未发", EXCLUDED_STATUS}:
        return None
    if status_value == "已发待回":
        return "已发"
    if status_value == "已回":
        return "已收到有效回复"
    return status_value


def _profile_auto_customer_type(profile_path: Path) -> str | None:
    try:
        with profile_path.open("r", encoding="utf-8") as handle:
            profile = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(profile, dict):
        return None
    company = profile.get("company") if isinstance(profile.get("company"), dict) else {}
    raw_type = str(company.get("step4_customer_type") or "").strip()
    return CUSTOMER_TYPE_AUTO_MAP.get(raw_type)


def _auto_customer_type_for_lead(slug: str, run_id: str) -> str | None:
    if not (_safe_path_component(run_id) and _safe_path_component(slug)):
        return None
    path = RUNS_DIR / run_id / "05_profiles" / "profiles" / f"{slug}.json"
    return _profile_auto_customer_type(path)


def _iter_profile_json_paths():
    if not RUNS_DIR.is_dir():
        return
    for run_dir in sorted(RUNS_DIR.iterdir()):
        profiles_dir = run_dir / "05_profiles" / "profiles"
        if not profiles_dir.is_dir():
            continue
        for profile_path in sorted(profiles_dir.glob("*.json")):
            yield run_dir.name, profile_path


def _migrate_customer_type(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE customer_type
        SET type = CASE type
            WHEN '分销商' THEN '原料分销商'
            WHEN '品牌方' THEN '品牌商'
            WHEN '工厂客户' THEN 'OEM制造商'
            ELSE type
        END
        """
    )
    for run_id, profile_path in _iter_profile_json_paths() or ():
        auto_type = _profile_auto_customer_type(profile_path)
        if not auto_type:
            continue
        cursor = conn.execute(
            """
            UPDATE customer_type
            SET auto_type = ?
            WHERE profile_slug = ? AND run_id = ?
            """,
            (auto_type, profile_path.stem, run_id),
        )
        if cursor.rowcount:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO customer_type (
                profile_slug, run_id, auto_type, source
            )
            VALUES (?, ?, ?, 'auto')
            """,
            (profile_path.stem, run_id, auto_type),
        )


def _compute_lead_priority(
    conn: sqlite3.Connection,
    slug: str,
    run_id: str,
    effective_status: str | None = None,
) -> float:
    inputs = load_ranking_inputs(
        conn,
        RUNS_DIR,
        slug,
        run_id,
        status_override=effective_status,
    )
    return compute_final_score(inputs)


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
                profile_slug, run_id, auto_status, manual_status,
                effective_status, updated_at, lead_priority
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slug,
                run_id,
                auto_status,
                manual_status,
                effective_status,
                updated_at,
                _compute_lead_priority(conn, slug, run_id, effective_status),
            ),
        )


def _init_lead_priority(conn: sqlite3.Connection) -> None:
    """Backfill ranking priority once when lead_priority is first added."""
    rows = conn.execute(
        "SELECT profile_slug, run_id, effective_status FROM lead_status"
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            UPDATE lead_status
            SET lead_priority = ?
            WHERE profile_slug = ? AND run_id = ?
            """,
            (
                _compute_lead_priority(
                    conn,
                    row["profile_slug"],
                    row["run_id"],
                    row["effective_status"],
                ),
                row["profile_slug"],
                row["run_id"],
            ),
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
                is_test INTEGER NOT NULL DEFAULT 0,
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
        _ensure_column(conn, "send_tracking", "is_test", "is_test INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS web_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_event_id INTEGER UNIQUE,
                ts INTEGER NOT NULL,
                received_at TEXT NOT NULL,
                session_id TEXT NOT NULL,
                visitor_id TEXT,
                buyer_id INTEGER,
                token TEXT,
                campaign_id TEXT,
                profile_slug TEXT,
                run_id TEXT,
                event_type TEXT NOT NULL,
                url TEXT,
                page_path TEXT,
                page_title TEXT,
                referrer TEXT,
                payload_json TEXT,
                ip_prefix TEXT,
                ip_hash TEXT,
                country TEXT,
                colo TEXT,
                user_agent TEXT,
                is_bot INTEGER NOT NULL DEFAULT 0,
                bot_reason TEXT,
                raw_json TEXT NOT NULL
            )
            """
        )
        _ensure_column(conn, "web_events", "profile_slug", "profile_slug TEXT")
        _ensure_column(conn, "web_events", "run_id", "run_id TEXT")
        _ensure_column(
            conn,
            "web_events",
            "is_demo",
            "is_demo INTEGER NOT NULL DEFAULT 0",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_events_buyer_ts "
            "ON web_events(buyer_id, ts DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_events_session_ts "
            "ON web_events(session_id, ts DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_events_type_ts "
            "ON web_events(event_type, ts DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_events_token_ts "
            "ON web_events(token, ts DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_events_profile_ts "
            "ON web_events(profile_slug, run_id, ts DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracking_ai_summary (
                group_key TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                model TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                token_input INTEGER,
                token_output INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS match_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_token TEXT NOT NULL UNIQUE,
                buyer_id INTEGER,
                profile_slug TEXT,
                run_id TEXT,
                campaign_id TEXT,
                email_token TEXT,
                session_id TEXT,
                visitor_id TEXT,
                company_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'started',
                result_url TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                last_activity_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_match_sessions_buyer "
            "ON match_sessions(buyer_id, last_activity_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_match_sessions_profile "
            "ON match_sessions(profile_slug, run_id, last_activity_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_match_sessions_visitor "
            "ON match_sessions(visitor_id, last_activity_at DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS match_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                question_key TEXT NOT NULL,
                answer_json TEXT NOT NULL,
                answered_at TEXT NOT NULL,
                UNIQUE(match_id, question_key),
                FOREIGN KEY (match_id) REFERENCES match_sessions(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_match_answers_match "
            "ON match_answers(match_id, question_key)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS match_results (
                match_id INTEGER PRIMARY KEY,
                application_scenario TEXT NOT NULL,
                recommended_skus_json TEXT NOT NULL,
                reference_application_id TEXT,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (match_id) REFERENCES match_sessions(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS brief_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_token TEXT NOT NULL UNIQUE,
                match_id INTEGER,
                buyer_id INTEGER,
                profile_slug TEXT,
                run_id TEXT,
                campaign_id TEXT,
                company_name TEXT NOT NULL DEFAULT '',
                challenge_text TEXT NOT NULL DEFAULT '',
                timeline TEXT NOT NULL DEFAULT '',
                deliverables_json TEXT NOT NULL DEFAULT '[]',
                contact_email TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (match_id) REFERENCES match_sessions(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_brief_submissions_match "
            "ON brief_submissions(match_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_brief_submissions_profile "
            "ON brief_submissions(profile_slug, run_id, created_at DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS brief_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brief_id INTEGER,
                match_id INTEGER,
                note_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (brief_id) REFERENCES brief_submissions(id),
                FOREIGN KEY (match_id) REFERENCES match_sessions(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_brief_notes_match "
            "ON brief_notes(match_id, created_at DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS received_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imap_uid TEXT NOT NULL,
                message_id TEXT,
                in_reply_to TEXT,
                profile_slug TEXT,
                run_id TEXT,
                company_name TEXT NOT NULL DEFAULT '',
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
        _ensure_column(conn, "received_replies", "company_name", "company_name TEXT NOT NULL DEFAULT ''")
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
        priority_was_missing = "lead_priority" not in _table_columns(conn, "lead_status")
        _ensure_column(
            conn,
            "lead_status",
            "lead_priority",
            "lead_priority REAL NOT NULL DEFAULT 0.5",
        )
        _ensure_column(
            conn,
            "lead_status",
            "feedback_score",
            "feedback_score REAL",
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
                auto_type TEXT,
                type TEXT,
                source TEXT NOT NULL DEFAULT 'manual',
                assigned_by TEXT,
                assigned_at TEXT,
                PRIMARY KEY (profile_slug, run_id)
            )
            """
        )
        _ensure_column(conn, "customer_type", "auto_type", "auto_type TEXT")
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS synthesis_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                triggered_by TEXT NOT NULL,
                feedback_count INTEGER NOT NULL DEFAULT 0,
                valid_reply_count INTEGER NOT NULL DEFAULT 0,
                content_flag_count INTEGER NOT NULL DEFAULT 0,
                priority_updates_json TEXT NOT NULL DEFAULT '[]',
                prompt_suggestions_json TEXT NOT NULL DEFAULT '{}',
                patterns_json TEXT NOT NULL DEFAULT '[]',
                summary TEXT NOT NULL DEFAULT '',
                llm_raw TEXT NOT NULL DEFAULT '',
                run_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_synthesis_run_at "
            "ON synthesis_log(run_at DESC)"
        )
        _seed_initial_users(conn)
        _migrate_lead_status(conn)
        _migrate_customer_type(conn)
        if priority_was_missing:
            _init_lead_priority(conn)
        _backfill_reply_company_names(conn)


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


def _tracking_ingest_secret() -> str:
    return (
        os.environ.get("WEB_TRACKING_INGEST_SECRET")
        or os.environ.get("ECS_API_SECRET")
        or ""
    ).strip()


def _require_tracking_ingest(request: Request) -> None:
    secret = _tracking_ingest_secret()
    if not secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Tracking ingest secret not configured")
    provided = (
        request.headers.get("X-API-Key")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )
    if not secrets.compare_digest(provided or "", secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid tracking ingest secret")


def _event_value(event: dict, key: str, default: object = None) -> object:
    value = event.get(key)
    return default if value is None else value


def _coerce_event_int(value: object) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _store_web_events(events: list[dict]) -> int:
    received_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    inserted = 0
    with _db() as conn:
        for event in events:
            if not isinstance(event, dict):
                continue
            source_event_id = _coerce_event_int(event.get("id"))
            ts = _coerce_event_int(event.get("ts")) or int(time.time())
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO web_events (
                    source_event_id, ts, received_at, session_id, visitor_id, buyer_id,
                    token, campaign_id, profile_slug, run_id, event_type, url, page_path, page_title, referrer,
                    payload_json, ip_prefix, ip_hash, country, colo, user_agent, is_bot,
                    bot_reason, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_event_id,
                    ts,
                    received_at,
                    str(_event_value(event, "session_id", ""))[:160],
                    str(_event_value(event, "visitor_id", "") or "")[:160],
                    _coerce_event_int(event.get("buyer_id")),
                    str(_event_value(event, "token", "") or "")[:160],
                    str(_event_value(event, "campaign_id", "") or "")[:160],
                    str(_event_value(event, "profile_slug", "") or "")[:160],
                    str(_event_value(event, "run_id", "") or "")[:160],
                    str(_event_value(event, "event_type", "") or "")[:80],
                    str(_event_value(event, "url", "") or "")[:1200],
                    str(_event_value(event, "page_path", "") or "")[:600],
                    str(_event_value(event, "page_title", "") or "")[:300],
                    str(_event_value(event, "referrer", "") or "")[:1200],
                    str(_event_value(event, "payload_json", "") or "")[:12000],
                    str(_event_value(event, "ip_prefix", "") or "")[:80],
                    str(_event_value(event, "ip_hash", "") or "")[:128],
                    str(_event_value(event, "country", "") or "")[:16],
                    str(_event_value(event, "colo", "") or "")[:16],
                    str(_event_value(event, "user_agent", "") or "")[:700],
                    1 if _coerce_event_int(event.get("is_bot")) else 0,
                    str(_event_value(event, "bot_reason", "") or "")[:120],
                    json.dumps(event, ensure_ascii=False),
                ),
            )
            inserted += cursor.rowcount
        conn.commit()
    return inserted


@app.post("/api/ingest_events")
async def ingest_events(request: Request) -> dict:
    _require_tracking_ingest(request)
    payload = await request.json()
    events = payload.get("events") if isinstance(payload, dict) else payload
    if not isinstance(events, list):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="events must be a list")
    if len(events) > 1000:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="max 1000 events per request")
    inserted = _store_web_events(events)
    return {"ok": True, "received": len(events), "inserted": inserted}


@app.get("/api/web_events_summary")
def web_events_summary(_: str = Depends(require_admin)) -> dict:
    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM web_events").fetchone()[0]
        human = conn.execute("SELECT COUNT(*) FROM web_events WHERE is_bot = 0").fetchone()[0]
        latest = conn.execute(
            """
            SELECT ts, buyer_id, campaign_id, profile_slug, run_id, event_type, page_path, country, is_bot
            FROM web_events
            ORDER BY id DESC
            LIMIT 50
            """
        ).fetchall()
        by_type = conn.execute(
            """
            SELECT event_type, COUNT(*) AS count
            FROM web_events
            WHERE is_bot = 0
            GROUP BY event_type
            ORDER BY count DESC, event_type
            LIMIT 20
            """
        ).fetchall()
    return {
        "ok": True,
        "total": total,
        "human": human,
        "by_type": [dict(row) for row in by_type],
        "latest": [dict(row) for row in latest],
    }


MATCH_QUESTION_LABELS = {
    "q1": "Product category",
    "q2": "Preferred formats",
    "q3": "Annual volume",
    "q4": "Application scenario",
}
MATCH_VALUE_LABELS = {
    "beverage": "Beverage",
    "bar": "Bar & snack",
    "supplement": "Supplement",
    "bakery": "Bakery",
    "confectionery": "Confectionery",
    "other": "Something else",
    "whole": "Whole dried berry",
    "powder": "Freeze-dried or fruit powder",
    "puree": "Puree",
    "juice": "Juice or concentrate",
    "extract": "Leaf, extract, or oil",
    "open": "Open to suggestions",
    "lt500": "Less than 500 kg",
    "500_5t": "500 kg - 5 tonnes",
    "5_20t": "5 - 20 tonnes",
    "gt20": "More than 20 tonnes",
    "rtd_beverage": "RTD beverage",
    "bar_formulation": "Bar formulation",
    "hot_beverage_mix": "Hot beverage & tea blend",
    "capsule_fill": "Capsule fill",
    "powder_blend": "Functional powder blend",
    "topping_decoration": "Topping & decoration",
    "exploring": "Still exploring",
    "now": "Sourcing now",
    "6mo": "Within 6 months",
    "next_year": "Exploring for next year",
    "research": "Just researching",
    "spec": "Spec sheets",
    "coa": "Current CoAs",
    "sample": "Sample request",
    "pricing": "Pricing tier",
}
MATCH_EVENT_TYPES = {
    "match_started",
    "match_step_answered",
    "match_abandoned",
    "match_completed",
    "recommendation_viewed",
    "brief_submitted",
    "brief_note_added",
}
MATCH_FORMAT_FILTERS = {
    "whole": {"whole-dried-berry"},
    "powder": {"functional-extract", "leaf-powder"},
    "puree": {"puree"},
    "juice": {"puree"},
    "extract": {"leaf", "leaf-powder", "functional-extract", "oil"},
}


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _new_public_token(prefix: str) -> str:
    return prefix + "_" + secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]


def _json_dumps(value: object) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _json_loads(value: object, fallback: object = None) -> object:
    if fallback is None:
        fallback = {}
    try:
        return json.loads(str(value or ""))
    except (TypeError, json.JSONDecodeError):
        return fallback


def _answer_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value).strip()]


def _answer_label(value: object) -> str:
    values = _answer_list(value)
    return ", ".join(MATCH_VALUE_LABELS.get(item, item) for item in values)


def _safe_public_text(value: object, max_len: int = 2000) -> str:
    return str(value or "").strip()[:max_len]


def _public_attribution(request: Request, body: dict) -> dict:
    cookies = request.cookies
    buyer_id = _coerce_event_int(body.get("buyer_id") or cookies.get("_rv_bid"))
    profile_slug = _safe_public_text(body.get("profile_slug") or "", 160)
    run_id = _safe_public_text(body.get("run_id") or "", 160)
    company_name = _safe_public_text(body.get("company_name") or "", 256)
    if not company_name and profile_slug and run_id:
        company_name = _profile_company_name(run_id, profile_slug)
    return {
        "buyer_id": buyer_id,
        "profile_slug": profile_slug,
        "run_id": run_id,
        "campaign_id": _safe_public_text(body.get("campaign_id") or cookies.get("_rv_campaign") or "", 160),
        "email_token": _safe_public_text(body.get("token") or body.get("email_token") or cookies.get("_rv_token") or "", 160),
        "session_id": _safe_public_text(body.get("session_id") or cookies.get("_rv_sess") or "", 160),
        "visitor_id": _safe_public_text(body.get("visitor_id") or body.get("client_id") or "", 160),
        "company_name": company_name,
    }


def _insert_public_event(
    conn: sqlite3.Connection,
    request: Request,
    *,
    event_type: str,
    attribution: dict,
    payload: dict,
    url: str = "",
    page_path: str = "",
    page_title: str = "",
) -> None:
    now_ts = int(time.time())
    received_at = _now_iso()
    raw = {
        "source": "redvia-match-api",
        "event_type": event_type,
        "payload": payload,
    }
    conn.execute(
        """
        INSERT INTO web_events (
            source_event_id, ts, received_at, session_id, visitor_id, buyer_id,
            token, campaign_id, profile_slug, run_id, event_type, url, page_path,
            page_title, referrer, payload_json, ip_prefix, ip_hash, country, colo,
            user_agent, is_bot, bot_reason, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            None,
            now_ts,
            received_at,
            attribution.get("session_id") or attribution.get("visitor_id") or attribution.get("email_token") or "anonymous",
            attribution.get("visitor_id") or "",
            attribution.get("buyer_id"),
            attribution.get("email_token") or "",
            attribution.get("campaign_id") or "",
            attribution.get("profile_slug") or "",
            attribution.get("run_id") or "",
            event_type,
            _safe_public_text(url or str(request.url), 1200),
            _safe_public_text(page_path or "", 600),
            _safe_public_text(page_title or "", 300),
            _safe_public_text(request.headers.get("Referer") or "", 1200),
            _json_dumps(payload)[:12000],
            "",
            "",
            _safe_public_text(request.headers.get("CF-IPCountry") or "", 16),
            "",
            _safe_public_text(request.headers.get("User-Agent") or "", 700),
            0,
            "",
            _json_dumps(raw),
        ),
    )


def _match_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    token = _safe_public_text(token, 80)
    if not token:
        return None
    return conn.execute(
        "SELECT * FROM match_sessions WHERE public_token = ?",
        (token,),
    ).fetchone()


def _brief_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    token = _safe_public_text(token, 80)
    if not token:
        return None
    return conn.execute(
        "SELECT * FROM brief_submissions WHERE public_token = ?",
        (token,),
    ).fetchone()


def _match_answers(conn: sqlite3.Connection, match_id: int) -> dict[str, object]:
    rows = conn.execute(
        "SELECT question_key, answer_json FROM match_answers WHERE match_id = ?",
        (match_id,),
    ).fetchall()
    return {
        row["question_key"]: _json_loads(row["answer_json"], [])
        for row in rows
    }


def _load_match_matrix() -> dict:
    return _read_json(REPO_ROOT / "redvia-site" / "data" / "sku_application_matrix.json")


def _load_reference_applications() -> dict[str, dict]:
    payload = _read_json(REPO_ROOT / "redvia-site" / "data" / "reference_applications.json")
    apps = payload.get("applications") if isinstance(payload, dict) else []
    return {
        str(app.get("application_scenario_id")): app
        for app in apps
        if isinstance(app, dict) and app.get("application_scenario_id")
    }


def _scenario_from_answers(answers: dict[str, object]) -> str:
    q4 = _answer_list(answers.get("q4"))
    if q4 and q4[0] != "exploring":
        return q4[0]
    q1 = (_answer_list(answers.get("q1")) or [""])[0]
    return {
        "beverage": "rtd_beverage",
        "bar": "bar_formulation",
        "supplement": "capsule_fill",
        "bakery": "topping_decoration",
        "confectionery": "topping_decoration",
    }.get(q1, "rtd_beverage")


def _selected_format_set(answers: dict[str, object]) -> set[str]:
    selected = _answer_list(answers.get("q2"))
    if not selected or "open" in selected:
        return set()
    formats: set[str] = set()
    for item in selected:
        formats.update(MATCH_FORMAT_FILTERS.get(item, set()))
    return formats


def _compute_match_result(answers: dict[str, object]) -> dict:
    matrix = _load_match_matrix()
    skus = matrix.get("skus") if isinstance(matrix, dict) else []
    priority = matrix.get("priority") if isinstance(matrix, dict) else {}
    scenario = _scenario_from_answers(answers)
    scenario_priority = priority.get(scenario) if isinstance(priority, dict) else {}
    format_filter = _selected_format_set(answers)
    references = _load_reference_applications()
    reference = references.get(scenario, {})
    reference_order = {
        str(sku_id): idx
        for idx, sku_id in enumerate(reference.get("recommended_sku_ids") or [])
    }

    scored = []
    for index, sku in enumerate(skus if isinstance(skus, list) else []):
        if not isinstance(sku, dict):
            continue
        sku_id = str(sku.get("id") or "")
        score = int(scenario_priority.get(sku_id, 0)) if isinstance(scenario_priority, dict) else 0
        if score <= 0:
            continue
        format_bonus = 1 if not format_filter or str(sku.get("format") or "") in format_filter else 0
        ref_rank = reference_order.get(sku_id, 999 + index)
        scored.append((format_bonus, score, ref_rank, index, sku_id, sku))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
    chosen = [item[5] for item in scored[:3]]
    if len(chosen) < 3:
        chosen_ids = {str(item.get("id") or "") for item in chosen}
        fallback = [
            sku for _, _, _, _, _, sku in sorted(scored, key=lambda item: (-item[1], item[2], item[3]))
            if str(sku.get("id") or "") not in chosen_ids
        ]
        chosen.extend(fallback[: 3 - len(chosen)])

    return {
        "application_scenario": scenario,
        "application_label": MATCH_VALUE_LABELS.get(scenario, scenario),
        "recommended_skus": [
            {
                "id": sku.get("id"),
                "name": sku.get("name"),
                "format": sku.get("format"),
                "image": sku.get("image"),
            }
            for sku in chosen
        ],
        "reference_application_id": scenario,
        "reference_title": reference.get("title") or MATCH_VALUE_LABELS.get(scenario, scenario),
    }


@app.post("/api/match/start")
async def match_start(request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="JSON object required")
    existing_token = _safe_public_text(body.get("public_token") or body.get("match_public_token") or "", 80)
    with _db() as conn:
        if existing_token:
            existing = _match_by_token(conn, existing_token)
            if existing:
                return {"ok": True, "match_id": existing["id"], "public_token": existing["public_token"]}
        attribution = _public_attribution(request, body)
        now = _now_iso()
        public_token = _new_public_token("m")
        cursor = conn.execute(
            """
            INSERT INTO match_sessions (
                public_token, buyer_id, profile_slug, run_id, campaign_id, email_token,
                session_id, visitor_id, company_name, status, started_at, last_activity_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', ?, ?, ?)
            """,
            (
                public_token,
                attribution["buyer_id"],
                attribution["profile_slug"],
                attribution["run_id"],
                attribution["campaign_id"],
                attribution["email_token"],
                attribution["session_id"],
                attribution["visitor_id"],
                attribution["company_name"],
                now,
                now,
                now,
            ),
        )
        match_id = int(cursor.lastrowid)
        _insert_public_event(
            conn,
            request,
            event_type="match_started",
            attribution=attribution,
            payload={"match_id": match_id, "match_token": public_token},
            url=_safe_public_text(body.get("url") or "", 1200),
            page_path=_safe_public_text(body.get("page_path") or "", 600),
            page_title=_safe_public_text(body.get("page_title") or "", 300),
        )
    return {"ok": True, "match_id": match_id, "public_token": public_token}


@app.post("/api/match/answer")
async def match_answer(request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="JSON object required")
    public_token = _safe_public_text(body.get("public_token") or body.get("match_public_token") or "", 80)
    question_key = _safe_public_text(body.get("question_key") or "", 24)
    if question_key not in MATCH_QUESTION_LABELS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid question_key")
    answer = body.get("answer")
    now = _now_iso()
    with _db() as conn:
        match = _match_by_token(conn, public_token)
        if not match:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found")
        conn.execute(
            """
            INSERT INTO match_answers (match_id, question_key, answer_json, answered_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(match_id, question_key) DO UPDATE SET
              answer_json = excluded.answer_json,
              answered_at = excluded.answered_at
            """,
            (match["id"], question_key, _json_dumps(answer), now),
        )
        conn.execute(
            """
            UPDATE match_sessions
            SET status = CASE WHEN status = 'started' THEN 'in_progress' ELSE status END,
                last_activity_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, match["id"]),
        )
        attribution = {
            "buyer_id": match["buyer_id"],
            "profile_slug": match["profile_slug"],
            "run_id": match["run_id"],
            "campaign_id": match["campaign_id"],
            "email_token": match["email_token"],
            "session_id": match["session_id"],
            "visitor_id": match["visitor_id"],
        }
        _insert_public_event(
            conn,
            request,
            event_type="match_step_answered",
            attribution=attribution,
            payload={
                "match_id": match["id"],
                "match_token": public_token,
                "question_key": question_key,
                "question_label": MATCH_QUESTION_LABELS[question_key],
                "answer": answer,
                "answer_label": _answer_label(answer),
            },
            url=_safe_public_text(body.get("url") or "", 1200),
            page_path=_safe_public_text(body.get("page_path") or "", 600),
            page_title=_safe_public_text(body.get("page_title") or "", 300),
        )
    return {"ok": True}


@app.post("/api/match/complete")
async def match_complete(request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="JSON object required")
    public_token = _safe_public_text(body.get("public_token") or body.get("match_public_token") or "", 80)
    incoming_answers = body.get("answers") if isinstance(body.get("answers"), dict) else {}
    now = _now_iso()
    with _db() as conn:
        match = _match_by_token(conn, public_token)
        if not match:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found")
        for question_key, answer in incoming_answers.items():
            question_key = _safe_public_text(question_key, 24)
            if question_key not in MATCH_QUESTION_LABELS:
                continue
            conn.execute(
                """
                INSERT INTO match_answers (match_id, question_key, answer_json, answered_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(match_id, question_key) DO UPDATE SET
                  answer_json = excluded.answer_json,
                  answered_at = excluded.answered_at
                """,
                (match["id"], question_key, _json_dumps(answer), now),
            )
        answers = _match_answers(conn, int(match["id"]))
        result = _compute_match_result(answers)
        result_url = _safe_public_text(body.get("result_url") or "", 500) or f"recommendation.html?m={public_token}"
        conn.execute(
            """
            INSERT OR REPLACE INTO match_results (
                match_id, application_scenario, recommended_skus_json,
                reference_application_id, result_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                match["id"],
                result["application_scenario"],
                _json_dumps(result["recommended_skus"]),
                result["reference_application_id"],
                _json_dumps(result),
                now,
            ),
        )
        conn.execute(
            """
            UPDATE match_sessions
            SET status = 'completed', completed_at = COALESCE(completed_at, ?),
                result_url = ?, last_activity_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, result_url, now, now, match["id"]),
        )
        attribution = {
            "buyer_id": match["buyer_id"],
            "profile_slug": match["profile_slug"],
            "run_id": match["run_id"],
            "campaign_id": match["campaign_id"],
            "email_token": match["email_token"],
            "session_id": match["session_id"],
            "visitor_id": match["visitor_id"],
        }
        _insert_public_event(
            conn,
            request,
            event_type="match_completed",
            attribution=attribution,
            payload={
                "match_id": match["id"],
                "match_token": public_token,
                "answers": answers,
                "application_scenario": result["application_scenario"],
                "application_label": result["application_label"],
                "recommended_skus": result["recommended_skus"],
                "reference_title": result["reference_title"],
            },
            url=_safe_public_text(body.get("url") or "", 1200),
            page_path=_safe_public_text(body.get("page_path") or "", 600),
            page_title=_safe_public_text(body.get("page_title") or "", 300),
        )
    return {"ok": True, "public_token": public_token, "result": result, "redirect_url": result_url}


@app.post("/api/brief")
async def brief_submit(request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="JSON object required")
    public_token = _safe_public_text(body.get("match_public_token") or body.get("public_token") or "", 80)
    now = _now_iso()
    with _db() as conn:
        match = _match_by_token(conn, public_token)
        if not match:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found")
        brief_token = _new_public_token("b")
        deliverables = body.get("deliverables") if isinstance(body.get("deliverables"), list) else []
        cursor = conn.execute(
            """
            INSERT INTO brief_submissions (
                public_token, match_id, buyer_id, profile_slug, run_id, campaign_id,
                company_name, challenge_text, timeline, deliverables_json,
                contact_email, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                brief_token,
                match["id"],
                match["buyer_id"],
                match["profile_slug"],
                match["run_id"],
                match["campaign_id"],
                match["company_name"],
                _safe_public_text(body.get("challenge_text"), 5000),
                _safe_public_text(body.get("timeline"), 200),
                _json_dumps(deliverables),
                _safe_public_text(body.get("contact_email"), 256),
                now,
            ),
        )
        brief_id = int(cursor.lastrowid)
        conn.execute(
            """
            UPDATE match_sessions
            SET status = 'brief_submitted', last_activity_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, match["id"]),
        )
        attribution = {
            "buyer_id": match["buyer_id"],
            "profile_slug": match["profile_slug"],
            "run_id": match["run_id"],
            "campaign_id": match["campaign_id"],
            "email_token": match["email_token"],
            "session_id": match["session_id"],
            "visitor_id": match["visitor_id"],
        }
        _insert_public_event(
            conn,
            request,
            event_type="brief_submitted",
            attribution=attribution,
            payload={
                "match_id": match["id"],
                "brief_id": brief_id,
                "brief_token": brief_token,
                "challenge_text": _safe_public_text(body.get("challenge_text"), 5000),
                "timeline": _safe_public_text(body.get("timeline"), 200),
                "timeline_label": MATCH_VALUE_LABELS.get(_safe_public_text(body.get("timeline"), 80), _safe_public_text(body.get("timeline"), 80)),
                "deliverables": deliverables,
                "deliverables_label": [_answer_label(item) for item in deliverables],
                "has_contact_email": bool(_safe_public_text(body.get("contact_email"), 256)),
            },
            url=_safe_public_text(body.get("url") or "", 1200),
            page_path=_safe_public_text(body.get("page_path") or "", 600),
            page_title=_safe_public_text(body.get("page_title") or "", 300),
        )
    redirect_url = _safe_public_text(body.get("redirect_url") or "", 500) or f"brief.html?b={brief_token}&m={public_token}"
    return {"ok": True, "brief_id": brief_id, "brief_public_token": brief_token, "redirect_url": redirect_url}


@app.post("/api/brief/note")
async def brief_note(request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="JSON object required")
    note_text = _safe_public_text(body.get("note_text"), 2000)
    if not note_text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="note_text is required")
    brief_token = _safe_public_text(body.get("brief_public_token") or body.get("public_token") or "", 80)
    match_token = _safe_public_text(body.get("match_public_token") or "", 80)
    now = _now_iso()
    with _db() as conn:
        brief = _brief_by_token(conn, brief_token)
        match = _match_by_token(conn, match_token) if match_token else None
        if brief and not match and brief["match_id"]:
            match = conn.execute("SELECT * FROM match_sessions WHERE id = ?", (brief["match_id"],)).fetchone()
        if not brief and not match:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brief or match not found")
        cursor = conn.execute(
            """
            INSERT INTO brief_notes (brief_id, match_id, note_text, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                brief["id"] if brief else None,
                match["id"] if match else None,
                note_text,
                now,
            ),
        )
        if match:
            attribution = {
                "buyer_id": match["buyer_id"],
                "profile_slug": match["profile_slug"],
                "run_id": match["run_id"],
                "campaign_id": match["campaign_id"],
                "email_token": match["email_token"],
                "session_id": match["session_id"],
                "visitor_id": match["visitor_id"],
            }
            _insert_public_event(
                conn,
                request,
                event_type="brief_note_added",
                attribution=attribution,
                payload={
                    "match_id": match["id"],
                    "brief_id": brief["id"] if brief else None,
                    "note_text": note_text,
                },
                url=_safe_public_text(body.get("url") or "", 1200),
                page_path=_safe_public_text(body.get("page_path") or "", 600),
                page_title=_safe_public_text(body.get("page_title") or "", 300),
            )
    return {"ok": True, "note_id": int(cursor.lastrowid)}


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


_SENDER_BASE_URL = "https://redvia-tracking.redvia.workers.dev"


def _buyer_marker(slug: str, run_id: str) -> str:
    key = f"{slug}|{run_id}".encode("utf-8")
    return hashlib.sha1(key).hexdigest()[:8]


def _sender_text(text: object, sender_name: str, buyer_marker: str = "") -> str:
    value = str(text or "")
    # Display-layer brand normalization for legacy outreach data.
    # Old profile_enrich runs baked in berylgoji.com / Bairuiyuan; new runs use Redvia.
    value = value.replace("http://berylgoji.com", _SENDER_BASE_URL)
    value = value.replace("https://berylgoji.com", _SENDER_BASE_URL)
    value = value.replace("berylgoji.com", "redvia-tracking.redvia.workers.dev")
    value = value.replace("Bairuiyuan Goji", "Redvia")
    value = value.replace("Bairuiyuan", "Redvia")
    # Per-buyer tracking marker so sales can tell which lead an email belongs to.
    # The /t/<marker> path is the Worker's tracking redirect; bogus markers still
    # 302 to the site root, so clicking is safe even though no D1 token exists.
    if buyer_marker:
        value = value.replace(_SENDER_BASE_URL, f"{_SENDER_BASE_URL}/t/{buyer_marker}")
    if not sender_name:
        return value
    value = value.replace("Nicky", sender_name)
    value = value.replace("[Your Name]", sender_name)
    value = value.replace("[Your Title]", "Sales Manager")
    value = value.replace("[Your Company]", "Redvia")
    return value


def _compute_auto_status_from_conn(conn: sqlite3.Connection, slug: str, run_id: str) -> str:
    valid_reply = conn.execute(
        """
        SELECT 1 FROM received_replies
        WHERE profile_slug = ? AND run_id = ? AND llm_verdict = 'valid'
        LIMIT 1
        """,
        (slug, run_id),
    ).fetchone()
    if valid_reply:
        return "已收到有效回复"

    sent = conn.execute(
        """
        SELECT 1 FROM send_tracking
        WHERE profile_slug = ? AND run_id = ? AND send_status = 'sent'
        LIMIT 1
        """,
        (slug, run_id),
    ).fetchone()
    return "已发" if sent else "未发"


def _refresh_priority_for_lead(conn: sqlite3.Connection, slug: str, run_id: str) -> float:
    row = conn.execute(
        "SELECT * FROM lead_status WHERE profile_slug = ? AND run_id = ?",
        (slug, run_id),
    ).fetchone()
    now = dt.datetime.now().isoformat(timespec="seconds")
    if row:
        effective_status = row["effective_status"]
    else:
        auto_status = _compute_auto_status_from_conn(conn, slug, run_id)
        effective_status = auto_status
        conn.execute(
            """
            INSERT OR IGNORE INTO lead_status (
                profile_slug, run_id, auto_status, manual_status,
                effective_status, updated_at, lead_priority
            )
            VALUES (?, ?, ?, NULL, ?, ?, ?)
            """,
            (slug, run_id, auto_status, effective_status, now, DEFAULT_PRIORITY),
        )
    lead_priority = _compute_lead_priority(conn, slug, run_id, effective_status)
    conn.execute(
        """
        UPDATE lead_status
        SET lead_priority = ?
        WHERE profile_slug = ? AND run_id = ?
        """,
        (lead_priority, slug, run_id),
    )
    return lead_priority


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
        mode = profile.get("outreach_mode")
        if mode and mode != "sales":
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
        slug = json_path.stem
        marker = _buyer_marker(slug, run_id)
        rows.append(
            {
                "slug": slug,
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
                    marker,
                ),
                "body": _sender_text(
                    outreach.get("cold_email_body", ""),
                    sender_name,
                    marker,
                ),
                "whatsapp": _sender_text(
                    outreach.get("whatsapp_or_linkedin_message", ""),
                    sender_name,
                    marker,
                ),
                "follow_up": _sender_text(
                    outreach.get("follow_up_email", ""),
                    sender_name,
                    marker,
                ),
                "bio": body.get("bio_cn", ""),
                "business_relevance": body.get("business_relevance_cn", ""),
            }
        )
    if rows:
        with _db() as conn:
            priority_by_slug = {
                row["slug"]: _refresh_priority_for_lead(conn, row["slug"], run_id)
                for row in rows
            }
        for row in rows:
            row["lead_priority"] = priority_by_slug.get(row["slug"], DEFAULT_PRIORITY)
    return rows


def load_all_leads(sender_name: str = "", runs: list[str] | None = None) -> list[dict]:
    """Load leads from selected runs, newest run first."""
    all_rows: list[dict] = []
    for run_id in (runs if runs is not None else list_available_runs()):
        all_rows.extend(load_sales_leads(run_id, sender_name=sender_name))
    all_rows.sort(key=lambda r: r.get("lead_priority", DEFAULT_PRIORITY), reverse=True)
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


def _compute_auto_status(slug: str, run_id: str) -> str:
    with _db() as conn:
        return _compute_auto_status_from_conn(conn, slug, run_id)


def get_lead_status(slug: str, run_id: str) -> dict:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM lead_status WHERE profile_slug = ? AND run_id = ?",
            (slug, run_id),
        ).fetchone()
    if row:
        return {
            "auto_status": row["auto_status"],
            "manual_status": row["manual_status"],
            "effective_status": row["effective_status"],
            "updated_at": row["updated_at"],
            "is_manual_override": bool(row["manual_status"]),
        }
    auto_status = _compute_auto_status(slug, run_id)
    return {
        "auto_status": auto_status,
        "manual_status": None,
        "effective_status": auto_status,
        "updated_at": "",
        "is_manual_override": False,
    }


def _set_auto_lead_status(slug: str, run_id: str, new_status: str, reason: str = "") -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM lead_status WHERE profile_slug = ? AND run_id = ?",
            (slug, run_id),
        ).fetchone()
        if row and row["auto_status"] == "已收到有效回复" and new_status == "已发":
            return
        old_effective = row["effective_status"] if row else None
        manual_status = row["manual_status"] if row else None
        effective_status = manual_status or new_status
        lead_priority = _compute_lead_priority(conn, slug, run_id, effective_status)
        if row:
            conn.execute(
                """
                UPDATE lead_status
                SET auto_status = ?, effective_status = ?, updated_at = ?, lead_priority = ?
                WHERE profile_slug = ? AND run_id = ?
                """,
                (new_status, effective_status, now, lead_priority, slug, run_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO lead_status (
                    profile_slug, run_id, auto_status, manual_status,
                    effective_status, updated_at, lead_priority
                )
                VALUES (?, ?, ?, NULL, ?, ?, ?)
                """,
                (slug, run_id, new_status, effective_status, now, lead_priority),
            )
        if old_effective != effective_status:
            conn.execute(
                """
                INSERT INTO status_change_log (
                    profile_slug, run_id, changed_at, changed_by,
                    from_status, to_status, source, reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (slug, run_id, now, "system", old_effective, effective_status, "auto", reason),
            )


def set_lead_status(
    slug: str,
    run_id: str,
    new_status: str,
    changed_by: str,
    source: str = "manual",
) -> None:
    if new_status not in STATUS_OPTIONS:
        raise ValueError("invalid status")
    now = dt.datetime.now().isoformat(timespec="seconds")
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM lead_status WHERE profile_slug = ? AND run_id = ?",
            (slug, run_id),
        ).fetchone()
        auto_status = row["auto_status"] if row else _compute_auto_status(slug, run_id)
        old_effective = row["effective_status"] if row else auto_status
        lead_priority = _compute_lead_priority(conn, slug, run_id, new_status)
        if row:
            conn.execute(
                """
                UPDATE lead_status
                SET auto_status = ?, manual_status = ?, effective_status = ?, updated_at = ?, lead_priority = ?
                WHERE profile_slug = ? AND run_id = ?
                """,
                (auto_status, new_status, new_status, now, lead_priority, slug, run_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO lead_status (
                    profile_slug, run_id, auto_status, manual_status,
                    effective_status, updated_at, lead_priority
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (slug, run_id, auto_status, new_status, new_status, now, lead_priority),
            )
        if old_effective != new_status:
            conn.execute(
                """
                INSERT INTO status_change_log (
                    profile_slug, run_id, changed_at, changed_by,
                    from_status, to_status, source, reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (slug, run_id, now, changed_by, old_effective, new_status, source, None),
            )


def get_customer_type(slug: str, run_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM customer_type WHERE profile_slug = ? AND run_id = ?",
            (slug, run_id),
        ).fetchone()
    if not row:
        auto_type = _auto_customer_type_for_lead(slug, run_id)
        if not auto_type:
            return None
        return {
            "auto_type": auto_type,
            "manual_type": None,
            "effective_type": auto_type,
            "is_manual_override": False,
            "type": auto_type,
        }
    data = dict(row)
    auto_type = data.get("auto_type")
    manual_type = CUSTOMER_TYPE_MANUAL_REMAP.get(data.get("type"), data.get("type"))
    effective_type = manual_type or auto_type
    data.update(
        {
            "auto_type": auto_type,
            "manual_type": manual_type,
            "effective_type": effective_type,
            "is_manual_override": bool(manual_type),
            "type": effective_type,
        }
    )
    return data


def set_customer_type(slug: str, run_id: str, new_type: str, changed_by: str) -> None:
    if new_type and new_type not in CUSTOMER_TYPE_OPTIONS:
        raise ValueError("invalid customer type")
    now = dt.datetime.now().isoformat(timespec="seconds")
    value = new_type or None
    with _db() as conn:
        row = conn.execute(
            "SELECT type, auto_type FROM customer_type WHERE profile_slug = ? AND run_id = ?",
            (slug, run_id),
        ).fetchone()
        old_type = row["type"] if row else None
        auto_type = row["auto_type"] if row else _auto_customer_type_for_lead(slug, run_id)
        if row:
            conn.execute(
                """
                UPDATE customer_type
                SET type = ?, source = 'manual', assigned_by = ?, assigned_at = ?
                WHERE profile_slug = ? AND run_id = ?
                """,
                (value, changed_by, now, slug, run_id),
            )
        elif auto_type or value:
            conn.execute(
                """
                INSERT INTO customer_type (
                    profile_slug, run_id, auto_type, type, source, assigned_by, assigned_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    slug,
                    run_id,
                    auto_type,
                    value,
                    "manual" if value else "auto",
                    changed_by if value else None,
                    now if value else None,
                ),
            )
        if old_type != value:
            conn.execute(
                """
                INSERT INTO customer_type_change_log (
                    profile_slug, run_id, changed_at, changed_by, from_type, to_type
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (slug, run_id, now, changed_by, old_type, value),
            )
        _refresh_priority_for_lead(conn, slug, run_id)


def get_received_replies(slug: str, run_id: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM received_replies
            WHERE profile_slug = ? AND run_id = ?
            ORDER BY received_at DESC, id DESC
            """,
            (slug, run_id),
        ).fetchall()
    return [dict(row) for row in rows]


def _send_count(conn: sqlite3.Connection, submitted_by: str | None, start_date: str | None = None) -> int:
    conditions = ["send_status = 'sent'", "run_id NOT LIKE 'test_%'", "is_test = 0"]
    params: list[object] = []
    if submitted_by:
        conditions.append("submitted_by = ?")
        params.append(submitted_by)
    if start_date:
        conditions.append("date(submitted_at) >= ?")
        params.append(start_date)
    sql = f"SELECT count(*) AS n FROM send_tracking WHERE {' AND '.join(conditions)}"
    return int(conn.execute(sql, params).fetchone()["n"])


def _lead_key(slug: str, run_id: str) -> tuple[str, str]:
    return slug, run_id


def _sent_lead_set(
    conn: sqlite3.Connection,
    submitted_by: str | None,
    start_date: str | None = None,
) -> set[tuple[str, str]]:
    conditions = ["send_status = 'sent'", "run_id NOT LIKE 'test_%'", "is_test = 0"]
    params: list[object] = []
    if submitted_by:
        conditions.append("submitted_by = ?")
        params.append(submitted_by)
    if start_date:
        conditions.append("date(submitted_at) >= ?")
        params.append(start_date)
    sql = (
        "SELECT DISTINCT profile_slug, run_id FROM send_tracking "
        f"WHERE {' AND '.join(conditions)}"
    )
    return {
        _lead_key(row["profile_slug"], row["run_id"])
        for row in conn.execute(sql, params).fetchall()
    }


def _reply_lead_set(
    conn: sqlite3.Connection,
    verdicts: tuple[str, ...],
    start_date: str | None = None,
) -> set[tuple[str, str]]:
    conditions = [
        "profile_slug IS NOT NULL",
        "run_id IS NOT NULL",
        "run_id NOT LIKE 'test_%'",
        "llm_verdict IN ({})".format(", ".join("?" for _ in verdicts)),
    ]
    params: list[object] = list(verdicts)
    if start_date:
        conditions.append("date(received_at) >= ?")
        params.append(start_date)
    sql = (
        "SELECT DISTINCT profile_slug, run_id FROM received_replies "
        f"WHERE {' AND '.join(conditions)}"
    )
    return {
        _lead_key(row["profile_slug"], row["run_id"])
        for row in conn.execute(sql, params).fetchall()
    }


def _status_lead_set(
    conn: sqlite3.Connection,
    statuses: tuple[str, ...],
) -> set[tuple[str, str]]:
    sql = (
        "SELECT profile_slug, run_id FROM lead_status "
        "WHERE run_id NOT LIKE 'test_%' "
        "AND effective_status IN ({})".format(", ".join("?" for _ in statuses))
    )
    return {
        _lead_key(row["profile_slug"], row["run_id"])
        for row in conn.execute(sql, list(statuses)).fetchall()
    }


def _generated_date(lead: dict) -> dt.date | None:
    raw = str(lead.get("generated_at") or "").strip()
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _build_funnel(
    conn: sqlite3.Connection,
    submitted_by_filter: str | None,
    since_date: str | None = None,
) -> dict:
    all_leads = load_all_leads()
    all_keys = {_lead_key(lead["slug"], lead["run_id"]) for lead in all_leads}
    week_start = dt.date.today() - dt.timedelta(days=dt.date.today().weekday())
    new_this_week = sum(
        1
        for lead in all_leads
        if (generated := _generated_date(lead)) and generated >= week_start
    )

    sent_keys = _sent_lead_set(conn, submitted_by_filter, since_date)
    total_count = new_this_week if since_date else len(all_keys)
    sent_count = len(sent_keys)

    replied_keys = _reply_lead_set(
        conn,
        ("valid", "rejection", "unclear"),
        since_date,
    )
    valid_reply_keys = _reply_lead_set(conn, ("valid",), since_date)
    valid_reply_keys |= _status_lead_set(conn, ("已收到有效回复",))
    inquired_keys = _status_lead_set(conn, ("已询价",))
    closed_keys = _status_lead_set(conn, ("已成交",))

    if submitted_by_filter:
        owned_keys = _sent_lead_set(conn, submitted_by_filter)
        replied_keys &= owned_keys
        valid_reply_keys &= owned_keys
        inquired_keys &= owned_keys
        closed_keys &= owned_keys

    return {
        "total": total_count,
        "sent": sent_count,
        "replied": len(replied_keys),
        "valid_reply": len(valid_reply_keys),
        "inquired": len(inquired_keys),
        "closed": len(closed_keys),
    }


def _type_distribution() -> dict[str, int]:
    counts = {t: 0 for t in CUSTOMER_TYPE_OPTIONS}
    counts["未分类"] = 0
    for lead in load_all_leads():
        ctype = get_customer_type(lead["slug"], lead["run_id"])
        value = ctype["effective_type"] if ctype and ctype.get("effective_type") else "未分类"
        counts[value if value in counts else "未分类"] += 1
    return counts


def _type_pie_style(type_dist: dict[str, int]) -> str:
    total = sum(type_dist.values())
    if not total:
        return "background:#ecf0f1"
    colors = {
        "原料分销商": "#d0e8fb",
        "OEM制造商": "#ede8e0",
        "品牌商": "#eaf5ee",
        "不相关": "#fdf0ee",
        "未分类": "#bdc3c7",
    }
    cursor = 0.0
    stops = []
    for label in CUSTOMER_TYPE_OPTIONS + ["未分类"]:
        pct = type_dist.get(label, 0) / total * 100
        if pct <= 0:
            continue
        start = cursor
        cursor += pct
        stops.append(f"{colors[label]} {start:.2f}% {cursor:.2f}%")
    return f"background: conic-gradient({', '.join(stops)})"


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
        "message_id": row["message_id"] if "message_id" in row.keys() else "",
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
    is_test: bool | None = None,
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
    lead_status = get_lead_status(slug, run_id)
    customer_type = get_customer_type(slug, run_id)
    received_replies = get_received_replies(slug, run_id)
    row_is_test = _is_test_run(run_id) if is_test is None else is_test
    return {
        "r": {
            "lead": lead,
            "latest": latest,
            "history": history,
            "latest_send": latest_send,
            "status": latest_status,
            "lead_status": lead_status,
            "customer_type": customer_type,
            "received_replies": received_replies,
        },
        "run_id": run_id,
        "status_options": STATUS_OPTIONS,
        "customer_type_options": CUSTOMER_TYPE_OPTIONS,
        "tag_options": TAG_OPTIONS,
        "send_mode": "live" if row_is_test else ("dry-run" if _is_dry_run() else "live"),
        "is_test": row_is_test,
        "truncate": _truncate,
    }


def _dashboard_payload(
    current_user: str,
    user: str = "",
    include_admin_people: bool = False,
) -> dict:
    current_user_display = _user_display_name(current_user)
    is_admin = _user_is_admin(current_user)
    showing_all = user == "all"
    if showing_all:
        if not is_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
        submitted_by_filter = None
        scope_label = "全部人员"
    else:
        submitted_by_filter = current_user_display
        scope_label = current_user_display

    today = dt.date.today()
    week_start = today - dt.timedelta(days=today.weekday())
    today_s = today.isoformat()
    week_start_s = week_start.isoformat()

    with _db() as conn:
        sends_today = _send_count(conn, submitted_by_filter, today_s)
        sends_week = _send_count(conn, submitted_by_filter, week_start_s)
        sends_total = _send_count(conn, submitted_by_filter)
        funnel_week = _build_funnel(conn, submitted_by_filter, week_start_s)
        funnel_total = _build_funnel(conn, submitted_by_filter)
        people = []
        if showing_all or (include_admin_people and is_admin):
            rows = conn.execute(
                """
                SELECT submitted_by,
                       SUM(CASE WHEN date(submitted_at) = ? THEN 1 ELSE 0 END) AS sends_today,
                       SUM(CASE WHEN date(submitted_at) >= ? THEN 1 ELSE 0 END) AS sends_week,
                       count(*) AS sends_total
                FROM send_tracking
                WHERE send_status = 'sent' AND run_id NOT LIKE 'test_%' AND is_test = 0
                GROUP BY submitted_by
                ORDER BY sends_total DESC, submitted_by
                """,
                (today_s, week_start_s),
            ).fetchall()
            people = [dict(row) for row in rows]

    type_dist = _type_distribution()
    return {
        "scope_label": scope_label,
        "showing_all": showing_all,
        "total_leads": funnel_total["total"],
        "sends_today": sends_today,
        "sends_week": sends_week,
        "sends_total": sends_total,
        "funnel_week": funnel_week,
        "funnel_total": funnel_total,
        "type_dist": type_dist,
        "type_pie_style": _type_pie_style(type_dist),
        "people": people,
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
        lead_status = get_lead_status(lead["slug"], run_id)
        customer_type = get_customer_type(lead["slug"], run_id)
        received_replies = get_received_replies(lead["slug"], run_id)
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
                "lead_status": lead_status,
                "customer_type": customer_type,
                "received_replies": received_replies,
            }
        )
    dash = _dashboard_payload(current_user, include_admin_people=True)
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
            "customer_type_options": CUSTOMER_TYPE_OPTIONS,
            "tag_options": TAG_OPTIONS,
            "send_mode": "dry-run" if _is_dry_run() else "live",
            "is_test": False,
            "truncate": _truncate,
            "dash_total_leads": dash["total_leads"],
            "dash_sends_today": dash["sends_today"],
            "dash_sends_week": dash["sends_week"],
            "dash_sends_total": dash["sends_total"],
            "dash_funnel_week": dash["funnel_week"],
            "dash_funnel_total": dash["funnel_total"],
            "dash_type_dist": dash["type_dist"],
            "dash_type_pie_style": dash["type_pie_style"],
            "dash_people": dash["people"],
        },
    )


@app.get("/test", response_class=HTMLResponse)
def test_index(
    request: Request,
    current_user: str = Depends(require_auth),
) -> HTMLResponse:
    current_user_display = _user_display_name(current_user)
    test_runs = list_test_runs()
    leads = load_all_leads(sender_name=current_user_display, runs=test_runs)
    enriched = []
    for lead in leads:
        run_id = lead["run_id"]
        latest = load_latest_feedback(lead["slug"], run_id)
        history = load_history(lead["slug"], run_id)
        latest_send = load_latest_send(lead["slug"], run_id)
        latest_status = load_latest_status(lead["slug"], run_id)
        lead_status = get_lead_status(lead["slug"], run_id)
        customer_type = get_customer_type(lead["slug"], run_id)
        received_replies = get_received_replies(lead["slug"], run_id)
        enriched.append(
            {
                "lead": lead,
                "latest": latest,
                "history": history,
                "latest_send": latest_send,
                "status": latest_status,
                "lead_status": lead_status,
                "customer_type": customer_type,
                "received_replies": received_replies,
            }
        )
    return templates.TemplateResponse(
        "test.html",
        {
            "request": request,
            "rows": enriched,
            "current_user": current_user,
            "current_user_display": current_user_display,
            "current_user_is_admin": _user_is_admin(current_user),
            "status_options": STATUS_OPTIONS,
            "customer_type_options": CUSTOMER_TYPE_OPTIONS,
            "tag_options": TAG_OPTIONS,
            "send_mode": "live",
            "is_test": True,
            "truncate": _truncate,
        },
    )


@app.post("/test/delete")
async def delete_test_data(
    request: Request,
    _: str = Depends(require_admin),
) -> RedirectResponse:
    form = await request.form()
    confirm_text = str(form.get("confirm_text") or "").strip()
    if confirm_text != "删除测试数据":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="confirm_text must be 删除测试数据",
        )

    with _db() as conn:
        test_class_rows = conn.execute(
            "SELECT id, profile_slug FROM edit_classifications WHERE run_id LIKE 'test_%'"
        ).fetchall()
        test_class_ids = [row["id"] for row in test_class_rows]
        test_slugs = {row["profile_slug"] for row in test_class_rows}
        if test_class_ids:
            placeholders = ", ".join("?" for _ in test_class_ids)
            conn.execute(
                f"DELETE FROM content_flags WHERE classification_id IN ({placeholders})",
                test_class_ids,
            )
            conn.execute(
                f"DELETE FROM edit_classifications WHERE id IN ({placeholders})",
                test_class_ids,
            )
        if test_slugs:
            placeholders = ", ".join("?" for _ in test_slugs)
            conn.execute(
                f"DELETE FROM tone_examples WHERE profile_slug IN ({placeholders})",
                list(test_slugs),
            )

        conn.execute("DELETE FROM content_flags WHERE run_id LIKE 'test_%'")
        conn.execute("DELETE FROM received_replies WHERE run_id LIKE 'test_%'")
        conn.execute("DELETE FROM send_tracking WHERE is_test = 1 OR run_id LIKE 'test_%'")
        conn.execute("DELETE FROM feedback WHERE run_id LIKE 'test_%'")
        conn.execute("DELETE FROM lead_status WHERE run_id LIKE 'test_%'")
        conn.execute("DELETE FROM status_change_log WHERE run_id LIKE 'test_%'")
        conn.execute("DELETE FROM customer_type WHERE run_id LIKE 'test_%'")
        conn.execute("DELETE FROM customer_type_change_log WHERE run_id LIKE 'test_%'")

    return RedirectResponse("/test", status_code=status.HTTP_303_SEE_OTHER)


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


@app.post("/set-status", response_class=HTMLResponse)
async def update_lead_status(
    request: Request,
    current_user: str = Depends(require_auth),
) -> HTMLResponse:
    form = await request.form()
    slug = str(form.get("slug") or "").strip()
    run_id = str(form.get("run_id") or _default_run_id()).strip() or _default_run_id()
    new_status = str(form.get("status") or "").strip()
    if not slug or not new_status:
        return HTMLResponse("missing required field", status_code=400)

    submitted_by = _user_display_name(current_user)
    try:
        set_lead_status(slug, run_id, new_status, submitted_by)
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=400)
    ctx = _build_row_context(slug, run_id, sender_name=submitted_by)
    if not ctx:
        return HTMLResponse("profile not found", status_code=404)
    ctx["request"] = request
    ctx["current_user"] = current_user
    ctx["current_user_display"] = submitted_by
    return templates.TemplateResponse("_row.html", ctx)


@app.post("/set-customer-type", response_class=HTMLResponse)
async def update_customer_type(
    request: Request,
    current_user: str = Depends(require_auth),
) -> HTMLResponse:
    form = await request.form()
    slug = str(form.get("slug") or "").strip()
    run_id = str(form.get("run_id") or _default_run_id()).strip() or _default_run_id()
    new_type = str(form.get("type") or "").strip()
    if not slug:
        return HTMLResponse("missing required field", status_code=400)

    submitted_by = _user_display_name(current_user)
    try:
        set_customer_type(slug, run_id, new_type, submitted_by)
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=400)
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
    is_test = str(form.get("is_test") or ("1" if _is_test_run(run_id) else "0")).strip() == "1"
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

    dry_run = False if is_test else _is_dry_run()
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
    message_id = None

    try:
        raw_smtp_response, message_id = _send_message(
            config=config,
            original_to=send_to,
            actual_to=actual_to,
            subject=final_subject,
            body=final_body,
        )
        smtp_response = _redact_secret(raw_smtp_response, config.smtp_pass)
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
                mode, actual_to, original_to, message_id, is_test,
                send_status, smtp_response, send_error,
                submitted_by, submitted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                message_id,
                int(is_test),
                send_status,
                smtp_response,
                send_error,
                submitted_by,
                submitted_at,
            ),
        )
        send_id = int(cursor.lastrowid)

    if send_status == "sent":
        _set_auto_lead_status(slug, run_id, "已发", reason="email sent")

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
            "is_test": is_test,
            "original_to": original_to,
            "actual_to": actual_to,
            "message_id": message_id,
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

    ctx = _build_row_context(slug, run_id, sender_name=submitted_by, is_test=is_test)
    if not ctx:
        return HTMLResponse("profile not found", status_code=404)
    ctx["request"] = request
    ctx["current_user"] = current_user
    ctx["current_user_display"] = submitted_by
    return templates.TemplateResponse("_row.html", ctx)


@app.get("/inbox", response_class=HTMLResponse)
def inbox(
    request: Request,
    current_user: str = Depends(require_auth),
) -> HTMLResponse:
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT r.*, ls.effective_status
            FROM received_replies r
            LEFT JOIN lead_status ls
              ON r.profile_slug = ls.profile_slug AND r.run_id = ls.run_id
            WHERE r.run_id IS NULL OR r.run_id NOT LIKE 'test_%'
            ORDER BY r.received_at DESC, r.id DESC
            LIMIT 200
            """
        ).fetchall()
    replies = []
    for row in rows:
        item = dict(row)
        item["matched"] = bool(item.get("profile_slug") and item.get("run_id"))
        replies.append(item)
    return templates.TemplateResponse(
        "inbox.html",
        {
            "request": request,
            "replies": replies,
            "current_user": current_user,
            "current_user_display": _user_display_name(current_user),
        },
    )


@app.post("/translate")
async def translate_text(
    request: Request,
    _: str = Depends(require_auth),
) -> dict[str, str]:
    form = await request.form()
    text = str(form.get("text") or "")
    target_lang = str(form.get("target_lang") or "").strip()
    allowed_langs = {"Turkish", "Russian", "French", "German", "Japanese", "Korean"}
    if not text.strip() or target_lang not in allowed_langs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="text and supported target_lang are required",
        )

    api_key = (
        os.environ.get("GLM_API_KEY")
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()
    if not api_key:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="LLM API key not set")

    prompt = (
        f"Translate the following sales outreach email text to {target_lang}. "
        "Return only the translated text, no explanation:\n\n"
        f"{text[:12000]}"
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
            model=os.environ.get("LLM_MODEL", "glm-4-flash"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
    except Exception as exc:
        detail = _truncate(str(exc).replace(api_key, "***"), 500)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc

    translated = str(response.choices[0].message.content or "").strip()
    return {"translated": translated}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: str = "",
    current_user: str = Depends(require_auth),
) -> HTMLResponse:
    current_user_display = _user_display_name(current_user)
    is_admin = _user_is_admin(current_user)
    dash = _dashboard_payload(current_user, user=user)
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "current_user": current_user,
            "current_user_display": current_user_display,
            "current_user_is_admin": is_admin,
            "scope_label": dash["scope_label"],
            "showing_all": dash["showing_all"],
            "dash_total_leads": dash["total_leads"],
            "dash_sends_today": dash["sends_today"],
            "dash_sends_week": dash["sends_week"],
            "dash_sends_total": dash["sends_total"],
            "dash_funnel_week": dash["funnel_week"],
            "dash_funnel_total": dash["funnel_total"],
            "dash_type_dist": dash["type_dist"],
            "dash_type_pie_style": dash["type_pie_style"],
            "dash_people": dash["people"],
        },
    )


TRACKING_SEED_CAMPAIGN_ID = "VALIDATION_2026_05_22"
TRACKING_LIVE_SUMMARY_SCOPE = "LIVE"
TRACKING_DEMO_KNOWN_BUYERS = {
    9101: {
        "name": "Comptoirs & Compagnies",
        "archetype": "hot_lead",
        "archetype_label": "Brief Submitted",
        "country_flag": "🇫🇷",
        "country_name": "France",
    },
    9102: {
        "name": "NaturaFit GmbH",
        "archetype": "active",
        "archetype_label": "Match Completed",
        "country_flag": "🇩🇪",
        "country_name": "Germany",
    },
    9103: {
        "name": "Brew & Bloom Ltd",
        "archetype": "returning",
        "archetype_label": "Reviewing Products",
        "country_flag": "🇬🇧",
        "country_name": "United Kingdom",
    },
    9104: {
        "name": "Ariza Ingredients BV",
        "archetype": "browsing",
        "archetype_label": "Match In Progress",
        "country_flag": "🇳🇱",
        "country_name": "Netherlands",
    },
}
TRACKING_DEMO_ANON_VISITORS = {
    "anon-vis-a1b2c3": {
        "name": "Anonymous · Germany",
        "archetype": "anonymous",
        "archetype_label": "Anonymous",
        "country_flag": "🌐",
        "country_name": "Germany (匿名)",
    },
    "anon-vis-d4e5f6": {
        "name": "Anonymous · Spain",
        "archetype": "anonymous",
        "archetype_label": "Anonymous",
        "country_flag": "🌐",
        "country_name": "Spain (匿名)",
    },
    "anon-vis-9z8y7x": {
        "name": "Anonymous · USA",
        "archetype": "anonymous",
        "archetype_label": "Anonymous → 提交 brief",
        "country_flag": "🌐",
        "country_name": "United States (匿名)",
    },
}
TRACKING_DEMO_PRODUCTS = {
    "ningxia-red-goji": "Ningxia Red Goji Berry",
    "qinghai-red-goji": "Qinghai Red Goji Berry",
    "black-goji-berry": "Black Goji Berry",
    "red-goji-puree": "Red Goji Puree",
    "black-goji-puree": "Black Goji Puree",
    "goji-leaf-tea": "Goji Leaf Tea",
    "goji-leaf-matcha-powder": "Goji Leaf Matcha Powder",
    "goji-polysaccharide-powder": "Goji Polysaccharide Powder",
    "goji-seed-oil": "Goji Seed Oil",
}
TRACKING_DEMO_EVENT_ICONS = {
    "page_view": "→",
    "product_view": "▸",
    "product_click": "↗",
    "product_family_click": "↗",
    "match_started": "◇",
    "match_step_answered": "✓",
    "match_completed": "◆",
    "recommendation_viewed": "◎",
    "brief_submitted": "✎",
    "brief_note_added": "+",
    "dwell_30s": "⏱",
    "dwell_60s": "⏱",
    "dwell": "⏱",
    "cert_open": "⌬",
    "form_submit": "✎",
    "mailto_click": "✉",
    "tel_click": "☏",
    "cta_click": "⬢",
}


def _tracking_demo_group_key(row: sqlite3.Row) -> str:
    buyer_id = _coerce_event_int(row["buyer_id"])
    if buyer_id is not None:
        return f"b{buyer_id}"
    visitor_id = str(row["visitor_id"] or "").strip()
    return f"v{visitor_id}" if visitor_id else ""


def _tracking_demo_group_filter(group_key: str) -> tuple[str, int | str]:
    if group_key.startswith("b"):
        return "buyer_id = ?", int(group_key[1:])
    return "buyer_id IS NULL AND visitor_id = ?", group_key[1:]


def _relative_time(now_ts: int, then_ts: int) -> str:
    diff = max(0, now_ts - then_ts)
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{diff // 60} min ago"
    if diff < 86400:
        return f"{diff // 3600} hours ago"
    return f"{diff // 86400} days ago"


def _human_dwell(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}m {remainder}s" if remainder else f"{minutes}m"


def _tracking_demo_payload_json(row: sqlite3.Row) -> dict:
    raw = row["payload_json"] or "{}"
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _tracking_demo_product_title(slug: str, fallback: object = "") -> str:
    fallback_text = str(fallback or "").strip()
    return fallback_text or TRACKING_DEMO_PRODUCTS.get(slug, slug)


def _tracking_demo_short_title(page_title: object, page_path: object = "") -> str:
    title = str(page_title or "").replace("· Redvia", "").strip()
    if title:
        return title
    return str(page_path or "page").strip() or "page"


def _tracking_demo_event_label(row: sqlite3.Row, payload: dict) -> str:
    event_type = str(row["event_type"] or "")
    page_path = str(row["page_path"] or "")
    page_title = row["page_title"]
    slug = str(payload.get("slug") or "")

    if event_type == "page_view":
        if page_path == "/ingredients.html":
            return "Browsed all products"
        if page_path == "/index.html":
            return "Landed on home page"
        return "Landed on " + _tracking_demo_short_title(page_title, page_path)
    if event_type == "product_view":
        return "Viewed " + _tracking_demo_product_title(slug, payload.get("title"))
    if event_type in {"product_click", "product_family_click"}:
        label = str(payload.get("label") or "").strip()
        label = label or _tracking_demo_product_title(slug, "")
        return "Clicked " + (label or "product") + " →"
    if event_type in {"dwell_30s", "dwell_60s", "dwell"}:
        target = _tracking_demo_product_title(
            slug,
            _tracking_demo_short_title(page_title, page_path),
        )
        if event_type == "dwell_30s":
            return "Stayed 30s on " + target
        if event_type == "dwell_60s":
            return "Stayed 60s on " + target
        dwell_ms = _coerce_event_int(payload.get("dwell_ms")) or 0
        return f"Stayed {max(1, dwell_ms // 1000)}s on {target}"
    if event_type == "cert_open":
        name = str(payload.get("name") or payload.get("cert") or "document").strip()
        return "Opened " + name + " cert"
    if event_type == "form_submit":
        return "Submitted brief"
    if event_type == "match_started":
        return "Started Find Your Match"
    if event_type == "match_step_answered":
        question = str(payload.get("question_label") or payload.get("question_key") or "question").strip()
        answer = str(payload.get("answer_label") or "").strip()
        return f"Answered {question}: {answer}" if answer else f"Answered {question}"
    if event_type == "match_completed":
        scenario = str(payload.get("application_label") or payload.get("application_scenario") or "").strip()
        return "Completed match" + (f": {scenario}" if scenario else "")
    if event_type == "recommendation_viewed":
        return "Viewed recommendation"
    if event_type == "brief_submitted":
        return "Submitted ingredient brief"
    if event_type == "brief_note_added":
        return "Added brief note"
    if event_type == "mailto_click":
        email = str(payload.get("email") or payload.get("label") or "").strip()
        return "Clicked email " + email if email else "Clicked email"
    if event_type == "tel_click":
        phone = str(
            payload.get("tel") or payload.get("phone") or payload.get("label") or ""
        ).strip()
        return "Clicked phone " + phone if phone else "Clicked phone"
    if event_type == "cta_click":
        label = str(payload.get("label") or "").strip()
        return "CTA → " + (label or "clicked")
    return event_type.replace("_", " ").title() or "Event"


def _tracking_demo_timeline_event(row: sqlite3.Row, payload: dict) -> dict:
    event_type = str(row["event_type"] or "")
    page_path = str(row["page_path"] or "")
    ts = int(row["ts"] or 0)
    ts_human = dt.datetime.fromtimestamp(ts).strftime("%a %H:%M")
    return {
        "ts": ts,
        "ts_human": ts_human,
        "event_type": event_type,
        "kind": event_type,
        "icon": TRACKING_DEMO_EVENT_ICONS.get(event_type, "•"),
        "label": _tracking_demo_event_label(row, payload),
        "detail": "" if event_type == "form_submit" else page_path,
        "brief_text": (
            str(
                payload.get("brief_text")
                or payload.get("challenge_text")
                or payload.get("note_text")
                or ""
            )
            if event_type in {"form_submit", "brief_submitted", "brief_note_added"}
            else ""
        ),
        "payload_pretty": json.dumps(payload, indent=2, ensure_ascii=False),
    }


def _tracking_group_match_rows(group_key: str) -> list[sqlite3.Row]:
    if group_key.startswith("b"):
        where_sql = "buyer_id = ?"
        value: object = int(group_key[1:])
    else:
        where_sql = "buyer_id IS NULL AND visitor_id = ?"
        value = group_key[1:]
    with _db() as conn:
        return conn.execute(
            f"""
            SELECT *
            FROM match_sessions
            WHERE {where_sql}
            ORDER BY last_activity_at DESC, id DESC
            """,
            (value,),
        ).fetchall()


def _tracking_match_summary(group_key: str) -> dict:
    match_rows = _tracking_group_match_rows(group_key)
    if not match_rows:
        return {"has_match": False}
    match = match_rows[0]
    with _db() as conn:
        answer_rows = conn.execute(
            """
            SELECT question_key, answer_json, answered_at
            FROM match_answers
            WHERE match_id = ?
            ORDER BY question_key
            """,
            (match["id"],),
        ).fetchall()
        result_row = conn.execute(
            "SELECT * FROM match_results WHERE match_id = ?",
            (match["id"],),
        ).fetchone()
        brief_row = conn.execute(
            """
            SELECT *
            FROM brief_submissions
            WHERE match_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (match["id"],),
        ).fetchone()
        note_rows = conn.execute(
            """
            SELECT *
            FROM brief_notes
            WHERE match_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 5
            """,
            (match["id"],),
        ).fetchall()

    answers = []
    for row in answer_rows:
        value = _json_loads(row["answer_json"], [])
        answers.append(
            {
                "question_key": row["question_key"],
                "question_label": MATCH_QUESTION_LABELS.get(row["question_key"], row["question_key"]),
                "answer": value,
                "answer_label": _answer_label(value),
                "answered_at": row["answered_at"],
            }
        )

    result = _json_loads(result_row["result_json"], {}) if result_row else {}
    recommended_skus = result.get("recommended_skus") if isinstance(result, dict) else []
    if not isinstance(recommended_skus, list):
        recommended_skus = []
    brief = None
    if brief_row:
        deliverables = _json_loads(brief_row["deliverables_json"], [])
        if not isinstance(deliverables, list):
            deliverables = []
        brief = {
            "id": brief_row["id"],
            "public_token": brief_row["public_token"],
            "challenge_text": brief_row["challenge_text"],
            "timeline": brief_row["timeline"],
            "timeline_label": MATCH_VALUE_LABELS.get(brief_row["timeline"], brief_row["timeline"]),
            "deliverables": deliverables,
            "deliverables_label": [MATCH_VALUE_LABELS.get(item, item) for item in deliverables],
            "contact_email": brief_row["contact_email"],
            "created_at": brief_row["created_at"],
        }

    return {
        "has_match": True,
        "match_id": match["id"],
        "public_token": match["public_token"],
        "status": match["status"],
        "status_label": str(match["status"] or "").replace("_", " ").title(),
        "company_name": match["company_name"],
        "started_at": match["started_at"],
        "completed_at": match["completed_at"],
        "answers": answers,
        "result": {
            "application_label": result.get("application_label") if isinstance(result, dict) else "",
            "reference_title": result.get("reference_title") if isinstance(result, dict) else "",
            "recommended_skus": recommended_skus,
        },
        "brief": brief,
        "notes": [dict(row) for row in note_rows],
    }


def _tracking_demo_meta(group_key: str, event_rows: list[sqlite3.Row]) -> dict:
    first = event_rows[0]
    match_summary = _tracking_match_summary(group_key)
    if match_summary.get("company_name"):
        return {
            "name": match_summary["company_name"],
            "archetype": "hot_lead" if match_summary.get("brief") else "active",
            "archetype_label": "Brief Submitted" if match_summary.get("brief") else match_summary.get("status_label", "Match Activity"),
            "country_flag": "",
            "country_name": str(first["country"] or "Known buyer"),
        }
    if group_key.startswith("b"):
        buyer_id = int(group_key[1:])
        profile_name = _profile_company_name(str(first["run_id"] or ""), str(first["profile_slug"] or ""))
        if profile_name:
            return {
                "name": profile_name,
                "archetype": "active",
                "archetype_label": "Tracked Buyer",
                "country_flag": "",
                "country_name": str(first["country"] or "Known buyer"),
            }
        return TRACKING_DEMO_KNOWN_BUYERS.get(
            buyer_id,
            {
                "name": f"Buyer {buyer_id}",
                "archetype": "unknown",
                "archetype_label": "Unknown",
                "country_flag": "",
                "country_name": str(first["country"] or ""),
            },
        )
    visitor_id = group_key[1:]
    return TRACKING_DEMO_ANON_VISITORS.get(
        visitor_id,
        {
            "name": "Anonymous visitor",
            "archetype": "anonymous",
            "archetype_label": "Anonymous",
            "country_flag": "🌐",
            "country_name": str(first["country"] or "Unknown") + " (匿名)",
        },
    )


def _tracking_demo_group_model(
    group_key: str,
    event_rows: list[sqlite3.Row],
    summary_row: sqlite3.Row | None,
    now_ts: int,
) -> dict:
    meta = _tracking_demo_meta(group_key, event_rows)
    match_summary = _tracking_match_summary(group_key)
    is_anonymous = group_key.startswith("v")
    buyer_id = int(group_key[1:]) if group_key.startswith("b") else None
    visitor_id = group_key[1:] if is_anonymous else None
    sessions = set()
    products_viewed = []
    seen_products = set()
    certs_opened = []
    seen_certs = set()
    dwell_by_page: dict[tuple[str, str], int] = {}
    submitted_brief = False
    timeline = []

    for row in event_rows:
        payload = _tracking_demo_payload_json(row)
        session_id = str(row["session_id"] or "")
        page_path = str(row["page_path"] or "")
        if session_id:
            sessions.add(session_id)

        event_type = str(row["event_type"] or "")
        if event_type == "product_view":
            slug = str(payload.get("slug") or "")
            title = _tracking_demo_product_title(slug, payload.get("title"))
            product_key = slug or title
            if product_key and product_key not in seen_products:
                products_viewed.append((slug, title))
                seen_products.add(product_key)
        if event_type == "cert_open":
            cert_name = str(payload.get("name") or payload.get("cert") or "").strip()
            if cert_name and cert_name not in seen_certs:
                certs_opened.append(cert_name)
                seen_certs.add(cert_name)
        if event_type == "form_submit":
            submitted_brief = True
        if event_type == "brief_submitted":
            submitted_brief = True

        dwell_ms = _coerce_event_int(payload.get("dwell_ms")) or 0
        if dwell_ms > 0:
            dwell_key = (session_id, page_path)
            dwell_by_page[dwell_key] = max(dwell_by_page.get(dwell_key, 0), dwell_ms)
        timeline.append(_tracking_demo_timeline_event(row, payload))

    total_dwell_seconds = sum(dwell_by_page.values()) // 1000
    last_seen_ts = int(event_rows[-1]["ts"] or 0)
    if match_summary.get("brief"):
        submitted_brief = True
    if match_summary.get("result", {}).get("recommended_skus") and not products_viewed:
        for sku in match_summary["result"]["recommended_skus"]:
            slug = str(sku.get("id") or "")
            title = str(sku.get("name") or slug)
            if slug or title:
                products_viewed.append((slug, title))
    return {
        "group_key": group_key,
        "buyer_id": buyer_id,
        "visitor_id": visitor_id,
        "is_anonymous": is_anonymous,
        "name": meta["name"],
        "country_flag": meta["country_flag"],
        "country_name": meta["country_name"],
        "archetype": "anonymous" if is_anonymous else meta["archetype"],
        "archetype_label": meta["archetype_label"],
        "event_count": len(event_rows),
        "session_count": len(sessions),
        "last_seen_ts": last_seen_ts,
        "last_seen_relative": _relative_time(now_ts, last_seen_ts),
        "submitted_brief": submitted_brief,
        "products_viewed": products_viewed,
        "certs_opened": certs_opened,
        "total_dwell_seconds": total_dwell_seconds,
        "dwell_human": _human_dwell(total_dwell_seconds),
        "timeline": timeline,
        "match_summary": match_summary,
        "ai_summary": summary_row["summary"] if summary_row else None,
        "ai_summary_at": summary_row["generated_at"] if summary_row else None,
    }


def _tracking_demo_group_rows(group_key: str) -> list[sqlite3.Row]:
    where_sql, value = _tracking_demo_group_filter(group_key)
    with _db() as conn:
        live_rows = conn.execute(
            f"""
            SELECT *
            FROM web_events
            WHERE COALESCE(is_demo, 0) = 0
              AND COALESCE(campaign_id, '') != ?
              AND {where_sql}
            ORDER BY ts ASC
            """,
            (TRACKING_SEED_CAMPAIGN_ID, value),
        ).fetchall()
        if live_rows:
            return live_rows
        return conn.execute(
            f"""
            SELECT *
            FROM web_events
            WHERE campaign_id = ? AND {where_sql}
            ORDER BY ts ASC
            """,
            (TRACKING_SEED_CAMPAIGN_ID, value),
        ).fetchall()


def _tracking_demo_brief_text(event_rows: list[sqlite3.Row]) -> str:
    for row in event_rows:
        if row["event_type"] not in {"form_submit", "brief_submitted"}:
            continue
        payload = _tracking_demo_payload_json(row)
        brief = str(payload.get("brief_text") or payload.get("challenge_text") or "").strip()
        if brief:
            return brief
    return ""


def _tracking_demo_view_model() -> dict:
    now_ts = int(time.time())
    with _db() as conn:
        live_event_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM web_events
            WHERE COALESCE(is_demo, 0) = 0
              AND COALESCE(campaign_id, '') != ?
            """,
            (TRACKING_SEED_CAMPAIGN_ID,),
        ).fetchone()[0]
        live_match_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM match_sessions
            WHERE COALESCE(campaign_id, '') != ?
            """,
            (TRACKING_SEED_CAMPAIGN_ID,),
        ).fetchone()[0]
        use_live = bool(live_event_count or live_match_count)
        summary_scope = TRACKING_LIVE_SUMMARY_SCOPE if use_live else TRACKING_SEED_CAMPAIGN_ID
        if use_live:
            rows = conn.execute(
                """
                SELECT *
                FROM web_events
                WHERE COALESCE(is_demo, 0) = 0
                  AND COALESCE(campaign_id, '') != ?
                ORDER BY ts ASC
                """,
                (TRACKING_SEED_CAMPAIGN_ID,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT *
                FROM web_events
                WHERE campaign_id = ?
                ORDER BY ts ASC
                """,
                (TRACKING_SEED_CAMPAIGN_ID,),
            ).fetchall()
        sent_count = conn.execute(
            """
            SELECT COUNT(DISTINCT profile_slug || '|' || run_id)
            FROM send_tracking
            WHERE send_status = 'sent'
              AND run_id NOT LIKE 'test_%'
              AND is_test = 0
            """
        ).fetchone()[0]
        summary_rows = conn.execute(
            """
            SELECT *
            FROM tracking_ai_summary
            WHERE campaign_id = ?
            """,
            (summary_scope,),
        ).fetchall()

    summaries = {row["group_key"]: row for row in summary_rows}
    events_by_group: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        group_key = _tracking_demo_group_key(row)
        if group_key:
            events_by_group.setdefault(group_key, []).append(row)

    buyers = [
        _tracking_demo_group_model(
            group_key,
            event_rows,
            summaries.get(group_key),
            now_ts,
        )
        for group_key, event_rows in events_by_group.items()
    ]
    buyers.sort(key=lambda buyer: buyer["last_seen_ts"], reverse=True)

    product_counts: dict[str, int] = {}
    product_titles: dict[str, str] = {}
    for event_rows in events_by_group.values():
        for row in event_rows:
            if row["event_type"] != "product_view":
                continue
            payload = _tracking_demo_payload_json(row)
            slug = str(payload.get("slug") or "")
            title = _tracking_demo_product_title(slug, payload.get("title"))
            product_key = slug or title
            if product_key:
                product_counts[product_key] = product_counts.get(product_key, 0) + 1
                product_titles[product_key] = title

    emails_sent = max(int(sent_count or 0), len(buyers)) if use_live else len(TRACKING_DEMO_KNOWN_BUYERS)
    clickers = sum(
        1 for buyer in buyers
        if any(ev["event_type"] in {"email_click", "token_identify", "page_view"} for ev in buyer["timeline"])
    )
    if not use_live:
        clickers = emails_sent
    completed_matches = sum(
        1 for buyer in buyers
        if buyer["match_summary"].get("status") in {"completed", "brief_submitted"}
    )
    kpis = {
        "campaign_id": summary_scope,
        "data_mode": "live" if use_live else "seeded",
        "emails_sent": emails_sent,
        "clickers": clickers,
        "click_rate": round(clickers / emails_sent * 100) if emails_sent else 0,
        "hot_leads": sum(1 for buyer in buyers if buyer["submitted_brief"]),
        "completed_matches": completed_matches,
        "total_events": len(rows),
    }

    max_product_views = max(product_counts.values(), default=0)
    top_products = [
        {
            "slug": slug,
            "title": product_titles[slug],
            "views": views,
            "bar_pct": (
                round(views / max_product_views * 100) if max_product_views else 0
            ),
        }
        for slug, views in sorted(
            product_counts.items(),
            key=lambda item: (-item[1], product_titles[item[0]]),
        )[:5]
    ]

    top_buyers_source = sorted(
        buyers,
        key=lambda buyer: (-buyer["total_dwell_seconds"], buyer["name"]),
    )[:5]
    max_buyer_dwell = max(
        (buyer["total_dwell_seconds"] for buyer in top_buyers_source),
        default=0,
    )
    top_buyers = [
        {
            "name": buyer["name"],
            "country_flag": buyer["country_flag"],
            "dwell_human": buyer["dwell_human"],
            "bar_pct": (
                round(buyer["total_dwell_seconds"] / max_buyer_dwell * 100)
                if max_buyer_dwell
                else 0
            ),
        }
        for buyer in top_buyers_source
    ]

    return {
        "kpis": kpis,
        "data_mode": "live" if use_live else "seeded",
        "buyers": buyers,
        "top_products": top_products,
        "top_buyers": top_buyers,
    }


@app.get("/buyer-tracking", response_class=HTMLResponse)
@app.get("/tracking-demo", response_class=HTMLResponse)
def tracking_demo(
    request: Request,
    current_user: str = Depends(require_admin),
) -> HTMLResponse:
    view_model = _tracking_demo_view_model()
    return templates.TemplateResponse(
        "tracking_demo.html",
        {
            "request": request,
            "current_user_display": _user_display_name(current_user),
            **view_model,
        },
    )


def _tracking_demo_summary_prompt(group: dict, event_rows: list[sqlite3.Row]) -> str:
    product_names = [title for _, title in group["products_viewed"]]
    products = ", ".join(product_names) if product_names else "None"
    certs = ", ".join(group["certs_opened"]) if group["certs_opened"] else "None"
    brief_text = _tracking_demo_brief_text(event_rows) or "None"
    submitted = "yes" if group["submitted_brief"] else "no"
    match_summary = group.get("match_summary") or {}
    match_answers = "; ".join(
        f"{item['question_label']}: {item['answer_label']}"
        for item in match_summary.get("answers", [])
        if item.get("answer_label")
    ) or "None"
    recommended = ", ".join(
        str(sku.get("name") or sku.get("id") or "")
        for sku in match_summary.get("result", {}).get("recommended_skus", [])
    ) or "None"
    return "\n".join(
        [
            "You are a B2B sales assistant for Redvia. Based on this buyer's website behavior and Find Your Match answers, write 2-3 concise English follow-up notes for sales.",
            "Requirements:",
            "- Identify the likely product/application interest.",
            "- Mention the concrete next step: which spec sheet, CoA, sample, or question to send.",
            "- Do not use markdown bullets. Do not repeat the raw timeline.",
            "",
            f"Buyer: {group['name']} ({group['country_name']})",
            f"Status: {group['archetype_label']}",
            (
                f"Sessions: {group['session_count']}  "
                f"Events: {group['event_count']}  "
                f"Dwell: {group['dwell_human']}"
            ),
            f"Brief submitted: {submitted}",
            f"Match answers: {match_answers}",
            f"Recommended SKUs: {recommended}",
            f"Viewed products: {products}",
            f"Opened certificates: {certs}",
            f"Brief text if any: {brief_text}",
            "",
            "Return only the follow-up notes, no prefix and no JSON.",
        ]
    )


def _call_tracking_demo_llm(prompt: str) -> tuple[str, str, int | None, int | None]:
    api_key = (
        os.environ.get("GLM_API_KEY") or os.environ.get("LLM_API_KEY") or ""
    ).strip()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LLM 未配置 GLM_API_KEY",
        )

    model = os.environ.get("LLM_MODEL", "glm-4-flash")
    try:
        from openai import OpenAI

        client_kwargs: dict = {"api_key": api_key}
        base_url = (os.environ.get("LLM_BASE_URL") or "").strip()
        if base_url:
            client_kwargs["base_url"] = base_url
        client_kwargs["timeout"] = float(os.environ.get("LLM_TIMEOUT_SECONDS", "120"))
        client = OpenAI(**client_kwargs)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=300,
        )
    except HTTPException:
        raise
    except Exception as exc:
        detail = _truncate(str(exc).replace(api_key, "***"), 500)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM 调用失败: {detail}",
        ) from exc

    summary = str(response.choices[0].message.content or "").strip()
    usage = getattr(response, "usage", None)
    token_input = getattr(usage, "prompt_tokens", None) if usage else None
    token_output = getattr(usage, "completion_tokens", None) if usage else None
    return summary, model, token_input, token_output


@app.post("/buyer-tracking/summarize/{group_key}")
@app.post("/tracking-demo/summarize/{group_key}")
def tracking_demo_summarize(
    group_key: str,
    current_user: str = Depends(require_admin),
) -> dict:
    if not re.fullmatch(r"(b\d+|v[A-Za-z0-9_-]+)", group_key or ""):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid group_key",
        )

    event_rows = _tracking_demo_group_rows(group_key)
    if not event_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tracking visitor not found",
        )
    summary_scope = (
        TRACKING_LIVE_SUMMARY_SCOPE
        if any(
            int(row["is_demo"] or 0) == 0
            and str(row["campaign_id"] or "") != TRACKING_SEED_CAMPAIGN_ID
            for row in event_rows
        )
        else TRACKING_SEED_CAMPAIGN_ID
    )

    with _db() as conn:
        cached = conn.execute(
            """
            SELECT summary, generated_at, model
            FROM tracking_ai_summary
            WHERE group_key = ? AND campaign_id = ?
            """,
            (group_key, summary_scope),
        ).fetchone()
    if cached:
        return {
            "summary": cached["summary"],
            "generated_at": cached["generated_at"],
            "model": cached["model"],
            "cached": True,
        }

    group = _tracking_demo_group_model(group_key, event_rows, None, int(time.time()))
    prompt = _tracking_demo_summary_prompt(group, event_rows)
    summary, model, token_input, token_output = _call_tracking_demo_llm(prompt)
    generated_at = dt.datetime.now().isoformat(timespec="seconds")

    with _db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO tracking_ai_summary (
                group_key, campaign_id, summary, model, generated_at,
                token_input, token_output
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_key,
                summary_scope,
                summary,
                model,
                generated_at,
                token_input,
                token_output,
            ),
        )
        conn.commit()

    return {
        "summary": summary,
        "generated_at": generated_at,
        "model": model,
        "cached": False,
    }


@app.get("/admin", response_class=HTMLResponse)
def admin_page(
    request: Request,
    synthesis: Optional[str] = None,
    current_user: str = Depends(require_admin),
) -> HTMLResponse:
    with _db() as conn:
        users = conn.execute(
            "SELECT username, display_name, is_admin FROM users ORDER BY id"
        ).fetchall()
        content_flags = conn.execute(
            "SELECT * FROM content_flags WHERE resolved = 0 ORDER BY flagged_at DESC"
        ).fetchall()
        last_synthesis = conn.execute(
            "SELECT * FROM synthesis_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "users": users,
            "content_flags": content_flags,
            "last_synthesis": dict(last_synthesis) if last_synthesis else None,
            "synthesis_flash": synthesis,
            "current_user": current_user,
            "current_user_display": _user_display_name(current_user),
        },
    )


@app.post("/admin/synthesize")
def trigger_synthesis(_: str = Depends(require_admin)) -> RedirectResponse:
    from webui.synthesizer import run_synthesis

    threading.Thread(
        target=run_synthesis,
        kwargs={"db_path": DB_PATH, "trigger": "manual"},
        daemon=True,
    ).start()
    return RedirectResponse(
        "/admin?synthesis=queued",
        status_code=status.HTTP_303_SEE_OTHER,
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
