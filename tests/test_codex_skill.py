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
        self.assertIn("uv run invest-agent portfolio --format json", self.skill)
        self.assertIn("uv run invest-agent audit --format json", self.skill)
        self.assertIn("uv run invest-agent bonds --format json", self.skill)
        self.assertIn("uv run invest-agent credit --format json", self.skill)
        self.assertIn("uv run invest-agent fundamentals --format json", self.skill)
        self.assertIn("Do not reconstruct holdings from conversation memory", self.skill)

    def test_skill_keeps_token_out_of_chat(self) -> None:
        self.assertIn("Never request the token value", self.skill)
        self.assertIn("Never ask the user to paste BCS tokens", self.project_rules)

    def test_skill_cannot_execute_orders_in_current_mvp(self) -> None:
        self.assertIn("The current MVP cannot send broker orders", self.skill)
        self.assertIn("Never infer trade approval", self.project_rules)


if __name__ == "__main__":
    unittest.main()
