"""Equal native selection within an already validated subscription member set."""
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from litellm.router_strategy.simple_shuffle import simple_shuffle

Deployment = TypeVar('Deployment', bound=Mapping[str, Any])


def choose_distribution_deployment(healthy_deployments: Sequence[Deployment], model: str) -> Deployment:
    """Give each eligible member equal preference, independent of caller/global weights.

    Availability, cooldown and membership checks belong to the common managed
    routing boundary. This selector only picks among its surviving candidates;
    it does not change global routing, estimate quotas or promise exact counts.
    """
    weights = {
        str(deployment['model_info']['id']): 1.0
        for deployment in healthy_deployments
    }
    return simple_shuffle(
        resolve_model_alias=lambda _: None,
        healthy_deployments=healthy_deployments,
        model=model,
        request_kwargs={'_router_weights': {model: weights}},
    )
