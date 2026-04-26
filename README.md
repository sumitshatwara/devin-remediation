# Devin Remediation Demo

> **An event-driven automation that uses the [Devin API](https://docs.devin.ai/api-reference/overview) to automatically remediate security issues — demonstrated on a fork of [Apache Superset](https://github.com/sumitshatwara/superset).**

[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/docker-compose-2496ED.svg)](https://www.docker.com/)
[![Devin API v3](https://img.shields.io/badge/Devin_API-v3-6366f1.svg)](https://docs.devin.ai)

---

## Table of Contents

- [What This System Does](#-what-this-system-does)
- [Architecture](#-architecture)
- [Project Structure](#-project-structure)
- [Prerequisites](#-prerequisites)
- [Quickstart (Docker)](#-quickstart-docker)
- [Environment Variables](#-environment-variables)
- [Configure the GitHub Webhook](#-configure-the-github-webhook)
- [Real Vulnerabilities Found in the Fork](#-real-vulnerabilities-found-in-the-fork)
- [Observability Dashboard](#-observability-dashboard)
- [API Reference](#-api-reference)
- [Testing Without a Live Webhook](#-testing-without-a-live-webhook)
- [How Devin Remediates an Issue](#-how-devin-remediates-an-issue)
- [Status Lifecycle](#-status-lifecycle)
- [Why Devin?](#-why-devin)
- [Future Improvements — SCA/SAST Pipeline](#-future-improvements--scasast-pipeline)
- [Troubleshooting](#-troubleshooting)

---

## 🎯 What This System Does

Engineering teams accumulate security issues faster than they can fix them. This system closes that gap: a GitHub Issue labeled `auto-remediate` triggers a fully autonomous Devin session that reads the code, writes a targeted fix with tests, and opens a PR — no human engineer needed between issue creation and pull request.

**End-to-end flow in under 10 minutes:**

1. A GitHub Issue is labeled `auto-remediate` in [sumitshatwara/superset](https://github.com/sumitshatwara/superset)
2. GitHub fires a webhook to this system's FastAPI backend
3. The backend creates a Devin API v3 session with a scoped, structured prompt
4. Devin reads the issue, locates the vulnerable line, writes a minimal fix + test, and opens a PR
5. The backend polls Devin every 30 seconds, updating the database as status changes
6. The **Devin Remediation Dashboard** shows real-time session status, PR links, and KPIs

---

## 🏗️ Architecture

```
GitHub Issue (labeled "auto-remediate")
         │
         ▼  [POST /webhook/github]
┌──────────────────────────────────────┐
│         FastAPI Backend              │
│         (Dockerized, port 8000)      │
│                                      │
│  • Validates webhook payload         │
│  • Builds scoped Devin prompt        │
│  • Calls Devin API v3 /sessions      │
│  • Spawns background polling task    │
│  • Serves live dashboard + REST API  │
└──────────────┬───────────────────────┘
               │  [Devin API v3 — Create Session]
               ▼
      ┌──────────────────┐
      │   Devin Agent    │
      │                  │
      │  1. Reads issue  │
      │  2. Clones repo  │
      │  3. Finds root   │
      │     cause        │
      │  4. Writes fix   │
      │  5. Adds test    │
      │  6. Opens PR     │
      └────────┬─────────┘
               │  [Background poll every 30s]
               ▼
      ┌──────────────────┐
      │   SQLite DB      │  /app/data/sessions.db
      │                  │
      │  session_id      │
      │  issue_number    │
      │  status          │
      │  pr_url          │
      │  timestamps      │
      └────────┬─────────┘
               │
               ▼
      ┌──────────────────────────┐
      │  Devin Remediation       │  GET /  or  GET /dashboard
      │  Dashboard (Chart.js)    │
      │                          │
      │  • KPI cards             │
      │  • Status distribution   │
      │  • Throughput over time  │
      │  • Live sessions table   │
      └──────────────────────────┘
```

---

## 📁 Project Structure

```
devin-remediation/
├── app/
│   └── main.py              # All application logic — webhook, Devin API, polling,
│                            # /api/* endpoints, Devin Remediation Dashboard HTML
├── data/                    # SQLite DB persisted here (auto-created at runtime)
│   └── sessions.db
├── Dockerfile               # Python 3.11-slim, installs requirements
├── docker-compose.yml       # Mounts .env, maps port 8000, persists ./data volume
├── requirements.txt         # fastapi uvicorn requests
├── .env.example             # Template — copy to .env and fill in your secrets
└── README.md
```

> **Note:** `main.py` is intentionally a single file to simplify demo deployment. In production, split into `routes/`, `services/`, `models/`, `db/`.

---

## ✅ Prerequisites

| Requirement | Notes |
|---|---|
| Docker 24+ | With Compose plugin (`docker compose`) |
| Devin Service User Key | `cog_...` from [app.devin.ai → Settings → Service users](https://app.devin.ai) |
| Devin Org ID | Found in your Devin organization URL |
| GitHub PAT | Scopes: `repo` (to read issues and receive webhooks) |
| ngrok or public IP | Required for GitHub webhook to reach your local machine |

**Getting a Devin Service User Key:**
1. Log in to [app.devin.ai](https://app.devin.ai)
2. Go to **Settings → Service Users → Provision Service User** — the key starts with `cog_`
3. Your `DEVIN_ORG_ID` is the UUID in your org URL: `https://app.devin.ai/organizations/{DEVIN_ORG_ID}/...`

---

## 🚀 Quickstart (Docker)

```bash
# 1. Clone this repo
git clone https://github.com/YOUR_USERNAME/devin-remediation-system
cd devin-remediation-system

# 2. Set up environment variables
cp .env.example .env
# Edit .env with your keys

# 3. Build and start
docker compose up --build -d

# 4. Verify it's running
curl http://localhost:8000/health

# 5. Open the dashboard
open http://localhost:8000
```

Expected health response:
```json
{"status": "ok", "timestamp": "2026-04-25T12:00:00", "total_sessions": 0}
```

**Expose locally with ngrok:**
```bash
ngrok http 8000
# Copy the Forwarding URL — use it as the GitHub webhook Payload URL
```

---

## 🔐 Environment Variables

Copy `.env.example` to `.env` and fill in all values:

| Variable | Required | Description | Example |
|---|---|---|---|
| `DEVIN_API_KEY` | ✅ | Your Devin Service User key | `cog_abc123...` |
| `DEVIN_ORG_ID` | ✅ | Your Devin organization UUID | `org_abc123...` |
| `GITHUB_TOKEN` | ✅ | GitHub PAT with `repo` scope | `ghp_abc123...` |
| `REPO_OWNER` | ✅ | GitHub username of your fork | `sumitshatwara` |
| `REPO_NAME` | ✅ | Repository name | `superset` |

> **Security:** Never commit `.env` to Git. It is listed in `.gitignore`.

---

## 🔧 Configure the GitHub Webhook

In [sumitshatwara/superset](https://github.com/sumitshatwara/superset) → **Settings → Webhooks → Add webhook**:

| Field | Value |
|---|---|
| Payload URL | `https://YOUR_NGROK_URL/webhook/github` |
| Content type | `application/json` |
| Which events? | **Issues only** |
| Active | ✅ |

The webhook fires when `action` is `opened` or `labeled` **and** the issue has the `auto-remediate` label. All other events return `{"status": "ignored"}`.

---

## 🔍 Real Vulnerabilities Found in the Fork

> **All four findings below were identified by direct code review of [`sumitshatwara/superset`](https://github.com/sumitshatwara/superset) — specifically `superset/config.py`. Every line number is validated against the live file. They are intentionally simple: each is a 1–4 line fix, easy to explain to a non-technical audience, and demonstrates Devin's ability to reason about security context — not just search-and-replace.**

---

### Issue #1 — Hardcoded Guest Token JWT Secret (CWE-321)

**File:** `superset/config.py` · **Line:** 2346

**Vulnerable code in the repo:**
```python
GUEST_TOKEN_JWT_SECRET = "test-guest-secret-change-me"  # noqa: S105
```

**What it means:**
Superset's embedded dashboard feature issues short-lived JWT tokens (Guest Tokens) that grant anonymous access to specific dashboards. These tokens are signed using `GUEST_TOKEN_JWT_SECRET`. With the default value publicly visible in source code, any attacker can generate a cryptographically valid forged guest token, bypass row-level security (RLS) rules, and view data that belongs to other users or tenants.

**CWE-321:** Use of Hard-coded Cryptographic Key — the secret is not secret when it is in a public repository.

**Easy to explain:** *"The lock on our embedded dashboards ships with a default key that's written on the box. Anyone can copy it."*

**Proposed fix:**
```python
GUEST_TOKEN_JWT_SECRET = os.environ.get("SUPERSET_GUEST_TOKEN_JWT_SECRET", "")
if not GUEST_TOKEN_JWT_SECRET:
    raise RuntimeError(
        "SUPERSET_GUEST_TOKEN_JWT_SECRET must be set to a strong random value. "
        "Generate one with: openssl rand -base64 42"
    )
```

**Label to apply:** `auto-remediate`

---

### Issue #2 — Session Cookies Sent Over HTTP (CWE-614)

**File:** `superset/config.py` · **Line:** 2245

**Vulnerable code in the repo:**
```python
SESSION_COOKIE_SECURE = False  # Prevent cookie from being transmitted over non-tls?
```

**What it means:**
When `SESSION_COOKIE_SECURE = False`, the browser will include the Superset session cookie in both HTTP and HTTPS requests. On any unencrypted network — a coffee shop, a corporate proxy without TLS inspection, or a misconfigured internal network — an attacker can passively capture the cookie and immediately impersonate the authenticated user without knowing their password. This is a textbook session hijacking attack.

**CWE-614:** Sensitive Cookie in HTTPS Session Without 'Secure' Attribute — the browser's built-in protection is explicitly disabled.

**Easy to explain:** *"Our login session ID travels in plain text on HTTP. Anyone on the same Wi-Fi can steal it and log in as that user."*

**Proposed fix:**
```python
SESSION_COOKIE_SECURE = os.environ.get("SUPERSET_ENV") == "production"
```

**Label to apply:** `auto-remediate`

---

### Issue #3 — Hardcoded SMTP Password in Plain Text (CWE-256)

**File:** `superset/config.py` · **Lines:** 1621–1629

**Vulnerable code in the repo:**
```python
SMTP_HOST = "localhost"
SMTP_STARTTLS = True
SMTP_SSL = False
SMTP_USER = "superset"
SMTP_PORT = 25
SMTP_PASSWORD = "superset"  # noqa: S105
```

**What it means:**
The SMTP credentials used to send alert and report emails are hardcoded with the default password `"superset"` in plain text. If this configuration is shipped to production without change (a common mistake with default configs), any attacker with access to the config file — or the repository — knows the mail server password. This can be used to send phishing emails on behalf of the organization through the application's mail server.

**CWE-256:** Unprotected Storage of Credentials — credentials stored in source code are trivially discoverable.

**Easy to explain:** *"The email server password is 'superset'. It is written right here in the code, in plain text, for anyone who can read the repository."*

**Proposed fix:**
```python
SMTP_HOST     = os.environ.get("SUPERSET_SMTP_HOST", "localhost")
SMTP_USER     = os.environ.get("SUPERSET_SMTP_USER", "superset")
SMTP_PASSWORD = os.environ.get("SUPERSET_SMTP_PASSWORD", "")
SMTP_PORT     = int(os.environ.get("SUPERSET_SMTP_PORT", "25"))
```

**Label to apply:** `auto-remediate`

---

### Issue #4 — Rate Limiting Disabled Outside Production (CWE-307)

**File:** `superset/config.py` · **Line:** 342

**Vulnerable code in the repo:**
```python
RATELIMIT_ENABLED = os.environ.get("SUPERSET_ENV") == "production"
```

**What it means:**
Rate limiting is only enabled when `SUPERSET_ENV` is explicitly set to `"production"`. In all other environments — development, staging, QA, or any deployment that forgets to set this variable — rate limiting is silently disabled. This means the login endpoint, the API, and all authenticated routes accept unlimited requests per second. An attacker can run automated credential-stuffing or brute-force attacks with no throttling, and data-intensive API scraping faces no limits.

**CWE-307:** Improper Restriction of Excessive Authentication Attempts — the control that prevents brute-force is opt-in rather than opt-out.

**Easy to explain:** *"Our rate limiting is OFF by default. A script can hammer the login page thousands of times per second with no resistance unless someone explicitly flips a switch."*

**Proposed fix:**
```python
# Default to enabled; allow explicit opt-out for local dev only
RATELIMIT_ENABLED = os.environ.get("SUPERSET_ENV") != "development"
```

**Label to apply:** `auto-remediate`

---

### Summary

| # | Issue Title | File | Line | CWE | Fix |
|---|---|---|---|---|---|
| 1 | Hardcoded Guest Token JWT secret | `config.py` | 2346 | CWE-321 | Read from env var + startup guard |
| 2 | Session cookies sent over HTTP | `config.py` | 2245 | CWE-614 | Tie `SECURE` flag to production env |
| 3 | Hardcoded SMTP password in plain text | `config.py` | 1629 | CWE-256 | Read all SMTP credentials from env |
| 4 | Rate limiting disabled outside production | `config.py` | 342 | CWE-307 | Default to enabled, opt-out for dev |

---

## 📊 Observability Dashboard

Visit **http://localhost:8000** to open the **Devin Remediation Dashboard**.

### KPI Cards

| Metric | Description |
|---|---|
| **Total Sessions** | All Devin sessions triggered, all time |
| **Completed** | Sessions where Devin exited successfully and raised a PR |
| **Running** | Sessions currently active |
| **Suspended** | Sessions paused — Devin may need additional context |
| **Success Rate** | `completed / total × 100%` |
| **PRs Opened** | Sessions where a GitHub PR URL was detected in output |

### Charts
- **Status Distribution** — Doughnut chart: completed / running / suspended / failed / pending
- **Sessions Over Time** — Bar chart of session volume per day over the last 7 days

### Sessions Table
Each row shows: issue number (linked to GitHub), issue title, animated status badge, Devin session ID, start/end timestamps, PR link when available, link to the live Devin session, and a **↻ Sync button** to force-poll status instantly.

The dashboard auto-refreshes every **15 seconds**. Toggle between dark (default) and light mode with the sun/moon icon in the topbar.

---

## 🔁 API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/` or `/dashboard` | Devin Remediation Dashboard (HTML) |
| `GET` | `/health` | Health check with session count |
| `GET` | `/api/sessions` | All sessions (latest 100, UI-mapped statuses) |
| `GET` | `/api/stats` | Aggregate KPI counts |
| `POST` | `/webhook/github` | GitHub Issues webhook receiver |
| `POST` | `/trigger` | Manually start a Devin session from dashboard |
| `POST` | `/sessions/{id}/sync` | Force-sync one session from Devin API |

**`GET /api/stats` response:**
```json
{
  "total": 4, "completed": 3, "running": 1,
  "blocked": 0, "timeout": 0, "with_pr": 3, "success_rate": 75.0
}
```

**`GET /api/sessions` single item:**
```json
{
  "id": 1,
  "session_id": "sess_abc123",
  "issue_number": 1,
  "issue_title": "Hardcoded Guest Token JWT secret (CWE-321)",
  "issue_url": "https://github.com/sumitshatwara/superset/issues/1",
  "status": "completed",
  "pr_url": "https://github.com/sumitshatwara/superset/pull/5",
  "started_at": "2026-04-25T07:00:00",
  "completed_at": "2026-04-25T07:08:32",
  "devin_url": "https://app.devin.ai/sessions/sess_abc123"
}
```

---

## 🧪 Testing Without a Live Webhook

```bash
curl -X POST http://localhost:8000/webhook/github \
  -H "Content-Type: application/json" \
  -d '{
    "action": "labeled",
    "issue": {
      "number": 1,
      "title": "Hardcoded Guest Token JWT secret bypasses embedded dashboard auth (CWE-321)",
      "body": "superset/config.py line 2346: GUEST_TOKEN_JWT_SECRET is set to the literal string \"test-guest-secret-change-me\". Replace with os.environ.get(\"SUPERSET_GUEST_TOKEN_JWT_SECRET\") and add a startup guard that raises RuntimeError if the value is empty or equals the default.",
      "html_url": "https://github.com/sumitshatwara/superset/issues/1",
      "labels": [{"name": "auto-remediate"}]
    }
  }'
```

Then open http://localhost:8000 and watch the session appear in the dashboard.

Alternatively, click **Trigger Devin** in the dashboard topbar to start a session from the UI modal.

---

## 🤖 How Devin Remediates an Issue

When a session is created, the backend sends Devin a structured prompt:

```
You are working on a fork of Apache Superset:
https://github.com/sumitshatwara/superset

GitHub Issue #{number}: {title}

Issue Description:
{body}

Your task:
1. Analyse and implement a targeted fix for the problem described above.
2. Write or update relevant tests to cover the fix.
3. Create a Pull Request with:
   - Title: "fix: {title} (Issue #{number})"
   - Body: root cause, fix applied, tests added
   - Reference to: {issue_url}

Keep the fix minimal and ensure existing tests continue to pass.
```

Devin then: clones the repo → reads `superset/config.py` at the exact line → writes a minimal fix → adds a unit test verifying the guard → opens a PR referencing the issue.

---

## 🔄 Status Lifecycle

| Devin API v3 Status | Dashboard Badge | Meaning |
|---|---|---|
| `new` | 🔵 running | Session queued |
| `claimed` | 🔵 running | Devin picked up the session |
| `running` | 🔵 running | Devin actively working |
| `resuming` | 🔵 running | Session resuming after pause |
| `exit` | ✅ completed | Devin finished — PR is open |
| `suspended` | 🟡 suspended | Paused — may need human context |
| `error` | 🔴 timeout | Session failed or errored out |

The background poller checks every **30 seconds** for up to **60 minutes** (120 polls). If no terminal status is reached, the session is marked `error` with `Polling timed out`.

---

## 💡 Why Devin?

| Approach | Time to fix | Needs engineer? | Fix quality | Scales? |
|---|---|---|---|---|
| Manual (senior engineer) | Hours–days | Yes | High | ❌ Linear headcount |
| Scripted linter / bot | Seconds | No | Shallow | ✅ Surface-level only |
| **Devin (this system)** | **5–15 min** | **No** | **Human-quality** | **✅ Fully** |

Traditional scanners can **find** vulnerabilities. They cannot **fix** them — because fixing requires reading intent, understanding the codebase, and making a judgment call about the right remedy.

Devin is uniquely qualified because it:
- Reads the issue in natural language and maps it to the exact line of code
- Understands the repository structure, imports, environment conventions, and test patterns
- Writes a targeted fix — not a regex substitution, but a reasoned code change with context
- Adds a test that verifies the security guard actually works
- Opens a PR that is immediately reviewable by a human engineer

This system turns Devin into a **software engineering primitive** — a callable unit in an event-driven pipeline, as reliable and composable as any other microservice.

---

## 🔮 Future Improvements — SCA/SAST Pipeline

The current system is triggered via a GitHub label or the dashboard modal. The natural next step is **fully automated discovery-to-remediation**: the scanner finds the issue, an AI agent writes the GitHub issue, and Devin fixes it — with zero human steps before the PR review.

### Proposed: Wiz + Green Agent → Devin Auto-Remediation

```
┌─────────────────────────────────────────────────┐
│  Wiz Security Platform                          │
│                                                 │
│  SCA — Software Composition Analysis:           │
│  • Scans package manifests (pyproject.toml,     │
│    requirements.txt) against CVE/NVD/OSV        │
│  • Identifies outdated deps with known exploits │
│  • Provides fix version + EPSS exploit score    │
│                                                 │
│  SAST — Static Application Security Testing:   │
│  • Scans Python source for hardcoded secrets,   │
│    insecure defaults, injection patterns,       │
│    broken auth, and misconfigurations           │
│  • Maps findings to CWE categories             │
└────────────────┬────────────────────────────────┘
                 │  Webhook on new critical/high finding
                 ▼
┌────────────────────────────────────────────────┐
│  Issues Agent (AI Coding Assistant)             │
│                                                │
│  • Parses raw Wiz finding (CVE ID, file, line) │
│  • Enriches it: maps CVE → root cause → fix    │
│  • Generates a well-structured GitHub Issue:   │
│    - Plain-language title (no CVE jargon)      │
│    - Exact file + line number reference        │
│    - Proposed fix code snippet in the body     │
│    - Severity and CWE classification           │
│  • Applies label: "auto-remediate"             │
└────────────────┬───────────────────────────────┘
                 │  GitHub Issue created automatically
                 ▼
┌────────────────────────────────────────────────┐
│  This System (Devin Orchestrator)              │
│                                                │
│  • Webhook fires on "auto-remediate" label     │
│  • Creates Devin session with rich prompt      │
│  • Polls for completion, stores PR URL         │
│  • Dashboard shows live progress + KPIs        │
└────────────────────────────────────────────────┘
```

**End result: Zero-touch remediation.** A new CVE is published → Wiz detects it in the next scan → Green writes the issue in 10 seconds → Devin opens a PR in 10 minutes. The only human step is the final PR review.

### Why Wiz?

Wiz provides unified **cloud-native SCA and SAST** that understands both the infrastructure context (where the app runs, what IAM permissions it has) and the code context (which packages are in scope, what secrets exist in config). Wiz findings are prioritized by actual exploitability — not just CVSS score — which makes the auto-remediation queue actionable rather than overwhelming.

### Why Issues Agent?

Issues Agent bridges the gap between a raw scanner finding (a JSON payload with a CVE ID and a file path) and a well-written GitHub Issue that Devin can act on effectively. It translates scanner jargon into plain-language descriptions and seeds Devin with the right approach before the session even starts — reducing the chance of Devin spending time on root-cause analysis that the scanner already did.

### Additional Roadmap Items

- [ ] **CVSS / EPSS score filtering** — only auto-remediate findings above a severity threshold (e.g., EPSS ≥ 5%, CVSS ≥ 7.0)
- [ ] **Slack / Teams notifications** — post PR link and fix summary to a security channel on session completion
- [ ] **GitHub Actions integration** — trigger on `push` to catch new vulnerable dependency introductions immediately in CI
- [ ] **Multi-repo support** — route findings from multiple repositories through the same orchestrator instance
- [ ] **Retry on suspension** — re-trigger a suspended Devin session after a human adds clarifying context
- [ ] **Webhook signature validation** — verify `X-Hub-Signature-256` HMAC header for production-grade security
- [ ] **PostgreSQL backend** — replace SQLite for multi-instance, horizontally-scaled deployments
- [ ] **PR quality scoring** — measure diff size, test coverage delta, and lint pass rate as quality signals per session
- [ ] **Knowledge Base** — one-time setup script that loads Superset's security conventions (env var naming, test location, PR standards) into Devin's knowledge store
- [ ] **Devin Review** — automated PR quality gate triggered on pull_request webhook events with a [Devin] PR title filter, closes the fix → review → merge loop
- [ ] **Multi-Devin Orchestration (Parallel Devins)** — ASCII diagram showing parent session spawning 4 or more parallel child sessions, one per CWE, reducing total time from ~40 min to ~10 min
- [ ] **MCP Marketplace Integration** -  Trigger sessions from Linear issue or Jira issues, Datadog anamolies or Sentry exceptions


---

## 🛠 Troubleshooting

**Devin session not starting**
- Verify `DEVIN_API_KEY` starts with `cog_` and is not expired
- Confirm `DEVIN_ORG_ID` is the correct UUID from your org settings
- Check container logs: `docker compose logs -f`

**Webhook not firing**
- Confirm the Payload URL is publicly reachable — use ngrok for local dev
- Check GitHub's delivery log: **Settings → Webhooks → Recent Deliveries**
- Ensure the issue label is exactly `auto-remediate`

**Dashboard shows no sessions**
- Use the **Trigger Devin** button or the `curl` test command above
- Inspect SQLite directly:
  ```bash
  docker exec -it devin-remediation sqlite3 /app/data/sessions.db "SELECT * FROM sessions;"
  ```

**Status stuck on running**
- Use the **↻ Sync button** on the dashboard row to force-poll Devin instantly
- Or call: `curl -X POST http://localhost:8000/sessions/{session_id}/sync`
- Check the session directly at `https://app.devin.ai/sessions/{session_id}`

**Rebuild after code changes**
```bash
docker compose down && docker compose up --build -d
```
