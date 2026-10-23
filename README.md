# Observable Incident Agent (starter)

## Deploy on Render
1. Create a public GitHub repository and upload `main.py`, `requirements.txt`, and `render.yaml`.
2. In Render, choose **New + → Web Service**, connect the repository, and deploy.
3. The public URL should be HTTPS. Check `/health` first.
4. Do not use the grader's Check or Save until you have confirmed the assignment's exact API contract.

## Routes
- `GET /` service information
- `GET /health` health check
- `POST /diagnose` diagnosis without persistence
- `POST /run` diagnose, select an action, and persist run and telemetry
- `POST /check` compatibility route for a run
- `POST /save` save a supplied observed run record
- `GET /state` stored incident state
- `GET /replay/{incident_id}` replay a stored run
- `GET /telemetry` telemetry records and audit events

## Important limitations
This starter implements a generic, deterministic contract because the assignment text provided so far does not name the evaluator's exact paths, payload schema, diagnostic tools, action vocabulary, approval protocol, or persistence expectations. It must be adapted to the actual contract if one is available in the course UI. The free Render filesystem is ephemeral unless a persistent disk or external database is configured; SQLite here is a starter only, not a guarantee of durable state across redeploys/restarts.
