"""Synthetic ChatGPT OAuth, catalog and inference boundary for acceptance tests."""
import base64
import json
import time
import uuid
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

app = FastAPI()
state = {"identity": "seat-a", "catalog_error": False, "status": 200, "seen": []}
codes = {}
fixtures = json.loads((Path(__file__).parents[2] / "deployment_patches/fixtures/chatgpt_sse.json").read_text())


def jwt(identity):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return '.'.join((encode({"alg": "none"}), encode({"exp": time.time() + 86400, "email": identity + "@example.test", "https://api.openai.com/auth": {"chatgpt_account_id": identity, "chatgpt_user_id": identity}}), "synthetic"))

@app.post('/control')
async def control(request: Request):
    changes = await request.json()
    for key in ('identity', 'catalog_error', 'status'):
        if key in changes:
            state[key] = changes[key]
    if changes.get('clear'):
        state['seen'] = []
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
async def models():
    if state['catalog_error']:
        return JSONResponse({"error": "Synthetic catalog unavailable"}, status_code=503)
    return {"models": [{"slug": "gpt-5.4", "display_name": "Synthetic GPT", "visibility": "list", "priority": 1, "context_window": 272000, "supported_reasoning_levels": []}]}

@app.get('/usage')
async def usage():
    return {"rate_limit": {"allowed": True, "primary_window": {"used_percent": 20, "limit_window_seconds": 18000, "reset_at": time.time()+3600}}}

@app.post('/responses')
async def responses(request: Request):
    identity = request.headers.get('chatgpt-account-id')
    state['seen'].append(identity)
    if state['status'] != 200:
        return JSONResponse({"error": {"type": "server_error", "message": "Synthetic upstream failure"}}, status_code=state['status'])
    async def stream():
        for event in fixtures['text']:
            yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
    return StreamingResponse(stream(), media_type='text/event-stream')

uvicorn.run(app, host='0.0.0.0', port=8091, log_level='warning')
