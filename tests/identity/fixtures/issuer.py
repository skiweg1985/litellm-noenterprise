"""Owned OIDC issuer for public HTTP acceptance tests; never use in production."""

import base64
import hashlib
import json
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

WRONG_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
JWK.update(kid="test-key", use="sig", alg="RS256")
ISSUER = "http://issuer:8080"
ACTOR = {"sub": "alice", "oid": "stable-alice", "email": "shared@example.test", "roles": ["access"]}
CODES = {}
AUTH_METHOD = {"method": "client_secret_post"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/.well-known/openid-configuration":
            return self.reply(
                {
                    "issuer": ISSUER,
                    "authorization_endpoint": ISSUER + "/authorize",
                    "token_endpoint": ISSUER + "/token",
                    "jwks_uri": ISSUER + "/jwks",
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "response_types_supported": ["code"],
                    "subject_types_supported": ["public"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": [AUTH_METHOD["method"]],
                }
            )
        if url.path == "/jwks":
            return self.reply({"keys": [JWK]})
        if url.path == "/authorize":
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if query.get("response_type") != "code" or query.get("code_challenge_method") != "S256":
                return self.reply({"error": "invalid_request"}, 400)
            code = secrets.token_urlsafe(24)
            CODES[code] = (query, dict(ACTOR))
            self.send_response(302)
            self.send_header(
                "Location", query["redirect_uri"] + "?" + urlencode({"code": code, "state": query["state"]})
            )
            self.end_headers()
            return
        return self.reply({"error": "not_found"}, 404)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/control/actor":
            ACTOR.clear()
            ACTOR.update(json.loads(body))
            return self.reply({"ok": True})
        if self.path == "/control/metadata":
            AUTH_METHOD.update(json.loads(body))
            return self.reply({"ok": True})
        if self.path == "/token":
            data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            expected_basic = "Basic " + base64.b64encode(b"litellm-test:fixture-only").decode()
            valid = (
                self.headers.get("Authorization") == expected_basic
                if AUTH_METHOD["method"] == "client_secret_basic"
                else data.get("client_id") == "litellm-test" and data.get("client_secret") == "fixture-only"
            )
            if not valid:
                return self.reply({"error": "invalid_client"}, 401)
            saved = CODES.pop(data.get("code"), None)
            if not saved:
                return self.reply({"error": "invalid_grant"}, 400)
            query, actor = saved
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(data.get("code_verifier", "").encode()).digest())
                .decode()
                .rstrip("=")
            )
            if challenge != query["code_challenge"] or data.get("redirect_uri") != query["redirect_uri"]:
                return self.reply({"error": "invalid_grant"}, 400)
            claims = {
                "iss": ISSUER,
                "aud": query["client_id"],
                "iat": int(time.time()),
                "exp": int(time.time()) + 300,
                "nonce": query["nonce"],
                **actor,
            }
            return self.reply(
                {
                    "access_token": "opaque-provider-token-not-for-browser",
                    "token_type": "Bearer",
                    "id_token": jwt.encode(
                        claims,
                        WRONG_KEY if actor.get("_sign_with_wrong_key") else KEY,
                        algorithm="RS256",
                        headers={"kid": "test-key"},
                    ),
                }
            )
        return self.reply({"error": "not_found"}, 404)


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
