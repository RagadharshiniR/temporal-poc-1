"""
test_api_server.py
"""

import sys
import types
import pytest
from fastapi.testclient import TestClient

# Async helper for signal method
async def _async_noop(*a, **kw):
    """Async no-op function for stubbing async methods."""
    pass

# Stub external modules before importing our code
sys.modules.setdefault("temporalio", types.ModuleType("temporalio"))
sys.modules.setdefault("temporalio.client", types.ModuleType("temporalio.client"))

class _FakeClient:
    @staticmethod
    async def connect(addr): return _FakeClient()
    async def start_workflow(self, *a, **kw): pass
    def get_workflow_handle(self, wf_id):
        obj = types.SimpleNamespace()
        obj.signal = _async_noop  # Must be async-compatible
        return obj

sys.modules["temporalio.client"].Client = _FakeClient

# Stub psycopg2
psycopg2_extras = sys.modules.setdefault("psycopg2.extras", types.ModuleType("psycopg2.extras"))
psycopg2_extras.Json = lambda v: v
psycopg2_extras.RealDictCursor = type("_RealDictCursor", (), {})
sys.modules.setdefault("psycopg2", types.ModuleType("psycopg2"))
sys.modules.setdefault("worker", types.ModuleType("worker"))

import contract_worker.postgres_db as pg_mod  # noqa: E402

# In-memory contract storage for testing
_CONTRACTS = {}


class _FakeContract:
    """Mock contract object matching DB schema."""
    def __init__(self, **kw):
        self.contract_id = kw.get("contract_id", "cid-001")
        self.workflow_run_id = kw.get("workflow_run_id", "wf-001")
        self.name = kw.get("name", "Contract.pdf")
        self.status = type("Status", (), {"value": kw.get("status", "ingesting")})()
        self.upload_date = kw.get("upload_date", "2025-01-01T00:00:00")
        self.risk_score = kw.get("risk_score", None)
        self.risk_label = kw.get("risk_label", None)
        self.retry_count = kw.get("retry_count", 0)
        self.reviewer_email = kw.get("reviewer_email", "r@test.com")
        self.ai_summary = kw.get("ai_summary", None)
        self.clauses = kw.get("clauses", [])
        self.reviews = kw.get("reviews", [])
        self.timeline = kw.get("timeline", [])


class _FakeDB:
    """In-memory mock of the database layer."""
    def create_contract(self, contract_id, name, content, upload_url,
                       reviewer_email, workflow_run_id=None):
        c = _FakeContract(contract_id=contract_id, workflow_run_id=workflow_run_id,
                         name=name, reviewer_email=reviewer_email)
        _CONTRACTS[contract_id] = c
        return c

    def get_contract(self, contract_id):
        return _CONTRACTS.get(contract_id)

    def list_contracts(self):
        return list(_CONTRACTS.values())

    def save_review(self, contract_id, reviewer_name, action, notes, reviewer_email=None):
        c = _CONTRACTS.get(contract_id)
        if c:
            c.reviews.append({"reviewer_name": reviewer_name, "action": action, "notes": notes})


pg_mod.db = _FakeDB()

# Stub contract_worker.workflow
wf_mod = types.ModuleType("contract_worker.workflow")
wf_mod.ContractReviewInput = type("ContractReviewInput", (), {})
wf_mod.ContractReviewWorkflow = type("ContractReviewWorkflow", (), {"run": lambda *a, **kw: None})
# ReviewDecisionSignal stub must accept arbitrary kwargs for signal instantiation
wf_mod.ReviewDecisionSignal = type("ReviewDecisionSignal", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["contract_worker.workflow"] = wf_mod

# Import app and TestClient
from api_server.main import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


# ─────────────────────────────────────────────────────────────────────────────
# Test Helpers
# ─────────────────────────────────────────────────────────────────────────────

def clear_contracts():
    """Clear the in-memory contract store."""
    _CONTRACTS.clear()


def add_contract(**kw) -> _FakeContract:
    """Add a fake contract to the in-memory store."""
    c = _FakeContract(**kw)
    _CONTRACTS[c.contract_id] = c
    return c


# ═══════════════════════════════════════════════════════════════════════════════
# Health endpoint
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthEndpoint:
    def setup_method(self):
        clear_contracts()

    def test_health_returns_200(self):
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_has_status_ok(self):
        r = client.get("/health")
        assert r.json()["status"] == "ok"

    def test_health_reports_contract_count(self):
        add_contract(contract_id="cid-h1")
        r = client.get("/health")
        assert r.json()["contracts"] >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/contracts
# ═══════════════════════════════════════════════════════════════════════════════

class TestListContracts:
    def setup_method(self):
        clear_contracts()

    def test_empty_list_returns_200(self):
        r = client.get("/api/contracts")
        assert r.status_code == 200

    def test_empty_list_contracts_key_is_list(self):
        r = client.get("/api/contracts")
        assert isinstance(r.json()["contracts"], list)

    def test_added_contract_appears_in_list(self):
        add_contract(contract_id="cid-001", name="Deal.pdf")
        r = client.get("/api/contracts")
        ids = [c["contract_id"] for c in r.json()["contracts"]]
        assert "cid-001" in ids

    def test_list_item_has_required_keys(self):
        add_contract(contract_id="cid-002")
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
        clear_contracts()

    def test_existing_contract_returns_200(self):
        add_contract(contract_id="cid-get1")
        assert client.get("/api/contracts/cid-get1").status_code == 200

    def test_missing_contract_returns_404(self):
        assert client.get("/api/contracts/does-not-exist").status_code == 404

    def test_response_has_all_expected_fields(self):
        add_contract(contract_id="cid-get2", name="NDA.pdf",
                    reviewer_email="alice@co.com")
        data = client.get("/api/contracts/cid-get2").json()
        for key in ["contract_id", "name", "status", "upload_date",
                    "reviewer_email", "risk_score", "risk_label",
                    "ai_summary", "clauses", "reviews", "timeline",
                    "retry_count"]:
            assert key in data, f"Missing key: {key}"

    def test_reviewer_email_matches(self):
        add_contract(contract_id="cid-get3", reviewer_email="bob@co.com")
        data = client.get("/api/contracts/cid-get3").json()
        assert data["reviewer_email"] == "bob@co.com"


# ═══════════════════════════════════════════════════════════════════════════════
# POST /api/review/{workflow_run_id}
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubmitReviewDecision:
    def setup_method(self):
        clear_contracts()

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
        add_contract(contract_id="cid-rv1", workflow_run_id="wf-rv1",
                    retry_count=0)
        r = self._post("wf-rv1", action="delete")
        assert r.status_code == 400

    def test_approve_returns_200(self):
        add_contract(contract_id="cid-rv2", workflow_run_id="wf-rv2",
                    retry_count=0)
        r = self._post("wf-rv2", action="approve")
        assert r.status_code == 200

    def test_revise_returns_200(self):
        add_contract(contract_id="cid-rv3", workflow_run_id="wf-rv3",
                    retry_count=0)
        r = self._post("wf-rv3", action="revise", notes="Fix clause 4")
        assert r.status_code == 200

    def test_escalate_returns_200(self):
        add_contract(contract_id="cid-rv4", workflow_run_id="wf-rv4",
                    retry_count=0)
        r = self._post("wf-rv4", action="escalate", notes="Too risky")
        assert r.status_code == 200

    def test_revise_beyond_retry_limit_becomes_escalate(self):
        """When retry_count >= 3, a 'revise' action must be normalized to 'escalate'."""
        add_contract(contract_id="cid-rv5", workflow_run_id="wf-rv5",
                    retry_count=3)
        r = self._post("wf-rv5", action="revise", notes="one more")
        data = r.json()
        assert r.status_code == 200
        assert data.get("action") == "escalate"


# ═══════════════════════════════════════════════════════════════════════════════
# ReviewDecision model validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestAllowedReviewActions:
    """Verify ALLOWED_REVIEW_ACTIONS constant covers exactly the right set."""

    def test_allowed_actions_set(self):
        from api_server.main import ALLOWED_REVIEW_ACTIONS
        assert ALLOWED_REVIEW_ACTIONS == {"approve", "revise", "escalate"}


# ═══════════════════════════════════════════════════════════════════════════════
# _find_contract_by_wf_run helper
# ═══════════════════════════════════════════════════════════════════════════════

class TestFindContractByWfRun:
    def setup_method(self):
        clear_contracts()

    def test_finds_existing(self):
        from api_server.main import _find_contract_by_wf_run
        add_contract(contract_id="cid-f1", workflow_run_id="wf-find1")
        c = _find_contract_by_wf_run("wf-find1")
        assert c is not None
        assert c.contract_id == "cid-f1"

    def test_returns_none_for_missing(self):
        from api_server.main import _find_contract_by_wf_run
        assert _find_contract_by_wf_run("wf-does-not-exist") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])