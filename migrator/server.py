import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .core import analyze

WEB = Path(__file__).resolve().parent.parent / 'web'

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.reply(200, {'status': 'ok'})
        elif self.path in ('/', '/app.js', '/style.css'):
            name = 'index.html' if self.path == '/' else self.path[1:]
            data = (WEB / name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', {'html': 'text/html; charset=utf-8', 'js': 'text/javascript', 'css': 'text/css'}[name.split('.')[-1]])
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.reply(404, {'error': 'Not found'})

    def reply(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != '/api/analyze':
            self.reply(404, {'error': 'Not found'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 5_000_000:
                self.reply(413, {'error': 'JSON payload must be between 1 byte and 5 MB'})
                return
            body = json.loads(self.rfile.read(length))
            self.reply(200, analyze(body['source'], body['target'], body['namespaces'], body.get('old_registry', ''), body.get('new_registry', '')))
        except (ValueError, KeyError, TypeError, AttributeError):
            self.reply(400, {'error': 'Invalid manifest or namespace selection; check the JSON inputs.'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()

if __name__ == '__main__':
    main()
