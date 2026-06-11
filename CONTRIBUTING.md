# Contributing to chuhai-pipeline

Welcome. This repo coordinates a single-operator B2B buyer-acquisition pipeline plus a
sales-facing webUI and a Cloudflare-hosted product site. The collaboration model below
applies to **the current (旧项目稳定期) phase**. New projects spun out of this repo
(e.g. the Apollo/Clay-based pipeline that will replace Xiaoman) will adopt a distributed
ownership model and update this document.

> **Current status (2026-06-11):** Devcontainer is planned (see
> [`docs/superpowers/plans/2026-06-04-foundation-collab-scaffolding.md`](docs/superpowers/plans/2026-06-04-foundation-collab-scaffolding.md))
> but not yet built. For now, run locally in a Python 3.11 venv + Node 20.

## Before you start

1. Read [`ARCHITECTURE.md`](ARCHITECTURE.md) to understand the system topology.
2. Clone the repo, create a Python 3.11 venv, `pip install -r requirements.txt`.
   `cd cloudflare && npm install` for the Worker side.
3. Ask the maintainer (@sunyireece-sys) for secrets. Until Vaultwarden is up, secrets
   are handed over out-of-band — never in GitHub Issues, PR descriptions, or chat
   plaintext.

## What you can edit without coordination

| Path | Who can review |
|---|---|
| `webui/` | any collaborator with write access |
| `redvia-site/` | any collaborator |
| `docs/`, `docs/architecture/` | any collaborator |
| Helper scripts (`scripts/`, `tools/`) | any collaborator |
| `requirements.txt`, top-level configs (`pyproject.toml`, `.gitignore`) | any collaborator |

## What requires @sunyireece-sys review

These paths are owned by the maintainer per [`.github/CODEOWNERS`](.github/CODEOWNERS):

- `pipeline.py` — orchestrator, money path
- `xiaoman_playwright.py` — login-state-bound scraper, fragile to small changes
- `llm_judge.py` — LLM provider abstraction; cross-cuts step 4 and step 5
- `send_outreach.py` — outbound email; SMTP creds + tracking integration
- `cloudflare/` — Worker + D1 schema for tracking
- `.env.example`, `.github/workflows/`, `.github/CODEOWNERS` itself

If your change touches one of these, expect a slower review cycle. Open an issue first
to surface intent before writing code.

## Pull request flow

1. Branch off `main`. Name your branch like `feat/<short>` or `fix/<short>`.
2. Make focused commits. Frequent small commits are preferred over one mega-commit.
3. Open a PR. The CI workflow runs ruff + pytest + a wrangler dry-run.
4. If CI is red, fix it before requesting review.
5. Wait for review from the appropriate owner. A non-CODEOWNERS reviewer approval is
   enough for non-protected paths; CODEOWNERS approval is required for protected paths.
6. Squash-merge when approved.

## Running pieces locally

```bash
pytest webui/                                 # run all webui tests
ruff check .                                  # lint
cd cloudflare && npx wrangler dev             # Worker local preview
python pipeline.py runs/<dir>/01_keywords.md --skip-step3   # pipeline without Xiaoman
```

Xiaoman step 3 (`xiaoman_playwright.py`) requires a logged-in Chromium profile on the
maintainer's Mac and cannot run in CI. If your work depends on step-3 output, ask the
maintainer to run it and push fresh `runs/<dir>/03_xiaoman.xlsx` to a shared location.

## Operational notes

- `工作日志/`, `会议/`, `Learning materials/`, `独立站产品素材/` are gitignored and
  contain internal notes. Do not move content from them into tracked files without
  scrubbing.
- The legacy `reeceYYYY-MM-DD.md` short-form work log is retired. Use the detailed
  `工作日志/<date>.md` form (or the `工作日志/ai交接日志_<date>.md` form) only.
- Architecture decisions belong in `docs/architecture/000N-*.md` (ADRs), not in commit
  messages or chat logs.

## Coding agents

If you're a coding agent (Codex, Claude Code, etc.), read [`AGENTS.md`](AGENTS.md)
before doing anything else.
