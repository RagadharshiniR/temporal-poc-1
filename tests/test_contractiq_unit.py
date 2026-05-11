"""
Unit tests for ContractIQ (no infrastructure required).

These tests intentionally avoid live dependencies like Postgres / Temporal.
"""
import os
import pytest


from contract_worker.postgres_db import ReviewStatus  # noqa: E402


class TestReviewStatus:
    def test_values(self):
        assert ReviewStatus.INGESTING.value == "ingesting"
        assert ReviewStatus.ANALYZING.value == "analyzing"
        assert ReviewStatus.PENDING_REVIEW.value == "pending_review"
        assert ReviewStatus.APPROVED.value == "approved"
        assert ReviewStatus.REVISION_REQUESTED.value == "revision_requested"
        assert ReviewStatus.ESCALATED.value == "escalated"

    def test_str_enum(self):
        # ReviewStatus is a str-enum so comparisons against plain strings work
        assert ReviewStatus.APPROVED == "approved"


class TestGeminiAnalyzerMock:
    """Exercises the mock path (no GEMINI_API_KEY set)."""

    def setup_method(self):
        import importlib
        import sys

        os.environ.pop("GEMINI_API_KEY", None)
        if "contract_worker.gemini_analyzer" in sys.modules:
            importlib.reload(sys.modules["contract_worker.gemini_analyzer"])

        from contract_worker.gemini_analyzer import GeminiContractAnalyzer

        self.analyzer = GeminiContractAnalyzer()

    def test_extract_clauses_returns_list(self):
        clauses = self.analyzer.extract_clauses("Sample contract text")
        assert isinstance(clauses, list)
        assert len(clauses) > 0

    def test_clauses_have_required_keys(self):
        clauses = self.analyzer.extract_clauses("Any text")
        for clause in clauses:
            assert "name" in clause, f"Missing 'name' in {clause}"
            assert "type" in clause, f"Missing 'type' in {clause}"
            assert "risk" in clause, f"Missing 'risk' in {clause}"
            assert "summary" in clause, f"Missing 'summary' in {clause}"

    def test_risk_values_are_valid(self):
        clauses = self.analyzer.extract_clauses("Any text")
        valid = {"high", "medium", "low"}
        for clause in clauses:
            assert clause["risk"] in valid, f"Unexpected risk '{clause['risk']}'"

    def test_score_risk_returns_int_in_range(self):
        clauses = [
            {"name": "Indemnity", "type": "indemnity", "risk": "high", "summary": "Test"},
            {"name": "Payment", "type": "payment", "risk": "low", "summary": "Test"},
        ]
        score = self.analyzer.score_risk("Contract text", clauses, "test-id")
        assert isinstance(score, int)
        assert 0 <= score <= 100

    def test_score_risk_is_deterministic_for_same_id(self):
        """Same contract_id must always produce the same score (seeded RNG)."""
        clauses = [{"name": "IP", "type": "ip", "risk": "high", "summary": "Test"}]
        score1 = self.analyzer.score_risk("text", clauses, "fixed-id")
        score2 = self.analyzer.score_risk("text", clauses, "fixed-id")
        assert score1 == score2

    def test_score_high_risk_clauses_higher_than_low(self):
        high_clauses = [
            {"name": "A", "type": "indemnity", "risk": "high", "summary": "x"},
            {"name": "B", "type": "ip", "risk": "high", "summary": "x"},
        ]
        low_clauses = [
            {"name": "A", "type": "payment", "risk": "low", "summary": "x"},
            {"name": "B", "type": "termination", "risk": "low", "summary": "x"},
        ]
        high_score = self.analyzer.score_risk("text", high_clauses, "h-id")
        low_score = self.analyzer.score_risk("text", low_clauses, "l-id")
        assert high_score > low_score

    def test_generate_summary_returns_string(self):
        clauses = [{"name": "IP", "type": "ip", "risk": "high", "summary": "Test"}]
        summary = self.analyzer.generate_summary("Contract text", clauses, 75)
        assert isinstance(summary, str)
        assert len(summary) > 10

    def test_summary_mentions_score(self):
        clauses = [{"name": "IP", "type": "ip", "risk": "medium", "summary": "Test"}]
        summary = self.analyzer.generate_summary("text", clauses, 45)
        assert "45" in summary

    def test_longer_contract_extracts_more_clauses(self):
        """The mock adds an extra clause for longer text."""
        short_clauses = self.analyzer._mock_extract_clauses("short")
        long_clauses = self.analyzer._mock_extract_clauses("x" * 600)
        assert len(long_clauses) >= len(short_clauses)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

