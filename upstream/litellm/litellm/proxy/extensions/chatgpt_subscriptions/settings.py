"""Validated, non-secret subscription policy settings shared by storage and HTTP."""
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, RootModel, StringConstraints, model_validator, with_config
from typing_extensions import NotRequired, TypedDict

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


def validate_model_id(value: str) -> str:
    if value.startswith(("openai/", "chatgpt/")):
        raise ValueError("Use the upstream model ID without a provider prefix.")
    return value


ModelId = Annotated[Identifier, AfterValidator(validate_model_id)]
Price = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class Like(Settings):
    like: ModelId


class ModelFactor(Settings):
    factor: Annotated[Price, Field(le=100)]


@with_config(ConfigDict(extra="forbid", strict=True))
class ExplicitPrices(TypedDict):
    input: Price
    output: Price
    cache_read: NotRequired[Price]


class ModelWeights(RootModel[dict[ModelId, Like | ModelFactor | ExplicitPrices]]):
    model_config = ConfigDict(strict=True)

    @model_validator(mode="after")
    def check_like_targets(self):
        for rule in self.root.values():
            if isinstance(rule, Like) and isinstance(self.root.get(rule.like), Like):
                raise ValueError("A like override cannot reference another like override.")
        return self
