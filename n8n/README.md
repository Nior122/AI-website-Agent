# n8n workflow examples

These are importable examples for self-hosted n8n. They make authenticated calls to the optional local application API; they do not contain credentials, run code-generation, store business state, or send messages.

Set `AI_WEBSITE_AGENT_API_URL` and `AI_WEBSITE_AGENT_API_TOKEN` in n8n's own environment/credential store. Start the application API separately and privately only when approved:

```powershell
$env:AI_WEBSITE_AGENT_API_TOKEN = "local-secret-not-committed"
python -m orchestration.cli serve
```

The API binds to `127.0.0.1:8787` by default and fails to start if the token is absent. For Docker-based n8n, use an intentionally configured private network hostname instead of exposing the API publicly. Never put a real secret in an exported workflow.

- `01-create-job.json` creates an idempotent, dry-run `SCOUT` job from a manual trigger.
- `02-inspect-job.json` reads one job record to let an operator inspect status, attempts, error, and next work.

Workers remain separate processes/agents. Before calling `POST /jobs/:id/run` in a future integration, register an approved worker that calls the existing application services and honors `dry_run`. The current API deliberately returns `409` for that endpoint rather than silently running code.
