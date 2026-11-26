# Security Policy

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/balakrishnagatla/llm-cost-guard/security/advisories/new)
instead. I'll acknowledge reports within 5 days.

## Supported versions

Only the latest release on `main` gets security fixes.

## Running LLM Cost Guard safely

- **Keep it private by default.** `docker-compose.yml` binds to `127.0.0.1` only.
- **Before exposing it on a network**, set `COST_GUARD_KEYS` (per-team proxy keys) and
  `DASHBOARD_PASSWORD`, and put it behind HTTPS (a reverse proxy such as Caddy or nginx).
- **Never commit `.env`** or API keys. `.gitignore` excludes `.env`, and GitHub secret
  scanning with push protection should be enabled on the repository.
- **Set budgets** (`BUDGETS`) so a leaked proxy key can't run up an unlimited bill.
- The response cache is scoped per upstream API key, but cached answers are stored in
  plain text in `data/costguard.db`. Protect that folder like you would logs.
