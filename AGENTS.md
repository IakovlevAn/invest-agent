# Invest Agent — project rules

## Interface

The user works with this product through Codex conversation. Treat local Python
commands as internal tools used by Codex, not as the primary product interface.
Use `.agents/skills/investment-portfolio-agent/SKILL.md` for any portfolio,
security, market, recommendation, rebalance or trade-related request.

## Non-negotiable safety

- Never ask the user to paste BCS tokens, API keys or approval secrets into chat.
- The analytical process uses only a BCS read-only token.
- Current code has no trade executor. Never claim that a real order was sent.
- A future order may be sent only after a separate one-time Codex confirmation
  bound to the full immutable proposal digest: instrument, board, side, lots,
  limit price, quote time and validity.
- Never infer trade approval from phrases such as “давай”, “ок”, “согласен” or
  approval of an analysis plan.
- Never handle withdrawals, asset transfers, margin, futures or options.
- Treat blocked foreign securities as hold-only.

## Evidence

Use deterministic local code for portfolio values and risk limits. Verify current
market, issuer, rate, tax and regulatory facts from primary sources. Display the
source and observation time for material claims. If current data is unavailable,
say so instead of filling gaps from model memory.
