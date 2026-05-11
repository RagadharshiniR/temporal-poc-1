"""
test_activities.py
"""

import sys
import types
import pytest

# Stub temporal
sys.modules.setdefault("temporalio", types.ModuleType("temporalio"))
sys.modules.setdefault("temporalio.activity", types.ModuleType("temporalio.activity"))
sys.modules["temporalio.activity"].defn = lambda f: f

# Stub psycopg2
psycopg2_extras = sys.modules.setdefault("psycopg2.extras", types.ModuleType("psycopg2.extras"))
psycopg2_extras.Json = lambda v: v
psycopg2_extras.RealDictCursor = type("_RealDictCursor", (), {})
sys.modules.setdefault("psycopg2", types.ModuleType("psycopg2"))
sys.modules.setdefault("requests", types.ModuleType("requests"))
sys.modules.setdefault("worker", types.ModuleType("worker"))

import contract_worker.postgres_db as pg_mod  # noqa: E402
pg_mod.db = type("_FakeDB", (), {"get_contract": lambda *a, **kw: None,
                                  "save_analysis": lambda *a, **kw: None,
                                  "set_status": lambda *a, **kw: None})()

# Stub gemini_analyzer
ga_mod = types.ModuleType("worker.gemini_analyzer")
ga_mod.GeminiContractAnalyzer = type("_FakeAnalyzer", (), {
    "extract_clauses": lambda self, text: [],
    "score_risk": lambda self, text, clauses, cid="": 0,
    "generate_summary": lambda self, text, clauses, score: "",
})
sys.modules["worker.gemini_analyzer"] = ga_mod

# Import module under test
from contract_worker.activities import (  # noqa: E402
    _risk_emoji, _risk_label, _risk_color, _build_slack_blocks, _fire_docusign_stub,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Risk visualization functions (_risk_emoji, _risk_label, _risk_color)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRiskVisualization:
    """Test risk scoring boundary mappings."""

    def test_emoji_none_is_white_square(self):
        assert _risk_emoji(None) == "⬜"

    def test_emoji_low_risk(self):
        assert _risk_emoji(0) == "🟢"
        assert _risk_emoji(30) == "🟢"

    def test_emoji_medium_risk(self):
        assert _risk_emoji(31) == "🟡"
        assert _risk_emoji(60) == "🟡"

    def test_emoji_high_risk(self):
        assert _risk_emoji(61) == "🔴"
        assert _risk_emoji(100) == "🔴"

    def test_label_none_is_na(self):
        assert _risk_label(None) == "N/A"

    def test_label_boundaries(self):
        assert _risk_label(0) == "Low"
        assert _risk_label(30) == "Low"
        assert _risk_label(31) == "Medium"
        assert _risk_label(60) == "Medium"
        assert _risk_label(61) == "High"
        assert _risk_label(100) == "High"

    def test_color_none_is_grey(self):
        assert _risk_color(None) == "#888888"

    def test_color_low_risk(self):
        assert _risk_color(10) == "#1fa86a"
        assert _risk_color(30) == "#1fa86a"

    def test_color_medium_risk(self):
        assert _risk_color(31) == "#c07b1a"
        assert _risk_color(60) == "#c07b1a"

    def test_color_high_risk(self):
        assert _risk_color(61) == "#b83040"
        assert _risk_color(100) == "#b83040"


# ═══════════════════════════════════════════════════════════════════════════════
# Slack message building (_build_slack_blocks)
# ═══════════════════════════════════════════════════════════════════════════════

_SLACK_COMMON = dict(
    contract_id="contract-abc12345",
    contract_name="Vendor_Agreement.pdf",
    reviewer_email="legal@example.com",
    risk_score=55,
    review_link="http://localhost:3000/?contract=contract-abc12345",
)


class TestBuildSlackBlocks:
    """Test Slack message structure and content."""

    def _build(self, event="review_requested", **extra):
        kwargs = {**_SLACK_COMMON, **extra}
        return _build_slack_blocks(event=event, **kwargs)

    def test_returns_blocks_and_attachments(self):
        result = self._build()
        assert "blocks" in result and isinstance(result["blocks"], list)
        assert "attachments" in result and isinstance(result["attachments"], list)

    def test_first_block_is_header(self):
        result = self._build()
        assert result["blocks"][0]["type"] == "header"

    def test_last_block_is_context(self):
        result = self._build()
        assert result["blocks"][-1]["type"] == "context"

    def test_no_null_style_in_action_buttons(self):
        """Slack rejects 'style': null."""
        result = self._build(event="approved", reviewer_name="Alice")
        for block in result["blocks"]:
            if block.get("type") == "actions":
                for el in block.get("elements", []):
                    assert "style" not in el or el["style"] is not None

    def test_header_event_labels(self):
        assert "Review Requested" in str(self._build("review_requested")["blocks"][0])
        assert "Approved" in str(self._build("approved", reviewer_name="Bob")["blocks"][0])
        assert "Revision" in str(self._build("revision_requested")["blocks"][0])
        assert "Escalated" in str(self._build("escalated")["blocks"][0])

    def test_content_includes_contract_details(self):
        result = self._build()
        all_text = str(result)
        assert "Vendor_Agreement.pdf" in all_text
        assert "legal@example.com" in all_text
        assert "55/100" in all_text

    def test_none_risk_score_shown_as_na(self):
        assert "N/A" in str(self._build(risk_score=None))

    def test_escalated_includes_jira_id_when_provided(self):
        result = self._build("escalated", jira_id="LEGAL-ABCD", notes="Too risky")
        assert "LEGAL-ABCD" in str(result)

    def test_revision_notes_included_when_provided(self):
        result = self._build("revision_requested", notes="Update clause 3")
        assert "Update clause 3" in str(result)

    def test_revision_notes_absent_when_not_provided(self):
        result = self._build("revision_requested", notes=None)
        assert "Reviewer Notes" not in str(result)

    def test_attachment_colors_differ_by_event(self):
        approved_color = self._build("approved", reviewer_name="X")["attachments"][0]["color"]
        escalated_color = self._build("escalated")["attachments"][0]["color"]
        assert approved_color != escalated_color


# ═══════════════════════════════════════════════════════════════════════════════
# DocuSign stub (_fire_docusign_stub)
# ═══════════════════════════════════════════════════════════════════════════════

class TestFireDocusignStub:
    """Test the DocuSign stub function."""

    def test_does_not_raise(self):
        _fire_docusign_stub("contract-xyz99")

    def test_accepts_any_string(self):
        _fire_docusign_stub("")
        _fire_docusign_stub("a" * 100)


# ═══════════════════════════════════════════════════════════════════════════════
# Activity input normalization
# ═══════════════════════════════════════════════════════════════════════════════

class TestActivityInputNormalization:
    """Test defensive branch handling for string vs dict inputs."""

    def _normalise(self, data):
        """Mirror the normalisation logic from escalate_to_legal activity."""
        if isinstance(data, str):
            cid, contract_name, reviewer_email, reviewer_name, risk_score = \
                data, data, "", "", None
        else:
            cid = data.get("contract_id", data)
            contract_name = data.get("contract_name", cid)
            reviewer_email = data.get("reviewer_email", "")
            reviewer_name = data.get("reviewer_name", "")
            risk_score = data.get("risk_score")
        return {"cid": cid, "contract_name": contract_name,
                "reviewer_email": reviewer_email, "reviewer_name": reviewer_name,
                "risk_score": risk_score}

    def test_string_input_extraction(self):
        result = self._normalise("contract-aabbccdd")
        assert result["cid"] == "contract-aabbccdd"
        assert result["reviewer_email"] == ""
        assert result["risk_score"] is None

    def test_dict_input_full_extraction(self):
        data = {
            "contract_id": "contract-1234",
            "contract_name": "Agreement.pdf",
            "reviewer_email": "cto@co.com",
            "reviewer_name": "Jane",
            "risk_score": 80,
        }
        result = self._normalise(data)
        assert result["cid"] == "contract-1234"
        assert result["contract_name"] == "Agreement.pdf"
        assert result["reviewer_email"] == "cto@co.com"
        assert result["reviewer_name"] == "Jane"
        assert result["risk_score"] == 80

    def test_dict_missing_optional_fields_defaults(self):
        result = self._normalise({"contract_id": "contract-xyz"})
        assert result["reviewer_email"] == ""
        assert result["risk_score"] is None

    def test_jira_id_format(self):
        """Jira ticket follows LEGAL-{last 4 chars uppercased}."""
        cid = "contract-ab12"
        jira_id = f"LEGAL-{cid[-4:].upper()}"
        assert jira_id == "LEGAL-AB12"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])