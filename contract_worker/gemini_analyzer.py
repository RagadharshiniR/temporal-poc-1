"""
worker/gemini_analyzer.py
Mock contract analyzer — returns deterministic mock AI responses.
"""
import random
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)


class GeminiContractAnalyzer:
    def __init__(self, api_key=None):
        logger.info("Using mock AI responses (no real LLM configured)")

    # ── public API ─────────────────────────────────────────────────────────────

    def extract_clauses(self, contract_text: str) -> List[Dict]:
        return self._mock_extract_clauses(contract_text)

    def score_risk(self, contract_text: str, clauses: List[Dict], contract_id: str = "") -> int:
        return self._mock_score_risk(clauses, contract_id)

    def generate_summary(self, contract_text: str, clauses: List[Dict], risk_score: int) -> str:
        return self._mock_summary(clauses, risk_score)


    @staticmethod
    def _mock_extract_clauses(contract_text: str) -> List[Dict]:
        # Slightly vary based on content length to feel dynamic
        base = [
            {
                "name": "Indemnification",
                "type": "indemnity",
                "risk": "high",
                "summary": "Vendor must indemnify the client against all third-party claims arising from vendor's negligence.",
            },
            {
                "name": "Intellectual Property Rights",
                "type": "ip",
                "risk": "medium",
                "summary": "All work product and IP created during engagement is assigned to the client.",
            },
            {
                "name": "Payment Terms",
                "type": "payment",
                "risk": "low",
                "summary": "Net-30 payment terms from invoice date with 1.5% monthly interest on late payments.",
            },
            {
                "name": "Limitation of Liability",
                "type": "liability",
                "risk": "medium",
                "summary": "Total liability of either party capped at 12 months of fees paid in the preceding year.",
            },
            {
                "name": "Termination for Convenience",
                "type": "termination",
                "risk": "low",
                "summary": "Either party may terminate with 30 days written notice without cause.",
            },
            {
                "name": "Confidentiality & NDA",
                "type": "confidentiality",
                "risk": "medium",
                "summary": "Five-year mutual confidentiality obligation; excludes publicly available information.",
            },
        ]
        # Add an extra high-risk clause if contract text is long (simulates more complex contracts)
        if len(contract_text) > 500:
            base.append({
                "name": "Governing Law & Jurisdiction",
                "type": "other",
                "risk": "medium",
                "summary": "Agreement governed by laws of Delaware; disputes resolved by binding arbitration.",
            })
        return base

    @staticmethod
    def _mock_score_risk(clauses: List[Dict], contract_id: str) -> int:
        high   = sum(1 for c in clauses if c.get("risk") == "high")
        medium = sum(1 for c in clauses if c.get("risk") == "medium")
        base   = (high * 22) + (medium * 12)
        # Use contract_id as seed so the same contract always gets the same score
        rng = random.Random(contract_id or "default")
        jitter = rng.randint(-5, 10)
        return min(100, max(5, base + jitter))

    @staticmethod
    def _mock_summary(clauses: List[Dict], risk_score: int) -> str:
        label = "low" if risk_score <= 30 else "medium" if risk_score <= 60 else "high"
        high_clauses = [c["name"] for c in clauses if c.get("risk") == "high"]
        concern = f" Key concern: {high_clauses[0]}." if high_clauses else ""
        return (
            f"This contract carries a {label} overall legal risk score of {risk_score}/100."
            f"{concern} "
            f"The document contains {len(clauses)} identified clauses. "
            f"A human reviewer should verify the indemnification scope and liability caps before signing."
        )