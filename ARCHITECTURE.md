# Architecture

Chuhai Pipeline is a B2B buyer-acquisition system that finds overseas wholesale buyers for a Ningxia goji berry producer (Bairuiyuan, marketed internationally as **Redvia**), verifies they are real importers or distributors, enriches their decision-maker profiles, and supports cold outreach through a sales-facing WebUI. The Redvia static site and a Cloudflare Worker close the loop with first-party click tracking and a grounded product chat.

This document is the system map. For "what is this and how do I run it", see [`README.md`](README.md).

## High-level topology

```
                    Solo Operator's Mac
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│   01_keywords.md ──► pipeline.py                            │
│                          │                                  │
│                          ├─ step 2  Serper.dev SERP search  │
│                          ├─ step 3  Xiaoman (Playwright)    │
│                          ├─ step 4  Website + LLM judge     │
│                          └─ step 5  Persona enrichment      │
│                          │                                  │
│   runs/<id>/02..05  ◄────┘                                  │
│                          │                                  │
│   send_outreach.py ◄─────┘                                  │
│        │                                                    │
│        │ SMTP outbound + /t/<token> tracking                │
│        ▼                                                    │
└─────────────────────────────────────────────────────────────┘
                                       ▲
                                       │ runs/ pushed manually
                                       ▼
┌─────────────────────────────────────────────────────────────┐
│ Fly.io · WebUI                                              │
│ FastAPI · SQLite · HTMX · Jinja                             │
│ Sales feedback · lead priority · reply synth · admin        │
└─────────────────────────────────────────────────────────────┘
                                       ▲
                                       │ 1-min event sync cron
                                       ▼
┌─────────────────────────────────────────────────────────────┐
│ Cloudflare · redvia.com                                     │
│ Worker + D1 + static assets                                 │
│ Redvia marketing site · /t/<token> redirect · /api/chat     │
└─────────────────────────────────────────────────────────────┘
```

## Components

### `pipeline.py` — orchestrator

Entry point. Reads `runs/<dir>/01_keywords.md` and walks the five steps in order, treating each step's xlsx as a checkpoint. Delete an intermediate xlsx to re-run that step. Flags exist to skip steps (`--skip-step3`, `--skip-contacts`, `--skip-step4`, `--max-queries N`) for incremental work.

### Step 2 · Serper SERP search

- `serper_search.py` queries Serper.dev with country-code-aware language gates (e.g., Russia queries run in `hl=ru`, Turkey in `hl=tr`, everywhere else in English).
- `buyer_extract.py` turns SERP results into `CompanyCandidate` rows. It deliberately does not trust the first slice of the SERP title — domain heuristics and a brand-vs-product score pick the right segment.
- Output: `02_buyers.xlsx` with a stable column contract (`Company Name`, `Country`, `Lead Type`, ...) consumed by downstream steps.

### Step 3 · Xiaoman enrichment (the hard step)

- `xiaoman_playwright.py` drives a real Chromium via Playwright's `launch_persistent_context()` against the Xiaoman B2B database.
- The Xiaoman front-end computes request signatures itself, so the Python driver simply fills the search box, presses Enter, and intercepts the `searchListV2` JSON response. For top-1 matches with contact counts > 0, it intercepts `profileEmails` to pull decision-maker emails.
- First run opens a Chromium window for QR-code login; subsequent runs reuse the persisted profile in `~/.xiaoman_playwright_profile/`.
- Hard rate limit: ~60–70 companies per browser session before CAPTCHAs appear. Batches are intended to run with explicit pauses between sessions.
- Output: `03_xiaoman.xlsx`.

### Step 4 · Website verification + LLM judge

- `website_verify.py` fetches each candidate's homepage, About, and Products pages with light parsing.
- `llm_judge.py` is the only place LLM API calls live. The provider is interchangeable: any OpenAI-compatible base URL works (OpenAI, GLM, DeepSeek, ...). The judge scores buyer fit, evidence of being an importer/distributor, country alignment, and competitor signals — purely from site evidence, not from search context.
- Output: `04_verified.xlsx`.

### Step 5 · Profile enrichment

- `profile_enrich.py` synthesizes a per-company decision-maker persona from the verified site evidence plus a fixed supplier profile (`supplier_profile.json`).
- Output: `runs/<dir>/05_profiles/<slug>.json`, one file per buyer.

### `send_outreach.py` — outbound

- Reads `05_profiles/`, composes personalized emails, sends via SMTP (Outlook by default).
- Mints a unique `/t/<token>` tracking link per recipient via the Cloudflare Worker. The token carries `run_id` and `profile_slug` metadata so click events can be joined back to the originating lead.
- Dry-run is the default; live send requires both `--live` and `--i-confirm-live-send`.

### `webui/` — sales feedback FastAPI app

- Single FastAPI app (`webui/app.py`) serving dashboards, inbox, admin, and lead-priority views. HTMX + Jinja templates, SQLite persistence (`feedback.db`).
- `webui/lead_priority.py` combines objective sales status (`已发`, `已询价`, `已成交`, ...) with sales-side rating into a priority score for surfacing leads.
- `webui/imap_poller.py` polls the configured mailbox; replies are matched back to the sent `Message-ID` and classified by an LLM into `valid` / `invalid`.
- Once three or more new `valid` replies accumulate, the app auto-triggers `webui/synthesizer.py`, which produces a per-product synthesis of buyer interest signals.
- Currently deployed on Fly.io (Singapore). Planned to move to Aliyun ECS alongside other infra.

**Tech debt:** `webui/app.py` has grown to ~4000 lines and needs to be split into routers + services + db modules. This is the next refactor.

### `redvia-site/` — Redvia static marketing site

- Vanilla HTML/CSS/JS, no build step.
- Design tokens in `assets/css/tokens.css`; base styles in `base.css`; component styles in `components.css`.
- Product imagery in `assets/img/`. Staging directories (`incoming/`, `icons/raw/`) are kept out of git.
- A small product reference dataset lives at `redvia-site/data/`. Both the Cloudflare Worker chat endpoint and the WebUI's application-matching feature read from these JSON files.

### `cloudflare/` — Worker, D1, assets

- Serves `redvia.com` from `redvia-site/` as static assets.
- Issues and resolves `/t/<token>` tracking links; events are written to D1.
- Hosts `/api/chat`: a thin grounded GLM proxy. Grounding context is loaded from `redvia-site/data/sku_application_matrix.json` and `reference_applications.json` so that responses stay scoped to the actual product line.
- A 1-minute cron sweeps unsent tracking events from D1 to the WebUI's ingest endpoint.

## Data flow for one lead

1. Operator picks keywords for a country/anchor combination and runs `pipeline.py`.
2. SERP search returns candidate companies; `buyer_extract.py` cleans them.
3. Xiaoman lookup adds contacts and trade signals for top candidates.
4. Website verification + LLM judge filters out non-buyers and rates fit.
5. Profile enrichment writes one JSON per remaining buyer.
6. WebUI surfaces the profiles to the sales team for review, rating, and outbound.
7. `send_outreach.py` sends emails with a `/t/<token>` link per recipient.
8. The Cloudflare Worker logs each click; cron sync forwards events to the WebUI.
9. IMAP poller picks up replies; valid replies feed reply synthesis, which informs the next round of keyword and content tuning.

## Deployment topology (today)

| Component        | Where it runs                | Notes                                                       |
|------------------|------------------------------|-------------------------------------------------------------|
| Pipeline         | Operator's Mac               | Xiaoman requires a headful Chromium with persistent login.  |
| WebUI            | Fly.io (Singapore region)    | SQLite on a Fly volume.                                     |
| Redvia site      | Cloudflare Worker assets     | `redvia.com`.                                               |
| Tracking + chat  | Cloudflare Worker + D1       | 1-min cron sync of tracking events to the WebUI.            |

## Known limitations & planned work

- **Pipeline is bottlenecked on one machine.** Step 3 needs an authenticated Chromium session; until Xiaoman is replaced with an API-based trade database (Apollo / Clay are the candidates), the orchestrator stays local.
- **WebUI monolith.** `app.py` will be split into routers + services + db layer.
- **No multi-user auth yet.** A shared `.users.json` covers a small team; SSO is a future item.
- **`runs/` is not shared.** Today the operator's `runs/` syncs to Fly via a manual rsync; multi-machine pipeline runs would need a shared object store.
- **Test coverage is light.** `webui/test_lead_priority.py` covers the scoring rules; the rest relies on real-data smoke runs in `runs/test_*`.
- **Multi-person collaboration design.** A separate spec is in progress for PR workflow, devcontainer, secret migration to corporate accounts, and a sales-feedback → GitHub-issue bridge.
