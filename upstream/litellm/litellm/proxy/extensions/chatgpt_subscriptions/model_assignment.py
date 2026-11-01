"""Validate UI subscription assignments before native model persistence."""
import asyncio
import uuid
from collections.abc import Mapping
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import admin, tokens
from .accounts import Accounts
from .service import ModelsService, MonitoringError

ASSIGNMENT = 'subscription_assignment'


class SubscriptionAssignment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['fixed', 'priority', 'distribution']
    subscription_ids: list[str] = Field(min_length=1, max_length=16)
    revision: str | None = None


def _dump(value, *, supplied=False):
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    return value.model_dump(exclude_unset=supplied, exclude_none=not supplied)


def _params(existing):
    from litellm.proxy.common_utils.encrypt_decrypt_utils import decrypt_value_helper
    return {key: decrypt_value_helper(value=value, key=key, exception_type='debug', return_original_value=True)
            if isinstance(value, str) else value
            for key, value in _dump(existing).items()}


def is_subscription_model(params):
    return str(params.get('model', '')).startswith('chatgpt/') or params.get('custom_llm_provider') == 'chatgpt'


def require_subscription_admin(auth):
    if getattr(auth, 'oidc_identity_source', None) == 'provider' or getattr(auth, 'user_role', None) != 'proxy_admin':
        raise HTTPException(403, 'Use a proxy administrator dashboard session or native admin key to assign subscriptions.')


async def validate_model_assignment(incoming, auth, existing=None, router=None):
    """Normalize the native create/PATCH/legacy-update payload in place.

    Subscription IDs are trusted only after resolving the durable registry. The
    generated selector is persisted using native encrypted model parameters.
    """
    previous_params = _params(getattr(existing, 'litellm_params', None))
    submitted_params = _dump(incoming.litellm_params, supplied=True)
    effective = {**previous_params, **submitted_params}
    previous_info = _dump(getattr(existing, 'model_info', None))
    submitted_info = _dump(incoming.model_info, supplied=True)
    metadata = {**previous_info, **submitted_info}
    if existing is not None:
        metadata['id'] = existing.model_info.id
    assignment = metadata.get(ASSIGNMENT)
    name = incoming.model_name or getattr(existing, 'model_name', None)
    identifier = getattr(getattr(existing, 'model_info', None), 'id', None)
    peers = [item for item in (router.model_list if router is not None else [])
             if item.get('model_name') == name and (item.get('model_info') or {}).get('id') != identifier]
    if any((item.get('model_info') or {}).get(ASSIGNMENT) for item in peers):
        raise HTTPException(409, 'This public model name belongs to a managed subscription assignment.')
    if not (is_subscription_model(effective) or is_subscription_model(previous_params) or assignment is not None):
        return
    require_subscription_admin(auth)
    if not is_subscription_model(effective):
        if ASSIGNMENT in submitted_info and submitted_info[ASSIGNMENT] is not None:
            raise HTTPException(400, 'Subscription assignments require a ChatGPT model.')
        # Explicit provider switch discards the previous assignment. The native
        # writer honors this server-validated null separately from unrelated metadata.
        from litellm.types.router import ModelInfo
        incoming.model_info = ModelInfo(**{**metadata, ASSIGNMENT: None})
        return

    accounts = await asyncio.to_thread(Accounts)
    if assignment is not None:
        try:
            value = SubscriptionAssignment.model_validate(assignment)
        except ValidationError:
            raise HTTPException(400, 'Choose a valid routing mode and between one and sixteen subscriptions.') from None
        if len(set(value.subscription_ids)) != len(value.subscription_ids) or (value.mode == 'fixed' and len(value.subscription_ids) != 1):
            raise HTTPException(400, 'Choose distinct subscriptions; fixed mode requires exactly one.')
        by_id = {a.status()['subscription_id']: a for a in accounts}
        selected = [by_id.get(identifier) for identifier in value.subscription_ids]
        if any(account is None for account in selected):
            raise HTTPException(400, 'Unknown subscription. Refresh the subscription list.')
        mode = value.mode
        old = previous_info.get(ASSIGNMENT) or {}
        if value.revision is not None and old and value.revision != old.get('revision'):
            raise HTTPException(409, 'This subscription assignment changed. Reload the model before saving.')
    else:
        # Legacy selectors and named credentials remain supported, but share
        # server-side authorization and compatibility validation.
        api_key = effective.get('api_key')
        credential = effective.get('litellm_credential_name')
        if credential and api_key is None:
            values = admin._credential_values(credential)
            api_key = (values or {}).get('api_key')
        account = accounts.get(tokens.account_for_api_key(api_key))
        if account is None:
            raise HTTPException(400, 'Choose a connected ChatGPT subscription for this model.')
        selected, mode = [account], 'fixed'

    model = str(effective.get('model', ''))
    slug = model.removeprefix('chatgpt/')
    for account in selected:
        status = account.status()
        if status['retired'] or not (status['token_valid'] or status['state'] == 'token_expired'):
            raise HTTPException(409, 'A selected subscription is disconnected or retired. Connect or remove it before saving.')
        try:
            offered = (await ModelsService(account=account.name, path=account.path).get())['models']
        except MonitoringError:
            raise HTTPException(503, 'Subscription model catalog is unavailable. Retry after refreshing the connection.') from None
        if slug not in {entry['slug'] for entry in offered}:
            raise HTTPException(400, 'This model is not available from every selected subscription.')
    account = selected[0]

    # A fixed assignment cannot share its callable model name with unrelated
    # deployments, otherwise retry/load balancing would escape its membership.
    name = incoming.model_name or getattr(existing, 'model_name', None)
    identifier = getattr(getattr(existing, 'model_info', None), 'id', None)
    if router is not None:
        for deployment in router.model_list:
            if deployment.get('model_name') == name and (deployment.get('model_info') or {}).get('id') != identifier:
                raise HTTPException(409, 'Choose a unique public model name for a managed subscription assignment.')
    from litellm.types.router import LiteLLM_Params, ModelInfo, updateLiteLLMParams
    normalized = {**previous_params, **submitted_params, 'model': 'chatgpt/' + slug, 'api_key': tokens.ACCOUNT_KEY_PREFIX + account.name, 'litellm_credential_name': None}
    # Keep PATCH semantics for unrelated settings; explicit null detaches an old credential.
    incoming.litellm_params = (updateLiteLLMParams if existing is not None else LiteLLM_Params)(**normalized)
    old_assignment = previous_info.get(ASSIGNMENT) or {}
    ids = [account.status()['subscription_id'] for account in selected]
    revision = old_assignment.get('revision') if old_assignment.get('subscription_ids') == ids and old_assignment.get('mode') == mode and previous_params.get('model') == normalized['model'] else str(uuid.uuid4())
    incoming.model_info = ModelInfo(**{**metadata, ASSIGNMENT: {'mode': mode, 'subscription_ids': ids, 'revision': revision}})
