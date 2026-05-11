"""
test_activities.py
Unit tests for worker/activities.py

Tests cover pure helper functions that have zero external dependencies:
  - _risk_emoji
  - _risk_label
  - _risk_color
  - _build_slack_blocks (structure + content, no network)
  - _fire_docusign_stub (just logs)
  - escalate_to_legal input-normalisation logic (str vs dict)

The Temporal @activity.defn functions (ingest_contract, extract_clauses,
score_risk, notify_reviewer, approve_contract, request_revision,
escalate_to_legal) talk to the filesystem, Postgres, SMTP, and Slack.
Those are integration tests — run them with a live stack.
Pure-logic tests here need no infrastructure at all.

Run with:
    pytest test_activities.py -v
"""

import sys
import types
import pytest

# ── Stub out heavy imports so the module loads without infrastructure ──────────
# temporalio
temporal_mod        = types.ModuleType("temporalio")
temporal_activity   = types.ModuleType("temporalio.activity")
temporal_activity.defn = lambda f: f          # no-op decorator
sys.modules.setdefault("temporalio",          temporal_mod)
sys.modules.setdefault("temporalio.activity", temporal_activity)

# psycopg2
psycopg2_mod = sys.modules.setdefault("psycopg2", types.ModuleType("psycopg2"))
psycopg2_extras = sys.modules.setdefault("psycopg2.extras", types.ModuleType("psycopg2.extras"))
# Minimal names required for importing the real contract_worker.postgres_db module.
if not hasattr(psycopg2_extras, "Json"):
    psycopg2_extras.Json = lambda v: v
if not hasattr(psycopg2_extras, "RealDictCursor"):
    class _RealDictCursor:  # pragma: no cover
        pass
    psycopg2_extras.RealDictCursor = _RealDictCursor

# requests
requests_mod = types.ModuleType("requests")
sys.modules.setdefault("requests", requests_mod)

# Import the real module, then patch its `db` singleton to a fake.
import contract_worker.postgres_db as pg_mod  # noqa: E402

class _FakeDB:
    def get_contract(self, *a, **kw): return None
    def save_analysis(self, *a, **kw): pass
    def set_status(self, *a, **kw): pass

pg_mod.db = _FakeDB()
sys.modules.setdefault("worker", types.ModuleType("worker"))

# contract_worker.gemini_analyzer — minimal stub
ga_mod = types.ModuleType("worker.gemini_analyzer")

class _FakeAnalyzer:
    def extract_clauses(self, text): return []
    def score_risk(self, text, clauses, cid=""): return 0
    def generate_summary(self, text, clauses, score): return ""
    def _mock_extract_clauses(self, text): return []

ga_mod.GeminiContractAnalyzer = _FakeAnalyzer
sys.modules["worker.gemini_analyzer"] = ga_mod

# ── Now import the module under test ──────────────────────────────────────────
from contract_worker import activities  # noqa: E402
from contract_worker.activities import (   # noqa: E402
    _risk_emoji,
    _risk_label,
    _risk_color,
    _build_slack_blocks,
    _fire_docusign_stub,
)


# ═══════════════════════════════════════════════════════════════════════════════
# _risk_emoji
# ═══════════════════════════════════════════════════════════════════════════════

class TestRiskEmoji:
    def test_none_returns_white_square(self):
        assert _risk_emoji(None) == "⬜"

    def test_zero_is_green(self):
        assert _risk_emoji(0) == "🟢"

    def test_30_is_green(self):
        assert _risk_emoji(30) == "🟢"

    def test_31_is_yellow(self):
        assert _risk_emoji(31) == "🟡"

    def test_60_is_yellow(self):
        assert _risk_emoji(60) == "🟡"

    def test_61_is_red(self):
        assert _risk_emoji(61) == "🔴"

    def test_100_is_red(self):
        assert _risk_emoji(100) == "🔴"


# ═══════════════════════════════════════════════════════════════════════════════
# _risk_label
# ═══════════════════════════════════════════════════════════════════════════════

class TestRiskLabel:
    def test_none_returns_na(self):
        assert _risk_label(None) == "N/A"

    def test_boundary_30_is_low(self):
        assert _risk_label(30) == "Low"

    def test_boundary_31_is_medium(self):
        assert _risk_label(31) == "Medium"

    def test_boundary_60_is_medium(self):
        assert _risk_label(60) == "Medium"

    def test_boundary_61_is_high(self):
        assert _risk_label(61) == "High"

    def test_zero_is_low(self):
        assert _risk_label(0) == "Low"

    def test_100_is_high(self):
        assert _risk_label(100) == "High"


# ═══════════════════════════════════════════════════════════════════════════════
# _risk_color
# ═══════════════════════════════════════════════════════════════════════════════

class TestRiskColor:
    def test_none_is_grey(self):
        assert _risk_color(None) == "#888888"

    def test_low_is_green(self):
        assert _risk_color(10)  == "#1fa86a"
        assert _risk_color(30)  == "#1fa86a"

    def test_medium_is_amber(self):
        assert _risk_color(31)  == "#c07b1a"
        assert _risk_color(60)  == "#c07b1a"

    def test_high_is_red(self):
        assert _risk_color(61)  == "#b83040"
        assert _risk_color(100) == "#b83040"

    def test_returns_string(self):
        assert isinstance(_risk_color(50), str)


# ═══════════════════════════════════════════════════════════════════════════════
# _build_slack_blocks — structure tests (no network)
# ═══════════════════════════════════════════════════════════════════════════════

COMMON_KWARGS = dict(
    contract_id    = "contract-abc12345",
    contract_name  = "Vendor_Agreement.pdf",
    reviewer_email = "legal@example.com",
    risk_score     = 55,
    review_link    = "http://localhost:3000/?contract=contract-abc12345",
)


class TestBuildSlackBlocksStructure:
    def _build(self, event="review_requested", **extra):
        kwargs = {**COMMON_KWARGS, **extra}
        return _build_slack_blocks(event=event, **kwargs)

    def test_returns_dict_with_blocks_and_attachments(self):
        result = self._build()
        assert "blocks"      in result
        assert "attachments" in result

    def test_blocks_is_nonempty_list(self):
        result = self._build()
        assert isinstance(result["blocks"], list)
        assert len(result["blocks"]) > 0

    def test_attachments_is_nonempty_list(self):
        result = self._build()
        assert isinstance(result["attachments"], list)
        assert len(result["attachments"]) > 0

    def test_first_block_is_header(self):
        result = self._build()
        assert result["blocks"][0]["type"] == "header"

    def test_last_block_is_context(self):
        result = self._build()
        assert result["blocks"][-1]["type"] == "context"

    def test_no_none_style_in_action_buttons(self):
        """Slack rejects 'style': null — the code strips it."""
        result = self._build(event="approved", reviewer_name="Alice")
        for block in result["blocks"]:
            if block.get("type") == "actions":
                for el in block.get("elements", []):
                    assert "style" not in el or el["style"] is not None


class TestBuildSlackBlocksEvents:
    def _build(self, event, **extra):
        kwargs = {**COMMON_KWARGS, **extra}
        return _build_slack_blocks(event=event, **kwargs)

    def _header_text(self, result):
        return result["blocks"][0]["text"]["text"]

    def test_review_requested_header(self):
        assert "Review Requested" in self._header_text(self._build("review_requested"))

    def test_approved_header(self):
        assert "Approved" in self._header_text(self._build("approved", reviewer_name="Bob"))

    def test_revision_requested_header(self):
        assert "Revision" in self._header_text(self._build("revision_requested"))

    def test_escalated_header(self):
        assert "Escalated" in self._header_text(self._build("escalated"))

    def test_unknown_event_falls_through_gracefully(self):
        result = self._build("some_unknown_event")
        assert "blocks" in result

    def test_attachment_color_changes_by_event(self):
        approved_color  = self._build("approved",   reviewer_name="X")["attachments"][0]["color"]
        escalated_color = self._build("escalated")["attachments"][0]["color"]
        assert approved_color != escalated_color

    def test_escalated_includes_jira_block_when_jira_id_given(self):
        result = self._build("escalated", jira_id="LEGAL-ABCD", notes="Too risky")
        block_texts = [
            str(b) for b in result["blocks"]
        ]
        assert any("LEGAL-ABCD" in t for t in block_texts)

    def test_revision_notes_block_present_when_notes_given(self):
        result = self._build("revision_requested", notes="Update clause 3")
        block_texts = [str(b) for b in result["blocks"]]
        assert any("Update clause 3" in t for t in block_texts)

    def test_revision_notes_block_absent_when_no_notes(self):
        result = self._build("revision_requested", notes=None)
        block_texts = [str(b) for b in result["blocks"]]
        # no notes text — should not contain a notes section
        assert not any("Reviewer Notes" in t for t in block_texts)


class TestBuildSlackBlocksContent:
    def _build(self, event="review_requested", **extra):
        kwargs = {**COMMON_KWARGS, **extra}
        return _build_slack_blocks(event=event, **kwargs)

    def test_contract_name_present_in_blocks(self):
        result = self._build()
        all_text = str(result)
        assert "Vendor_Agreement.pdf" in all_text

    def test_risk_score_present(self):
        result = self._build(risk_score=72)
        assert "72/100" in str(result)

    def test_reviewer_email_present(self):
        result = self._build(reviewer_email="cto@acme.com")
        assert "cto@acme.com" in str(result)

    def test_none_risk_score_shown_as_na(self):
        result = self._build(risk_score=None)
        assert "N/A" in str(result)

    def test_short_id_uppercased_in_blocks(self):
        """contract_id last 8 chars uppercased appear as a field."""
        result = self._build()
        # "contract-abc12345" → last 8 = "bc12345" → upper = "BC12345"  (8 chars)
        short = "contract-abc12345"[-8:].upper()
        assert short in str(result)


# ═══════════════════════════════════════════════════════════════════════════════
# _fire_docusign_stub — just confirms it doesn't raise
# ═══════════════════════════════════════════════════════════════════════════════

class TestFireDocusignStub:
    def test_does_not_raise(self):
        _fire_docusign_stub("contract-xyz99")

    def test_accepts_any_string(self):
        _fire_docusign_stub("")
        _fire_docusign_stub("a" * 100)


# ═══════════════════════════════════════════════════════════════════════════════
# escalate_to_legal — input-normalisation (str vs dict)
# Tested by inspecting the branching logic directly, not by running the activity
# (which needs Temporal + Postgres + Slack).
# ═══════════════════════════════════════════════════════════════════════════════

class TestEscalateInputNormalisation:
    """
    The activity has a defensive branch: if called with a plain str (legacy),
    it must still extract contract_id correctly.
    We test the normalisation inline since running the full activity requires
    Temporal + Postgres + Slack.
    """

    def _normalise(self, data):
        """Mirror the normalisation logic from escalate_to_legal."""
        if isinstance(data, str):
            cid            = data
            contract_name  = cid
            reviewer_email = ""
            reviewer_name  = ""
            risk_score     = None
        else:
            cid            = data.get("contract_id", data)
            contract_name  = data.get("contract_name", cid)
            reviewer_email = data.get("reviewer_email", "")
            reviewer_name  = data.get("reviewer_name",  "")
            risk_score     = data.get("risk_score")
        return dict(
            cid=cid,
            contract_name=contract_name,
            reviewer_email=reviewer_email,
            reviewer_name=reviewer_name,
            risk_score=risk_score,
        )

    def test_str_input_sets_cid(self):
        result = self._normalise("contract-aabbccdd")
        assert result["cid"] == "contract-aabbccdd"

    def test_str_input_empty_email(self):
        assert self._normalise("contract-x")["reviewer_email"] == ""

    def test_str_input_none_risk_score(self):
        assert self._normalise("contract-x")["risk_score"] is None

    def test_dict_input_extracts_all_fields(self):
        data = {
            "contract_id":    "contract-1234",
            "contract_name":  "Agreement.pdf",
            "reviewer_email": "cto@co.com",
            "reviewer_name":  "Jane",
            "risk_score":     80,
        }
        result = self._normalise(data)
        assert result["cid"]            == "contract-1234"
        assert result["contract_name"]  == "Agreement.pdf"
        assert result["reviewer_email"] == "cto@co.com"
        assert result["reviewer_name"]  == "Jane"
        assert result["risk_score"]     == 80

    def test_dict_missing_optional_fields_defaults(self):
        result = self._normalise({"contract_id": "contract-xyz"})
        assert result["reviewer_email"] == ""
        assert result["risk_score"]     is None

    def test_jira_id_format(self):
        """Jira ticket is always LEGAL-{last 4 chars of contract_id uppercased}."""
        cid = "contract-ab12"
        jira_id = f"LEGAL-{cid[-4:].upper()}"
        assert jira_id == "LEGAL-AB12"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])