from __future__ import annotations

import base64
import binascii
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class Intent(str, Enum):
    discover = "discover"
    find = "find"
    taste = "taste"
    curate = "curate"
    buy = "buy"
    learn = "learn"


class InspirationImage(BaseModel):
    """A compressed image sent only with the request that uses it."""

    image_id: str = Field(min_length=1, max_length=80)
    name: str = Field(default="Inspiration image", max_length=160)
    notes: str = Field(default="", max_length=300)
    mime_type: Literal["image/jpeg", "image/png", "image/webp"]
    data: str = Field(min_length=32, max_length=500_000)

    @field_validator("data")
    @classmethod
    def validate_encoded_image(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Image data must be valid base64.") from exc
        if len(decoded) > 360 * 1024:
            raise ValueError("Each inspiration image must be 360 KB or smaller after compression.")
        if len(decoded) < 16:
            raise ValueError("The uploaded image is too small to process.")
        return value


class ExistingArtworkReference(BaseModel):
    """Lightweight metadata for a saved artwork used as a taste reference."""

    title: str = Field(default="", max_length=180)
    artist: str = Field(default="", max_length=140)
    medium: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=300)
    year: str = Field(default="", max_length=40)


class ConciergeRequest(BaseModel):
    intent: Intent = Intent.discover
    loves: list[str] = Field(default_factory=list, max_length=12)
    avoid_references: list[str] = Field(default_factory=list, max_length=12)
    additional_interests: list[str] = Field(default_factory=list, max_length=8)
    # Personalised Explore requests use saved artwork/profile context and are
    # restricted to commercial listings explicitly available to acquire.
    personalize_with_saved_taste: bool = False
    profile_notes: str | None = Field(default=None, max_length=1000)
    existing_ideas: list[str] = Field(default_factory=list, max_length=6)
    existing_artworks: list[ExistingArtworkReference] = Field(default_factory=list, max_length=6)
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
    exclude_artwork_ids: list[str] = Field(default_factory=list, max_length=20)
    inspiration_image_ids: list[str] = Field(default_factory=list, max_length=5)
    inspiration_images: list[InspirationImage] = Field(default_factory=list, max_length=5)

    @field_validator("existing_ideas")
    @classmethod
    def normalize_existing_ideas(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value).strip()[:180] for value in values if str(value).strip()))[:6]

    @model_validator(mode="after")
    def validate_request(self) -> "ConciergeRequest":
        if self.budget_min is not None and self.budget_max is not None and self.budget_min > self.budget_max:
            raise ValueError("budget_min must be less than or equal to budget_max")
        if self.intent is Intent.buy or self.personalize_with_saved_taste:
            self.purchase_required = True
        image_ids = [item.image_id for item in self.inspiration_images]
        if len(image_ids) != len(set(image_ids)):
            raise ValueError("Each inspiration image must have a unique ID.")
        if self.inspiration_image_ids and any(image_id not in self.inspiration_image_ids for image_id in image_ids):
            raise ValueError("Submitted images must match the selected inspiration images.")
        return self
