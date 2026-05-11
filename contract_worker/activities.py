import asyncio
import json
import logging
import os
import smtplib
from urllib import request
from datetime import datetime
from email.message import EmailMessage

import psycopg2

from temporalio import activity
from contract_worker.postgres_db import db, ReviewStatus
from contract_worker.gemini_analyzer import GeminiContractAnalyzer

logger = logging.getLogger(__name__)
analyzer = GeminiContractAnalyzer()


@activity.defn
async def ingest_contract(url: str) -> str:
    contract_id = (url or "").split("/")[-1]
    file_path = f"/app/data/uploads/{contract_id}"
    if os.path.exists(file_path):
        with open(file_path, "rb") as f:
            raw = f.read(200000)
        try:
            return raw.decode("utf-8", errors="ignore") or "Uploaded contract content unavailable."
        except Exception:
            return "Uploaded contract content unavailable."
    return "Sample contract text including indemnity, IP, payment, liability, and termination clauses."


@activity.defn
async def extract_clauses(text: str, revision_notes: str = None) -> list:
    merged_text = text
    if revision_notes:
        merged_text = f"{text}\n\nReviewer redline instructions:\n{revision_notes}"
        print(f"REVISION REDLINES APPLIED: {revision_notes}")
    clauses = analyzer.extract_clauses(merged_text)
    if not clauses:
        return analyzer._mock_extract_clauses(merged_text)
    return clauses


@activity.defn
async def score_risk(cid: str, clauses: list) -> int:
    contract = db.get_contract(cid)
    contract_text = contract.content if contract else ""
    score = analyzer.score_risk(contract_text, clauses, cid)
    summary = analyzer.generate_summary(contract_text, clauses, score)
    db.save_analysis(contract_id=cid, risk_score=score, clauses=clauses, ai_summary=summary)
    return score


@activity.defn
async def notify_reviewer(data: dict) -> bool:
    reviewer_email = data["reviewer_email"]
    contract_id = data["contract_id"]
    contract_name = data.get("contract_name", contract_id)
    risk_score = data.get("risk_score")
    review_link = f"http://localhost:3000/?contract={contract_id}"
    local_attachment = f"/app/data/uploads/{contract_id}"

    subject = f"Contract review requested: {contract_name}"
    body = (
        f"A contract is waiting for your review.\n\n"
        f"Contract: {contract_name}\n"
        f"Contract ID: {contract_id}\n"
        f"Risk Score: {risk_score if risk_score is not None else 'N/A'}/100\n"
        f"Review URL: {review_link}\n"
    )
    await asyncio.to_thread(_send_email, reviewer_email, subject, body, local_attachment)
    await asyncio.to_thread(
        _notify_slack,
        event="review_requested",
        contract_id=contract_id,
        contract_name=contract_name,
        reviewer_email=reviewer_email,
        risk_score=risk_score,
        review_link=review_link,
        local_attachment=local_attachment,
        notes=None,
    )
    return True


@activity.defn
async def approve_contract(data: dict) -> bool:
    cid = data["contract_id"]
    contract_name = data.get("contract_name", cid)
    reviewer_email = data.get("reviewer_email", "")
    reviewer_name = data.get("reviewer_name", "Reviewer")
    risk_score = data.get("risk_score")
    local_attachment = f"/app/data/uploads/{cid}"

    _write_approval_record_to_postgres(cid)
    _fire_docusign_stub(cid)
    db.set_status(cid, ReviewStatus.APPROVED, "User approved")

    subject = f"Contract approved: {contract_name}"
    body = (
        f"Your contract has been approved.\n\n"
        f"Contract: {contract_name}\n"
        f"Contract ID: {cid}\n"
        f"Risk Score: {risk_score if risk_score is not None else 'N/A'}/100\n"
    )
    await asyncio.to_thread(_send_email, reviewer_email, subject, body, local_attachment)
    await asyncio.to_thread(
        _notify_slack,
        event="approved",
        contract_id=cid,
        contract_name=contract_name,
        reviewer_email=reviewer_email,
        reviewer_name=reviewer_name,
        risk_score=risk_score,
        review_link=f"http://localhost:3000/?contract={cid}",
        local_attachment=local_attachment,
        notes=None,
    )
    return True


@activity.defn
async def request_revision(data: dict) -> bool:
    cid = data["contract_id"]
    notes = data["notes"]
    reviewer_email = data.get("reviewer_email", "")
    reviewer_name = data.get("reviewer_name", "Reviewer")
    contract_name = data.get("contract_name", cid)
    risk_score = data.get("risk_score")
    local_attachment = f"/app/data/uploads/{cid}"

    print(
        f"EMAIL WITH REDLINES SENT: to={reviewer_email}, "
        f"contract={cid}, reviewer={reviewer_name}, redlines={notes}"
    )
    db.set_status(cid, ReviewStatus.REVISION_REQUESTED, f"Revision: {notes}")

    await asyncio.sleep(1)
    db.set_status(cid, ReviewStatus.ANALYZING, "Restarting AI Loop")
    return True


@activity.defn
async def escalate_to_legal(data) -> bool:
   
    if isinstance(data, str):
        cid = data
        contract_name = cid
        reviewer_email = ""
        reviewer_name = ""
        risk_score = None
    else:
        cid = data.get("contract_id", data) if isinstance(data, dict) else data
        contract_name = data.get("contract_name", cid)
        reviewer_email = data.get("reviewer_email", "")
        reviewer_name = data.get("reviewer_name", "")
        risk_score = data.get("risk_score")

    jira_id = f"LEGAL-{cid[-4:].upper()}"
    local_attachment = f"/app/data/uploads/{cid}"
    print(f"JIRA TICKET CREATED: {jira_id} for contract {cid}")
    db.set_status(cid, ReviewStatus.ESCALATED, f"Escalated to Legal. Ticket: {jira_id}")

    await asyncio.to_thread(
        _notify_slack,
        event="escalated",
        contract_id=cid,
        contract_name=contract_name,
        reviewer_email=reviewer_email,
        reviewer_name=reviewer_name,
        risk_score=risk_score,
        review_link=f"http://localhost:3000/?contract={cid}",
        local_attachment=local_attachment,
        notes=f"Jira ticket created: {jira_id}",
        jira_id=jira_id,
    )
    return True


# ── Helpers ────────────────────────────────────────────────────────────────────

def _risk_emoji(score) -> str:
    if score is None:
        return "⬜"
    if score <= 30:
        return "🟢"
    if score <= 60:
        return "🟡"
    return "🔴"


def _risk_label(score) -> str:
    if score is None:
        return "N/A"
    if score <= 30:
        return "Low"
    if score <= 60:
        return "Medium"
    return "High"


def _risk_color(score) -> str:
    """Slack attachment sidebar color."""
    if score is None:
        return "#888888"
    if score <= 30:
        return "#1fa86a"
    if score <= 60:
        return "#c07b1a"
    return "#b83040"


def _build_slack_blocks(
    event: str,
    contract_id: str,
    contract_name: str,
    reviewer_email: str,
    risk_score,
    review_link: str,
    notes: str = None,
    reviewer_name: str = None,
    jira_id: str = None,
) -> dict:
    """
    Build a rich Slack Block Kit payload for different contract lifecycle events.
    Returns a dict with 'blocks' and 'attachments' keys ready for chat.postMessage or webhook.
    """
    risk_emoji = _risk_emoji(risk_score)
    risk_lbl   = _risk_label(risk_score)
    risk_color = _risk_color(risk_score)
    score_str  = f"{risk_score}/100" if risk_score is not None else "N/A"
    short_id   = contract_id[-8:].upper()
    now_str    = datetime.utcnow().strftime("%b %d, %Y · %H:%M UTC")

    # ── Event-specific header copy ──
    if event == "review_requested":
        header_emoji = "📋"
        header_text  = "Contract Review Requested"
        subtext      = f"A contract is awaiting human review. Please examine the AI analysis and make a decision."
        accent_color = "#2562c8"
        cta_text     = "Open Review Dashboard"
        cta_url      = review_link
    elif event == "approved":
        header_emoji = "✅"
        header_text  = "Contract Approved"
        subtext      = f"The contract has been reviewed and *approved* by {reviewer_name or reviewer_email}."
        accent_color = "#1fa86a"
        cta_text     = "View Approved Contract"
        cta_url      = review_link
    elif event == "revision_requested":
        header_emoji = "🔄"
        header_text  = "Revision Requested"
        subtext      = f"The reviewer has requested changes. The AI is re-analyzing with the redline instructions."
        accent_color = "#c07b1a"
        cta_text     = "View Contract"
        cta_url      = review_link
    elif event == "escalated":
        header_emoji = "⚠️"
        header_text  = "Escalated to Legal Counsel"
        subtext      = f"This contract has been escalated and requires legal review."
        accent_color = "#b83040"
        cta_text     = "View Escalated Contract"
        cta_url      = review_link
    else:
        header_emoji = "📄"
        header_text  = "Contract Update"
        subtext      = ""
        accent_color = "#888888"
        cta_text     = "View Contract"
        cta_url      = review_link

    # ── Build blocks ──
    blocks = [
        # Header
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{header_emoji}  {header_text}",
                "emoji": True,
            },
        },
        # Subtext context
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": subtext,
            },
        },
        {"type": "divider"},
        # Contract details — two-column fields
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*📄 Contract*\n`{contract_name}`",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*🔑 Contract ID*\n`{short_id}`",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*{risk_emoji} Risk Score*\n*{score_str}* — {risk_lbl} Risk",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*👤 Reviewer*\n{reviewer_email or 'N/A'}",
                },
            ],
        },
    ]

    # ── Revision notes block ──
    if notes and event == "revision_requested":
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*📝 Reviewer Notes & Redlines*\n>{notes}",
            },
        })

    # ── Jira ticket block ──
    if jira_id and event == "escalated":
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*🎫 Jira Ticket*\n`{jira_id}`",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*📨 Assigned To*\nLegal Team",
                },
            ],
        })

    # ── CTA button + timestamp ──
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": cta_text, "emoji": True},
                "url": cta_url,
                "style": "primary" if event in ("review_requested",) else None,
            },
            *(
                [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": f"🎫 View Jira {jira_id}", "emoji": True},
                        "url": f"https://demo-jira.atlassian.net/browse/{jira_id}", #if needed, change to the actual Jira URL
                    }
                ]
                if jira_id else []
            ),
        ],
    })

    for block in blocks:
        if block.get("type") == "actions":
            for el in block.get("elements", []):
                if el.get("style") is None:
                    el.pop("style", None)

    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": f"ContractIQ · {now_str}",
            }
        ],
    })

    # ── Attachment (sidebar color accent) ──
    attachments = [
        {
            "color": accent_color,
            "fallback": f"{header_text} — {contract_name} ({score_str})",
        }
    ]

    return {"blocks": blocks, "attachments": attachments}


def _notify_slack(
    event: str,
    contract_id: str,
    contract_name: str,
    reviewer_email: str,
    risk_score,
    review_link: str,
    local_attachment: str,
    notes: str = None,
    reviewer_name: str = None,
    jira_id: str = None,
) -> None:
    payload_dict = _build_slack_blocks(
        event=event,
        contract_id=contract_id,
        contract_name=contract_name,
        reviewer_email=reviewer_email,
        risk_score=risk_score,
        review_link=review_link,
        notes=notes,
        reviewer_name=reviewer_name,
        jira_id=jira_id,
    )

    # ── Webhook path (supports blocks) ──
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if webhook:
        webhook_payload = {
            "blocks": payload_dict["blocks"],
            "attachments": payload_dict["attachments"],
        }
        try:
            req = request.Request(
                webhook,
                data=json.dumps(webhook_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(req, timeout=8):
                pass
            logger.info("SLACK BLOCK KIT WEBHOOK POSTED: event=%s contract=%s", event, contract_id)
        except Exception as exc:
            logger.warning("SLACK WEBHOOK FAILED: %s", exc)
            logger.info("SLACK WEBHOOK MOCK: event=%s contract=%s", event, contract_id)
    else:
        logger.info("SLACK WEBHOOK MOCK (no URL set): event=%s contract=%s", event, contract_id)


def _write_approval_record_to_postgres(contract_id: str) -> None:
    conn = None
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "postgres"),
            port=os.getenv("DB_PORT", "5432"),
            user=os.getenv("DB_USER", "temporal"),
            password=os.getenv("DB_PASSWORD", "temporal"),
            dbname=os.getenv("DB_NAME", "contracts"),
            connect_timeout=3,
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id SERIAL PRIMARY KEY,
                    contract_id VARCHAR(64) NOT NULL,
                    approved_at TIMESTAMP NOT NULL,
                    source VARCHAR(32) NOT NULL
                )
                """
            )
            cur.execute(
                """
                INSERT INTO approvals (contract_id, approved_at, source)
                VALUES (%s, %s, %s)
                """,
                (contract_id, datetime.utcnow(), "temporal-worker"),
            )
        conn.commit()
        logger.info("POSTGRES APPROVAL WRITE OK: contract_id=%s", contract_id)
    except Exception as exc:
        logger.warning("POSTGRES APPROVAL WRITE FAILED for %s: %s", contract_id, exc)
    finally:
        if conn:
            conn.close()


def _fire_docusign_stub(contract_id: str) -> None:
    logger.info("DOCUSIGN STUB FIRED: envelope_created_for=%s", contract_id)


def _send_email(to_email: str, subject: str, body: str, attachment_path: str = "") -> None:
    try:
        if not to_email:
            logger.info("EMAIL MOCK (no recipient): subject=%s", subject)
            return

        smtp_host     = os.getenv("SMTP_HOST", "").strip()
        smtp_port     = int(os.getenv("SMTP_PORT", "587"))
        smtp_user     = os.getenv("SMTP_USER", "").strip()
        smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
        smtp_from     = os.getenv("SMTP_FROM_EMAIL", smtp_user or "noreply@contractiq.local")
        smtp_starttls = os.getenv("SMTP_STARTTLS", "true").lower() == "true"

        msg = EmailMessage()
        msg["From"]    = smtp_from
        msg["To"]      = to_email
        msg["Subject"] = subject
        msg.set_content(body)

        if attachment_path and os.path.exists(attachment_path):
            with open(attachment_path, "rb") as f:
                msg.add_attachment(
                    f.read(),
                    maintype="application",
                    subtype="pdf",
                    filename=os.path.basename(attachment_path) + ".pdf",
                )

        if not smtp_host:
            logger.info("EMAIL MOCK SENT: to=%s subject=%s", to_email, subject)
            return

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            if smtp_starttls:
                server.starttls()
            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)
            server.send_message(msg)
        logger.info("EMAIL SENT: to=%s subject=%s", to_email, subject)
    except Exception as exc:
        logger.warning("EMAIL SEND FAILED, falling back to mock log: %s", exc)
        logger.info("EMAIL MOCK SENT: to=%s subject=%s", to_email, subject)