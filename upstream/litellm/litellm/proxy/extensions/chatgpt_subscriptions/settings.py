"""Validated, non-secret subscription policy settings shared by storage and HTTP."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Identifier = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]
Threshold = Annotated[int, Field(strict=True, ge=1, le=100)]
Factor = Annotated[float, Field(strict=True, ge=0, le=10, allow_inf_nan=False)]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Access(Settings):
    teams: list[Identifier] = Field(default_factory=list)
    users: list[Identifier] = Field(default_factory=list)


class FairUse(Settings):
    enabled: bool = True
    primary_threshold: Threshold = 60
    secondary_threshold: Threshold = 80


class Factors(Settings):
    teams: dict[Identifier, Factor] = Field(default_factory=dict)
    users: dict[Identifier, Factor] = Field(default_factory=dict)
