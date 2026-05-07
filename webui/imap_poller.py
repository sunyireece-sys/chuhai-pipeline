from __future__ import annotations

import datetime as dt
import imaplib
import json
import os
import re
import sqlite3
from email import policy
from email.header import make_header, decode_header
from email.message import Message
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

AUTO_REPLY_RE = re.compile(
    r"\b(out of office|auto-reply|automatic reply|vacation|away from)\b",
    re.IGNORECASE,
)
REJECTION_RE = re.compile(
    r"\b(not interested|unsubscribe|remove me|no thank you|please remove)\b",
    re.IGNORECASE,
)
MSGID_RE = re.compile(r"<[^>]+>")


def _db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _decode_header(value: object) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def _message_body(message: Message) -> str:
    if message.is_multipart():
        html_fallback = ""
        for part in message.walk():
            if part.is_multipart():
                continue
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            content_type = part.get_content_type()
            try:
                content = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                content = payload.decode(charset, errors="replace")
            if content_type == "text/plain":
                return str(content).strip()
            if content_type == "text/html" and not html_fallback:
                html_fallback = str(content)
        return _html_to_text(html_fallback).strip()

    try:
        content = message.get_content()
    except Exception:
        payload = message.get_payload(decode=True) or b""
        charset = message.get_content_charset() or "utf-8"
        content = payload.decode(charset, errors="replace")
    if message.get_content_type() == "text/html":
        return _html_to_text(str(content)).strip()
    return str(content).strip()


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html or "")
    html = re.sub(r"(?s)<br\s*/?>", "\n", html)
    html = re.sub(r"(?s)</p\s*>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"[ \t\r\f\v]+", " ", text)


def _received_at(message: Message) -> str:
    raw_date = message.get("Date")
    if raw_date:
        try:
            parsed = parsedate_to_datetime(str(raw_date))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone().isoformat(timespec="seconds")
        except Exception:
            pass
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _message_id_candidates(value: str) -> list[str]:
    matches = MSGID_RE.findall(value or "")
    if not matches and value:
        stripped = value.strip()
        matches = [stripped if stripped.startswith("<") else f"<{stripped}>"]
    out = []
    for match in matches:
        cleaned = match.strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _match_sent_message(
    conn: sqlite3.Connection,
    *,
    from_email: str,
    in_reply_to: str,
) -> tuple[str | None, str | None, str]:
    for candidate in _message_id_candidates(in_reply_to):
        row = conn.execute(
            """
            SELECT profile_slug, run_id FROM send_tracking
            WHERE message_id = ?
            ORDER BY submitted_at DESC, id DESC LIMIT 1
            """,
            (candidate,),
        ).fetchone()
        if row:
            return row["profile_slug"], row["run_id"], "in_reply_to"

    if from_email:
        row = conn.execute(
            """
            SELECT profile_slug, run_id FROM send_tracking
            WHERE send_status = 'sent'
              AND (lower(original_to) = ? OR lower(actual_to) = ?)
            ORDER BY submitted_at DESC, id DESC LIMIT 1
            """,
            (from_email.lower(), from_email.lower()),
        ).fetchone()
        if row:
            return row["profile_slug"], row["run_id"], "from_email"

    return None, None, "unmatched"


def _rule_verdict(subject: str, body: str) -> dict[str, str] | None:
    text = f"{subject}\n{body}"
    if AUTO_REPLY_RE.search(text):
        return {"verdict": "auto-reply", "reason": "规则命中自动回复关键词"}
    if REJECTION_RE.search(text):
        return {"verdict": "rejection", "reason": "规则命中拒绝或退订关键词"}
    return None


def _llm_judge_reply(subject: str, body: str) -> dict[str, str]:
    api_key = (
        os.environ.get("GLM_API_KEY")
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()
    if not api_key:
        return {"verdict": "unclear", "reason": "GLM_API_KEY not set"}

    prompt = (
        "你是一个邮件分类器。判断这封回复邮件是否为有效商业回复。\n"
        "分类：\n"
        "- valid：有实质内容的商业回复（询价、表示兴趣、提问等）\n"
        "- rejection：明确拒绝、表示不需要\n"
        "- auto-reply：自动回复/外出通知\n"
        "- unclear：无法判断\n\n"
        '只回复一个 JSON：{"verdict": "valid"|"rejection"|"auto-reply"|"unclear", "reason": "一句话原因"}\n\n'
        f"邮件主题：{subject}\n"
        f"邮件内容（前500字）：{body[:500]}"
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
            temperature=0,
            max_tokens=200,
        )
        payload = json.loads(response.choices[0].message.content or "{}")
    except Exception as exc:
        return {"verdict": "unclear", "reason": f"error: {exc}"[:1000]}

    verdict = str(payload.get("verdict") or "unclear").strip().lower()
    if verdict not in {"valid", "rejection", "auto-reply", "unclear"}:
        verdict = "unclear"
    reason = str(payload.get("reason") or "").strip()
    return {"verdict": verdict, "reason": reason}


def _judge_reply(subject: str, body: str) -> dict[str, str]:
    return _rule_verdict(subject, body) or _llm_judge_reply(subject, body)


def _update_valid_reply_status(
    conn: sqlite3.Connection,
    *,
    profile_slug: str,
    run_id: str,
    reason: str,
) -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT * FROM lead_status WHERE profile_slug = ? AND run_id = ?",
        (profile_slug, run_id),
    ).fetchone()
    old_effective = row["effective_status"] if row else None
    manual_status = row["manual_status"] if row else None
    new_auto = "已收到有效回复"
    new_effective = manual_status or new_auto
    if row:
        conn.execute(
            """
            UPDATE lead_status
            SET auto_status = ?, effective_status = ?, updated_at = ?
            WHERE profile_slug = ? AND run_id = ?
            """,
            (new_auto, new_effective, now, profile_slug, run_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO lead_status (
                profile_slug, run_id, auto_status, manual_status, effective_status, updated_at
            )
            VALUES (?, ?, ?, NULL, ?, ?)
            """,
            (profile_slug, run_id, new_auto, new_effective, now),
        )
    if old_effective != new_effective:
        conn.execute(
            """
            INSERT INTO status_change_log (
                profile_slug, run_id, changed_at, changed_by,
                from_status, to_status, source, reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (profile_slug, run_id, now, "imap", old_effective, new_effective, "auto", reason),
        )


def _imap_config() -> tuple[str, int, str, str] | None:
    host = (os.environ.get("IMAP_HOST") or "imap.mxhichina.com").strip()
    port = int((os.environ.get("IMAP_PORT") or "993").strip())
    user = (os.environ.get("IMAP_USER") or os.environ.get("SMTP_USER") or "").strip()
    password = (os.environ.get("IMAP_PASS") or os.environ.get("SMTP_PASS") or "").strip()
    if not host or not user or not password:
        return None
    return host, port, user, password


def _raw_message_from_fetch(fetch_data: list) -> bytes | None:
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def poll_once(db_path: Path) -> None:
    config = _imap_config()
    if config is None:
        return
    host, port, user, password = config
    imap = imaplib.IMAP4_SSL(host, port)
    try:
        imap.login(user, password)
        status, _ = imap.select("INBOX")
        if status != "OK":
            return
        status, search_data = imap.uid("search", None, "UNSEEN")
        if status != "OK" or not search_data:
            return
        for uid_bytes in search_data[0].split():
            imap_uid = uid_bytes.decode("ascii", errors="ignore")
            with _db(db_path) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM received_replies WHERE imap_uid = ?",
                    (imap_uid,),
                ).fetchone()
            if exists:
                continue

            status, fetch_data = imap.uid("fetch", uid_bytes, "(RFC822)")
            if status != "OK":
                continue
            raw_message = _raw_message_from_fetch(fetch_data)
            if raw_message is None:
                continue

            message = BytesParser(policy=policy.default).parsebytes(raw_message)
            subject = _decode_header(message.get("Subject"))
            from_email = parseaddr(str(message.get("From") or ""))[1].lower()
            body_text = _message_body(message)
            message_id = str(message.get("Message-ID") or "").strip()
            in_reply_to = str(message.get("In-Reply-To") or "").strip()
            received_at = _received_at(message)

            with _db(db_path) as conn:
                profile_slug, run_id, match_method = _match_sent_message(
                    conn,
                    from_email=from_email,
                    in_reply_to=in_reply_to,
                )
                result = _judge_reply(subject, body_text)
                verdict = str(result.get("verdict") or "unclear").strip().lower()
                reasoning = str(result.get("reason") or "").strip()
                judged_at = dt.datetime.now().isoformat(timespec="seconds")
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO received_replies (
                        imap_uid, message_id, in_reply_to, profile_slug, run_id,
                        received_at, from_email, subject, body_text, match_method,
                        llm_verdict, llm_reasoning, judged_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        imap_uid,
                        message_id,
                        in_reply_to,
                        profile_slug,
                        run_id,
                        received_at,
                        from_email,
                        subject,
                        body_text,
                        match_method,
                        verdict,
                        reasoning,
                        judged_at,
                    ),
                )
                if cursor.rowcount and profile_slug and run_id and verdict == "valid":
                    _update_valid_reply_status(
                        conn,
                        profile_slug=profile_slug,
                        run_id=run_id,
                        reason=reasoning,
                    )
    finally:
        try:
            imap.logout()
        except Exception:
            pass
