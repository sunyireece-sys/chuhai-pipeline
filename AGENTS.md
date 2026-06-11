# Agents Working in This Repo

This file is for coding agents (Codex, Claude Code, …). Read [`CONTRIBUTING.md`](CONTRIBUTING.md)
first for the human collaboration model — those rules apply to you too.

## Hard constraints

1. **Do not touch `xiaoman_playwright.py` without an explicit task that names it.** The
   selectors and login-state assumptions are fragile and tied to a Mac-only Chromium
   profile. Changes here have broken production runs in the past.
2. **CODEOWNERS-protected paths require a human review.** You may propose changes in a
   PR, but do not self-merge.
3. **No mock databases in tests.** Integration tests for webui must hit a real SQLite
   instance. Mocking has masked migration breakage before.
4. **No fabricated personas in outbound copy.** If you're touching `send_outreach.py`
   or any email template, do not invent sender names ("Nicky", "James", …). Default to
   "We". This rule exists because invented personas have leaked into production sends.
5. **Verify before claiming.** Before saying "this works" or "tests pass", actually run
   the command and quote the output. Especially when reviewing or judging existing
   work.

## Where to do which kind of work

| Task type | Tool of choice |
|---|---|
| Pure code change (bug fix, refactor in `webui/` or `cloudflare/`, test coverage) | Codex task — write a focused task brief to `工作日志/codex任务/<topic>.md` |
| Visual / copy / layout change in `redvia-site/` | Claude Code direct edit |
| Documentation, ADRs, this file | Claude Code direct edit |
| Anything in `pipeline.py`, `xiaoman_playwright.py`, `llm_judge.py`, `send_outreach.py` | Codex task with explicit user sign-off in advance |

## Before you start a task

1. Read the latest `工作日志/ai交接日志_*.md` to know what state the previous session
   left things in.
2. Check `docs/superpowers/specs/` and `docs/superpowers/plans/` for any active spec
   or plan that this task feeds into.
3. If you're about to do any creative or design work, use the `superpowers:brainstorming`
   skill first. Implementation skills (frontend-design, etc.) come AFTER spec approval.

## After a meaningful change

- Run `ruff check .` and `pytest webui/` locally.
- Commit in small, descriptive chunks (Conventional Commits style is fine but not
  required).
- Push, open a PR, let CI run. Do not bypass `--no-verify`.
- If you ran a long-running task, update `工作日志/ai交接日志_<today>.md` with the
  state you're leaving the repo in. The next agent (or human) starts from there.

## Memory hygiene

If you maintain a persistent memory file system (`~/.claude/projects/.../memory/`),
sync user-facing and project-facing facts at meaningful milestones. Don't wait until
the end of a session — terminals can crash.
