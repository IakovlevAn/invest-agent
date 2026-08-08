from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CodexSkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = (ROOT / ".agents/skills/investment-portfolio-agent/SKILL.md").read_text()
        cls.project_rules = (ROOT / "AGENTS.md").read_text()

    def test_live_portfolio_is_required_for_decisions(self) -> None:
        self.assertIn("uv run invest-agent recommend --format json", self.skill)
        self.assertIn("Do not reconstruct holdings from conversation memory", self.skill)
        self.assertIn("Do not block a routine recommendation on annual reports", self.skill)
        self.assertIn("uv run invest-agent disclosures --format json", self.skill)

    def test_codex_is_the_only_user_interface(self) -> None:
        self.assertIn("Codex is the only user interface", self.skill)
        self.assertIn("Never ask the user to run CLI commands", self.skill)

    def test_skill_keeps_token_out_of_chat(self) -> None:
        self.assertIn("Never request the token value", self.skill)
        self.assertIn("Never ask the user to paste BCS tokens", self.project_rules)

    def test_only_exact_approved_order_list_can_be_executed(self) -> None:
        self.assertIn("exactly confirmed BCS limit", self.skill)
        self.assertIn("Never claim submission or execution without a BCS status", self.skill)
        self.assertIn("reconcile", self.skill)
        self.assertIn("Never infer trade approval", self.project_rules)

    def test_skill_requires_exact_one_time_codex_confirmation(self) -> None:
        self.assertIn("The user does not type or copy the digest", self.skill)
        self.assertIn("single active proposal", self.skill)
        self.assertIn("--user-confirmation", self.skill)
        self.assertIn("APPROVED_AWAITING_ISOLATED_EXECUTOR", self.skill)
        self.assertIn("quote/order book are fresh", self.skill)


if __name__ == "__main__":
    unittest.main()
