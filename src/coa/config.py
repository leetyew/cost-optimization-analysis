"""Configuration loading.

A plain dataclass over `config.yaml`. Deliberately not pydantic: this file is
operator-authored, not a trust boundary, and the one validation that matters
(pricing constants still null) is handled as a report banner by design rather
than as a load-time error — analysis must still run and produce price-free
relative shares while pricing is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config.yaml")


@dataclass(frozen=True)
class TierRates:
    """Per-million-token rates for one service tier, plus the per-call search fee.

    `cached_input_per_mtok` is separate because `cache_read` is a *subset* of
    input tokens billed at a steep discount — the formula is
    `(input - cache_read) * input_rate + cache_read * cached_rate`. There is no
    reasoning rate: reasoning tokens sit inside `output_tokens` and bill at the
    output rate, so a separate constant would invite double-counting.
    """

    input_per_mtok: float | None = None
    cached_input_per_mtok: float | None = None
    output_per_mtok: float | None = None
    fee_per_1k_search_calls: float | None = None

    def missing(self) -> list[str]:
        return [k for k, v in vars(self).items() if v is None]


@dataclass(frozen=True)
class Pricing:
    """Billing constants, per service tier.

    Tiers are not a detail: flex bills near batch rates and priority roughly
    double standard, so a single rate could be wrong by ~4x. All null until an
    operator fills them from the billing dashboard — published list prices do
    not reflect what an account actually pays.
    """

    tiers: dict[str, TierRates] = field(default_factory=dict)

    def for_tier(self, tier: str | None) -> TierRates:
        """Rates for one tier.

        A **missing** `service_tier` falls back to `standard`, because absent
        means the request did not ask for a tier and standard is the API default
        — an inference, so anything costing it must label the assumption. A tier
        that is named but not configured gets empty rates instead, which reports
        as unpriced rather than borrowing another tier's numbers.
        """
        return self.tiers.get(tier or "standard", TierRates())

    @property
    def is_verified(self) -> bool:
        """True only when every tier present has every rate filled in."""
        return bool(self.tiers) and not any(r.missing() for r in self.tiers.values())

    def missing(self) -> list[str]:
        """`tier.field` names still null, for the UNVERIFIED PRICING banner."""
        if not self.tiers:
            return ["<no tiers configured>"]
        return [f"{tier}.{k}" for tier, r in sorted(self.tiers.items()) for k in r.missing()]


@dataclass(frozen=True)
class Thresholds:
    head_templates_export: int = 200


@dataclass(frozen=True)
class AnomalySettings:
    max_excerpt_chars: int = 2000
    default_sample: int = 5
    max_payload_rows: int = 200


@dataclass(frozen=True)
class Config:
    data_root: Path = Path("data/cost_optimization")
    db: Path = Path("coa.sqlite")
    reports: Path = Path("reports")
    archetype_groups: Path = Path("archetype_groups.csv")
    pricing: Pricing = field(default_factory=Pricing)
    thresholds: Thresholds = field(default_factory=Thresholds)
    anomalies: AnomalySettings = field(default_factory=AnomalySettings)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
        """Read config.yaml. A missing file is fine — every field has a usable default."""
        path = Path(path)
        raw: dict[str, Any] = {}
        if path.exists():
            raw = yaml.safe_load(path.read_text()) or {}

        paths = raw.get("paths") or {}
        return cls(
            data_root=Path(paths.get("data_root", "data/cost_optimization")),
            db=Path(paths.get("db", "coa.sqlite")),
            reports=Path(paths.get("reports", "reports")),
            archetype_groups=Path(paths.get("archetype_groups", "archetype_groups.csv")),
            pricing=_load_pricing(raw.get("pricing")),
            thresholds=Thresholds(**(raw.get("thresholds") or {})),
            anomalies=AnomalySettings(**(raw.get("anomalies") or {})),
        )


def _load_pricing(raw: dict[str, Any] | None) -> Pricing:
    """Build per-tier rates, tolerating a tier the config names but leaves empty."""
    return Pricing(tiers={tier: TierRates(**(rates or {})) for tier, rates in (raw or {}).items()})
