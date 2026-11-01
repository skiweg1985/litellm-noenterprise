"""Review optional model membership changes before applying native model updates."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from . import admin, tokens
from .model_assignment import ASSIGNMENT, _params, is_subscription_model
from .service import ModelsService, MonitoringError

API = "/chatgpt/api/accounts/{name}/model-assignments"
Mode = Literal["fixed", "priority", "distribution"]


class AssignmentPreview(BaseModel):
    mode: Mode
    subscription_ids: list[str]
    names: list[str]


class ModelAssignmentPreview(BaseModel):
    model_id: str
    model_name: str
    upstream_model: str
    version: str | None
    current: AssignmentPreview | None
    proposed: AssignmentPreview | None = None
    eligible: bool
    selected: bool = False
    reason: str | None = None


class BulkPreview(BaseModel):
    subscription_id: str
    display_name: str
    models: list[ModelAssignmentPreview]


class ModelChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=100)
    action: Literal["append", "replace", "priority", "distribution"]


class BulkChoices(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: list[ModelChoice] = Field(min_length=1, max_length=100)


class ModelSaveResult(BaseModel):
    model_id: str
    status: Literal["saved", "conflict", "error"]
    message: str


class BulkResults(BaseModel):
    results: list[ModelSaveResult]


async def target_catalog(accounts, name):
    account = accounts.get(name)
    if account is None:
        raise HTTPException(404, "Subscription not found.")
    status = account.status()
    if status["retired"] or not status["token_valid"]:
        raise HTTPException(409, "Connect this subscription before assigning models.")
    try:
        catalog = await ModelsService(account=account.name, path=account.path).get()
    except MonitoringError:
        raise HTTPException(
            503, "The subscription catalog is unavailable. Retry the review."
        ) from None
    return status, {model["slug"] for model in catalog["models"]}


def summary(assignment, labels):
    if not assignment:
        return None
    ids = assignment["subscription_ids"]
    return AssignmentPreview(
        mode=assignment["mode"],
        subscription_ids=ids,
        names=[
            labels.get(identifier, "Unavailable subscription") for identifier in ids
        ],
    )


def model_assignment(params, info, accounts):
    if info.get(ASSIGNMENT):
        return info[ASSIGNMENT]
    selector = params.get("api_key")
    if selector is None and params.get("litellm_credential_name"):
        selector = (
            admin._credential_values(params["litellm_credential_name"]) or {}
        ).get("api_key")
    account = accounts.get(tokens.account_for_api_key(selector))
    return (
        {"mode": "fixed", "subscription_ids": [account.status()["subscription_id"]]}
        if account
        else None
    )


async def preview(accounts, name):
    from litellm.proxy.proxy_server import llm_router, prisma_client, store_model_in_db

    status, offered = await target_catalog(accounts, name)
    if prisma_client is None:
        raise HTTPException(503, "Model storage is unavailable. Retry later.")
    labels = {
        a.status()["subscription_id"]: a.status()["display_name"] for a in accounts
    }
    rows = await prisma_client.db.litellm_proxymodeltable.find_many()
    candidates = [
        (
            row.model_id,
            row.model_name,
            _params(row.litellm_params),
            row.model_info or {},
            row.updated_at.isoformat() if store_model_in_db else None,
        )
        for row in rows
    ]
    stored_ids = {row.model_id for row in rows}
    for deployment in llm_router.model_list if llm_router else []:
        info = deployment.get("model_info") or {}
        if info.get("id") not in stored_ids:
            candidates.append(
                (
                    info.get("id", ""),
                    deployment["model_name"],
                    deployment["litellm_params"],
                    info,
                    None,
                )
            )
    result = []
    for identifier, public_name, params, info, version in candidates:
        if not is_subscription_model(params):
            continue
        slug = str(params.get("model", "")).removeprefix("chatgpt/")
        assignment = model_assignment(params, info, accounts)
        reason = None
        if version is None:
            reason = (
                "Configuration-backed model. Edit its server configuration instead."
            )
        elif slug not in offered:
            reason = "This upstream model is not offered by the subscription."
        elif assignment is None:
            reason = "Choose a valid assignment in the model editor first."
        elif status["subscription_id"] in assignment["subscription_ids"]:
            reason = "This subscription is already assigned."
        elif len(assignment["subscription_ids"]) >= 16:
            reason = "This model already has sixteen subscriptions."
        proposed = None
        if reason is None and assignment["mode"] != "fixed":
            proposed = {
                "mode": assignment["mode"],
                "subscription_ids": [
                    *assignment["subscription_ids"],
                    status["subscription_id"],
                ],
            }
        result.append(
            ModelAssignmentPreview(
                model_id=identifier,
                model_name=public_name,
                upstream_model=slug,
                version=version,
                current=summary(assignment, labels),
                proposed=summary(proposed, labels),
                eligible=reason is None,
                selected=proposed is not None,
                reason=reason,
            )
        )
    return BulkPreview(
        subscription_id=status["subscription_id"],
        display_name=status["display_name"],
        models=result,
    )


async def apply_choices(accounts, name, choices, auth):
    from litellm.proxy.management_endpoints.model_management_endpoints import (
        patch_model_record,
    )
    from litellm.types.router import updateDeployment

    if len({choice.model_id for choice in choices.models}) != len(choices.models):
        raise HTTPException(400, "Choose each model once.")
    plan = await preview(accounts, name)
    by_id = {row.model_id: row for row in plan.models}
    results = []
    for choice in choices.models:
        row = by_id.get(choice.model_id)
        if row is not None and row.version != choice.version:
            results.append(
                ModelSaveResult(
                    model_id=choice.model_id,
                    status="conflict",
                    message="The model changed since review. Refresh the review before saving.",
                )
            )
            continue
        if row is None or not row.eligible or row.current is None:
            results.append(
                ModelSaveResult(
                    model_id=choice.model_id,
                    status="conflict",
                    message=row.reason
                    if row and row.reason
                    else "Model is no longer eligible. Refresh the review.",
                )
            )
            continue
        current = row.current
        if current.mode != "fixed" and choice.action != "append":
            results.append(
                ModelSaveResult(
                    model_id=choice.model_id,
                    status="error",
                    message="Existing reserve and distribution models can only add the new subscription here.",
                )
            )
            continue
        if choice.action == "append" and current.mode == "fixed":
            results.append(
                ModelSaveResult(
                    model_id=choice.model_id,
                    status="error",
                    message="Choose whether to replace the fixed subscription or change its mode.",
                )
            )
            continue
        mode = (
            current.mode
            if choice.action == "append"
            else "fixed"
            if choice.action == "replace"
            else choice.action
        )
        ids = (
            [plan.subscription_id]
            if choice.action == "replace"
            else [*current.subscription_ids, plan.subscription_id]
        )
        try:
            await patch_model_record(
                choice.model_id,
                updateDeployment(
                    model_info={ASSIGNMENT: {"mode": mode, "subscription_ids": ids}}
                ),
                auth,
                expected_version=choice.version,
            )
            results.append(
                ModelSaveResult(
                    model_id=choice.model_id,
                    status="saved",
                    message="Assignment saved.",
                )
            )
        except Exception as exc:
            code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
            results.append(
                ModelSaveResult(
                    model_id=choice.model_id,
                    status="conflict" if str(code) == "409" else "error",
                    message="Model changed or could not be saved. Refresh the review and retry.",
                )
            )
    return BulkResults(results=results)


def router(accounts, dependency=None):
    require_admin = dependency or admin.litellm_admin()
    routes = APIRouter(tags=["ChatGPT subscriptions"])

    @routes.get(API, response_model=BulkPreview)
    async def review(name: str, response: Response, auth=Depends(require_admin)):
        response.headers["Cache-Control"] = "no-store"
        return await preview(accounts, name)

    @routes.post(API, response_model=BulkResults)
    async def save(
        name: str, body: BulkChoices, response: Response, auth=Depends(require_admin)
    ):
        response.headers["Cache-Control"] = "no-store"
        return await apply_choices(accounts, name, body, auth)

    return routes
