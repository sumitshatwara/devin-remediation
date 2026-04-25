import os
import re
import json
import time
import sqlite3
import requests
from datetime import datetime
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="Devin Remediation System")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DEVIN_API_KEY  = os.getenv("DEVIN_API_KEY",  "your_devin_api_key")
DEVIN_ORG_ID   = os.getenv("DEVIN_ORG_ID",   "your_org_id")
DEVIN_BASE_URL = f"https://api.devin.ai/v3/organizations/{DEVIN_ORG_ID}"
REPO_OWNER     = os.getenv("REPO_OWNER",     "your_github_username")
REPO_NAME      = os.getenv("REPO_NAME",      "superset")
DB_PATH        = "/app/data/sessions.db"

# ── Devin v3 status constants ─────────────────────────────────────────────────
# v3 statuses: new | claimed | running | resuming | exit | error | suspended
TERMINAL_STATUSES = {"exit", "error", "suspended"}
RUNNING_STATUSES  = {"new", "claimed", "running", "resuming"}

def to_ui_status(db_status: str) -> str:
    """Map Devin v3 API statuses → UI badge statuses used by the dashboard."""
    return {
        "exit":      "completed",
        "error":     "timeout",
        "suspended": "blocked",
        "new":       "running",
        "claimed":   "running",
        "running":   "running",
        "resuming":  "running",
        "triggered": "pending",
    }.get(db_status, "pending")

PR_REGEX = re.compile(r'https://github\.com/[^\s\'"<>]+/pull/\d+')

# ── DB ────────────────────────────────────────────────────────────────────────
def init_db():
    os.makedirs("/app/data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            devin_session_id     TEXT,
            github_issue_number  INTEGER,
            github_issue_title   TEXT,
            github_issue_url     TEXT,
            status               TEXT DEFAULT 'triggered',
            pr_url               TEXT,
            started_at           TEXT,
            updated_at           TEXT,
            completed_at         TEXT,
            error_message        TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_db():
    return sqlite3.connect(DB_PATH)

def row_to_api(row: tuple) -> dict:
    cols = ["id","devin_session_id","github_issue_number","github_issue_title",
            "github_issue_url","status","pr_url","started_at","updated_at",
            "completed_at","error_message"]
    d = dict(zip(cols, row))
    sid = d.get("devin_session_id") or ""
    return {
        "id":           d["id"],
        "session_id":   sid,
        "issue_number": d["github_issue_number"],
        "issue_title":  d["github_issue_title"],
        "issue_url":    d["github_issue_url"],
        "status":       to_ui_status(d["status"]),
        "raw_status":   d["status"],
        "pr_url":       d["pr_url"],
        "started_at":   d["started_at"],
        "completed_at": d["completed_at"],
        "updated_at":   d["updated_at"],
        "error_message":d["error_message"],
        "devin_url":    f"https://app.devin.ai/sessions/{sid}" if sid else None,
    }

# ── Devin API helpers ─────────────────────────────────────────────────────────
def extract_pr_url(data: dict) -> str | None:
    match = PR_REGEX.search(json.dumps(data))
    return match.group(0) if match else None

def create_devin_session(issue_number, issue_title, issue_body, issue_url) -> str:
    prompt = f"""You are working on a fork of Apache Superset:
https://github.com/{REPO_OWNER}/{REPO_NAME}

GitHub Issue #{issue_number}: {issue_title}

Issue Description:
{issue_body}

Your task:
1. Analyse and implement a targeted fix for the problem described above.
2. Write or update relevant tests to cover the fix.
3. Create a Pull Request with:
   - Title: "fix: {issue_title} (Issue #{issue_number})"
   - Body: root cause, fix applied, tests added
   - Reference to: {issue_url}

Keep the fix minimal and ensure existing tests continue to pass."""

    headers = {"Authorization": f"Bearer {DEVIN_API_KEY}", "Content-Type": "application/json"}
    resp = requests.post(f"{DEVIN_BASE_URL}/sessions", headers=headers,
                         json={"prompt": prompt}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("session_id") or data.get("id")

def get_devin_session(devin_session_id: str) -> dict:
    headers = {"Authorization": f"Bearer {DEVIN_API_KEY}"}
    resp = requests.get(f"{DEVIN_BASE_URL}/sessions/{devin_session_id}",
                        headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()

# ── Background poller ─────────────────────────────────────────────────────────
def poll_devin_session(devin_session_id: str, db_row_id: int):
    max_polls = 120
    consecutive_errors = 0
    for _ in range(max_polls):
        time.sleep(30)
        try:
            data   = get_devin_session(devin_session_id)
            status = data.get("status", "running")
            pr_url = extract_pr_url(data)
            now    = datetime.utcnow().isoformat()
            consecutive_errors = 0
            conn = get_db()
            conn.execute(
                "UPDATE sessions SET status=?, pr_url=COALESCE(?,pr_url), updated_at=? WHERE id=?",
                (status, pr_url, now, db_row_id))
            conn.commit()
            conn.close()
            if status in TERMINAL_STATUSES:
                conn = get_db()
                conn.execute("UPDATE sessions SET completed_at=? WHERE id=?", (now, db_row_id))
                conn.commit()
                conn.close()
                break
        except Exception as exc:
            consecutive_errors += 1
            conn = get_db()
            conn.execute("UPDATE sessions SET error_message=?, updated_at=? WHERE id=?",
                         (str(exc), datetime.utcnow().isoformat(), db_row_id))
            conn.commit()
            conn.close()
            if consecutive_errors >= 5:
                conn = get_db()
                conn.execute(
                    "UPDATE sessions SET status='error', completed_at=? WHERE id=?",
                    (datetime.utcnow().isoformat(), db_row_id))
                conn.commit()
                conn.close()
                break
    else:
        conn = get_db()
        conn.execute(
            "UPDATE sessions SET status='error', error_message='Polling timed out', completed_at=? WHERE id=?",
            (datetime.utcnow().isoformat(), db_row_id))
        conn.commit()
        conn.close()

# ── API Routes ────────────────────────────────────────────────────────────────
@app.post("/webhook/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    payload      = await request.json()
    event_action = payload.get("action", "")
    issue        = payload.get("issue", {})
    #labels       = [lbl.get("name", "").lower() for lbl in issue.get("labels", [])]
    label_added  = payload.get("label", {}).get("name", "").lower()

    if event_action != "labeled" or label_added != "auto-remediate":
        return {"status": "ignored", "reason": f"action={event_action}, label={label_added} — not an auto-remediate trigger"}

    issue_number = issue.get("number")
    issue_title  = issue.get("title", "")
    issue_body   = issue.get("body", "")
    issue_url    = issue.get("html_url", "")
    
    # ── Safety net: deduplication guard ──────────────────────────────────────
    # Reject if an active (non-terminal) session already exists for this issue.
    # Terminal statuses: exit, error, suspended — anything else is still live.
    conn = get_db()
    existing = conn.execute(
        """SELECT devin_session_id, status FROM sessions
           WHERE github_issue_number = ?
           AND   status NOT IN ('exit', 'error', 'suspended')
           ORDER BY started_at DESC LIMIT 1""",
        (issue_number,)
    ).fetchone()
    conn.close()

    if existing:
        return {
            "status":               "duplicate_ignored",
            "reason":               f"Active session already exists for issue #{issue_number}",
            "existing_session_id":  existing[0],
            "existing_status":      existing[1],
        }
        
    try:
        devin_session_id = create_devin_session(issue_number, issue_title, issue_body, issue_url)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to create Devin session: {exc}")

    now = datetime.utcnow().isoformat()
    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO sessions (devin_session_id, github_issue_number, github_issue_title,
           github_issue_url, status, started_at, updated_at)
           VALUES (?, ?, ?, ?, 'running', ?, ?)""",
        (devin_session_id, issue_number, issue_title, issue_url, now, now))
    row_id = cursor.lastrowid
    conn.commit()
    conn.close()
    background_tasks.add_task(poll_devin_session, devin_session_id, row_id)
    return {"status": "triggered", "devin_session_id": devin_session_id, "issue_number": issue_number}


@app.post("/trigger")
async def manual_trigger(request: Request, background_tasks: BackgroundTasks):
    """Manual trigger from the dashboard modal."""
    body         = await request.json()
    issue_number = body.get("issue_number", 0)
    issue_title  = body.get("issue_title", "Manual trigger")
    issue_body   = body.get("issue_body", "")
    issue_url    = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}"

    try:
        devin_session_id = create_devin_session(issue_number, issue_title, issue_body, issue_url)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to create Devin session: {exc}")

    now = datetime.utcnow().isoformat()
    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO sessions (devin_session_id, github_issue_number, github_issue_title,
           github_issue_url, status, started_at, updated_at)
           VALUES (?, ?, ?, ?, 'running', ?, ?)""",
        (devin_session_id, issue_number, issue_title, issue_url, now, now))
    row_id = cursor.lastrowid
    conn.commit()
    conn.close()
    background_tasks.add_task(poll_devin_session, devin_session_id, row_id)
    return {
        "status":     "triggered",
        "session_id": devin_session_id,
        "devin_url":  f"https://app.devin.ai/sessions/{devin_session_id}"
    }


@app.get("/api/sessions")
def api_sessions():
    conn = get_db()
    rows = conn.execute("SELECT * FROM sessions ORDER BY started_at DESC LIMIT 100").fetchall()
    conn.close()
    return [row_to_api(r) for r in rows]


@app.get("/api/stats")
def api_stats():
    conn = get_db()
    rows = conn.execute("SELECT * FROM sessions").fetchall()
    conn.close()
    sessions = [row_to_api(r) for r in rows]
    total     = len(sessions)
    completed = sum(1 for s in sessions if s["status"] == "completed")
    running   = sum(1 for s in sessions if s["status"] == "running")
    blocked   = sum(1 for s in sessions if s["status"] == "blocked")
    timeout   = sum(1 for s in sessions if s["status"] == "timeout")
    with_pr   = sum(1 for s in sessions if s["pr_url"])
    rate      = round(completed / total * 100, 1) if total else 0
    return {
        "total": total, "completed": completed, "running": running,
        "blocked": blocked, "timeout": timeout, "with_pr": with_pr,
        "success_rate": rate
    }


@app.post("/sessions/{devin_session_id}/sync")
async def force_sync(devin_session_id: str):
    conn = get_db()
    row = conn.execute("SELECT id FROM sessions WHERE devin_session_id=?",
                       (devin_session_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    db_row_id = row[0]
    try:
        data     = get_devin_session(devin_session_id)
        status   = data.get("status", "unknown")
        pr_url   = extract_pr_url(data)
        now      = datetime.utcnow().isoformat()
        completed_at = now if status in TERMINAL_STATUSES else None
        conn = get_db()
        conn.execute(
            """UPDATE sessions SET status=?, pr_url=COALESCE(?,pr_url),
               updated_at=?, completed_at=COALESCE(?,completed_at) WHERE id=?""",
            (status, pr_url, now, completed_at, db_row_id))
        conn.commit()
        conn.close()
        return {"status": to_ui_status(status), "raw_status": status, "pr_url": pr_url, "synced": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/health")
def health():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat(), "total_sessions": total}


# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Devin Remediation Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300..700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {
  --text-xs:   clamp(0.75rem, 0.7rem + 0.25vw, 0.875rem);
  --text-sm:   clamp(0.875rem, 0.8rem + 0.35vw, 1rem);
  --text-base: clamp(1rem, 0.95rem + 0.25vw, 1.125rem);
  --text-lg:   clamp(1.125rem, 1rem + 0.75vw, 1.5rem);
  --text-xl:   clamp(1.5rem, 1.2rem + 1.25vw, 2.25rem);
  --space-1:.25rem; --space-2:.5rem; --space-3:.75rem; --space-4:1rem;
  --space-6:1.5rem; --space-8:2rem; --space-10:2.5rem; --space-12:3rem;
  --radius-sm:.375rem; --radius-md:.5rem; --radius-lg:.75rem; --radius-xl:1rem;
  --font-body:'Inter',system-ui,sans-serif;
  --font-mono:'JetBrains Mono','Fira Code',monospace;
  --transition:180ms cubic-bezier(0.16,1,0.3,1);
}
[data-theme="dark"] {
  --color-bg:#0d0f14; --color-surface:#131720; --color-surface-2:#191e2a;
  --color-surface-offset:#1e2433; --color-border:#262d3d;
  --color-text:#e2e8f0; --color-text-muted:#8892a4; --color-text-faint:#4a5568;
  --color-primary:#6366f1; --color-primary-glow:rgba(99,102,241,0.15);
  --color-success:#10b981; --color-success-glow:rgba(16,185,129,0.15);
  --color-warning:#f59e0b; --color-warning-glow:rgba(245,158,11,0.15);
  --color-error:#ef4444;   --color-error-glow:rgba(239,68,68,0.15);
  --color-blue:#3b82f6;
  --shadow-sm:0 1px 3px rgba(0,0,0,.4); --shadow-md:0 4px 16px rgba(0,0,0,.5);
  --shadow-lg:0 12px 40px rgba(0,0,0,.6);
}
[data-theme="light"] {
  --color-bg:#f1f5f9; --color-surface:#ffffff; --color-surface-2:#f8fafc;
  --color-surface-offset:#f1f5f9; --color-border:#e2e8f0;
  --color-text:#0f172a; --color-text-muted:#64748b; --color-text-faint:#94a3b8;
  --color-primary:#4f46e5; --color-primary-glow:rgba(79,70,229,0.1);
  --color-success:#059669; --color-success-glow:rgba(5,150,105,0.1);
  --color-warning:#d97706; --color-warning-glow:rgba(217,119,6,0.1);
  --color-error:#dc2626;   --color-error-glow:rgba(220,38,38,0.1);
  --color-blue:#2563eb;
  --shadow-sm:0 1px 3px rgba(0,0,0,.08); --shadow-md:0 4px 16px rgba(0,0,0,.1);
  --shadow-lg:0 12px 40px rgba(0,0,0,.12);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth;-webkit-font-smoothing:antialiased}
body{font-family:var(--font-body);font-size:var(--text-sm);background:var(--color-bg);
  color:var(--color-text);min-height:100dvh;transition:background var(--transition),color var(--transition)}
button{cursor:pointer;background:none;border:none;font:inherit;color:inherit}
a{text-decoration:none}
.app{display:grid;grid-template-rows:56px 1fr;min-height:100dvh}

/* Topbar */
.topbar{display:flex;align-items:center;justify-content:space-between;
  padding:0 var(--space-6);background:var(--color-surface);
  border-bottom:1px solid var(--color-border);position:sticky;top:0;z-index:100;
  box-shadow:var(--shadow-sm)}
.topbar-brand{display:flex;align-items:center;gap:var(--space-3)}
.brand-name{font-weight:700;font-size:var(--text-base);letter-spacing:-0.02em}
.brand-sub{font-size:var(--text-xs);color:var(--color-text-muted)}
.topbar-actions{display:flex;align-items:center;gap:var(--space-3)}
.live-badge{display:flex;align-items:center;gap:var(--space-2);font-size:var(--text-xs);
  color:var(--color-success);background:var(--color-success-glow);
  padding:var(--space-1) var(--space-3);border-radius:var(--radius-sm)}
.live-dot{width:6px;height:6px;border-radius:50%;background:var(--color-success);
  animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(1.4)}}
.theme-btn{width:36px;height:36px;border-radius:var(--radius-md);
  display:flex;align-items:center;justify-content:center;color:var(--color-text-muted);
  transition:background var(--transition),color var(--transition)}
.theme-btn:hover{background:var(--color-surface-offset);color:var(--color-text)}

/* Main */
.main{padding:var(--space-6);max-width:1400px;margin:0 auto;width:100%}

/* Sys info bar */
.sys-info{display:flex;gap:var(--space-6);flex-wrap:wrap;font-size:var(--text-xs);
  color:var(--color-text-muted);margin-bottom:var(--space-6)}
.sys-info span{display:flex;align-items:center;gap:var(--space-2)}

/* KPI Grid */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(200px,100%),1fr));
  gap:var(--space-4);margin-bottom:var(--space-6)}
.kpi-card{background:var(--color-surface);border:1px solid var(--color-border);
  border-radius:var(--radius-xl);padding:var(--space-6);box-shadow:var(--shadow-sm);
  transition:box-shadow var(--transition),transform var(--transition)}
.kpi-card:hover{box-shadow:var(--shadow-md);transform:translateY(-1px)}
.kpi-label{font-size:var(--text-xs);color:var(--color-text-muted);text-transform:uppercase;
  letter-spacing:.06em;margin-bottom:var(--space-3)}
.kpi-value{font-size:var(--text-xl);font-weight:700;letter-spacing:-0.03em;
  font-variant-numeric:tabular-nums}
.kpi-sub{font-size:var(--text-xs);color:var(--color-text-muted);margin-top:var(--space-2)}
.kpi-card.success .kpi-value{color:var(--color-success)}
.kpi-card.primary .kpi-value{color:var(--color-primary)}
.kpi-card.warning .kpi-value{color:var(--color-warning)}
.kpi-card.error   .kpi-value{color:var(--color-error)}

/* Cards */
.charts-row{display:grid;grid-template-columns:1fr 1fr;gap:var(--space-4);margin-bottom:var(--space-6)}
@media(max-width:768px){.charts-row{grid-template-columns:1fr}}
.card{background:var(--color-surface);border:1px solid var(--color-border);
  border-radius:var(--radius-xl);box-shadow:var(--shadow-sm);overflow:hidden}
.card-header{display:flex;align-items:center;justify-content:space-between;
  padding:var(--space-4) var(--space-6);border-bottom:1px solid var(--color-border)}
.card-title{font-weight:600;font-size:var(--text-sm)}
.card-body{padding:var(--space-6)}
.chart-wrap{position:relative;height:240px}
.table-card{margin-bottom:var(--space-6)}
.table-wrap{overflow-x:auto}

/* Table */
table{width:100%;border-collapse:collapse}
thead th{padding:var(--space-3) var(--space-4);text-align:left;font-size:var(--text-xs);
  color:var(--color-text-muted);text-transform:uppercase;letter-spacing:.06em;
  background:var(--color-surface-offset);border-bottom:1px solid var(--color-border);font-weight:500}
tbody tr{border-bottom:1px solid var(--color-border);transition:background var(--transition)}
tbody tr:hover{background:var(--color-surface-offset)}
tbody tr:last-child{border-bottom:none}
tbody td{padding:var(--space-3) var(--space-4);font-size:var(--text-xs);font-variant-numeric:tabular-nums}
.cell-mono{font-family:var(--font-mono);font-size:11px;color:var(--color-text-muted)}
.cell-title{max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* Badges */
.badge{display:inline-flex;align-items:center;gap:4px;padding:2px var(--space-2);
  border-radius:var(--radius-sm);font-size:11px;font-weight:500;white-space:nowrap}
.badge-running  {background:var(--color-primary-glow); color:var(--color-primary)}
.badge-completed{background:var(--color-success-glow); color:var(--color-success)}
.badge-blocked  {background:var(--color-warning-glow); color:var(--color-warning)}
.badge-timeout  {background:var(--color-error-glow);   color:var(--color-error)}
.badge-pending  {background:rgba(100,116,139,0.15);    color:var(--color-text-muted)}

/* Buttons */
.btn-primary{display:inline-flex;align-items:center;gap:var(--space-2);
  background:var(--color-primary);color:#fff;padding:var(--space-2) var(--space-4);
  border-radius:var(--radius-md);font-size:var(--text-xs);font-weight:600;
  transition:opacity var(--transition)}
.btn-primary:hover{opacity:.85}
.btn-ghost{display:inline-flex;align-items:center;gap:var(--space-2);
  border:1px solid var(--color-border);color:var(--color-text-muted);
  padding:var(--space-2) var(--space-4);border-radius:var(--radius-md);font-size:var(--text-xs);
  transition:background var(--transition),color var(--transition)}
.btn-ghost:hover{background:var(--color-surface-offset);color:var(--color-text)}
.btn-sync{background:var(--color-surface-offset);border:1px solid var(--color-border);
  color:var(--color-text-muted);padding:2px 10px;border-radius:var(--radius-sm);
  font-size:11px;font-family:var(--font-body);transition:all var(--transition)}
.btn-sync:hover{background:var(--color-border);color:var(--color-text)}

/* Empty state */
.empty-state{display:flex;flex-direction:column;align-items:center;
  padding:var(--space-12);color:var(--color-text-faint);text-align:center}
.empty-state svg{margin-bottom:var(--space-4)}
.empty-state h3{font-size:var(--text-sm);color:var(--color-text-muted);margin-bottom:var(--space-2)}
.empty-state p{font-size:var(--text-xs);max-width:36ch}

/* Skeleton */
@keyframes shimmer{0%{background-position:-200% 0}100%{background-position:200% 0}}
.skeleton{background:linear-gradient(90deg,var(--color-surface-offset) 25%,var(--color-border) 50%,var(--color-surface-offset) 75%);
  background-size:200% 100%;animation:shimmer 1.5s ease-in-out infinite;border-radius:var(--radius-sm)}
.skeleton-kpi{height:2.5rem;width:60%;border-radius:var(--radius-sm)}

/* Modal */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:200;
  display:flex;align-items:center;justify-content:center;padding:var(--space-4);
  opacity:0;pointer-events:none;transition:opacity var(--transition)}
.modal-overlay.open{opacity:1;pointer-events:all}
.modal{background:var(--color-surface);border:1px solid var(--color-border);
  border-radius:var(--radius-xl);padding:var(--space-8);width:100%;max-width:480px;
  box-shadow:var(--shadow-lg);transform:translateY(12px);transition:transform var(--transition)}
.modal-overlay.open .modal{transform:translateY(0)}
.modal h2{font-size:var(--text-base);font-weight:700;margin-bottom:var(--space-6)}
.form-group{margin-bottom:var(--space-4)}
.form-group label{display:block;font-size:var(--text-xs);color:var(--color-text-muted);margin-bottom:var(--space-2)}
.form-group input,.form-group textarea{width:100%;background:var(--color-surface-offset);
  border:1px solid var(--color-border);border-radius:var(--radius-md);
  padding:var(--space-3) var(--space-4);font-family:var(--font-body);font-size:var(--text-sm);
  color:var(--color-text);transition:border-color var(--transition)}
.form-group input:focus,.form-group textarea:focus{outline:none;border-color:var(--color-primary)}
.form-group textarea{resize:vertical;min-height:80px}
.modal-actions{display:flex;gap:var(--space-3);justify-content:flex-end;margin-top:var(--space-6)}

/* Toast */
.toast{position:fixed;bottom:24px;right:24px;background:var(--color-surface-2);
  border:1px solid var(--color-border);padding:10px 18px;border-radius:var(--radius-md);
  font-size:var(--text-xs);opacity:0;transition:opacity .3s;pointer-events:none;z-index:999;
  box-shadow:var(--shadow-md)}
.toast.show{opacity:1}

/* Footer */
.footer{text-align:center;padding:var(--space-6);font-size:var(--text-xs);
  color:var(--color-text-faint);border-top:1px solid var(--color-border);margin-top:var(--space-8)}
</style>
</head>
<body>
<div class="app">

  <header class="topbar">
    <div class="topbar-brand">
      <svg width="32" height="32" viewBox="0 0 32 32" fill="none" aria-label="Devin Observatory">
        <rect width="32" height="32" rx="8" fill="#6366f1"/>
        <circle cx="16" cy="16" r="7" stroke="white" stroke-width="2" fill="none"/>
        <path d="M16 9 L16 16 L21 16" stroke="white" stroke-width="2" stroke-linecap="round"/>
        <circle cx="16" cy="16" r="2" fill="white"/>
      </svg>
      <div>
        <div class="brand-name">Devin Remediation Dashboard</div>
        <div class="brand-sub">Devin API · Apache Superset · Cognition Demo</div>
      </div>
    </div>
    <div class="topbar-actions">
      <div class="live-badge"><div class="live-dot"></div>Live</div>
      <button class="btn-primary" onclick="openModal()">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
        Trigger Devin
      </button>
      <button class="theme-btn" data-theme-toggle aria-label="Toggle theme">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
      </button>
    </div>
  </header>

  <main class="main">

    <div class="sys-info">
      <span>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M9 9h6M9 12h6M9 15h4"/></svg>
        Repo: <strong>REPO_OWNER_PLACEHOLDER/REPO_NAME_PLACEHOLDER</strong>
      </span>
      <span>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
        Auto-refresh: every 15s
      </span>
      <span style="margin-left:auto" id="last-updated">Last updated: —</span>
    </div>

    <div class="kpi-grid">
      <div class="kpi-card"><div class="kpi-label">Total Sessions</div><div class="kpi-value skeleton skeleton-kpi" id="kpi-total">—</div><div class="kpi-sub">All time</div></div>
      <div class="kpi-card success"><div class="kpi-label">Completed</div><div class="kpi-value skeleton skeleton-kpi" id="kpi-completed">—</div><div class="kpi-sub">Successfully resolved</div></div>
      <div class="kpi-card primary"><div class="kpi-label">Running</div><div class="kpi-value skeleton skeleton-kpi" id="kpi-running">—</div><div class="kpi-sub">Devin working now</div></div>
      <div class="kpi-card warning"><div class="kpi-label">Suspended</div><div class="kpi-value skeleton skeleton-kpi" id="kpi-blocked">—</div><div class="kpi-sub">Needs review</div></div>
      <div class="kpi-card success"><div class="kpi-label">Success Rate</div><div class="kpi-value skeleton skeleton-kpi" id="kpi-rate">—</div><div class="kpi-sub">Completed / total</div></div>
      <div class="kpi-card"><div class="kpi-label">PRs Opened</div><div class="kpi-value skeleton skeleton-kpi" id="kpi-prs">—</div><div class="kpi-sub">With linked PR</div></div>
    </div>

    <div class="charts-row">
      <div class="card">
        <div class="card-header"><span class="card-title">Session Status Distribution</span></div>
        <div class="card-body"><div class="chart-wrap"><canvas id="doughnutChart"></canvas></div></div>
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">Sessions Over Time (Last 7 Days)</span></div>
        <div class="card-body"><div class="chart-wrap"><canvas id="barChart"></canvas></div></div>
      </div>
    </div>

    <div class="card table-card">
      <div class="card-header">
        <span class="card-title">Remediation Sessions</span>
        <button class="btn-ghost" onclick="loadData()">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>
          Refresh
        </button>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>#</th><th>Issue</th><th>Title</th><th>Status</th>
            <th>Session ID</th><th>Started</th><th>Completed</th><th>PR</th><th>Devin</th><th></th>
          </tr></thead>
          <tbody id="sessions-tbody">
            <tr><td colspan="10"><div class="empty-state">
              <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
              <h3>No sessions yet</h3>
              <p>Sessions appear here once Devin starts remediating GitHub issues.</p>
            </div></td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card" style="margin-bottom:var(--space-6)">
      <div class="card-header"><span class="card-title">How It Works</span></div>
      <div class="card-body" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:var(--space-4)">
        <div style="display:flex;flex-direction:column;gap:var(--space-2)">
          <div style="color:var(--color-primary);font-weight:700;font-size:var(--text-sm)">① Issue Created</div>
          <div style="font-size:var(--text-xs);color:var(--color-text-muted)">GitHub issue labeled <code style="background:var(--color-surface-offset);padding:1px 5px;border-radius:3px;font-family:var(--font-mono)">auto-remediate</code> triggers the webhook automatically.</div>
        </div>
        <div style="display:flex;flex-direction:column;gap:var(--space-2)">
          <div style="color:var(--color-primary);font-weight:700;font-size:var(--text-sm)">② Devin Dispatched</div>
          <div style="font-size:var(--text-xs);color:var(--color-text-muted)">The orchestrator calls Devin API v3, creating an autonomous session with a structured remediation prompt and repo context.</div>
        </div>
        <div style="display:flex;flex-direction:column;gap:var(--space-2)">
          <div style="color:var(--color-primary);font-weight:700;font-size:var(--text-sm)">③ Devin Fixes</div>
          <div style="font-size:var(--text-xs);color:var(--color-text-muted)">Devin clones the repo, locates the issue, writes a targeted fix, runs tests, and opens a PR — fully autonomously.</div>
        </div>
        <div style="display:flex;flex-direction:column;gap:var(--space-2)">
          <div style="color:var(--color-primary);font-weight:700;font-size:var(--text-sm)">④ Observable</div>
          <div style="font-size:var(--text-xs);color:var(--color-text-muted)">Every session is tracked in real-time here. Status auto-syncs every 15s. Use the ↻ Sync button for instant status refresh.</div>
        </div>
      </div>
    </div>

  </main>

  <footer class="footer">Built with Devin API v3 · FastAPI · Docker · Presented by Sumit · Cognition Take-Home 2026</footer>
</div>

<!-- Trigger Modal -->
<div class="modal-overlay" id="modal-overlay">
  <div class="modal">
    <h2>Manually Trigger Devin</h2>
    <div class="form-group">
      <label>Issue Number</label>
      <input type="number" id="t-issue-num" placeholder="e.g. 42" value="1">
    </div>
    <div class="form-group">
      <label>Issue Title</label>
      <input type="text" id="t-issue-title" placeholder="e.g. Upgrade psycopg2-binary to 2.9.12">
    </div>
    <div class="form-group">
      <label>Issue Body</label>
      <textarea id="t-issue-body" placeholder="Describe the problem, affected files, and steps to reproduce..."></textarea>
    </div>
    <div class="modal-actions">
      <button class="btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn-primary" onclick="triggerDevin()">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
        Dispatch Devin
      </button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
// ── Theme toggle ──────────────────────────────────────────────────────────────
(function(){
  const btn = document.querySelector('[data-theme-toggle]');
  const root = document.documentElement;
  let theme = 'dark';
  root.setAttribute('data-theme', theme);
  btn && btn.addEventListener('click', () => {
    theme = theme === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', theme);
    btn.innerHTML = theme === 'dark'
      ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
      : '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>';
    if (doughnutChart || barChart) { destroyCharts(); initCharts(); loadData(); }
  });
})();

// ── Charts ────────────────────────────────────────────────────────────────────
let doughnutChart, barChart;

function chartColors() {
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  return {
    grid: dark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)',
    text: dark ? '#8892a4' : '#64748b'
  };
}

function destroyCharts() {
  if (doughnutChart) { doughnutChart.destroy(); doughnutChart = null; }
  if (barChart) { barChart.destroy(); barChart = null; }
}

function initCharts() {
  const { grid, text } = chartColors();

  doughnutChart = new Chart(document.getElementById('doughnutChart').getContext('2d'), {
    type: 'doughnut',
    data: {
      labels: ['Completed', 'Running', 'Suspended', 'Failed', 'Pending'],
      datasets: [{ data: [0,0,0,0,0],
        backgroundColor: ['#10b981','#6366f1','#f59e0b','#ef4444','#64748b'],
        borderWidth: 0, hoverOffset: 6 }]
    },
    options: { responsive: true, maintainAspectRatio: false, cutout: '68%',
      plugins: { legend: { position: 'right',
        labels: { color: text, font: { size: 11 }, boxWidth: 12, padding: 12 } } } }
  });

  barChart = new Chart(document.getElementById('barChart').getContext('2d'), {
    type: 'bar',
    data: {
      labels: getLast7Days(),
      datasets: [{ label: 'Sessions', data: [0,0,0,0,0,0,0],
        backgroundColor: 'rgba(99,102,241,0.6)', borderColor: '#6366f1',
        borderWidth: 1, borderRadius: 4 }]
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: grid }, ticks: { color: text, font: { size: 10 } } },
        y: { grid: { color: grid }, ticks: { color: text, font: { size: 10 }, stepSize: 1 } }
      } }
  });
}

function getLast7Days() {
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(); d.setDate(d.getDate() - 6 + i);
    return d.toLocaleDateString('en-IN', { month: 'short', day: 'numeric' });
  });
}

// ── Data Load ─────────────────────────────────────────────────────────────────
async function loadData() {
  try {
    const [stats, sessions] = await Promise.all([
      fetch('/api/stats').then(r => r.json()),
      fetch('/api/sessions').then(r => r.json())
    ]);
    renderKPIs(stats);
    renderTable(sessions);
    updateCharts(stats, sessions);
    document.getElementById('last-updated').textContent =
      'Last updated: ' + new Date().toLocaleTimeString('en-IN');
  } catch(e) {
    console.warn('Failed to load data:', e);
  }
}

function renderKPIs(s) {
  const fields = { total: s.total, completed: s.completed, running: s.running, blocked: s.blocked };
  Object.entries(fields).forEach(([k, v]) => {
    const el = document.getElementById(`kpi-${k}`);
    if (el) { el.classList.remove('skeleton','skeleton-kpi'); el.textContent = v ?? '0'; }
  });
  const rateEl = document.getElementById('kpi-rate');
  if (rateEl) { rateEl.classList.remove('skeleton','skeleton-kpi'); rateEl.textContent = (s.success_rate ?? 0) + '%'; }
  const prEl = document.getElementById('kpi-prs');
  if (prEl) { prEl.classList.remove('skeleton','skeleton-kpi'); prEl.textContent = s.with_pr ?? '0'; }
}

function statusBadge(status) {
  const cls   = { running:'badge-running', completed:'badge-completed', blocked:'badge-blocked', timeout:'badge-timeout', pending:'badge-pending' };
  const icons = {
    running:   '<svg width="8" height="8" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="8"><animate attributeName="opacity" values="1;0.4;1" dur="1.5s" repeatCount="indefinite"/></circle></svg>',
    completed: '<svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>',
    blocked:   '<svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/></svg>',
    timeout:   '<svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>'
  };
  return `<span class="badge ${cls[status]||'badge-pending'}">${icons[status]||''}${status}</span>`;
}

function fmtDate(iso) {
  if (!iso) return '<span style="color:var(--color-text-faint)">—</span>';
  const d = new Date(iso);
  return d.toLocaleDateString('en-IN', { month:'short', day:'numeric' }) + ' ' +
         d.toLocaleTimeString('en-IN', { hour:'2-digit', minute:'2-digit' });
}

function renderTable(sessions) {
  const tbody = document.getElementById('sessions-tbody');
  if (!sessions || sessions.length === 0) {
    tbody.innerHTML = `<tr><td colspan="10"><div class="empty-state">
      <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
      <h3>No sessions yet</h3><p>Create a GitHub issue labeled <code>auto-remediate</code> to begin.</p>
    </div></td></tr>`;
    return;
  }
  tbody.innerHTML = sessions.map(s => `
    <tr id="row-${s.session_id}">
      <td style="color:var(--color-text-faint)">#${s.id}</td>
      <td><a href="${s.issue_url||'#'}" target="_blank" rel="noopener" style="color:var(--color-primary)">#${s.issue_number}</a></td>
      <td class="cell-title" title="${s.issue_title||''}">${s.issue_title||'—'}</td>
      <td>${statusBadge(s.status)}</td>
      <td class="cell-mono">${(s.session_id||'').slice(0,14)}…</td>
      <td style="color:var(--color-text-muted)">${fmtDate(s.started_at)}</td>
      <td style="color:var(--color-text-muted)">${fmtDate(s.completed_at)}</td>
      <td id="pr-${s.session_id}">${s.pr_url ? `<a href="${s.pr_url}" target="_blank" rel="noopener" style="color:var(--color-success);font-size:11px">View PR →</a>` : '<span style="color:var(--color-text-faint)">—</span>'}</td>
      <td>${s.devin_url ? `<a href="${s.devin_url}" target="_blank" rel="noopener" style="color:var(--color-primary);font-size:11px">Open →</a>` : '<span style="color:var(--color-text-faint)">—</span>'}</td>
      <td>${s.session_id ? `<button class="btn-sync" onclick="syncSession('${s.session_id}')" title="Force sync from Devin API">↻</button>` : ''}</td>
    </tr>`).join('');
}

function updateCharts(stats, sessions) {
  if (doughnutChart) {
    const pending = Math.max(0, stats.total - stats.completed - stats.running - stats.blocked - (stats.timeout||0));
    doughnutChart.data.datasets[0].data = [stats.completed, stats.running, stats.blocked, stats.timeout||0, pending];
    doughnutChart.update();
  }
  if (barChart) {
    const days = getLast7Days();
    barChart.data.datasets[0].data = days.map(day =>
      (sessions||[]).filter(s => {
        if (!s.started_at) return false;
        return new Date(s.started_at).toLocaleDateString('en-IN',{month:'short',day:'numeric'}) === day;
      }).length
    );
    barChart.update();
  }
}

// ── Sync ──────────────────────────────────────────────────────────────────────
async function syncSession(sessionId) {
  showToast('Syncing…');
  try {
    const r = await fetch(`/sessions/${sessionId}/sync`, { method: 'POST' });
    const d = await r.json();
    if (r.ok) {
      const badgeMap = {
        completed: '<span class="badge badge-completed"><svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>completed</span>',
        running:   '<span class="badge badge-running"><svg width="8" height="8" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="8"><animate attributeName="opacity" values="1;0.4;1" dur="1.5s" repeatCount="indefinite"/></circle></svg>running</span>',
        blocked:   '<span class="badge badge-blocked">suspended</span>',
        timeout:   '<span class="badge badge-timeout">failed</span>',
      };
      const row = document.getElementById(`row-${sessionId}`);
      if (row) {
        row.querySelectorAll('td')[3].innerHTML = badgeMap[d.status] || `<span class="badge badge-pending">${d.status}</span>`;
        if (d.pr_url) {
          const prCell = document.getElementById(`pr-${sessionId}`);
          if (prCell) prCell.innerHTML = `<a href="${d.pr_url}" target="_blank" rel="noopener" style="color:var(--color-success);font-size:11px">View PR →</a>`;
        }
      }
      showToast(`✓ ${d.status}${d.pr_url ? ' · PR found' : ''}`);
    } else {
      showToast('Error: ' + (d.detail || 'Unknown'));
    }
  } catch(e) { showToast('Network error'); }
}

// ── Modal ─────────────────────────────────────────────────────────────────────
function openModal()  { document.getElementById('modal-overlay').classList.add('open'); }
function closeModal() { document.getElementById('modal-overlay').classList.remove('open'); }
document.getElementById('modal-overlay').addEventListener('click', e => {
  if (e.target === document.getElementById('modal-overlay')) closeModal();
});

async function triggerDevin() {
  const num   = parseInt(document.getElementById('t-issue-num').value) || 1;
  const title = document.getElementById('t-issue-title').value || 'Manual trigger';
  const body  = document.getElementById('t-issue-body').value || '';
  try {
    const resp = await fetch('/trigger', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ issue_number: num, issue_title: title, issue_body: body })
    });
    const data = await resp.json();
    closeModal();
    if (resp.ok) {
      showToast(`✅ Session started: ${(data.session_id||'').slice(0,12)}…`);
      setTimeout(loadData, 2000);
    } else {
      showToast('Error: ' + (data.detail || 'Failed'));
    }
  } catch(e) { showToast('Network error — is the backend running?'); closeModal(); }
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initCharts();
  loadData();
  setInterval(loadData, 15000);
});
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    html = DASHBOARD_HTML \
        .replace("REPO_OWNER_PLACEHOLDER", REPO_OWNER) \
        .replace("REPO_NAME_PLACEHOLDER", REPO_NAME)
    return HTMLResponse(content=html)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
