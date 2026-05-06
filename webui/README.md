# WebUI Deployment

## Local Run

Set the required environment variables before starting the app:

```bash
export WEBUI_PASSWORD="change-me"
export SMTP_HOST="smtp.example.com"
export SMTP_PORT="465"
export SMTP_USER="trade@example.com"
export SMTP_PASS="your-smtp-password"
export SMTP_TEST_RECIPIENT="tester@example.com"
export SERPER_API_KEY="optional-for-pipeline"
webui/.venv/bin/python -m uvicorn webui.app:app --host 127.0.0.1 --port 8000
```

The local SQLite database defaults to `webui/feedback.db`. Set `FEEDBACK_DB_PATH`
to override it.

## Fly Deploy

The app is configured by `fly.toml` and builds from the project root:

```bash
fly launch --copy-config --no-deploy --region hkg
fly volumes create feedback_data --region hkg --size 1
fly secrets set WEBUI_PASSWORD="change-me"
fly secrets set SMTP_HOST="smtp.example.com" SMTP_PORT="465" SMTP_USER="trade@example.com"
fly secrets set SMTP_PASS="your-smtp-password" SMTP_TEST_RECIPIENT="tester@example.com"
fly secrets set SERPER_API_KEY="optional-for-pipeline"
fly deploy
```

Do not commit real passwords or SMTP credentials. Store runtime values only in
Fly secrets.

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

To change the shared password:

```bash
fly secrets set WEBUI_PASSWORD="new-password"
fly deploy
```

The default URL is `https://chuhai-webui.fly.dev`. Access from mainland China
may occasionally be slow or unstable.
