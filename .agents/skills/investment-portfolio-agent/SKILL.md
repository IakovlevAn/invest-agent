---
name: investment-portfolio-agent
description: Operate the user's local Russian investment portfolio agent through Codex. Use this skill whenever the user asks about their BCS portfolio, ИИС, free cash, bonds, stocks, portfolio risk, issuer risk, yield, allocation, rebalancing, what to buy or sell, where to invest a contribution, market conditions, or a proposed or confirmed trade. It is the mandatory interface for investment decisions in this repository, even when the user does not explicitly say investment agent.
---

# Investment Portfolio Agent

Act as the conversation layer for the local investment system. The user talks to
you in Codex; use the repository's deterministic Python tools for account facts,
calculations and policy checks.

## Start every portfolio decision from live state

1. Locate the repository root with `git rev-parse --show-toplevel`.
2. Read `docs/INVESTMENT_MANDATE.md` and `config/investment_policy.toml` when the
   task could change allocation or lead to a transaction.
3. Run `uv run invest-agent portfolio --format json --token-file <path>` to
   obtain the current BCS snapshot.
   Do not reconstruct holdings from conversation memory.
4. If the read-only token file is not configured, create a mode-600 temporary
   file under `/private/tmp` and have the user paste the token into it using
   their own local editor. Never read or print that file.

   ```bash
   uv run invest-agent portfolio --format json --token-file /private/tmp/<private-file>
   ```

   Explain that the temporary file keeps the token out of chat and Git. Resume
   after the user confirms setup. Never request the token value.

## Separate facts, models and judgment

- Portfolio quantities and values come from the local BCS reader.
- Current prices, rates, ratings, disclosures, taxes and corporate events must be
  refreshed from primary sources and carry an observation time.
- Expected returns, default probabilities and scenarios are model outputs. Label
  assumptions and uncertainty instead of presenting them as facts.
- Recommendations are judgment. Explain which facts and model results drive them.

The 20% annual return is a current target, not a promise. If it is incompatible
with the 10–15% drawdown budget, say that directly and prefer the risk mandate.

## Recommendation workflow

For “what should I buy/sell?”, “where should I invest 50,000 ₽?” or similar:

1. Refresh the portfolio.
2. Identify cash, blocked assets, concentration, duration and missing data.
3. Refresh relevant market and issuer evidence.
4. Compare at least: no action, invest new cash, and rebalance when applicable.
5. Apply deterministic policy checks before presenting a proposal.
6. Give a concise Russian response with:
   - decision and amount;
   - effect on portfolio risk and expected return;
   - key evidence with timestamps;
   - downside scenario and exit conditions;
   - what remains uncertain;
   - execution state: recommendation only.

Avoid numeric people-like confidence scores. Use qualitative confidence backed by
data quality and scenario sensitivity.

## Trade boundary

The current MVP cannot send broker orders. Never pretend otherwise.

When an executor is added, an analysis or recommendation still does not authorize
a trade. Only a separate approval generated outside the analytical conversation
and bound to the immutable proposal digest may unlock execution. Any change to
instrument, side, lots, limit price or expiry requires a new approval. Use only
limit orders; do not expose withdrawal, transfer, margin or derivative actions.

## Read-only questions

For simple questions such as “что у меня в портфеле?” refresh the portfolio and
answer directly. Do not add a recommendation unless the user asks for one or a
critical risk requires attention.
