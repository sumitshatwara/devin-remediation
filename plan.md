# Devin Vulnerability Remediation - Architecture Plan

## Architecture
- Event Trigger: GitHub Issues (webhook on issue label "vulnerability-remediation")
- Backend: Python FastAPI (Dockerized)
- Devin API: v3 (create sessions, poll for status)
- Observability: SQLite + simple HTML dashboard
- Output: PR raised by Devin against fork of apache/superset

## API Endpoints
- POST /webhook/github - receive GitHub issue webhook
- GET /dashboard - observability UI
- GET /sessions - list all Devin sessions + status

## Devin Session Flow
1. GitHub Issue created with label "vulnerability"
2. Webhook triggers FastAPI endpoint
3. FastAPI calls Devin API to create session with issue context
4. Poll Devin session for status every 30s
5. On completion, log PR link + status to SQLite
6. Dashboard shows all sessions, statuses, PR links

