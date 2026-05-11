"""Shared Postgres-backed storage for contracts and review state."""
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional
from datetime import datetime

import psycopg2
from psycopg2.extras import Json, RealDictCursor


class ReviewStatus(str, Enum):
    INGESTING          = "ingesting"
    ANALYZING          = "analyzing"
    PENDING_REVIEW     = "pending_review"
    APPROVED           = "approved"
    REVISION_REQUESTED = "revision_requested"
    ESCALATED          = "escalated"


@dataclass
class Contract:
    contract_id:    str
    name:           str
    content:        str
    upload_url:     str
    reviewer_email: str
    upload_date:    str
    status:         ReviewStatus = ReviewStatus.INGESTING
    workflow_run_id: Optional[str] = None
    risk_score:     Optional[int] = None
    risk_label:     Optional[str] = None
    clauses:        List[dict] = field(default_factory=list)
    ai_summary:     Optional[str] = None
    reviews:        List[dict] = field(default_factory=list)
    timeline:       List[dict] = field(default_factory=list)
    retry_count:    int = 0


def _from_row(row: dict) -> Contract:
    return Contract(
        contract_id=row["contract_id"],
        name=row["name"],
        content=row.get("content", ""),
        upload_url=row["upload_url"],
        reviewer_email=row["reviewer_email"],
        upload_date=row["upload_date"].isoformat() if hasattr(row["upload_date"], "isoformat") else str(row["upload_date"]),
        status=ReviewStatus(row["status"]),
        workflow_run_id=row.get("workflow_run_id"),
        risk_score=row.get("risk_score"),
        risk_label=row.get("risk_label"),
        clauses=row.get("clauses") or [],
        ai_summary=row.get("ai_summary"),
        reviews=row.get("reviews") or [],
        timeline=row.get("timeline") or [],
        retry_count=row.get("retry_count") or 0,
    )


class PostgresDB:
    def __init__(self):
        self._schema_ready = False

    def _connect(self):
        return psycopg2.connect(
            host=os.getenv("DB_HOST", "postgres"),
            port=os.getenv("DB_PORT", "5432"),
            user=os.getenv("DB_USER", "temporal"),
            password=os.getenv("DB_PASSWORD", "temporal"),
            dbname=os.getenv("DB_NAME", "contracts"),
            connect_timeout=3,
        )

    def _ensure_schema(self):
        if self._schema_ready:
            return
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS contracts (
                        contract_id VARCHAR(64) PRIMARY KEY,
                        name TEXT NOT NULL,
                        content TEXT NOT NULL,
                        upload_url TEXT NOT NULL,
                        reviewer_email TEXT NOT NULL,
                        upload_date TIMESTAMP NOT NULL,
                        status TEXT NOT NULL,
                        workflow_run_id TEXT,
                        risk_score INTEGER,
                        risk_label TEXT,
                        clauses JSONB NOT NULL DEFAULT '[]'::jsonb,
                        ai_summary TEXT,
                        reviews JSONB NOT NULL DEFAULT '[]'::jsonb,
                        timeline JSONB NOT NULL DEFAULT '[]'::jsonb,
                        retry_count INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
            conn.commit()
            self._schema_ready = True
        finally:
            conn.close()

    # ── contracts ──────────────────────────────────────────────────────────────

    def create_contract(
        self,
        contract_id: str,
        name: str,
        content: str,
        upload_url: str,
        reviewer_email: str,
        workflow_run_id: Optional[str] = None,
    ) -> Contract:
        self._ensure_schema()
        now = datetime.utcnow()
        timeline = [{
            "type": "uploaded",
            "detail": "Contract uploaded and ingestion started",
            "ts": now.isoformat(),
        }]
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO contracts (
                        contract_id, name, content, upload_url, reviewer_email,
                        upload_date, status, workflow_run_id, timeline
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        contract_id, name, content, upload_url, reviewer_email,
                        now, ReviewStatus.INGESTING.value, workflow_run_id, Json(timeline),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        return Contract(
            contract_id=contract_id,
            name=name,
            content=content,
            upload_url=upload_url,
            reviewer_email=reviewer_email,
            upload_date=now.isoformat(),
            workflow_run_id=workflow_run_id,
            timeline=timeline,
        )

    def get_contract(self, contract_id: str) -> Optional[Contract]:
        self._ensure_schema()
        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM contracts WHERE contract_id = %s", (contract_id,))
                row = cur.fetchone()
                return _from_row(row) if row else None
        finally:
            conn.close()

    def list_contracts(self) -> List[Contract]:
        self._ensure_schema()
        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM contracts ORDER BY upload_date DESC")
                return [_from_row(r) for r in cur.fetchall()]
        finally:
            conn.close()

    # ── status ─────────────────────────────────────────────────────────────────

    def set_status(self, contract_id: str, status: ReviewStatus, note: str = ""):
        self._ensure_schema()
        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT timeline FROM contracts WHERE contract_id = %s", (contract_id,))
                row = cur.fetchone()
                if not row:
                    return
                timeline = row.get("timeline") or []
                timeline.append({
                    "type": status.value,
                    "detail": note or status.value,
                    "ts": datetime.utcnow().isoformat(),
                })
                cur.execute(
                    "UPDATE contracts SET status = %s, timeline = %s WHERE contract_id = %s",
                    (status.value, Json(timeline), contract_id),
                )
            conn.commit()
        finally:
            conn.close()

    # ── analysis ───────────────────────────────────────────────────────────────

    def save_analysis(
        self,
        contract_id: str,
        risk_score: int,
        clauses: List[dict],
        ai_summary: str,
    ):
        self._ensure_schema()
        label = _risk_label(risk_score)
        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT timeline FROM contracts WHERE contract_id = %s", (contract_id,))
                row = cur.fetchone()
                if not row:
                    return
                timeline = row.get("timeline") or []
                timeline.append({
                    "type": "ai_analysis_complete",
                    "detail": (
                        f"AI analysis complete. Risk score: {risk_score}/100 ({label}). "
                        f"{len(clauses)} clauses extracted."
                    ),
                    "ts": datetime.utcnow().isoformat(),
                })
                cur.execute(
                    """
                    UPDATE contracts
                    SET risk_score = %s,
                        risk_label = %s,
                        clauses = %s,
                        ai_summary = %s,
                        status = %s,
                        timeline = %s
                    WHERE contract_id = %s
                    """,
                    (
                        risk_score, label, Json(clauses), ai_summary,
                        ReviewStatus.PENDING_REVIEW.value, Json(timeline), contract_id,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    # ── human review ───────────────────────────────────────────────────────────

    def save_review(
        self,
        contract_id: str,
        reviewer_name: str,
        action: str,
        notes: str,
        reviewer_email: Optional[str] = None,
    ):
        self._ensure_schema()
        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT reviews, timeline, retry_count, reviewer_email FROM contracts WHERE contract_id = %s",
                    (contract_id,),
                )
                row = cur.fetchone()
                if not row:
                    return
                reviews = row.get("reviews") or []
                timeline = row.get("timeline") or []
                retry_count = row.get("retry_count") or 0
                review = {
                    "reviewer_name": reviewer_name,
                    "reviewer_email": reviewer_email or row.get("reviewer_email", ""),
                    "action": action,
                    "notes": notes,
                    "timestamp": datetime.utcnow().isoformat(),
                }
                reviews.append(review)
                timeline.append({
                    "type": f"human_{action}",
                    "detail": f"{reviewer_name}: {notes}",
                    "ts": datetime.utcnow().isoformat(),
                })
                next_status = None
                if action == "approve":
                    next_status = ReviewStatus.APPROVED.value
                elif action == "revise":
                    next_status = ReviewStatus.REVISION_REQUESTED.value
                    retry_count += 1
                elif action == "escalate":
                    next_status = ReviewStatus.ESCALATED.value

                cur.execute(
                    """
                    UPDATE contracts
                    SET reviews = %s,
                        timeline = %s,
                        status = COALESCE(%s, status),
                        retry_count = %s
                    WHERE contract_id = %s
                    """,
                    (Json(reviews), Json(timeline), next_status, retry_count, contract_id),
                )
            conn.commit()
        finally:
            conn.close()


def _risk_label(score: int) -> str:
    if score <= 30:
        return "low"
    if score <= 60:
        return "medium"
    return "high"


# Singleton
db = PostgresDB()