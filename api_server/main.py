"""
api_server/main.py
FastAPI server — exposes REST endpoints consumed by the frontend UI.
Also serves the static index.html.
"""
import logging
import os
import uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from temporalio.client import Client

# Shared storage used by both API and worker processes
from contract_worker.postgres_db import db, ReviewStatus
from contract_worker.workflow import ContractReviewInput, ContractReviewWorkflow, ReviewDecisionSignal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Contract Intelligence Pipeline", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static UI assets (index.html, styles.css, app.js)
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# ── Temporal helper ────────────────────────────────────────────────────────────

async def _temporal_client() -> Client:
    host = os.getenv("TEMPORAL_HOST", "temporal")
    port = int(os.getenv("TEMPORAL_PORT", "7233"))
    return await Client.connect(f"{host}:{port}")


# ── Pydantic models ────────────────────────────────────────────────────────────

class ReviewDecision(BaseModel):
    action: str          # "approve" | "revise" | "escalate"
    notes: str
    reviewer_name: str
    reviewer_email: str


ALLOWED_REVIEW_ACTIONS = {"approve", "revise", "escalate"}


# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    os.makedirs("/app/data/uploads", exist_ok=True)
    os.makedirs("/app/data", exist_ok=True)
    logger.info("API server started.")


# ── Static files ───────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


# ── Contracts ──────────────────────────────────────────────────────────────────

@app.post("/api/contracts")
async def upload_contract(
    file: UploadFile = File(...),
    reviewer_email: str = Form(...),
):
    """
    Upload contract → save to disk → create DB record → start Temporal workflow.
    Returns contract_id and workflow_run_id immediately.
    """
    try:
        contract_id     = f"contract-{uuid.uuid4().hex[:8]}"
        workflow_run_id = f"wf-{uuid.uuid4().hex[:12]}"

        upload_dir = "/app/data/uploads"
        os.makedirs(upload_dir, exist_ok=True)
        file_path = f"{upload_dir}/{contract_id}"
        content = await file.read()
        with open(file_path, "wb") as f:
            f.write(content)

        upload_url = f"/api/files/{contract_id}"

        # Create DB record BEFORE starting workflow so the UI sees it immediately
        db.create_contract(
            contract_id=contract_id,
            name=file.filename,
            content="(binary — see upload)",
            upload_url=upload_url,
            reviewer_email=reviewer_email,
            workflow_run_id=workflow_run_id,
        )

        # Start Temporal workflow
        client = await _temporal_client()
        wf_input = ContractReviewInput(
            contract_id=contract_id,
            upload_url=upload_url,
            reviewer_email=reviewer_email,
            contract_name=file.filename,
        )
        await client.start_workflow(
            ContractReviewWorkflow.run,
            wf_input,
            id=workflow_run_id,
            task_queue="contract-review-tq",
        )

        logger.info(f"Workflow started: {workflow_run_id} for contract {contract_id}")
        return {
            "contract_id": contract_id,
            "workflow_run_id": workflow_run_id,
            "status": "ingesting",
        }

    except Exception as e:
        logger.exception(f"Failed to start workflow: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/contracts")
async def list_contracts():
    """Returns summary list of all contracts. Polled by the UI every 3 seconds."""
    contracts = db.list_contracts()
    return {
        "contracts": [
            {
                "contract_id":   c.contract_id,
                "workflow_run_id": c.workflow_run_id,
                "name":          c.name,
                "status":        c.status.value,
                "upload_date":   c.upload_date,
                "risk_score":    c.risk_score,
                "risk_label":    c.risk_label,
                "retry_count":   c.retry_count,
                "reviewer_email": c.reviewer_email,
            }
            for c in contracts
        ]
    }


@app.get("/api/contracts/{contract_id}")
async def get_contract(contract_id: str):
    """Returns full contract details including clauses, AI summary, timeline, reviews."""
    c = db.get_contract(contract_id)
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")

    return {
        "contract_id":    c.contract_id,
        "workflow_run_id": c.workflow_run_id,
        "name":           c.name,
        "status":         c.status.value,
        "upload_date":    c.upload_date,
        "reviewer_email": c.reviewer_email,
        # AI analysis
        "risk_score":  c.risk_score,
        "risk_label":  c.risk_label,
        "ai_summary":  c.ai_summary,
        "clauses":     c.clauses,
        # Human review history
        "reviews":     c.reviews,
        "retry_count": c.retry_count,
        # Audit trail
        "timeline":    c.timeline,
    }


# ── Human Review Signal ────────────────────────────────────────────────────────

@app.post("/api/review/{workflow_run_id}")
async def submit_review_decision(workflow_run_id: str, decision: ReviewDecision):
    """
    1. Persists the review in the DB immediately (so UI updates at once).
    2. Sends a Temporal signal to the paused workflow.
    """
    # Find the contract first
    contract = _find_contract_by_wf_run(workflow_run_id)
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found for this workflow run")

    if decision.action not in ALLOWED_REVIEW_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action '{decision.action}'. Allowed: approve, revise, escalate.",
        )

    normalized_action = decision.action
    normalized_notes = decision.notes
    if decision.action == "revise" and contract.retry_count >= 3:
        normalized_action = "escalate"
        normalized_notes = (
            f"{decision.notes}\nNo more revision attempts left (limit reached). Escalating to legal."
        )

    # Persist review immediately so the UI sees the new status without waiting
    db.save_review(
        contract_id=contract.contract_id,
        reviewer_name=decision.reviewer_name,
        action=normalized_action,
        notes=normalized_notes,
        reviewer_email=decision.reviewer_email,
    )

    # Then signal Temporal
    try:
        client = await _temporal_client()
        handle = client.get_workflow_handle(workflow_run_id)

        signal_payload = ReviewDecisionSignal(
            action=normalized_action,
            notes=normalized_notes,
            reviewer_name=decision.reviewer_name,
            reviewer_email=decision.reviewer_email,
        )
        await handle.signal("reviewDecision", signal_payload)

        logger.info(
            f"Signal sent to {workflow_run_id}: action={normalized_action} by {decision.reviewer_name}"
        )
        return {"status": "signal_sent", "action": normalized_action}

    except Exception as e:
        logger.error(f"Signal error for {workflow_run_id}: {e}")
        # DB already updated; just warn. Don't fail the whole request.
        return {"status": "db_updated_signal_failed", "error": str(e)}


# ── File download ──────────────────────────────────────────────────────────────

@app.get("/api/files/{contract_id}")
async def download_file(contract_id: str):
    path = f"/app/data/uploads/{contract_id}"
    if os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(status_code=404, detail="File not found")


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "contracts": len(db.list_contracts())}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _find_contract_by_wf_run(workflow_run_id: str):
    for c in db.list_contracts():
        if c.workflow_run_id == workflow_run_id:
            return c
    return None


# ── Dev entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=3000,
        reload=False,
        access_log=os.getenv("ACCESS_LOG", "false").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )