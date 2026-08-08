# Invest Agent — project rules

## Interface

The user works with this product through Codex conversation. Treat local Python
commands as internal tools used by Codex, not as the primary product interface.
Use `.agents/skills/investment-portfolio-agent/SKILL.md` for any portfolio,
security, market, recommendation, rebalance or trade-related request.

## Non-negotiable safety

- Never ask the user to paste BCS tokens, API keys or approval secrets into chat.
- The analytical process uses only a BCS read-only token.
- The isolated executor is the only component allowed to load the separate BCS
  trade token. Never expose that token to recommendation or proposal commands.
- An order may be sent only after a separate one-time Codex confirmation
  bound to the full immutable proposal digest: instrument, board, side, lots,
  limit price, quote time and validity.
- The user does not need to type the digest. Accept a separate semantic
  confirmation only when it unambiguously authorizes the displayed trade package,
  for example “подтверждаю выставление предложенных заявок” or “покупаем этот
  пакет”. Codex must bind it internally to the single active digest, run `confirm`,
  and then `execute` that same digest only.
- Never infer trade approval from a bare “давай”, “ок”, “готово”, a question,
  conditional wording or approval of an analysis/recommendation.
- Before submission, recheck the current portfolio, BCS catalogue, quote,
  session and displayed order-book quantity. Any mismatch blocks the whole
  package and requires a fresh proposal and confirmation.
- Report an order as submitted, filled or cancelled only from the BCS response
  and local execution journal. A multi-order broker basket is not atomic; report
  partial fills honestly even after emergency cancellation.
- Never handle withdrawals, asset transfers, margin, futures or options.
- Treat blocked foreign securities as hold-only.

## Evidence

Use deterministic local code for portfolio values and risk limits. Verify current
market, issuer, rate, tax and regulatory facts from primary sources. Display the
source and observation time for material claims. If current data is unavailable,
say so instead of filling gaps from model memory.
