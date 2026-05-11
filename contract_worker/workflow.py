"""
worker/workflow.py
Temporal workflow: ContractReviewWorkflow
Sequence: ingest → extract → score → notify → wait_for_signal (5m) → branch
On "revise": loops back to extract (max 3 retries)
On timeout: escalates to legal
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from contract_worker.activities import (
        ingest_contract,
        extract_clauses,
        score_risk,
        notify_reviewer,
        approve_contract,
        request_revision,
        escalate_to_legal,
    )
    from contract_worker.postgres_db import db, ReviewStatus

logger = logging.getLogger(__name__)


@dataclass
class ContractReviewInput:
    contract_id: str
    upload_url: str
    reviewer_email: str
    contract_name: str


@dataclass
class ReviewDecisionSignal:
    action: str           # "approve" | "revise" | "escalate"
    notes: str
    reviewer_name: str
    reviewer_email: str


@workflow.defn
class ContractReviewWorkflow:

    def __init__(self):
        self._decision: Optional[ReviewDecisionSignal] = None

    @workflow.signal
    async def reviewDecision(self, signal: ReviewDecisionSignal):
        workflow.logger.info(
            f"Signal received: action={signal.action} by {signal.reviewer_name}"
        )
        self._decision = signal

    @workflow.run
    async def run(self, inp: ContractReviewInput) -> dict:
        workflow.logger.info(f"▶  ContractReviewWorkflow started for {inp.contract_id}")

        with workflow.unsafe.imports_passed_through():
            db.set_status(inp.contract_id, ReviewStatus.INGESTING, "Ingesting contract file")

        # ── STEP 1: Ingest ─────────────────────────────────────────────────────
        contract_text: str = await workflow.execute_activity(
            ingest_contract,
            inp.upload_url,
            start_to_close_timeout=timedelta(seconds=60),
        )

        MAX_RETRIES = 3
        retry = 0
        revision_notes: Optional[str] = None

        while retry <= MAX_RETRIES:
            with workflow.unsafe.imports_passed_through():
                db.set_status(
                    inp.contract_id,
                    ReviewStatus.ANALYZING,
                    f"AI analysis — pass {retry + 1}",
                )

            # ── STEP 2: Extract clauses ────────────────────────────────────────
            clauses: list = await workflow.execute_activity(
                extract_clauses,
                args=[contract_text, revision_notes],
                start_to_close_timeout=timedelta(seconds=90),
            )

            # ── STEP 3: Score risk (saves to DB + sets status → pending_review)
            risk_score: int = await workflow.execute_activity(
                score_risk,
                args=[inp.contract_id, clauses],
                start_to_close_timeout=timedelta(seconds=90),
            )

            # ── STEP 4: Notify reviewer ────────────────────────────────────────
            await workflow.execute_activity(
                notify_reviewer,
                args=[{
                    "contract_id":    inp.contract_id,
                    "contract_name":  inp.contract_name,
                    "reviewer_email": inp.reviewer_email,
                    "risk_score":     risk_score,
                    "upload_url":     inp.upload_url,
                }],
                start_to_close_timeout=timedelta(seconds=30),
            )

            # ── PAUSE: wait for human signal (5 minutes) ───────────────────────
            self._decision = None

            try:
                await workflow.wait_condition(
                    lambda: self._decision is not None,
                    timeout=timedelta(minutes=5),
                )
            except asyncio.TimeoutError:
                workflow.logger.warning(
                    f"No reviewer response within 5m for {inp.contract_id}. Escalating."
                )
                
                await workflow.execute_activity(
                    escalate_to_legal,
                    args=[{
                        "contract_id":    inp.contract_id,
                        "contract_name":  inp.contract_name,
                        "reviewer_email": inp.reviewer_email,
                        "risk_score":     risk_score,
                    }],
                    start_to_close_timeout=timedelta(seconds=30),
                )
                return {"status": "escalated", "reason": "timeout", "risk_score": risk_score}

            decision = self._decision

            # ── BRANCH ────────────────────────────────────────────────────────
            if decision.action == "approve":
                await workflow.execute_activity(
                    approve_contract,
                    args=[{
                        "contract_id":    inp.contract_id,
                        "contract_name":  inp.contract_name,
                        "reviewer_email": inp.reviewer_email,
                        "reviewer_name":  decision.reviewer_name,
                        "risk_score":     risk_score,
                        "upload_url":     inp.upload_url,
                    }],
                    start_to_close_timeout=timedelta(seconds=30),
                )
                workflow.logger.info(f"✅ Contract {inp.contract_id} approved.")
                return {
                    "status":     "approved",
                    "risk_score": risk_score,
                    "reviewer":   decision.reviewer_name,
                }

            elif decision.action == "revise":
                retry += 1
                revision_notes = decision.notes
                workflow.logger.info(
                    f"🔄 Revision requested (attempt {retry}/{MAX_RETRIES})"
                )
                await workflow.execute_activity(
                    request_revision,
                    args=[{
                        "contract_id":    inp.contract_id,
                        "contract_name":  inp.contract_name,
                        "notes":          decision.notes,
                        "reviewer_email": decision.reviewer_email,
                        "reviewer_name":  decision.reviewer_name,
                        "risk_score":     risk_score,
                    }],
                    start_to_close_timeout=timedelta(seconds=30),
                )
                if retry > MAX_RETRIES:
                    workflow.logger.warning(
                        f"Max retries reached for {inp.contract_id}. Escalating."
                    )
                   
                    await workflow.execute_activity(
                        escalate_to_legal,
                        args=[{
                            "contract_id":    inp.contract_id,
                            "contract_name":  inp.contract_name,
                            "reviewer_email": decision.reviewer_email,
                            "reviewer_name":  decision.reviewer_name,
                            "risk_score":     risk_score,
                        }],
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                    return {
                        "status":  "escalated",
                        "reason":  "max_retries_exceeded",
                        "retries": retry,
                    }
                continue

            else:
                # action == "escalate" or unknown
                await workflow.execute_activity(
                    escalate_to_legal,
                    args=[{
                        "contract_id":    inp.contract_id,
                        "contract_name":  inp.contract_name,
                        "reviewer_email": decision.reviewer_email,
                        "reviewer_name":  decision.reviewer_name,
                        "risk_score":     risk_score,
                        "notes":          decision.notes,
                    }],
                    start_to_close_timeout=timedelta(seconds=30),
                )
                return {
                    "status": "escalated",
                    "reason": "manual",
                    "notes":  decision.notes,
                }

        return {"status": "unknown"}