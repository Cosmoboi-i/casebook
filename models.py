"""Pydantic models for the two Unstop endpoints.

Two deliberately separate models - the field sets genuinely differ:
  * ListingCandidate  <- ENDPOINT 1 (search-result list), every daily candidate
  * CompetitionDetail <- ENDPOINT 2 (competition/{id}), Tier B enrichment only

Both use extra="allow": Unstop adds fields without notice and we archive raw
JSON anyway, so unknown fields must never break parsing. Only fields the
pipeline actually reads are typed.
"""
from __future__ import annotations

import json
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------- shared bits

class Filter(_Loose):
    id: int
    name: str
    type: Optional[str] = None


class ListingTag(_Loose):
    """assignedTag - tags the LISTING (e.g. "d2c-trusted")."""
    tag: Optional[str] = None


class Prize(_Loose):
    id: Optional[int] = None
    rank: Optional[str] = None
    cash: Optional[float] = None
    currencyCode: Optional[str] = None
    others: Optional[str] = None
    certificate: Optional[int] = None


class PaymentService(_Loose):
    id: Optional[int] = None
    amount: Optional[float] = None
    required: Optional[int] = None


class RegnRequirements(_Loose):
    start_regn_dt: Optional[str] = None
    end_regn_dt: Optional[str] = None      # registration deadline - NOT end_date
    reg_status: Optional[str] = None
    min_team_size: Optional[int] = None
    max_team_size: Optional[int] = None
    eligibility: Optional[Any] = None      # JSON-encoded string on ENDPOINT 1

    def eligibility_dict(self) -> dict:
        e = self.eligibility
        if isinstance(e, str):
            try:
                e = json.loads(e)
            except ValueError:
                return {}
        return e if isinstance(e, dict) else {}


# ---------------------------------------------------------- ENDPOINT 1 model

class OrgSummary(_Loose):
    id: int
    name: str
    tier: Optional[int] = None             # present on ENDPOINT 1 only
    public_url: Optional[str] = None


class ListingCandidate(_Loose):
    id: int
    title: str
    type: str                              # competitions / quizzes / hackathons (mixed!)
    subtype: Optional[str] = None
    organization_id: int
    organisation: OrgSummary
    details: Optional[str] = None          # HTML
    status: Optional[str] = None
    regn_open: Optional[int] = None
    region: Optional[str] = None
    isPaid: Optional[bool] = None
    seo_url: Optional[str] = None
    updated_at: Optional[str] = None
    end_date: Optional[str] = None
    approved_date: Optional[str] = None    # odd format: "2026-09-10 18:20:40 GMT+0530"
    filters: List[Filter] = Field(default_factory=list)
    assignedTag: ListingTag = Field(default_factory=ListingTag)
    prizes: List[Prize] = Field(default_factory=list)
    payment_services: List[PaymentService] = Field(default_factory=list)
    regnRequirements: RegnRequirements = Field(default_factory=RegnRequirements)
    workfunction: List[dict] = Field(default_factory=list)
    # Volatile - change every poll. NEVER used for diffing (see diff.py).
    viewsCount: Optional[int] = None
    registerCount: Optional[int] = None


# ---------------------------------------------------------- ENDPOINT 2 model

class OrgDetail(_Loose):
    id: int
    name: str
    organisation_type: Optional[str] = None  # opaque code, e.g. "2" - UNVALIDATED signal
    website: Optional[str] = None
    description: Optional[str] = None


class AccountTag(_Loose):
    """assignTags - tags the ORGANISER ACCOUNT (entity_type App\\User),
    distinct from assignedTag which tags the listing. Often {"tag": null}."""
    tag: Optional[str] = None
    entity_type: Optional[str] = None


class Round(_Loose):
    id: Optional[int] = None
    round_order: Optional[int] = None
    status: Optional[str] = None
    subtype: Optional[str] = None


class CompetitionDetail(_Loose):
    id: int
    title: str
    type: str
    organization_id: int
    organisation: OrgDetail
    web_url: Optional[str] = None           # UNVALIDATED signal
    attachment: List[Any] = Field(default_factory=list)   # UNVALIDATED signal
    rounds: List[Round] = Field(default_factory=list)     # UNVALIDATED signal (count)
    assignTags: Optional[AccountTag] = None # UNVALIDATED signal (account-level)
    assignedTag: Optional[ListingTag] = None
    regnRequirements: Optional[RegnRequirements] = None

    def enrichment_fields(self) -> dict:
        """Exactly what goes into enrichment_log. Display-only, never scored."""
        return {
            "organisation_type": self.organisation.organisation_type,
            "web_url": self.web_url,
            "attachment": self.attachment,
            "rounds_count": len(self.rounds),
            "account_tag": self.assignTags.tag if self.assignTags else None,
        }
