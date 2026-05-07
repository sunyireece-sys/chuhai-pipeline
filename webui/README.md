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
