"""Synthetic ChatGPT OAuth, catalog and inference boundary for acceptance tests."""
import asyncio
import base64
import json
import time
import uuid
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

app = FastAPI()
state = {"identity": "seat-a", "catalog_error": False, "status": 200, "seen": [], "statuses": [], "stream_error": False, "stream_error_at": 3, "delay": 0, "status_by_identity": {}, "retry_after_by_identity": {}, "models_by_identity": {}, "active": 0, "peak_active": 0, "requests": []}
codes = {}
fixtures = json.loads((Path(__file__).parents[2] / "deployment_patches/fixtures/chatgpt_sse.json").read_text())


def jwt(identity):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return '.'.join((encode({"alg": "none"}), encode({"exp": time.time() + 86400, "email": identity + "@example.test", "https://api.openai.com/auth": {"chatgpt_account_id": identity, "chatgpt_user_id": identity}}), "synthetic"))

@app.post('/control')
async def control(request: Request):
    changes = await request.json()
    for key in ('identity', 'catalog_error', 'status', 'statuses', 'stream_error', 'stream_error_at', 'delay', 'status_by_identity', 'retry_after_by_identity', 'models_by_identity'):
        if key in changes:
            state[key] = changes[key]
    if changes.get('clear'):
        state['seen'] = []
        state['requests'] = []
        state['peak_active'] = state['active']
    return state

@app.get('/control')
async def status():
    return state

@app.post('/api/accounts/deviceauth/usercode')
async def code():
    value = uuid.uuid4().hex
    codes[value] = state['identity']
    return {"device_auth_id": value, "user_code": "SYNTHETIC", "interval": 0}

@app.post('/api/accounts/deviceauth/token')
async def authorization(request: Request):
    data = await request.json()
    return {"authorization_code": data['device_auth_id'], "code_verifier": "synthetic"}

@app.post('/oauth/token')
async def token(request: Request):
    if request.headers.get('content-type', '').startswith('application/json'):
        data = await request.json()
        identity = data['refresh_token'].removeprefix('refresh-')
    else:
        data = await request.form()
        identity = codes[str(data['code'])]
    value = jwt(identity)
    return {"access_token": value, "id_token": value, "refresh_token": 'refresh-' + identity}

@app.get('/models')
async def models(request: Request):
    if state['catalog_error']:
        return JSONResponse({"error": "Synthetic catalog unavailable"}, status_code=503)
    slugs = state['models_by_identity'].get(request.headers.get('chatgpt-account-id'), ['gpt-5.4'])
    return {"models": [{"slug": slug, "display_name": "Synthetic GPT", "visibility": "list", "priority": 1, "context_window": 272000, "supported_reasoning_levels": []} for slug in slugs]}

@app.get('/usage')
async def usage():
    return {"rate_limit": {"allowed": True, "primary_window": {"used_percent": 20, "limit_window_seconds": 18000, "reset_at": time.time()+3600}}}

@app.post('/responses')
async def responses(request: Request):
    identity = request.headers.get('chatgpt-account-id')
    state['seen'].append(identity)
    status = state['statuses'].pop(0) if state['statuses'] else state['status']
    account_status = state['status_by_identity'].get(identity)
    if isinstance(account_status, list):
        status = account_status.pop(0) if account_status else status
    elif account_status is not None:
        status = account_status
    response_id = 'resp_' + uuid.uuid4().hex
    try:
        bearer = request.headers.get('authorization', '').removeprefix('Bearer ')
        payload = bearer.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        credential_matches = claims['https://api.openai.com/auth']['chatgpt_account_id'] == identity
    except (ValueError, KeyError, IndexError):
        credential_matches = False
    state['requests'].append({'identity': identity, 'status': status, 'response_id': response_id,
                              'credential_matches': credential_matches,
                              'forwarded_probe': request.headers.get('x-subscription-probe') == 'forwarded-synthetic'})
    if status != 200:
        retry_after = state['retry_after_by_identity'].get(identity)
        headers = {'retry-after': str(retry_after)} if retry_after is not None else {}
        return JSONResponse({"error": {"type": "server_error", "message": "Synthetic upstream failure"}}, status_code=status, headers=headers)
    events = json.loads(json.dumps(fixtures['text']).replace('resp_synthetic', response_id))
    stream_error, delay = state['stream_error'], state['delay']
    error_at = state['stream_error_at']
    async def stream():
        state['active'] += 1
        state['peak_active'] = max(state['peak_active'], state['active'])
        try:
            for i, event in enumerate(events):
                if delay:
                    await asyncio.sleep(delay)
                if stream_error and i == error_at:
                    yield 'event: error\ndata: {"type":"error","code":"server_error","message":"Synthetic interrupted stream"}\n\n'
                    return
                yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        finally:
            state['active'] -= 1
    return StreamingResponse(stream(), media_type='text/event-stream')

@app.post('/chat/completions')
async def unmanaged_completion():
    return {"id": "chatcmpl-synthetic-unmanaged", "object": "chat.completion", "created": int(time.time()),
            "model": "gpt-4o-mini", "choices": [{"index": 0, "message": {"role": "assistant", "content": "OLD_UNMANAGED"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


uvicorn.run(app, host='0.0.0.0', port=8091, log_level='warning')
