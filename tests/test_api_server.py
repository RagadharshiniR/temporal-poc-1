"""
test_api_server.py
Unit / integration tests for api_server/main.py (FastAPI).

Pure-unit tests run with no infrastructure (Postgres / Temporal / filesystem).
Integration tests require a running stack — they are skipped automatically when
the stack is not available.

Run pure-unit tests only (CI-friendly):
    pytest test_api_server.py -v -m "not integration"

Run everything (requires full stack):
    DB_HOST=localhost TEMPORAL_HOST=localhost pytest test_api_server.py -v
"""

import os
import sys
import types
import uuid
import pytest


# ── Stub heavy dependencies before importing the app ──────────────────────────

# temporalio stubs
for mod_name in [
    "temporalio",
    "temporalio.client",
]:
    sys.modules.setdefault(mod_name, types.ModuleType(mod_name))

# Fake Temporal Client
class _FakeWorkflowHandle:
    async def signal(self, *a, **kw): pass

class _FakeClient:
    @staticmethod
    async def connect(addr): return _FakeClient()
    async def start_workflow(self, *a, **kw): pass
    def get_workflow_handle(self, wf_id): return _FakeWorkflowHandle()

sys.modules["temporalio.client"].Client = _FakeClient

# psycopg2 stubs
psycopg2_mod = sys.modules.setdefault("psycopg2", types.ModuleType("psycopg2"))
psycopg2_extras_mod = sys.modules.setdefault("psycopg2.extras", types.ModuleType("psycopg2.extras"))
# Minimal names required for importing the real contract_worker.postgres_db module.
# The tests patch the module-level `db` instance to avoid any real DB access.
if not hasattr(psycopg2_extras_mod, "Json"):
    psycopg2_extras_mod.Json = lambda v: v
if not hasattr(psycopg2_extras_mod, "RealDictCursor"):
    class _RealDictCursor:  # pragma: no cover
        pass
    psycopg2_extras_mod.RealDictCursor = _RealDictCursor

# worker package stubs
worker_mod = types.ModuleType("worker")
sys.modules.setdefault("worker", worker_mod)

# Import the real module, then patch its `db` singleton to a fake.
import contract_worker.postgres_db as pg_mod  # noqa: E402

class _FakeContract:
    def __init__(self, **kw):
        self.contract_id    = kw.get("contract_id",    "cid-001")
        self.workflow_run_id = kw.get("workflow_run_id","wf-001")
        self.name           = kw.get("name",           "Contract.pdf")
        self.status         = type("S", (), {"value": kw.get("status", "ingesting")})()
        self.upload_date    = kw.get("upload_date",    "2025-01-01T00:00:00")
        self.risk_score     = kw.get("risk_score",     None)
        self.risk_label     = kw.get("risk_label",     None)
        self.retry_count    = kw.get("retry_count",    0)
        self.reviewer_email = kw.get("reviewer_email", "r@test.com")
        self.ai_summary     = kw.get("ai_summary",     None)
        self.clauses        = kw.get("clauses",        [])
        self.reviews        = kw.get("reviews",        [])
        self.timeline       = kw.get("timeline",       [])

_CONTRACTS: dict = {}   # in-memory store for fake DB


class _FakeDB:
    def create_contract(self, contract_id, name, content, upload_url,
                        reviewer_email, workflow_run_id=None):
        c = _FakeContract(
            contract_id=contract_id,
            workflow_run_id=workflow_run_id,
            name=name,
            reviewer_email=reviewer_email,
        )
        _CONTRACTS[contract_id] = c
        return c

    def get_contract(self, contract_id):
        return _CONTRACTS.get(contract_id)

    def list_contracts(self):
        return list(_CONTRACTS.values())

    def save_review(self, contract_id, reviewer_name, action, notes,
                    reviewer_email=None):
        c = _CONTRACTS.get(contract_id)
        if c:
            c.reviews.append({
                "reviewer_name": reviewer_name,
                "action": action,
                "notes": notes,
            })


pg_mod.db = _FakeDB()

# contract_worker.workflow stub
wf_mod = types.ModuleType("contract_worker.workflow")

class ContractReviewInput:
    def __init__(self, **kw): pass

class ContractReviewWorkflow:
    @staticmethod
    async def run(*a, **kw): pass

class ReviewDecisionSignal:
    def __init__(self, **kw): pass

wf_mod.ContractReviewInput  = ContractReviewInput
wf_mod.ContractReviewWorkflow = ContractReviewWorkflow
wf_mod.ReviewDecisionSignal = ReviewDecisionSignal
sys.modules["contract_worker.workflow"] = wf_mod

# ── Now import the FastAPI app ────────────────────────────────────────────────
from fastapi.testclient import TestClient  # noqa: E402
from api_server.main import app            # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clear_contracts():
    _CONTRACTS.clear()


def _add_contract(**kw) -> _FakeContract:
    c = _FakeContract(**kw)
    _CONTRACTS[c.contract_id] = c
    return c


# ═══════════════════════════════════════════════════════════════════════════════
# Health endpoint
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthEndpoint:
    def setup_method(self):
        _clear_contracts()

    def test_health_returns_200(self):
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_has_status_ok(self):
        r = client.get("/health")
        assert r.json()["status"] == "ok"

    def test_health_reports_contract_count(self):
        _add_contract(contract_id="cid-h1")
        r = client.get("/health")
        assert r.json()["contracts"] >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/contracts
# ═══════════════════════════════════════════════════════════════════════════════

class TestListContracts:
    def setup_method(self):
        _clear_contracts()

    def test_empty_list_returns_200(self):
        r = client.get("/api/contracts")
        assert r.status_code == 200

    def test_empty_list_contracts_key_is_list(self):
        r = client.get("/api/contracts")
        assert isinstance(r.json()["contracts"], list)

    def test_added_contract_appears_in_list(self):
        _add_contract(contract_id="cid-001", name="Deal.pdf")
        r = client.get("/api/contracts")
        ids = [c["contract_id"] for c in r.json()["contracts"]]
        assert "cid-001" in ids

    def test_list_item_has_required_keys(self):
        _add_contract(contract_id="cid-002")
        item = client.get("/api/contracts").json()["contracts"][0]
        for key in ["contract_id", "workflow_run_id", "name", "status",
                    "upload_date", "risk_score", "risk_label",
                    "retry_count", "reviewer_email"]:
            assert key in item, f"Missing key: {key}"


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/contracts/{contract_id}
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetContract:
    def setup_method(self):
        _clear_contracts()

    def test_existing_contract_returns_200(self):
        _add_contract(contract_id="cid-get1")
        assert client.get("/api/contracts/cid-get1").status_code == 200

    def test_missing_contract_returns_404(self):
        assert client.get("/api/contracts/does-not-exist").status_code == 404

    def test_response_has_all_expected_fields(self):
        _add_contract(contract_id="cid-get2", name="NDA.pdf",
                      reviewer_email="alice@co.com")
        data = client.get("/api/contracts/cid-get2").json()
        for key in ["contract_id", "name", "status", "upload_date",
                    "reviewer_email", "risk_score", "risk_label",
                    "ai_summary", "clauses", "reviews", "timeline",
                    "retry_count"]:
            assert key in data, f"Missing key: {key}"

    def test_reviewer_email_matches(self):
        _add_contract(contract_id="cid-get3", reviewer_email="bob@co.com")
        data = client.get("/api/contracts/cid-get3").json()
        assert data["reviewer_email"] == "bob@co.com"


# ═══════════════════════════════════════════════════════════════════════════════
# POST /api/review/{workflow_run_id}
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubmitReviewDecision:
    def setup_method(self):
        _clear_contracts()

    def _payload(self, action="approve", notes="LGTM",
                 reviewer_name="Alice", reviewer_email="alice@co.com"):
        return dict(action=action, notes=notes,
                    reviewer_name=reviewer_name,
                    reviewer_email=reviewer_email)

    def _post(self, wf_run_id, **kw):
        return client.post(f"/api/review/{wf_run_id}",
                           json=self._payload(**kw))

    def test_unknown_workflow_returns_404(self):
        r = self._post("wf-nonexistent")
        assert r.status_code == 404

    def test_invalid_action_returns_400(self):
        _add_contract(contract_id="cid-rv1", workflow_run_id="wf-rv1",
                      retry_count=0)
        r = self._post("wf-rv1", action="delete")
        assert r.status_code == 400

    def test_approve_returns_200(self):
        _add_contract(contract_id="cid-rv2", workflow_run_id="wf-rv2",
                      retry_count=0)
        r = self._post("wf-rv2", action="approve")
        assert r.status_code == 200

    def test_revise_returns_200(self):
        _add_contract(contract_id="cid-rv3", workflow_run_id="wf-rv3",
                      retry_count=0)
        r = self._post("wf-rv3", action="revise", notes="Fix clause 4")
        assert r.status_code == 200

    def test_escalate_returns_200(self):
        _add_contract(contract_id="cid-rv4", workflow_run_id="wf-rv4",
                      retry_count=0)
        r = self._post("wf-rv4", action="escalate", notes="Too risky")
        assert r.status_code == 200

    def test_revise_beyond_retry_limit_becomes_escalate(self):
        """When retry_count >= 3, a 'revise' action must be normalized to 'escalate'."""
        _add_contract(contract_id="cid-rv5", workflow_run_id="wf-rv5",
                      retry_count=3)
        r = self._post("wf-rv5", action="revise", notes="one more")
        data = r.json()
        assert r.status_code == 200
        assert data.get("action") == "escalate"

    def test_all_valid_actions_accepted(self):
        for i, action in enumerate(["approve", "revise", "escalate"]):
            cid = f"cid-rv-all{i}"
            wf  = f"wf-rv-all{i}"
            _add_contract(contract_id=cid, workflow_run_id=wf, retry_count=0)
            r = self._post(wf, action=action)
            assert r.status_code == 200, f"Failed for action={action}"


# ═══════════════════════════════════════════════════════════════════════════════
# ReviewDecision model validation (allowed actions)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAllowedReviewActions:
    """Verify the ALLOWED_REVIEW_ACTIONS constant covers exactly the right set."""

    def test_allowed_actions_set(self):
        from api_server.main import ALLOWED_REVIEW_ACTIONS
        assert ALLOWED_REVIEW_ACTIONS == {"approve", "revise", "escalate"}

    def test_approve_is_allowed(self):
        from api_server.main import ALLOWED_REVIEW_ACTIONS
        assert "approve" in ALLOWED_REVIEW_ACTIONS

    def test_delete_is_not_allowed(self):
        from api_server.main import ALLOWED_REVIEW_ACTIONS
        assert "delete" not in ALLOWED_REVIEW_ACTIONS


# ═══════════════════════════════════════════════════════════════════════════════
# _find_contract_by_wf_run helper
# ═══════════════════════════════════════════════════════════════════════════════

class TestFindContractByWfRun:
    def setup_method(self):
        _clear_contracts()

    def test_finds_existing(self):
        from api_server.main import _find_contract_by_wf_run
        _add_contract(contract_id="cid-f1", workflow_run_id="wf-find1")
        c = _find_contract_by_wf_run("wf-find1")
        assert c is not None
        assert c.contract_id == "cid-f1"

    def test_returns_none_for_missing(self):
        from api_server.main import _find_contract_by_wf_run
        assert _find_contract_by_wf_run("wf-does-not-exist") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])