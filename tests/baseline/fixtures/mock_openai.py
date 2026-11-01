"""Controlled external OpenAI-compatible service for image acceptance."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert self.path == '/v1/chat/completions'
        assert data['messages'][0]['content'] == 'baseline probe'
        body=json.dumps({'id':'chatcmpl-baseline','object':'chat.completion','created':1,'model':data['model'],'choices':[{'index':0,'message':{'role':'assistant','content':'controlled upstream reached'},'finish_reason':'stop'}],'usage':{'prompt_tokens':2,'completion_tokens':3,'total_tokens':5}}).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self,*args):
        pass

HTTPServer(('0.0.0.0',8080),Handler).serve_forever()
