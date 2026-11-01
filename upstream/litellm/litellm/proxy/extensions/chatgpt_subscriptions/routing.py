"""Routing boundaries for administrator-managed subscription assignments."""
ASSIGNMENT = "subscription_assignment"


def fixed_subscription_group(router, model):
    """Only persisted deployment metadata can disable cross-model fallbacks."""
    model = router.model_group_alias.get(model, model)
    if isinstance(model, dict):
        model = model.get('model')
    deployments = [item for item in router.model_list
                   if item.get('model_name') == model or (item.get('model_info') or {}).get('id') == model]
    fixed = any(((item.get('model_info') or {}).get(ASSIGNMENT) or {}).get('mode') == 'fixed' for item in deployments)
    if fixed and len(deployments) != 1:
        import litellm
        raise litellm.ServiceUnavailableError(
            message='Fixed subscription model has conflicting deployments. Use a unique public model name.',
            model=str(model), llm_provider='chatgpt')
    return fixed


def pin_subscription_credentials(deployment, kwargs):
    """A caller cannot replace a managed model's administrator-selected account."""
    # Preserve the router's stable deployment ID before native client-side
    # credential handling/nested Responses calls derive temporary model IDs.
    logging_obj = kwargs.get('litellm_logging_obj')
    from litellm.litellm_core_utils.litellm_logging import Logging
    if isinstance(logging_obj, Logging):
        logging_obj._chatgpt_subscription_deployment_id = (deployment.get('model_info') or {}).get('id')
    assignment = (deployment.get('model_info') or {}).get(ASSIGNMENT)
    if not assignment:
        return
    params = deployment['litellm_params']
    for key in ('api_key', 'api_base', 'litellm_credential_name', 'custom_llm_provider'):
        kwargs.pop(key, None)
        if params.get(key) is not None:
            kwargs[key] = params[key]
    kwargs['custom_llm_provider'] = 'chatgpt'
