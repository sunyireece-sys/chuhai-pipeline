# Foundation Collaboration Scaffolding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land波 1 (Foundation) work items from spec `docs/superpowers/specs/2026-06-04-multi-person-collaboration-design.md`: tooling baseline, GitHub Actions CI + CODEOWNERS, Devcontainer entry, top-level docs (CONTRIBUTING / AGENTS / ADR), Issue templates + Projects board, and stage-A secret rotation to company LLM gateway.

**Architecture:** All changes are additive scaffolding — no existing runtime code is rewritten. Pipeline / webui / cloudflare logic stay untouched. New files cluster under `.devcontainer/`, `.github/`, `docs/architecture/`, plus root-level `CONTRIBUTING.md` / `AGENTS.md`. Spec §1 invariant: `xiaoman_playwright.py` stays Mac-only — Devcontainer explicitly does NOT support step3.

**Tech Stack:** Python 3.11 + Node 20 inside Devcontainer; `ruff` for lint; `pytest` for tests; GitHub Actions for CI; Docker Compose for container orchestration; GitHub Issues + Projects (v2) for task flow.

---

## Spec coverage map

| Spec section | Plan tasks |
|---|---|
| §3.1 顶层文档 CONTRIBUTING / AGENTS | Task 9 |
| §3.2 ADR 0001-0004 | Task 10 |
| §3.3 reece 简短版废除 | Task 9 (CONTRIBUTING calls it out) |
| §4.1 阶段 A secret 替换 | Task 13 |
| §5 Devcontainer | Tasks 5–8 |
| §7.1 CI 基线 | Tasks 1–3 |
| §7.2 CODEOWNERS | Task 4 |
| §8.1 Issue 模板 | Task 11 |
| §8.2 Projects 看板 | Task 12 |

Waves 2 (webui fly→ECS) and 3 (Vaultwarden + sales→issue sync) are out of scope for this plan; they get their own writing-plans cycles.

## File structure

**Create:**
- `pyproject.toml` — root project config; declares ruff + pytest sections, no packaging
- `.devcontainer/Dockerfile` — base = `python:3.11-slim`; layers Node 20 + Playwright + `bw` (Bitwarden CLI)
- `.devcontainer/docker-compose.yml` — single `dev` service mounting the workspace + a SQLite volume
- `.devcontainer/devcontainer.json` — IDE entry point referencing the compose service
- `.devcontainer/postCreate.sh` — runs after container build (pip install, playwright install, bootstrap env)
- `scripts/bootstrap-env.sh` — vault-aware `.env` materializer (Phase D will plug Vaultwarden; Phase A uses env-var fallback)
- `.github/workflows/ci.yml` — 3-step CI per PR
- `.github/CODEOWNERS` — core-5 ownership rules
- `.github/ISSUE_TEMPLATE/bug.md` / `feature.md` / `agent-task.md`
- `CONTRIBUTING.md` / `AGENTS.md`
- `docs/architecture/README.md` — ADR index
- `docs/architecture/0001-xiaoman-playwright-path.md`
- `docs/architecture/0002-cloudflare-tracking-stack.md`
- `docs/architecture/0003-webui-tech-stack.md`
- `docs/architecture/0004-llm-provider-abstraction.md`

**Modify:**
- `requirements.txt` — append `ruff>=0.6` and `pytest>=8.0` (loose pins for dev tools)
- `.env.example` — annotate which keys come from the company LLM gateway

---

## Task 1: Tooling baseline (pyproject.toml + ruff + pytest deps)

**Files:**
- Create: `pyproject.toml`
- Modify: `requirements.txt`

- [ ] **Step 1: Append dev tools to `requirements.txt`**

Open `requirements.txt`. Append at the end:

```
# dev tooling (lint + test) — added 2026-06-04 for CI baseline
ruff>=0.6,<1.0
pytest>=8.0,<9.0
```

- [ ] **Step 2: Create `pyproject.toml` at repo root**

```toml
[tool.ruff]
line-length = 100
target-version = "py311"
exclude = [
  ".venv",
  ".devcontainer",
  "runs",
  "backups",
  "redvia-site/assets",
  "工作日志",
  "会议",
]

[tool.ruff.lint]
# Start permissive: pyflakes (F) + pycodestyle errors (E) + import sort (I)
# Do NOT enable B/UP/N yet — existing code will explode.
select = ["E", "F", "I"]
ignore = [
  "E501",  # line length: we'll tighten later
]

[tool.pytest.ini_options]
testpaths = ["webui", "tests"]
python_files = ["test_*.py"]
addopts = "-q"
```

- [ ] **Step 3: Install dev tools locally**

Run:
```bash
source .venv/bin/activate
pip install -r requirements.txt
```
Expected: `ruff` and `pytest` install with no version conflicts.

- [ ] **Step 4: Verify versions**

Run:
```bash
.venv/bin/ruff --version
.venv/bin/pytest --version
```
Expected: ruff 0.6.x or higher; pytest 8.x.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml requirements.txt
git commit -m "Tooling: add ruff + pytest baseline for CI"
```

---

## Task 2: Ruff initial sweep and fix-up

**Files:**
- Modify: any `.py` that ruff flags
- (Per spec §7.1, CI step 1 = `ruff check .`. CI must be green on day 1, so we burn down errors now.)

- [ ] **Step 1: First ruff scan**

Run:
```bash
.venv/bin/ruff check .
```
Expected: list of warnings; record total count. Common findings: unused imports (`F401`), undefined names (`F821`), import order (`I001`).

- [ ] **Step 2: Apply auto-fixes**

Run:
```bash
.venv/bin/ruff check --fix .
```
Expected: most `F401` / `I001` issues auto-resolve. Re-run `.venv/bin/ruff check .` and confirm remaining issues are non-trivial (real bugs or intentional patterns).

- [ ] **Step 3: Triage remaining warnings**

For each remaining warning, choose ONE of:
- Fix the code (preferred for real issues like `F821` undefined name)
- Add `# noqa: F401` for legit re-exports (e.g., `__init__.py` re-exporting names)
- Add a per-file ignore in `pyproject.toml` under `[tool.ruff.lint.per-file-ignores]` for known-noisy files

Do NOT widen `ignore` globally to silence everything — that defeats the point.

- [ ] **Step 4: Verify clean**

Run:
```bash
.venv/bin/ruff check .
```
Expected: `All checks passed!`

- [ ] **Step 5: Verify pytest still passes locally**

Run:
```bash
.venv/bin/pytest webui/
```
Expected: existing webui tests pass (auto-fix may have touched imports; confirm no regression).

- [ ] **Step 6: Commit**

```bash
git add -u
git commit -m "Tooling: clear ruff baseline across repo"
```

---

## Task 3: GitHub Actions CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Create workflow file**

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [main]

jobs:
  lint-and-test:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip

      - name: Install Python deps
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Ruff lint
        run: ruff check .

      - name: Pytest (webui)
        run: pytest webui/ -q

  worker-typecheck:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    defaults:
      run:
        working-directory: cloudflare
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: "20"
          cache: npm
          cache-dependency-path: cloudflare/package-lock.json

      - name: Install Worker deps
        run: npm ci

      - name: Wrangler dry-run (esbuild bundle check)
        run: npx wrangler deploy --dry-run --outdir=/tmp/worker-bundle
        env:
          # wrangler needs a token shape to dry-run, but won't actually call CF API
          CLOUDFLARE_API_TOKEN: dryrun-no-network
```

- [ ] **Step 2: Validate YAML locally**

Run:
```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))" 2>&1 || \
  echo "skip: PyYAML not installed; will rely on GitHub's parse in Step 5 instead"
```
Expected: either "no output" (parse OK) or the skip message. If PyYAML is installed and the parse errors, fix syntax before continuing.

- [ ] **Step 3: Confirm cloudflare/ has package-lock.json**

Run:
```bash
ls cloudflare/package-lock.json cloudflare/package.json
```
If `package-lock.json` is missing, run `cd cloudflare && npm install` to generate it, then back out. Stage both files.

- [ ] **Step 4: Commit (push triggers first CI run)**

```bash
git add .github/workflows/ci.yml cloudflare/package-lock.json
git commit -m "CI: add GitHub Actions baseline (ruff + pytest + wrangler dry-run)"
git push origin main
```

- [ ] **Step 5: Verify the run on GitHub**

Open: https://github.com/sunyireece-sys/chuhai-pipeline/actions
Expected: workflow named "CI" runs against the just-pushed commit. Both `lint-and-test` and `worker-typecheck` jobs go green. If red, fix the failure inline and push again — do NOT mark this task done until CI is green.

---

## Task 4: CODEOWNERS

**Files:**
- Create: `.github/CODEOWNERS`

- [ ] **Step 1: Create CODEOWNERS**

```
# Files that require @sunyireece-sys review.
# Other files can be reviewed by any collaborator with write access.
# Phase note (spec §1): this is the旧项目稳定期 model;
# new projects (post-Apollo) will adopt distributed ownership.

pipeline.py             @sunyireece-sys
xiaoman_playwright.py   @sunyireece-sys
llm_judge.py            @sunyireece-sys
send_outreach.py        @sunyireece-sys
cloudflare/             @sunyireece-sys
.env.example            @sunyireece-sys
.github/workflows/      @sunyireece-sys
.github/CODEOWNERS      @sunyireece-sys
```

- [ ] **Step 2: Verify GitHub will parse it**

GitHub's CODEOWNERS syntax check happens on push. We can self-verify path patterns are valid before pushing:

Run:
```bash
# Each pattern should match at least the file that exists today
git ls-files | grep -E "^(pipeline\.py|xiaoman_playwright\.py|llm_judge\.py|send_outreach\.py|cloudflare/|\.env\.example|\.github/)" | head -20
```
Expected: at least 5 lines matched.

- [ ] **Step 3: Commit and push**

```bash
git add .github/CODEOWNERS
git commit -m "Collab: add CODEOWNERS protecting core 5 paths"
git push origin main
```

- [ ] **Step 4: Verify on GitHub**

Open https://github.com/sunyireece-sys/chuhai-pipeline/settings/branches
Optionally turn on branch protection for `main` and check "Require review from Code Owners". This is recommended but not blocking — note it as a follow-up if you skip.

---

## Task 5: Devcontainer container definition (Dockerfile + compose)

**Files:**
- Create: `.devcontainer/Dockerfile`
- Create: `.devcontainer/docker-compose.yml`

- [ ] **Step 1: Create Dockerfile**

```dockerfile
# Devcontainer base — Python 3.11 + Node 20 + Playwright + Bitwarden CLI.
# NOT a production image. ECS prod build will reuse the Python layer pattern but trim node/playwright.
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# OS deps: curl/git for tooling; build-essential for any C deps in requirements.txt;
# libs that Playwright Chromium will need (we install browsers in postCreate, not here, to keep the base layer cacheable).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg git build-essential \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Node 20 (for cloudflare/ Worker dev + wrangler dry-run)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Bitwarden CLI (Phase D vault client; harmless when vault is not up)
RUN npm install -g @bitwarden/cli

# Non-root user matching common host UID/GID; VSCode/Cursor will remap
ARG USERNAME=dev
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd --gid ${USER_GID} ${USERNAME} \
    && useradd --uid ${USER_UID} --gid ${USER_GID} -m -s /bin/bash ${USERNAME}

USER ${USERNAME}
WORKDIR /workspace
```

- [ ] **Step 2: Create docker-compose.yml**

```yaml
services:
  dev:
    build:
      context: ..
      dockerfile: .devcontainer/Dockerfile
    volumes:
      - ..:/workspace:cached
      - webui-data:/workspace/.webui-data
    command: sleep infinity
    environment:
      # bw CLI talks to either the public Bitwarden server or our Vaultwarden once it's up
      BW_SERVER: ${BW_SERVER:-https://vault.redvia.com}

volumes:
  webui-data:
```

- [ ] **Step 3: Build the image to confirm it's well-formed**

Run from repo root:
```bash
docker compose -f .devcontainer/docker-compose.yml build dev
```
Expected: build succeeds, ends with `naming to docker.io/library/<something>-dev` or similar. If apt or npm errors mid-build, fix layer ordering and re-run.

- [ ] **Step 4: Smoke-test the container**

```bash
docker compose -f .devcontainer/docker-compose.yml run --rm dev python --version
docker compose -f .devcontainer/docker-compose.yml run --rm dev node --version
docker compose -f .devcontainer/docker-compose.yml run --rm dev bw --version
```
Expected: Python 3.11.x, Node 20.x, bw 2024.x.

- [ ] **Step 5: Commit**

```bash
git add .devcontainer/Dockerfile .devcontainer/docker-compose.yml
git commit -m "Devcontainer: add Dockerfile + compose (Python 3.11 + Node 20 + bw)"
```

---

## Task 6: Devcontainer IDE entry + postCreate

**Files:**
- Create: `.devcontainer/devcontainer.json`
- Create: `.devcontainer/postCreate.sh`

- [ ] **Step 1: Create devcontainer.json**

```json
{
  "name": "chuhai-pipeline dev",
  "dockerComposeFile": "docker-compose.yml",
  "service": "dev",
  "workspaceFolder": "/workspace",
  "remoteUser": "dev",
  "postCreateCommand": "bash .devcontainer/postCreate.sh",
  "customizations": {
    "vscode": {
      "extensions": [
        "ms-python.python",
        "ms-python.vscode-pylance",
        "charliermarsh.ruff",
        "ms-azuretools.vscode-docker"
      ],
      "settings": {
        "python.defaultInterpreterPath": "/usr/local/bin/python",
        "python.testing.pytestEnabled": true,
        "[python]": {
          "editor.defaultFormatter": "charliermarsh.ruff",
          "editor.formatOnSave": true
        }
      }
    }
  }
}
```

- [ ] **Step 2: Create postCreate.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail

cd /workspace

echo "==> Installing Python deps"
python -m pip install --upgrade pip
pip install -r requirements.txt

echo "==> Installing Playwright Chromium (needed for website_verify.py only;"
echo "    step3 xiaoman_playwright.py is NOT supported in this container)"
playwright install chromium

echo "==> Bootstrapping .env"
bash scripts/bootstrap-env.sh || {
  echo "WARN: .env bootstrap incomplete. Until Vaultwarden is up, ask the maintainer"
  echo "for missing keys and write them into .env manually."
}

echo "==> postCreate done. Next:"
echo "  pytest webui/                 # run webui tests"
echo "  python pipeline.py --help     # see CLI"
echo "  # NB: 'python pipeline.py runs/.../01_keywords.md' will skip step3 in this container"
```

- [ ] **Step 3: Make postCreate executable**

```bash
chmod +x .devcontainer/postCreate.sh
```

- [ ] **Step 4: Validate JSON parses**

```bash
python -c "import json; json.load(open('.devcontainer/devcontainer.json'))"
```
Expected: no exception.

- [ ] **Step 5: Commit**

```bash
git add .devcontainer/devcontainer.json .devcontainer/postCreate.sh
git commit -m "Devcontainer: add IDE entry + postCreate bootstrap"
```

---

## Task 7: bootstrap-env.sh (Phase A — env-var fallback, Vaultwarden later)

**Files:**
- Create: `scripts/bootstrap-env.sh`
- Modify: `.env.example` — annotate company-gateway keys

- [ ] **Step 1: Create the script**

```bash
#!/usr/bin/env bash
# scripts/bootstrap-env.sh
#
# Materializes a local `.env` from one of two sources, in order:
#   1) Vaultwarden via `bw` CLI (Phase D — preferred once vault is up)
#   2) Pre-existing env vars in the current shell (Phase A — current state)
#
# If neither source provides a key, the script leaves it blank in .env and prints
# a warning. The caller (postCreate.sh / human) is expected to fill the gap.

set -euo pipefail

ENV_FILE="${ENV_FILE:-.env}"
EXAMPLE_FILE="${EXAMPLE_FILE:-.env.example}"

if [[ ! -f "$EXAMPLE_FILE" ]]; then
  echo "ERROR: $EXAMPLE_FILE missing — cannot determine which keys to populate" >&2
  exit 1
fi

if [[ -f "$ENV_FILE" ]]; then
  echo "==> $ENV_FILE already exists; not overwriting. Delete it first to re-run."
  exit 0
fi

# Determine source
SOURCE="env"
if command -v bw >/dev/null 2>&1 && bw status 2>/dev/null | grep -q '"status":"unlocked"'; then
  SOURCE="vault"
fi
echo "==> Bootstrapping $ENV_FILE from source: $SOURCE"

# Walk the example, copy comments/blanks, fill keys we can resolve.
MISSING=()
while IFS= read -r line; do
  if [[ -z "$line" || "$line" == \#* ]]; then
    echo "$line"
    continue
  fi
  key="${line%%=*}"
  case "$SOURCE" in
    vault)
      # Convention: each key stored as a Bitwarden item named "redvia/<KEY>" with the value in the password field
      value="$(bw get password "redvia/$key" 2>/dev/null || true)"
      ;;
    env)
      value="${!key:-}"
      ;;
  esac
  if [[ -z "$value" ]]; then
    MISSING+=("$key")
    echo "$key="
  else
    echo "$key=$value"
  fi
done < "$EXAMPLE_FILE" > "$ENV_FILE"

if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo ""
  echo "WARN: ${#MISSING[@]} key(s) left blank in $ENV_FILE:"
  printf '  - %s\n' "${MISSING[@]}"
  echo ""
  echo "Resolve by either:"
  echo "  · exporting them in your shell before re-running this script, or"
  echo "  · (once vault is up) adding them as 'redvia/<KEY>' items in Vaultwarden, or"
  echo "  · asking the maintainer for the missing values directly."
fi
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x scripts/bootstrap-env.sh
```

- [ ] **Step 3: Annotate `.env.example`**

Open `.env.example`. At the top, prepend:

```
# Keys marked "[company gateway]" are issued by the internal LLM gateway
# (Phase A of secret migration, see docs/superpowers/specs/2026-06-04-multi-person-collaboration-design.md §4).
# Ask the maintainer for the current gateway endpoint + key.
```

Add a `# [company gateway]` comment line directly above each of these entries:
- `LLM_API_KEY=`
- `LLM_BASE_URL=`
- `LLM_MODEL=`

- [ ] **Step 4: Smoke-test the script (host shell)**

```bash
# In a throwaway dir so we don't clobber the real .env
mkdir -p /tmp/bootstrap-env-test && cd /tmp/bootstrap-env-test
cp /Users/cdai@ideo.com/Documents/chuhai_pipeline/.env.example .
SERPER_API_KEY=test123 bash /Users/cdai@ideo.com/Documents/chuhai_pipeline/scripts/bootstrap-env.sh
grep SERPER_API_KEY .env
cd - && rm -rf /tmp/bootstrap-env-test
```
Expected: `SERPER_API_KEY=test123` line in the generated `.env`.

- [ ] **Step 5: Commit**

```bash
git add scripts/bootstrap-env.sh .env.example
git commit -m "Devcontainer: add bootstrap-env.sh (Phase A env-var fallback)"
```

---

## Task 8: End-to-end Devcontainer verification

This task has no new files — it verifies Tasks 5–7 work together.

- [ ] **Step 1: Rebuild + run postCreate inside the container**

```bash
docker compose -f .devcontainer/docker-compose.yml build dev
docker compose -f .devcontainer/docker-compose.yml run --rm dev bash .devcontainer/postCreate.sh
```
Expected: pip install completes; `playwright install chromium` completes; bootstrap-env.sh runs (will warn about missing keys, that's fine — we have no env vars in this raw `docker compose run`).

- [ ] **Step 2: Run pytest inside the container**

```bash
docker compose -f .devcontainer/docker-compose.yml run --rm dev pytest webui/
```
Expected: same green result as Task 2 step 5 on the host.

- [ ] **Step 3: Run ruff inside the container**

```bash
docker compose -f .devcontainer/docker-compose.yml run --rm dev ruff check .
```
Expected: `All checks passed!` (matches CI step 1).

- [ ] **Step 4: Confirm Devcontainer opens in your IDE**

Open VSCode / Cursor / Claude Code on the repo root. Use "Reopen in Container" (or "Dev Containers: Open Folder in Container"). The IDE should build/start the dev service and drop you into `/workspace` with the dev user. Run `pytest webui/` from the IDE terminal to confirm.

- [ ] **Step 5: No code commit needed — only document the verified state**

Add a single bullet to `工作日志/ai交接日志_2026-06-04.md` (create if missing):

```
- Devcontainer verified end-to-end on <YYYY-MM-DD HH:MM>: build OK, pytest webui green, ruff clean, IDE Reopen in Container works.
```

`工作日志/` is gitignored so this is a local record only. No git commit from this task.

---

## Task 9: CONTRIBUTING.md + AGENTS.md

**Files:**
- Create: `CONTRIBUTING.md`
- Create: `AGENTS.md`

- [ ] **Step 1: Create CONTRIBUTING.md**

```markdown
# Contributing to chuhai-pipeline

Welcome. This repo coordinates a single-operator B2B buyer-acquisition pipeline plus a
sales-facing webUI and a Cloudflare-hosted product site. The collaboration model below
applies to **the current (旧项目稳定期) phase**. New projects spun out of this repo
(e.g. the Apollo/Clay-based pipeline that will replace Xiaoman) will adopt a distributed
ownership model and update this document.

## Before you start

1. Read [`ARCHITECTURE.md`](ARCHITECTURE.md) to understand the system topology.
2. Open the repo in a Devcontainer-aware IDE (VSCode, Cursor, Claude Code) and accept
   "Reopen in Container". Everything you need (Python 3.11, Node 20, ruff, pytest,
   wrangler, Bitwarden CLI) is preinstalled.
3. Ask the maintainer (@sunyireece-sys) for secrets. Until Vaultwarden is up, secrets
   are handed over out-of-band — never in GitHub Issues, PR descriptions, or Slack
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

Inside the Devcontainer (recommended for everything except step 3):

```bash
pytest webui/                                 # run all webui tests
ruff check .                                  # lint
cd cloudflare && npx wrangler dev             # Worker local preview
python pipeline.py runs/<dir>/01_keywords.md --skip-step3   # pipeline without Xiaoman
```

Xiaoman step 3 (`xiaoman_playwright.py`) requires a logged-in Chromium profile on the
maintainer's Mac and cannot run in CI or the Devcontainer. If your work depends on
step-3 output, ask the maintainer to run it and push fresh `runs/<dir>/03_xiaoman.xlsx`
to a shared location.

## Operational notes

- `工作日志/`, `会议/`, `Learning materials/`, `独立站产品素材/` are gitignored and
  contain internal notes. Do not move content from them into tracked files without
  scrubbing.
- The legacy `reeceYYYY-MM-DD.md` short-form work log is retired. Use the detailed
  `工作日志/<date>.md` form (or the new `工作日志/ai交接日志_<date>.md` form) only.
- Architecture decisions belong in `docs/architecture/000N-*.md` (ADRs), not in commit
  messages or chat logs.

## Coding agents

If you're a coding agent (Codex, Claude Code, etc.), read [`AGENTS.md`](AGENTS.md)
before doing anything else.
```

- [ ] **Step 2: Create AGENTS.md**

```markdown
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

- Run `ruff check .` and `pytest webui/` inside the Devcontainer.
- Commit in small, descriptive chunks (Conventional Commits style is fine but not
  required).
- Push, open a PR, let CI run. Do not bypass `--no-verify`.
- If you ran a long-running task, update `工作日志/ai交接日志_<today>.md` with the
  state you're leaving the repo in. The next agent (or human) starts from there.

## Memory hygiene

If you maintain a persistent memory file system (`~/.claude/projects/.../memory/`),
sync user-facing and project-facing facts at meaningful milestones. Don't wait until
the end of a session — terminals can crash.
```

- [ ] **Step 3: Verify they render as Markdown**

Open both files in a Markdown previewer (any IDE will do). Confirm headings, tables,
and code fences render correctly.

- [ ] **Step 4: Commit**

```bash
git add CONTRIBUTING.md AGENTS.md
git commit -m "Docs: add CONTRIBUTING + AGENTS (phase-aware collab model)"
```

---

## Task 10: ADR directory + four backfills

**Files:**
- Create: `docs/architecture/README.md`
- Create: `docs/architecture/0001-xiaoman-playwright-path.md`
- Create: `docs/architecture/0002-cloudflare-tracking-stack.md`
- Create: `docs/architecture/0003-webui-tech-stack.md`
- Create: `docs/architecture/0004-llm-provider-abstraction.md`

- [ ] **Step 1: Create the ADR index**

`docs/architecture/README.md`:

```markdown
# Architecture Decision Records

Each ADR captures a cross-file architectural decision: context, options
considered, the choice, and the consequences. They are append-only: when a
decision is reversed, write a new ADR superseding the old one (don't edit the
old text — keep the historical reasoning intact).

## Index

| # | Title | Status |
|---|---|---|
| [0001](0001-xiaoman-playwright-path.md) | Xiaoman step 3 uses Playwright persistent profile | Accepted |
| [0002](0002-cloudflare-tracking-stack.md) | Click + open tracking lives on Cloudflare Worker + D1 | Accepted |
| [0003](0003-webui-tech-stack.md) | webUI is FastAPI + Jinja + HTMX + SQLite | Accepted |
| [0004](0004-llm-provider-abstraction.md) | LLM provider abstraction is centralized in `llm_judge.py` | Accepted |

## Template

When adding a new ADR, use this skeleton:

```markdown
# NNNN — <Title>

- **Status:** Accepted | Proposed | Superseded by [NNNN](NNNN-...md)
- **Date:** YYYY-MM-DD

## Context

What problem were we facing? What forces / constraints / prior decisions
shaped the space?

## Decision

What did we choose? (One paragraph.)

## Alternatives considered

- **Option A** — why it lost
- **Option B** — why it lost

## Consequences

- Positive: …
- Negative / accepted cost: …
- Follow-ups: …
\```
```

- [ ] **Step 2: Create ADR 0001**

`docs/architecture/0001-xiaoman-playwright-path.md`:

```markdown
# 0001 — Xiaoman step 3 uses Playwright persistent profile

- **Status:** Accepted
- **Date:** 2026-04-15 (backfilled 2026-06-04)

## Context

Step 3 of the pipeline scrapes company and contact data from xiaoman.com (Alibaba's
B2B import-export portal). Xiaoman requires login and computes a request signature
that, as of 2026-04-10 investigation, is **body-bound** (not session-bound) and
generated by frontend JavaScript that is non-trivial to replicate.

Prior implementations tried:

1. A Node-based scraper (`legacy/xiaoman_ca/xiaoman_stage2/`) that reimplemented the
   signature in Python. It broke whenever Xiaoman shipped frontend changes.
2. An AppleScript path that drove a real Chrome window via UI automation. Slow,
   brittle, and required the operator to babysit.

## Decision

Use **Playwright** with `launch_persistent_context()` against a long-lived profile
directory (`~/.xiaoman_playwright_profile/`). The first run prompts the operator to
scan a WeChat QR code in the real Chromium window; the session cookie persists in
the profile for subsequent runs. Python fills the search box and presses Enter;
`page.expect_response` intercepts the `searchListV2` and `profileEmails` XHRs and
parses their JSON.

## Alternatives considered

- **Reimplement the signature in Python** — failed twice; fragile to frontend churn.
- **Headless Playwright with cookie injection** — login flow on Xiaoman uses WeChat
  scan, which has no programmatic alternative; persistent context is the only way to
  keep that scan-once model working.

## Consequences

- Positive: stable across most frontend changes; no signature reverse-engineering.
- Negative: step 3 is Mac-only (operator's logged-in profile). CI and Devcontainer
  cannot run step 3.
- Negative: each browser session triggers CAPTCHA after ~60–70 companies. Pipeline
  batches via `--xiaoman-session-limit` (default 60) and `--xiaoman-session-pause`
  (default 300s).
- Follow-up: when Apollo / Clay replaces Xiaoman, this ADR will be superseded.
```

- [ ] **Step 3: Create ADR 0002**

`docs/architecture/0002-cloudflare-tracking-stack.md`:

```markdown
# 0002 — Click + open tracking lives on Cloudflare Worker + D1

- **Status:** Accepted
- **Date:** 2026-05-20 (backfilled 2026-06-04)

## Context

Outreach emails sent by `send_outreach.py` embed a tracking pixel and a redirect
URL of the form `https://redvia.com/t/<token>`. We need to record opens and clicks,
attribute them to a specific outreach token, and surface aggregated data to the
sales-facing webUI.

Original 2026-05-11 plan was to host tracking on the same Alibaba Cloud ECS as
the webUI, capture events with a small FastAPI endpoint, and reconcile against the
webUI database directly.

## Decision

Use a **Cloudflare Worker** (`redvia-tracking`) plus **D1** (`redvia_tracking`) for
ingestion. The Worker serves the redirect + 1x1 pixel, writes events to D1, and
runs a 1-minute scheduled task that batches new events to the webUI via signed
webhook.

## Alternatives considered

- **Alibaba Cloud ECS endpoint** — would have been a fifth service on the same VM
  shared with webUI. Cloudflare's global edge gives near-zero-latency tracking
  pixels worldwide and free DDoS / WAF.
- **Plausible / Fathom / a third-party tracker** — buyers click links from email
  clients; we need per-token attribution, which generic analytics products don't
  give us.

## Consequences

- Positive: tracking ingestion scales independently of webUI; D1 is free-tier
  generous; Worker `waitUntil` + scheduled syncs keep latency low.
- Negative: introduces a second persistence store. The webUI is the system of
  record for `send_tracking`; D1 is upstream and synced one-way.
- Follow-up: EU consent banner must land before any EU outreach goes out — current
  `redvia-site/assets/js/site.js` has no consent flow yet.
```

- [ ] **Step 4: Create ADR 0003**

`docs/architecture/0003-webui-tech-stack.md`:

```markdown
# 0003 — webUI is FastAPI + Jinja + HTMX + SQLite

- **Status:** Accepted
- **Date:** 2026-03-10 (backfilled 2026-06-04)

## Context

The sales-facing webUI started as a thin tool for capturing feedback on
pipeline-produced leads. It has since grown to cover lead prioritization, reply
synthesis, an admin panel, and a tracking dashboard. The next phase adds an
ingest endpoint for Cloudflare tracking events.

## Decision

Stay on **FastAPI** (Python) + **Jinja templates** + **HTMX** for incremental
updates + **SQLite** for storage. No React, no Postgres, no SPA build pipeline.

## Alternatives considered

- **Next.js / React** — overkill for a small internal tool; would force a second
  language for the same team; deploy complexity goes up.
- **Postgres** — current concurrency profile (a few sales + 1/min Cloudflare sync)
  is far below SQLite's single-writer ceiling. Switching DBs would require rewriting
  the DAO layer for no measurable benefit today.

## Consequences

- Positive: one-language stack, instant page renders, small deploy surface.
- Positive: the Devcontainer doesn't need to start a DB container.
- Negative: when concurrency does grow (e.g. webUI exposed to many simultaneous
  sales sessions), we will need a migration plan. ADR-supersession will land then.
- Follow-up: keep the DAO layer cleanly bounded so a future PG swap is feasible.
```

- [ ] **Step 5: Create ADR 0004**

`docs/architecture/0004-llm-provider-abstraction.md`:

```markdown
# 0004 — LLM provider abstraction is centralized in `llm_judge.py`

- **Status:** Accepted
- **Date:** 2026-04-25 (backfilled 2026-06-04)

## Context

Multiple parts of the system talk to an LLM: step 4 verification (`website_verify.py`
calls `llm_judge.py`), step 5 enrichment (`profile_enrich.py`), and the webUI reply
synthesizer (`webui/synthesizer.py`). We have used (in rough order) OpenAI, GLM,
DeepSeek, and a company-internal LLM gateway. Each provider has its own base URL,
auth header style, and quirks around streaming and tool calling.

## Decision

All LLM provider details live in **`llm_judge.py`**. Other modules import a
provider-neutral function from it (e.g. `judge(prompt, schema)` or
`chat(messages, model)`). Provider switching is configured via three environment
variables: `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`. Anything that needs the LLM
goes through this module — pipeline orchestration code never instantiates an
OpenAI client directly.

## Alternatives considered

- **One client per call site** — duplicated config-loading code and made it
  painful to swap providers (which we have done four times).
- **A dedicated `llm/` package with submodules per provider** — overkill for a
  three-method surface area. We can split later if the surface grows.

## Consequences

- Positive: provider rotation (Phase A of the secret migration is the latest
  rotation) is a one-file change.
- Positive: the company LLM gateway is OpenAI-compatible, so `LLM_BASE_URL` switch
  is enough — no code change needed.
- Negative: anything LLM-shaped that isn't routed through `llm_judge.py` is a
  potential drift point. Reviewers should reject PRs that import `openai` or
  `anthropic` directly outside this file.
```

- [ ] **Step 6: Commit**

```bash
git add docs/architecture/
git commit -m "Docs: add ADR directory with 4 backfilled decisions"
```

---

## Task 11: GitHub issue templates

**Files:**
- Create: `.github/ISSUE_TEMPLATE/bug.md`
- Create: `.github/ISSUE_TEMPLATE/feature.md`
- Create: `.github/ISSUE_TEMPLATE/agent-task.md`
- Create: `.github/ISSUE_TEMPLATE/config.yml` — disables blank issues

- [ ] **Step 1: Create config.yml**

```yaml
blank_issues_enabled: false
contact_links:
  - name: Sales feedback (via webUI)
    url: https://github.com/sunyireece-sys/chuhai-pipeline/blob/main/CONTRIBUTING.md#sales-feedback
    about: Sales should file feedback inside the webUI. A background sync job converts feedback into issues automatically.
```

- [ ] **Step 2: Create bug.md**

```markdown
---
name: Bug
about: Something is broken or behaving wrong
title: "[bug] "
labels: ["area:unknown", "priority:p2"]
---

## Where
Which page / pipeline step / Worker route? Paste URL or command.

## What you did
Steps to reproduce. Be specific.

## Expected
What you thought would happen.

## Actual
What happened. Include error message, screenshot, or run output if any.

## Environment
- Branch / commit:
- Where you ran it (host Mac / Devcontainer / fly / ECS / Worker):
```

- [ ] **Step 3: Create feature.md**

```markdown
---
name: Feature request
about: Propose new functionality or an enhancement
title: "[feat] "
labels: ["area:unknown", "priority:p2"]
---

## Use case
Who needs this and why. One paragraph from the user's perspective.

## Proposed behavior
What should happen. Mock-ups or example outputs welcome.

## Acceptance criteria
Bulleted list of conditions that must hold for this to be considered done.

- [ ] …
- [ ] …

## Out of scope
What this feature explicitly does NOT cover.

## Related
Spec / ADR / prior issue links.
```

- [ ] **Step 4: Create agent-task.md**

```markdown
---
name: Agent task
about: A focused task brief for Codex / Claude Code
title: "[agent] "
labels: ["owner:agent", "priority:p2"]
---

## Context
Why this task matters. Link to spec / plan / ADR if relevant.

## Files in scope
Exact paths the agent may modify. Anything not listed is off-limits.

- [ ] `<file>`
- [ ] `<file>`

## What to do
Step-by-step plan. Be explicit — no "use your judgment" placeholders.

1. …
2. …

## Acceptance commands
The agent must run these and quote the output before claiming done.

```bash
ruff check .
pytest webui/
# add any task-specific verification
```

## Hard constraints
List any CODEOWNERS-protected paths to avoid (default: see CONTRIBUTING.md).
```

- [ ] **Step 5: Verify the three templates appear in the GitHub UI**

After committing and pushing, open https://github.com/sunyireece-sys/chuhai-pipeline/issues/new/choose
Expected: three options visible (Bug / Feature request / Agent task) plus a contact link for sales.

- [ ] **Step 6: Commit**

```bash
git add .github/ISSUE_TEMPLATE/
git commit -m "Collab: add issue templates (bug / feature / agent-task)"
git push origin main
```

---

## Task 12: GitHub Projects board + labels

This task is **UI-driven** (GitHub Projects v2 has no first-class IaC story we want to take on for one board). Steps are clicks, not commits.

- [ ] **Step 1: Create the labels**

Open https://github.com/sunyireece-sys/chuhai-pipeline/labels and create:

| Name | Color | Description |
|---|---|---|
| `area:pipeline` | `#0E8A16` | pipeline.py and step modules |
| `area:webui` | `#1D76DB` | webui/ |
| `area:redvia` | `#5319E7` | redvia-site/ |
| `area:cloudflare` | `#FBCA04` | cloudflare/ Worker + D1 |
| `area:docs` | `#C5DEF5` | documentation |
| `area:unknown` | `#CCCCCC` | needs triage |
| `owner:user` | `#B60205` | @sunyireece-sys |
| `owner:intern` | `#D93F0B` | future intern |
| `owner:agent` | `#0052CC` | coding agent (Codex / Claude) |
| `owner:sales` | `#FBCA04` | sales-originated |
| `priority:p0` | `#B60205` | drop everything |
| `priority:p1` | `#D93F0B` | this week |
| `priority:p2` | `#FBCA04` | this month |

Delete GitHub's default labels (`bug`, `enhancement`, `help wanted`, etc.) — they overlap with the `area:` / `priority:` axes and add noise.

- [ ] **Step 2: Create the Projects v2 board**

Open https://github.com/users/sunyireece-sys/projects (or your org's projects page if applicable).

- Click "New project" → "Board" template.
- Name: `chuhai-pipeline workflow`.
- Visibility: Private (will revisit when the team grows).

- [ ] **Step 3: Configure board columns**

Replace the default columns with five:

`Backlog` → `Ready` → `In progress` → `In review` → `Done`

Drag in this exact order from left to right.

- [ ] **Step 4: Link the repo to the project**

Project sidebar → "Settings" → "Manage access" → add `chuhai-pipeline` repo. Then under "Workflows", enable:

- "Auto-add to project" for new issues in `chuhai-pipeline`.
- "Item closed" auto-moves to `Done`.

- [ ] **Step 5: Smoke-test by opening one of each issue type**

Open https://github.com/sunyireece-sys/chuhai-pipeline/issues/new/choose, file a throwaway "Bug" issue titled `[smoke test] please ignore`. Expected:

- Issue lands on the Projects board in the `Backlog` column.
- Labels `area:unknown` + `priority:p2` are auto-applied.
- The repo / labels match what we set up above.

Close the smoke-test issue. It should auto-move to `Done`.

- [ ] **Step 6: No git commit from this task**

The board is GitHub-side state. Make a one-line record in `工作日志/ai交接日志_2026-06-04.md`:

```
- GitHub Projects board configured 2026-06-04: 5 columns, 13 labels, auto-add + auto-close workflows on.
```

---

## Task 13: Secret rotation to company LLM gateway (Phase A)

Phase A from the spec: rotate `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, and (if available) `SERPER_API_KEY` from personal accounts to the company gateway. Three places must agree: local `.env`, fly secrets, Cloudflare Worker secrets.

- [ ] **Step 1: Get the gateway credentials from the maintainer's records**

The company LLM gateway endpoint + key live in the maintainer's existing notes (per the user's "公司已有中转站 API" statement). Confirm:
- `LLM_BASE_URL` (e.g. `https://gateway.company.example/v1`)
- `LLM_API_KEY`
- `LLM_MODEL` (the model name the gateway routes to, e.g. `glm-4-air` or the gateway's canonical alias)

Do NOT proceed if any of these are unclear. Ask first.

- [ ] **Step 2: Update local `.env`**

Edit `.env` (gitignored, do this on the host Mac):

```
LLM_API_KEY=<company-gateway-key>
LLM_BASE_URL=<company-gateway-url>
LLM_MODEL=<company-gateway-model-name>
```

If `SERPER_API_KEY` is also being migrated, replace it too.

- [ ] **Step 3: Verify locally**

Run:
```bash
source .venv/bin/activate
python -c "from llm_judge import judge; print(judge('return the word OK', schema={'type':'object'}))"
```
Expected: the function returns without an auth error. If `llm_judge.py`'s top-level entrypoint differs, substitute the actual smoke-test call (the goal is just one round-trip through the new gateway).

- [ ] **Step 4: Update fly secrets**

```bash
~/.fly/bin/flyctl secrets set \
  LLM_API_KEY=<company-gateway-key> \
  LLM_BASE_URL=<company-gateway-url> \
  LLM_MODEL=<company-gateway-model-name> \
  -a chuhai-webui
```

flyctl will trigger a rolling restart. Wait for it to complete:
```bash
~/.fly/bin/flyctl status -a chuhai-webui
```

- [ ] **Step 5: Verify webUI on fly**

Open the webUI's reply-synth or LLM-judge feature in the browser (whichever lives on a route that exercises the LLM). Confirm one round trip succeeds.

- [ ] **Step 6: Update Cloudflare Worker secret**

```bash
cd cloudflare
echo "<company-gateway-key>" | npx wrangler secret put LLM_API_KEY
```

If `LLM_BASE_URL` is also stored as a Worker secret (check `wrangler.toml` and `cloudflare/src/grounding.mjs`), update it the same way. If it's a `vars` entry instead, edit `wrangler.toml` and redeploy:

```bash
npx wrangler deploy
```

- [ ] **Step 7: Verify the Worker `/api/chat` endpoint**

```bash
curl -sS "https://redvia.com/api/chat" \
  -X POST -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"reply with the word OK"}]}'
```
Expected: a non-error JSON response containing "OK" or close to it. If the chat widget has its own preview server, hit it instead.

- [ ] **Step 8: Update the secrets registry**

Open `工作日志/账号与密钥登记.md` and change the relevant rows from `❌ 个人` to `✅ 公司 (gateway)` under the "密钥分布" sections (local `.env`, fly secrets, Worker secrets). Add a "Last rotated: 2026-06-04" footer to the file.

This is gitignored — local record only.

- [ ] **Step 9: No code commit from this task**

The rotation is operational, not source-tracked. Verification logs go to `工作日志/`.

---

## Final acceptance — all of波 1 done

After every task above is checked off, run this checklist:

- [ ] Open a draft PR with a trivial whitespace change. Confirm:
  - CI runs and goes green (Task 3 verification).
  - CODEOWNERS auto-requests `@sunyireece-sys` if the change touches a protected file (Task 4).
  - The PR appears on the Projects board (Task 12).
  - Issue templates are reachable (Task 11).
- [ ] Open the repo in your IDE → "Reopen in Container" → run `pytest webui/` from inside (Task 8).
- [ ] `ruff check .` clean both on host and inside the container (Tasks 2 + 8).
- [ ] All four ADRs render in GitHub's file viewer with no broken links (Task 10).
- [ ] CONTRIBUTING.md and AGENTS.md are linked from the README "Contributing" section (if README lacks the link, add it).
- [ ] Company LLM gateway key is the active key in `.env`, fly secrets, and Worker secret (Task 13).

Close the draft PR without merging.

When this checklist is fully ticked, the spec's wave-1 work items are landed and we can move to wave-2 (webui fly→ECS migration) via another `superpowers:writing-plans` run.
