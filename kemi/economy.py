"""Economy model and simulation (T17).

Kemi's credit supply has one source — the genesis faucet (every minted
identity starts with GENESIS_CREDITS) — and credits only move between
accounts, never inflate after minting. The open question is the long-run
behaviour: does the faucet cause runaway inflation as identities join, and
how do credits concentrate toward productive providers?

This module answers that with a deterministic simulation, and `kemi economy`
prints the report. Findings (see ``simulate``):

* Total supply grows strictly linearly with the number of identities
  (supply = N x GENESIS_CREDITS) and is otherwise conserved — there is no
  per-transaction emission, so there is no monetary inflation.
* Credits flow from consumers to providers, so providers' balances rise and
  pure consumers' balances fall toward zero, at which point they must provide
  to keep spending. This is the intended tit-for-tat pressure.
* Because minting an identity costs proof-of-work, the faucet cannot be
  farmed for free supply; the PoW cost is the real "price" of genesis credits.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .gossip_ledger import GENESIS_CREDITS


@dataclass
class EconomyReport:
    identities: int
    rounds: int
    total_supply: float
    conserved: bool                 # supply unchanged by transfers
    provider_share: float           # fraction of credits held by providers
    consumers_drained: int          # consumers that hit a zero floor
    gini: float                     # inequality of the balance distribution


def _gini(values: list[float]) -> float:
    vals = sorted(max(0.0, v) for v in values)
    n = len(vals)
    total = sum(vals)
    if n == 0 or total == 0:
        return 0.0
    cum = sum((i + 1) * v for i, v in enumerate(vals))
    return round((2 * cum) / (n * total) - (n + 1) / n, 4)


def simulate(identities: int = 100, providers: int = 30, rounds: int = 2000,
             price: float = 1.0, seed: int = 7) -> EconomyReport:
    """A self-contained credit-flow simulation (no network, no crypto)."""
    rng = random.Random(seed)
    balances = [GENESIS_CREDITS] * identities
    provider_ids = set(rng.sample(range(identities), min(providers, identities)))
    drained = 0
    initial_supply = sum(balances)

    for _ in range(rounds):
        consumer = rng.randrange(identities)
        if not provider_ids:
            break
        provider = rng.choice(list(provider_ids))
        if provider == consumer:
            continue
        if balances[consumer] >= price:
            balances[consumer] -= price
            balances[provider] += price
            if balances[consumer] < price:
                drained += 1

    supply = sum(balances)
    provider_credits = sum(balances[i] for i in provider_ids)
    return EconomyReport(
        identities=identities,
        rounds=rounds,
        total_supply=round(supply, 6),
        conserved=abs(supply - initial_supply) < 1e-6,
        provider_share=round(provider_credits / supply, 4) if supply else 0.0,
        consumers_drained=drained,
        gini=_gini(balances),
    )


def render(report: EconomyReport) -> str:
    lines = [
        "Kemi economy simulation",
        "=" * 40,
        f"identities (each minted with {GENESIS_CREDITS:.0f} genesis credits): "
        f"{report.identities}",
        f"total supply: {report.total_supply:.0f}  "
        f"(= identities x genesis; conserved by transfers: {report.conserved})",
        f"transfer rounds simulated: {report.rounds}",
        f"share of credits held by providers: {report.provider_share:.0%}",
        f"consumers that hit the zero floor (must now provide): "
        f"{report.consumers_drained}",
        f"balance inequality (Gini): {report.gini}",
        "",
        "Conclusion: supply grows only with identities (no per-tx emission, no",
        "inflation); minting costs proof-of-work, so the faucet can't be farmed;",
        "credits concentrate toward providers, creating tit-for-tat pressure to",
        "share compute. Roadmap lever: a small per-job sink (burn) would cap",
        "long-run supply if the identity count ever grows without bound.",
    ]
    return "\n".join(lines)
