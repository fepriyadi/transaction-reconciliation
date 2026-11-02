"""New-supplier rule: every document of a supplier's first month goes to a person.

Keyed on the supplier, not on how the page was read, so it holds for profile
(deterministic) and LLM-only extraction alike. A supplier missing from the
register is treated as new until someone registers it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from .normalize import norm_vendor


@dataclass
class SupplierRegister:
    onboarded: dict = field(default_factory=dict)     # norm_vendor -> go-live date
    review_days: int = 31

    @classmethod
    def from_dates(cls, onboarded: dict, review_days: int = 31) -> "SupplierRegister":
        """supplier name -> go-live date (config/supplier_register.json, validated by recon.config)."""
        return cls({norm_vendor(name): d for name, d in onboarded.items()}, review_days)

    def review_reason(self, vendor: str, doc_date: Optional[date]) -> str:
        """Why this document must be reviewed as a new supplier's; '' once the supplier is established."""
        since = self.onboarded.get(norm_vendor(vendor))
        if since is None:
            return f"{vendor or 'The supplier'} is not in the supplier register, so it is treated as a new supplier."
        until = since + timedelta(days=self.review_days)
        if doc_date is None or doc_date < until:
            return (f"{vendor} went live on {since:%d %b %Y}; every document dated before {until:%d %b %Y} "
                    f"is reviewed by a person (new-supplier period).")
        return ""
