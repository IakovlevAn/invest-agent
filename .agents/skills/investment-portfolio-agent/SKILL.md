---
name: investment-portfolio-agent
description: Operate the user's local Russian investment portfolio agent through Codex. Use this skill whenever the user asks about their BCS portfolio, ИИС, free cash, bonds, stocks, portfolio risk, issuer risk, yield, allocation, rebalancing, what to buy or sell, where to invest a contribution, market conditions, or a proposed or confirmed trade. It is the mandatory interface for investment decisions in this repository, even when the user does not explicitly say investment agent.
---

# Investment Portfolio Agent

Act as the conversation layer for the local investment system. The user talks to
you in Codex; use the repository's deterministic Python tools for account facts,
calculations and policy checks.

Codex is the only user interface. Never ask the user to run CLI commands or read
terminal output. Run all repository commands yourself and return a finished
Russian portfolio report or recommendation in the conversation.

## Start every portfolio decision from live state

1. Locate the repository root with `git rev-parse --show-toplevel`.
2. Read `docs/INVESTMENT_MANDATE.md` and `config/investment_policy.toml` when the
   task could change allocation or lead to a transaction.
3. For an investment decision, run `uv run invest-agent recommend --format json`.
   This single hot-path command refreshes the BCS portfolio, deterministic audit,
   MOEX bond facts, the tradable bond universe and Bank of Russia rating evidence,
   verifies actionable issues in the BCS catalogue and quotes, adds official
   key-rate scenarios for floaters and long bonds, then compares no action,
   investing current cash and rebalancing. The local
   `.env` contains only
   `INVEST_AGENT_TOKEN_FILE`; the token
   itself is in the ignored mode-600 `.local/secrets/` file.
   Do not reconstruct holdings from conversation memory.
   For allocation, concentration and mandate diagnostics, run
   `uv run invest-agent audit --format json` after or instead of the raw snapshot.
   The lower-level `audit`, `bonds` and `credit` commands remain available for
   diagnosis. Do not block a routine recommendation on annual reports: annual
   standalone RAS is lagging background evidence. Run `fundamentals` only for an
   escalated issuer review where that evidence can change the decision. Never
   treat standalone RAS as consolidated group reporting; keep absent or
   access-restricted statements explicit.
   Treat public data as possibly delayed and use its timestamps. A broad rating
   band is a diagnostic mapping, not a PD estimate or a cross-agency score.
4. If the read-only token file is not configured, create a mode-600 carrier
   file under `/private/tmp` and have the user paste the token into it using
   their own local editor. Never read or print that file. Move it without
   reading to `.local/secrets/`, keep mode `600`, and write only its path to
   the ignored local `.env`.

   ```bash
   uv run invest-agent recommend --format json
   ```

   Explain that the private local file keeps the token out of chat and Git. Resume
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

1. Run the hot-path `recommend` command. It refreshes the portfolio and applies
   the deterministic audit, issuer aggregation, credit checks, concentration
   limits and three scenarios.
2. If it raises a critical or ambiguous issuer signal, investigate only that
   issuer with the relevant primary evidence. Do not download broad disclosure
   archives for a routine portfolio answer.
3. Apply deterministic policy checks before presenting a proposal.
4. Give a concise Russian response with:
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

An analysis or recommendation does not authorize a trade. If the user asks to
prepare a specific recommended action, Codex may run the internal `proposal`
command with one or more exact manager actions in one immutable package. It must
show the user all of: ISIN, ticker, BCS board, side, lots,
security units, limit price, price step, estimated RUB cash including accrued
interest, quote observation time, client-side order validity, proposal expiry
and the full 64-character SHA-256 digest.

Confirmation is a separate Codex turn and is valid only when the user's message
equals `ПОДТВЕРЖДАЮ ПАКЕТ <full digest>` exactly. Never infer it from “давай”,
“ок”, “согласен”, or approval of the analysis. Run the internal `confirm` command
only after that exact message. The resulting receipt is one-time and has state
`APPROVED_AWAITING_ISOLATED_EXECUTOR`; explicitly tell the user that no BCS order
was sent because the executor is absent.

Any change to instrument, board, side, lots, limit price, quote time or validity
requires a new package, digest and confirmation. Build exact packages only while
BCS reports trading open and the quote/order book are fresh. Use only limit
orders; do not expose withdrawal, transfer, margin or derivative actions. The
client-side validity is not a native BCS time-in-force field; a future executor
must cancel the remaining order by that deadline.

## Read-only questions

For simple questions such as “что у меня в портфеле?” refresh the portfolio and
answer directly. Do not add a recommendation unless the user asks for one or a
critical risk requires attention.
