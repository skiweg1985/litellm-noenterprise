"""Strict synthetic OpenRouter Image API; never forwards external requests."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Valid 1x1 PNG used to assert browser decoding, not just an <img> element.
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
JPEG = '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDi6KKK+ZP3E//Z'
seen = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/control":
            self.respond(200, {"seen": seen})
        else:
            self.respond(404, {"error": {"message": "Unknown fixture endpoint"}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        if self.path != "/api/v1/images":
            self.respond(404, {"error": {"message": "Image model requires /api/v1/images, not chat/completions"}})
            return
        if self.headers.get("Authorization") != "Bearer fixture-image-provider-sentinel":
            self.respond(401, {"error": {"message": "Named image credential was not used"}})
            return
        if body.get("model") != "openai/gpt-image-2.5-sunburst" or not body.get("prompt") or "messages" in body:
            self.respond(400, {"error": {"message": "Expected native image model and prompt request"}})
            return
        seen.append({"path": self.path, "body": body, "named_credential_used": True})
        self.respond(200, {
            "id": "fixture-image-generation", "model": body["model"],
            "data": [{"b64_json": JPEG if body["prompt"].endswith("jpeg") else PNG, "media_type": "image/jpeg" if body["prompt"].endswith("jpeg") else "image/png"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18, "cost": 0.0123},
        })


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
