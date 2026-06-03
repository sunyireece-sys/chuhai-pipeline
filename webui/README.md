# WebUI Deployment

## Local Run

Set the required environment variables before starting the app:

```bash
export WEBUI_INITIAL_PASSWORD="change-me"
export SMTP_HOST="smtp.example.com"
export SMTP_PORT="465"
export SMTP_USER="trade@example.com"
export SMTP_PASS="your-smtp-password"
export SMTP_TEST_RECIPIENT="tester@example.com"
export WEB_TRACKING_INGEST_SECRET="same-secret-as-worker-ecs-api-secret"
export SERPER_API_KEY="optional-for-pipeline"
webui/.venv/bin/python -m uvicorn webui.app:app --host 127.0.0.1 --port 8000
```

The local SQLite database defaults to `webui/feedback.db`. Set `FEEDBACK_DB_PATH`
to override it. On first startup, `WEBUI_INITIAL_PASSWORD` seeds the initial
users `nicky`, `clement`, `jeff`, and `admin`; only password hashes are stored
in SQLite.

## Fly Deploy

The app is configured by `fly.toml` and builds from the project root:

```bash
fly launch --copy-config --no-deploy --region sin
fly volumes create feedback_data --region sin --size 1
fly secrets set WEBUI_INITIAL_PASSWORD="change-me"
fly secrets set SMTP_HOST="smtp.example.com" SMTP_PORT="465" SMTP_USER="trade@example.com"
fly secrets set SMTP_PASS="your-smtp-password" SMTP_TEST_RECIPIENT="tester@example.com"
fly secrets set WEB_TRACKING_INGEST_SECRET="same-secret-as-worker-ecs-api-secret"
fly secrets set SERPER_API_KEY="optional-for-pipeline"
fly deploy
```

Do not commit real passwords or SMTP credentials. Store runtime values only in
Fly secrets.

For compatibility, `WEBUI_PASSWORD` is also accepted as the first-start seed
password when `WEBUI_INITIAL_PASSWORD` is not set. After users exist, login is
validated against the `users` table instead of a shared password secret.

## Operations

View status and logs:

```bash
fly status
fly logs
```

Open a shell:

```bash
fly ssh console
```

Inspect send tracking:

```bash
sqlite3 /data/feedback.db "SELECT count(*) FROM send_tracking;"
```

Inspect synced website tracking:

```bash
sqlite3 /data/feedback.db "SELECT event_type, count(*) FROM web_events WHERE is_bot=0 GROUP BY event_type;"
```

Back up SQLite with Fly volume snapshots or by opening an SFTP shell:

```bash
fly ssh sftp shell
```

To change a user's password, sign in as `admin` and open `/admin`. For a fresh
deployment, rotate the initial seed secret before the first startup if needed:

```bash
fly secrets set WEBUI_INITIAL_PASSWORD="new-seed-password"
fly deploy
```

The default URL is `https://chuhai-webui.fly.dev`. Access from mainland China
may occasionally be slow or unstable.

## Test Market Runs

Runs named with a `test_` prefix (e.g. `test_2026-05-08_goji_tr_ru`) are
isolated from the main dashboard:

- **Main page** (`/`): only shows non-test runs, excluded from dashboard stats
- **Test page** (`/test`): shows only test-prefixed runs; send emails from here
  to validate a new market end-to-end before going live
- **Delete test data**: `POST /test/delete` (admin only) clears all test sends

To add a new test market, create a `runs/test_<date>_<product>_<countries>/`
directory with a `01_keywords.md`, then run the pipeline normally. The `test_`
prefix is the only required convention.

## Pipeline Integration

After `pipeline.py` completes step 5, it automatically calls `deploy.sh`
(which runs `fly deploy`). Pass `--no-deploy` to skip this:

```bash
python pipeline.py runs/<run>/01_keywords.md --no-deploy
```

Xiaoman (step 3) anti-scraping limit: each browser session handles ~60
companies before triggering a CAPTCHA. The `--xiaoman-session-limit` flag
(default 60) controls batch size; `--xiaoman-session-pause` (default 300s)
sets the wait between sessions.
