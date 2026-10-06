from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class Intent(str, Enum):
    discover = "discover"
    find = "find"
    taste = "taste"
    curate = "curate"
    buy = "buy"
    learn = "learn"


class ConciergeRequest(BaseModel):
    intent: Intent = Intent.discover
    loves: list[str] = Field(default_factory=list, max_length=12)
    avoid_references: list[str] = Field(default_factory=list, max_length=12)
    additional_interests: list[str] = Field(default_factory=list, max_length=8)
    art_interests: list[str] = Field(default_factory=list, max_length=8)
    mediums: list[str] = Field(default_factory=list, max_length=8)
    budget_min: float | None = Field(default=None, ge=0)
    budget_max: float | None = Field(default=None, ge=0)
    room: str | None = Field(default=None, max_length=80)
    size_preference: str | None = Field(default=None, max_length=80)
    preferred_market: str | None = Field(default=None, max_length=120)
    discovery_level: int = Field(default=50, ge=0, le=100)
    purchase_required: bool = False
    goal: str | None = Field(default=None, max_length=500)
    number_of_works: int = Field(default=1, ge=1, le=8)
    feedback: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_request(self) -> "ConciergeRequest":
        if self.budget_min is not None and self.budget_max is not None and self.budget_min > self.budget_max:
            raise ValueError("budget_min must be less than or equal to budget_max")
        if self.intent is Intent.buy:
            self.purchase_required = True
        return self
