"""Configuration loading.

A plain dataclass over `config.yaml`. Deliberately not pydantic: this file is
operator-authored, not a trust boundary, and the one validation that matters
(pricing constants still null) is handled as a report banner by design rather
than as a load-time error — analysis must still run and produce price-free
relative shares while pricing is unknown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config.yaml")


@dataclass(frozen=True)
class Pricing:
    """Billing constants. All optional — null means "operator has not verified yet"."""

    fee_per_1k_search_calls: float | None = None
    input_per_mtok: float | None = None
    output_per_mtok: float | None = None
    search_content_flat_tokens: int | None = None
    batch_discount: float | None = None

    @property
    def is_verified(self) -> bool:
        """True only when every constant has been filled in from the billing dashboard."""
        return all(
            v is not None
            for v in (
                self.fee_per_1k_search_calls,
                self.input_per_mtok,
                self.output_per_mtok,
            )
        )

    def missing(self) -> list[str]:
        """Names of the still-null constants, for the UNVERIFIED PRICING banner."""
        return [k for k, v in vars(self).items() if v is None]


@dataclass(frozen=True)
class Thresholds:
    run_burst_gap_seconds: int = 120
    max_sane_query_chars: int = 300
    head_templates_export: int = 200


@dataclass(frozen=True)
class AnomalySettings:
    max_excerpt_chars: int = 2000
    context_lines: int = 3
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
    noise_patterns: tuple[re.Pattern[str], ...] = ()

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
            pricing=Pricing(**(raw.get("pricing") or {})),
            thresholds=Thresholds(**(raw.get("thresholds") or {})),
            anomalies=AnomalySettings(**(raw.get("anomalies") or {})),
            noise_patterns=tuple(re.compile(p) for p in (raw.get("noise_patterns") or [])),
        )

    def is_noise(self, line: str) -> bool:
        """Whether a line is known boilerplate.

        Used by the log parser to decide that a non-ACTION line following an ACTION
        line is *not* a wrapped continuation of it.
        """
        return any(p.search(line) for p in self.noise_patterns)
