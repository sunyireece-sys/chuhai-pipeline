# Redvia Tracking Worker

This is the first-party tracking layer for the static Redvia site in `../web_claude`.

## What It Provides

- Static asset hosting for `web_claude/` through Cloudflare Workers assets.
- `GET /t/:token` and `GET /c/:token`: email tracking links, D1 attribution, first-party cookies, redirect to the site or a product page.
- `POST /api/identify`: supports landing URLs like `/?t=<token>`.
- `POST /api/event`: page views, product views, certificate opens, CTA clicks, mailto/tel clicks, contact form submits, and dwell events.
- `POST /api/match/*` and `POST /api/brief*`: optional proxy to the sales webUI match/brief API when `MATCH_API_BASE_URL` is configured.
- `GET /t.gif`: transparent pixel endpoint for lightweight tracking.
- `POST /admin/tokens`: create or update email tokens from the outreach pipeline.
- Cron sync every 5 minutes to `ECS_WEBHOOK_URL` when configured.

## First Deploy

```bash
cd cloudflare
npm install
npx wrangler login
npx wrangler d1 create redvia_tracking
```

Copy the generated `database_id` into `wrangler.toml`, then:

```bash
npx wrangler d1 migrations apply redvia_tracking --remote
npx wrangler secret put ADMIN_API_SECRET
npx wrangler secret put IP_HASH_SALT
npx wrangler secret put ECS_API_SECRET
npx wrangler secret put ECS_WEBHOOK_URL
npx wrangler deploy
```

`ECS_WEBHOOK_URL` must point to the webUI ingest endpoint, for example `https://<webui-host>/api/ingest_events`. If it is not set, the Worker still records D1 events but scheduled sync to webUI is skipped.

`MATCH_API_BASE_URL` is optional. Set it to the sales webUI origin while the static site and webUI are deployed separately; the Worker will enrich match/brief submissions with token attribution before proxying them. When the website and sales app move behind one Alibaba Cloud origin, the same `/api/match/*` and `/api/brief*` routes can be served directly by FastAPI instead.

`wrangler.toml` sets `COOKIE_DOMAIN = ".redvia.com"` so attribution survives across `redvia.com` and `www.redvia.com`. If the deployment uses a different root domain, update this value before deploy.

## Outreach Integration

`send_outreach.py` now supports tokenized links when these environment variables are set:

```bash
export TRACKING_WORKER_URL="https://redvia.com"
export TRACKING_ADMIN_SECRET="<same value as ADMIN_API_SECRET>"
export TRACKING_CAMPAIGN_ID="2026-05-redvia-launch"
```

Optional:

```bash
export TRACKING_PRODUCT_SLUG="goji-polysaccharide-powder"
export TRACKING_DEFAULT_DESTINATION_PATH="/ingredients/goji-polysaccharide-powder.html"
export TRACKING_CREATE_TOKENS_IN_DRY_RUN=1
```

When tracking is enabled, every sent email gets a unique `/t/<token>` URL. If the email body contains `{{tracking_link}}` or `{tracking_link}`, that placeholder is replaced. Otherwise the link is appended to the end of the email.

## Manual Token Test

```bash
TRACKING_WORKER_URL="https://redvia.com" \
TRACKING_ADMIN_SECRET="<secret>" \
npm run tokens:create -- --buyer_id=1001 --company="Acme Foods AG" --email="buyer@example.com" --campaign="test" --product="goji-seed-oil"
```

## Admin Inspection

```bash
curl -H "X-API-Key: <secret>" "https://redvia.com/admin/events?limit=20"
```

## Privacy Notes

- Cookies store only ids, not names or emails.
- `_rv_sess`, `_rv_bid`, `_rv_token`, and `_rv_campaign` are first-party HttpOnly cookies.
- Events store truncated IP prefix and salted IP hash, not raw IP.
- Contact form analytics store company, country, email domain, and brief length, but not the person's name, full email address, or brief text.
- The footer notice is injected by `site.js`: "We use first-party analytics to understand which products buyers care about."
