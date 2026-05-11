"""
worker/main.py
Temporal worker — listens on contract-review-tq task queue.
"""
import asyncio
import os
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from contract_worker.workflow import ContractReviewWorkflow
from contract_worker.activities import (
    ingest_contract,
    extract_clauses,
    score_risk,
    notify_reviewer,
    approve_contract,
    request_revision,
    escalate_to_legal,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


async def main():
    temporal_host = os.getenv("TEMPORAL_HOST", "temporal")
    temporal_port = int(os.getenv("TEMPORAL_PORT", "7233"))
    client = await Client.connect(f"{temporal_host}:{temporal_port}")

    worker = Worker(
        client,
        task_queue="contract-review-tq",
        workflows=[ContractReviewWorkflow],
        activities=[
            ingest_contract,
            extract_clauses,
            score_risk,
            notify_reviewer,
            approve_contract,
            request_revision,
            escalate_to_legal,
        ],
        max_concurrent_activities=5,
    )

    print("✅ Worker started — listening on contract-review-tq …")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())