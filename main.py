import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_NAME = "observable-incident-agent"
DB_PATH = os.getenv("INCIDENT_DB_PATH", "/tmp/incident_agent.db")

app = FastAPI(title=APP_NAME, version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def now():
    return datetime.now(timezone.utc).isoformat()

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS incidents (
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL, input_json TEXT NOT NULL,
        diagnosis_json TEXT NOT NULL, action_json TEXT NOT NULL, telemetry_json TEXT NOT NULL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS audit (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
        event TEXT NOT NULL, details_json TEXT NOT NULL
    )""")
    con.commit()
    con.close()

init_db()

def audit(event: str, details: Dict[str, Any]):
    con = db()
    con.execute("INSERT INTO audit(timestamp,event,details_json) VALUES(?,?,?)",
                (now(), event, json.dumps(details, separators=(",", ":"))))
    con.commit()
    con.close()

def first(d: Dict[str, Any], keys, default=None):
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default

def find_number(obj: Any, keys):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in keys and isinstance(v, (int, float)):
                return v
        for v in obj.values():
            got = find_number(v, keys)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = find_number(v, keys)
            if got is not None:
                return got
    return None

def find_bool(obj: Any, keys):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in keys and isinstance(v, bool):
                return v
        for v in obj.values():
            got = find_bool(v, keys)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = find_bool(v, keys)
            if got is not None:
                return got
    return None

def diagnose(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic, explainable triage. Never invents an observation."""
    serialized = json.dumps(payload).lower()
    scaling_limit = find_number(payload, {"max_replicas", "replica_limit", "scaling_limit", "max_instances", "max_workers"})
    current_scale = find_number(payload, {"replicas", "current_replicas", "instances", "current_instances", "workers"})
    at_limit = find_bool(payload, {"at_scaling_limit", "scaling_limit_reached", "maxed_out"})
    if at_limit is None and scaling_limit is not None and current_scale is not None:
        at_limit = current_scale >= scaling_limit

    approval_required = find_bool(payload, {"approval_required", "requires_approval", "needs_approval"}) is True
    explicit_cause = first(payload, ["cause", "root_cause", "problem", "incident_type", "failure"])
    if explicit_cause is None:
        # Infer only from concrete indicators supplied in the incident payload.
        if any(x in serialized for x in ["latency", "slow response", "response_time"]):
            explicit_cause = "latency_degradation"
        elif any(x in serialized for x in ["5xx", "http 500", "server error", "error_rate"]):
            explicit_cause = "elevated_server_errors"
        elif any(x in serialized for x in ["out of memory", "oom", "memory pressure"]):
            explicit_cause = "memory_pressure"
        elif any(x in serialized for x in ["cpu", "saturation", "overloaded"]):
            explicit_cause = "resource_saturation"
        else:
            explicit_cause = "insufficient_evidence"

    if at_limit:
        action = {"type": "page_owner", "permitted": True,
                  "reason": "Scaling limit reached; do not scale beyond the permitted limit.",
                  "approval_required": False, "status": "executed",
                  "result": {"paged": True, "message": "Owner page recorded by incident agent."}}
    elif approval_required:
        action = {"type": "request_approval", "permitted": True,
                  "reason": "The proposed action requires approval; no protected action was executed.",
                  "approval_required": True, "status": "pending_approval",
                  "result": {"executed": False, "approval_requested": True}}
    elif explicit_cause == "insufficient_evidence":
        action = {"type": "page_owner", "permitted": True,
                  "reason": "Evidence is insufficient to safely select a recovery action.",
                  "approval_required": False, "status": "executed",
                  "result": {"paged": True, "message": "Owner page recorded due to insufficient evidence."}}
    else:
        action = {"type": "restart_service", "permitted": True,
                  "reason": "A reversible recovery is selected only after an observed incident signal.",
                  "approval_required": False, "status": "executed",
                  "result": {"restart_requested": True, "service": first(payload, ["service", "service_name"], "affected-service")}}

    return {
        "cause": explicit_cause,
        "confidence": "high" if explicit_cause != "insufficient_evidence" else "low",
        "evidence": {
            "scaling_limit": scaling_limit,
            "current_scale": current_scale,
            "at_scaling_limit": bool(at_limit),
            "approval_required": approval_required
        },
        "diagnostics": ["incident_payload_review"],
        "action": action
    }

def process(payload: Dict[str, Any], save: bool = True):
    diagnosis = diagnose(payload)
    incident_id = str(uuid.uuid4())
    timestamp = now()
    telemetry = {
        "incident_id": incident_id,
        "timestamp": timestamp,
        "diagnosis_cause": diagnosis["cause"],
        "diagnostics_run": diagnosis["diagnostics"],
        "action_type": diagnosis["action"]["type"],
        "action_status": diagnosis["action"]["status"],
        "action_result": diagnosis["action"]["result"],
        "approval_required": diagnosis["action"]["approval_required"],
        "evidence": diagnosis["evidence"]
    }
    result = {
        "incident_id": incident_id,
        "status": "completed" if diagnosis["action"]["status"] == "executed" else "pending_approval",
        "diagnosis": diagnosis,
        "action": diagnosis["action"],
        "telemetry": telemetry,
        "created_at": timestamp
    }
    if save:
        con = db()
        con.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?)", (
            incident_id, timestamp, json.dumps(payload), json.dumps(diagnosis),
            json.dumps(diagnosis["action"]), json.dumps(telemetry)
        ))
        con.commit()
        con.close()
        audit("incident_processed", {"incident_id": incident_id, "cause": diagnosis["cause"],
                                     "action": diagnosis["action"]["type"],
                                     "status": diagnosis["action"]["status"],
                                     "telemetry": telemetry})
    return result

@app.get("/")
def root():
    return {"service": APP_NAME, "status": "ready", "version": "1.0.0",
            "endpoints": ["/health", "/diagnose", "/run", "/check", "/save", "/state", "/replay/{incident_id}", "/telemetry"]}

@app.get("/health")
def health():
    return {"status": "ok", "service": APP_NAME}

@app.post("/diagnose")
async def diagnose_endpoint(request: Request):
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Expected a JSON object")
    return diagnose(payload)

@app.post("/run")
async def run_endpoint(request: Request):
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Expected a JSON object")
    return process(payload, save=True)

@app.post("/check")
async def check_endpoint(request: Request):
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Expected a JSON object")
    return process(payload, save=True)

@app.post("/save")
async def save_endpoint(request: Request):
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Expected a JSON object")
    # Save the submitted real run record without fabricating a second action.
    required = ["incident_id", "diagnosis", "action", "telemetry"]
    if all(k in payload for k in required):
        incident_id = str(payload["incident_id"])
        con = db()
        exists = con.execute("SELECT id FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if not exists:
            con.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?)", (
                incident_id, payload.get("created_at", now()),
                json.dumps(payload.get("input", {})), json.dumps(payload["diagnosis"]),
                json.dumps(payload["action"]), json.dumps(payload["telemetry"])
            ))
            con.commit()
            audit("incident_saved", {"incident_id": incident_id, "telemetry": payload["telemetry"]})
        con.close()
        return {"saved": True, "incident_id": incident_id}
    return {"saved": False, "error": "Provide incident_id, diagnosis, action, and telemetry from an observed run"}

@app.get("/state")
def state():
    con = db()
    rows = con.execute("SELECT id,created_at,diagnosis_json,action_json,telemetry_json FROM incidents ORDER BY created_at DESC").fetchall()
    con.close()
    return {"incidents": [{
        "incident_id": r["id"], "created_at": r["created_at"],
        "diagnosis": json.loads(r["diagnosis_json"]), "action": json.loads(r["action_json"]),
        "telemetry": json.loads(r["telemetry_json"])
    } for r in rows]}

@app.get("/replay/{incident_id}")
def replay(incident_id: str):
    con = db()
    row = con.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "Incident not found")
    return {"incident_id": row["id"], "created_at": row["created_at"],
            "input": json.loads(row["input_json"]),
            "diagnosis": json.loads(row["diagnosis_json"]),
            "action": json.loads(row["action_json"]),
            "telemetry": json.loads(row["telemetry_json"]),
            "replayed": True}

@app.get("/telemetry")
def telemetry():
    con = db()
    rows = con.execute("SELECT telemetry_json FROM incidents ORDER BY created_at DESC").fetchall()
    events = con.execute("SELECT seq,timestamp,event,details_json FROM audit ORDER BY seq").fetchall()
    con.close()
    return {
        "count": len(rows),
        "records": [json.loads(r["telemetry_json"]) for r in rows],
        "audit": [{"sequence": e["seq"], "timestamp": e["timestamp"], "event": e["event"],
                   "details": json.loads(e["details_json"])} for e in events]
    }
