"""AI synthesis: collect feedback signals, update lead priority, and persist prompt hints."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from webui.lead_priority import compute_final_score, load_ranking_inputs

DB_PATH_DEFAULT = Path(
    os.environ.get("FEEDBACK_DB_PATH")
    or (Path(__file__).resolve().parent / "feedback.db")
)
RUNS_DIR = Path(
    os.environ.get("RUNS_DIR")
    or (Path(__file__).resolve().parent.parent / "runs")
)
PROMPT_HINTS_PATH = Path(
    os.environ.get("PROMPT_HINTS_PATH")
    or (DB_PATH_DEFAULT.parent / "prompt_hints.json")
)

_synthesis_lock = threading.Lock()


def run_synthesis(db_path: Path | None = None, trigger: str = "manual") -> dict:
    """
    Run one synthesis pass.

    Returns: {"ok": bool, "summary": str, "error": str | None}
    """
    if not _synthesis_lock.acquire(blocking=False):
        return {"ok": False, "summary": "", "error": "synthesis already running"}
    try:
        return _run_synthesis_impl(db_path or DB_PATH_DEFAULT, trigger)
    finally:
        _synthesis_lock.release()


def _run_synthesis_impl(db_path: Path, trigger: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        signals = _collect_signals(conn)
        prompt = _build_prompt(signals)

        try:
            payload, llm_raw = _call_llm(prompt)
        except Exception as exc:
            logging.error("synthesis LLM error: %s", exc)
            return {"ok": False, "summary": "", "error": str(exc)}

        priority_updates, prompt_suggestions, patterns, summary = _normalize_payload(payload)
        now = dt.datetime.now().isoformat(timespec="seconds")

        conn.execute(
            """
            INSERT INTO synthesis_log (
                triggered_by, feedback_count, valid_reply_count, content_flag_count,
                priority_updates_json, prompt_suggestions_json, patterns_json,
                summary, llm_raw, run_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trigger,
                sum(int(row["cnt"]) for row in signals["edit_stats"]),
                len(signals["valid_replies"]),
                len(signals["content_flags"]),
                json.dumps(priority_updates, ensure_ascii=False),
                json.dumps(prompt_suggestions, ensure_ascii=False),
                json.dumps(patterns, ensure_ascii=False),
                summary,
                llm_raw[:8000],
                now,
            ),
        )

        for update in priority_updates:
            slug = str(update.get("profile_slug") or "").strip()
            run_id = str(update.get("run_id") or "").strip()
            if not slug or not run_id or run_id.startswith("test_"):
                continue
            try:
                llm_score = float(update.get("priority", 0.5))
            except (TypeError, ValueError):
                continue

            current = conn.execute(
                """
                SELECT effective_status
                FROM lead_status
                WHERE profile_slug = ? AND run_id = ?
                """,
                (slug, run_id),
            ).fetchone()
            if not current:
                continue

            score = max(0.0, min(1.0, llm_score))
            conn.execute(
                """
                UPDATE lead_status
                SET feedback_score = ?, updated_at = ?
                WHERE profile_slug = ? AND run_id = ?
                """,
                (score, now, slug, run_id),
            )
            inputs = load_ranking_inputs(conn, RUNS_DIR, slug, run_id)
            inputs.feedback_score = score
            conn.execute(
                """
                UPDATE lead_status
                SET lead_priority = ?
                WHERE profile_slug = ? AND run_id = ?
                """,
                (compute_final_score(inputs), slug, run_id),
            )

        conn.commit()

        try:
            PROMPT_HINTS_PATH.write_text(
                json.dumps(
                    {
                        "generated_at": now,
                        "triggered_by": trigger,
                        "summary": summary,
                        "suggestions": prompt_suggestions,
                        "patterns": patterns,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logging.warning("failed to write prompt_hints.json: %s", exc)

        return {"ok": True, "summary": summary, "error": None}
    finally:
        conn.close()


def _normalize_payload(payload: Any) -> tuple[list[dict], dict, list, str]:
    if not isinstance(payload, dict):
        return [], {}, [], ""

    raw_updates = payload.get("priority_updates")
    priority_updates = (
        [item for item in raw_updates if isinstance(item, dict)]
        if isinstance(raw_updates, list)
        else []
    )

    raw_suggestions = payload.get("prompt_suggestions")
    prompt_suggestions = raw_suggestions if isinstance(raw_suggestions, dict) else {}

    raw_patterns = payload.get("patterns")
    patterns = raw_patterns if isinstance(raw_patterns, list) else []

    summary = str(payload.get("summary") or "").strip()
    return priority_updates, prompt_suggestions, patterns, summary


def _collect_signals(conn: sqlite3.Connection) -> dict:
    exclude_test = r"run_id NOT LIKE 'test\_%' ESCAPE '\'"

    edit_stats = conn.execute(
        f"""
        SELECT field, classification, COUNT(*) AS cnt
        FROM edit_classifications
        WHERE {exclude_test}
        GROUP BY field, classification
        """
    ).fetchall()

    content_flags = conn.execute(
        f"""
        SELECT field, original_text, edited_text, reasoning
        FROM content_flags
        WHERE resolved = 0 AND {exclude_test}
        ORDER BY flagged_at DESC
        LIMIT 20
        """
    ).fetchall()

    tone_examples = conn.execute(
        r"""
        SELECT te.field, te.original_text, te.edited_text, te.username
        FROM tone_examples te
        WHERE te.profile_slug NOT IN (
            SELECT profile_slug
            FROM edit_classifications
            WHERE run_id LIKE 'test\_%' ESCAPE '\'
        )
        ORDER BY te.recorded_at DESC
        LIMIT 30
        """
    ).fetchall()

    valid_replies = conn.execute(
        f"""
        SELECT profile_slug, run_id, company_name, llm_reasoning, received_at
        FROM received_replies
        WHERE llm_verdict = 'valid'
          AND {exclude_test}
        ORDER BY received_at DESC
        LIMIT 50
        """
    ).fetchall()

    leads = conn.execute(
        f"""
        SELECT ls.profile_slug, ls.run_id, ls.effective_status,
               ls.lead_priority, ct.type AS customer_type
        FROM lead_status ls
        LEFT JOIN customer_type ct
          ON ct.profile_slug = ls.profile_slug AND ct.run_id = ls.run_id
        WHERE ls.{exclude_test}
        ORDER BY ls.lead_priority DESC
        LIMIT 200
        """
    ).fetchall()

    last_summary = conn.execute(
        "SELECT summary, run_at FROM synthesis_log ORDER BY id DESC LIMIT 1"
    ).fetchone()

    return {
        "edit_stats": [dict(row) for row in edit_stats],
        "content_flags": [dict(row) for row in content_flags],
        "tone_examples": [dict(row) for row in tone_examples],
        "valid_replies": [dict(row) for row in valid_replies],
        "leads": [dict(row) for row in leads],
        "last_summary": dict(last_summary) if last_summary else None,
    }


def _build_prompt(signals: dict) -> str:
    lines = [
        "你是一个 sales outreach 系统的优化助手。",
        "我们是中国枸杞出口商（白瑞源），向海外买家发英文邮件外联。",
        "请基于下面的反馈数据，输出 lead feedback 微调和 prompt 改进建议。",
        "注意：priority 是 0-1 的反馈分，只作为最终 ranking 的一个加权输入，不直接覆盖最终排序。",
        "",
        "=== 邮件编辑统计（按字段 × 分类）===",
    ]

    for row in signals["edit_stats"]:
        lines.append(f"  {row['field']} | {row['classification']}: {row['cnt']} 次")
    if not signals["edit_stats"]:
        lines.append("  （暂无）")

    lines += ["", "=== 事实性错误（content_flags，待修正）==="]
    if signals["content_flags"]:
        for flag in signals["content_flags"][:10]:
            lines.append(f"  字段={flag['field']}")
            lines.append(f"  原文: {(flag['original_text'] or '')[:120]}")
            lines.append(f"  改后: {(flag['edited_text'] or '')[:120]}")
            lines.append(f"  原因: {flag['reasoning'] or ''}")
            lines.append("")
    else:
        lines.append("  （暂无）")

    lines += ["=== 语气示例（tone_examples，每个字段最多 3 条）==="]
    by_field: dict[str, list[dict]] = {}
    for example in signals["tone_examples"]:
        by_field.setdefault(example["field"], []).append(example)
    if by_field:
        for field, examples in by_field.items():
            lines.append(f"  {field}:")
            for example in examples[:3]:
                original = (example["original_text"] or "")[:80]
                edited = (example["edited_text"] or "")[:80]
                lines.append(f"    [{example['username']}] {original} -> {edited}")
    else:
        lines.append("  （暂无）")

    lines += ["", "=== 有效回复 ==="]
    if signals["valid_replies"]:
        for reply in signals["valid_replies"]:
            company = reply["company_name"] or reply["profile_slug"]
            lines.append(
                f"  {company} ({reply['run_id']}) - "
                f"{(reply['llm_reasoning'] or '')[:100]}"
            )
    else:
        lines.append("  （暂无）")

    lines += ["", "=== 当前 lead 列表（slug | run_id | 状态 | priority | 类型）==="]
    if signals["leads"]:
        for lead in signals["leads"]:
            lines.append(
                f"  {lead['profile_slug']} | {lead['run_id']} | "
                f"{lead['effective_status']} | {float(lead['lead_priority']):.2f} | "
                f"{lead.get('customer_type') or '未知'}"
            )
    else:
        lines.append("  （暂无）")

    if signals["last_summary"]:
        lines += [
            "",
            "=== 上次 synthesis 摘要（连续记忆，可参考）===",
            signals["last_summary"]["summary"],
        ]

    lines += [
        "",
        "请严格按以下 JSON schema 输出；不要 markdown 代码块，不要注释：",
        "{",
        '  "priority_updates": [',
        '    {"profile_slug": "...", "run_id": "...", "priority": 0.0}',
        "  ],",
        '  "prompt_suggestions": {',
        '    "cold_email_subject": "具体可操作建议；如无则省略此 key",',
        '    "cold_email_body": "...",',
        '    "whatsapp_or_linkedin_message": "...",',
        '    "follow_up_email": "..."',
        "  },",
        '  "patterns": ["归纳的规律1", "规律2"],',
        '  "summary": "2-3 句话：什么有效、什么需改进"',
        "}",
        "",
        "priority 规则：",
        "- 已收到有效回复、已询价等高意向状态通常给高反馈分",
        "- 已发但暂无回复可在 0.40-0.70 间细分",
        "- 未发 lead 可按客户类型和反馈规律排序",
        "- 不相关、国家错、邮箱无效等低价值状态不要提高",
        "- 只为有证据可调整的 lead 出现在 priority_updates 里，其余省略",
    ]

    return "\n".join(lines)


def _call_llm(prompt: str) -> tuple[dict, str]:
    """Return (parsed_payload, raw_text) from the configured OpenAI-compatible LLM."""
    api_key = (
        os.environ.get("GLM_API_KEY")
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()
    if not api_key:
        raise RuntimeError("No LLM API key in env")

    from openai import OpenAI

    client_kwargs: dict = {"api_key": api_key}
    base_url = (os.environ.get("LLM_BASE_URL") or "").strip()
    if base_url:
        client_kwargs["base_url"] = base_url
    client_kwargs["timeout"] = float(os.environ.get("LLM_TIMEOUT_SECONDS", "120"))
    client = OpenAI(**client_kwargs)
    response = client.chat.completions.create(
        model=os.environ.get("LLM_MODEL", "glm-4-flash"),
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=2000,
    )
    raw = (response.choices[0].message.content or "").strip()
    return json.loads(raw), raw
