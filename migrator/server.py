import argparse
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .core import analyze, load_exclusions, PROTECTED, STATIC_EXCLUSIONS, DEFAULT_EXCLUSIONS_FILE
from .kubernetes import ClusterError, contexts, namespaces, live_analyze, sync_plan, sync_execute, operation_health, rollback_plan, rollback_execute, discard_plans, source_scale_plan, source_scale_execute

from . import jobs, history, iam_jobs, azure_iam, iam, iam_history, auth

WEB = Path(__file__).resolve().parent.parent / 'web'

class Handler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','same-origin')
        if auth.enabled():self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        super().end_headers()

    def dispatch(self, callback):
        token=None
        try:
            if auth.enabled():
                if self.path!='/health' and not auth.allowed_request(self):
                    self.reply(403, {'error':'Request origin is not allowed.'});return
                if auth.handle(self):return
                if self.path not in ('/health','/style.css','/signin.js'):
                    sid=auth.session_id(self)
                    if not auth.session(sid,False):
                        if self.path=='/':
                            self.send_response(302);self.send_header('Location','/signin');self.send_header('Cache-Control','no-store');self.end_headers()
                        else:self.reply(401,{'error':'Sign in with Microsoft to continue.'})
                        return
                    token=auth.current_session.set(sid)
            callback()
        except auth.AuthError as error:self.reply(error.status,{'error':str(error)})
        except (ValueError,TypeError):self.reply(400,{'error':'Invalid request.'})
        finally:
            if token is not None:auth.current_session.reset(token)

    def do_GET(self):self.dispatch(self.handle_GET)
    def do_POST(self):self.dispatch(self.handle_POST)

    def handle_GET(self):
        if self.path == '/api/namespace-policy':
            self.reply(200, {'protected_namespaces': sorted(PROTECTED), 'static_exclusions': sorted(STATIC_EXCLUSIONS)})
        elif self.path == '/api/iam/history':
            try:
                self.reply(200, iam_history.listing())
            except (OSError, sqlite3.Error):
                self.reply(503, {'error': 'IAM history is unavailable. Check local state directory access.'})
        elif self.path.startswith('/api/iam/jobs/'):
            try:
                self.reply(200, iam_jobs.status(self.path.removeprefix('/api/iam/jobs/')))
            except ClusterError as error:
                self.reply(error.status, {'error': str(error)})
        elif self.path.startswith('/api/jobs/'):
            try:
                self.reply(200, jobs.status(self.path.removeprefix('/api/jobs/')))
            except ClusterError as error:
                self.reply(error.status, {'error': str(error), 'category': error.category})
        elif self.path == '/api/history':
            try:
                self.reply(200, history.listing())
            except (OSError, sqlite3.Error):
                self.reply(503, {'error': 'Local operation history is unavailable. Check state directory access.'})
        elif self.path == '/api/contexts':
            try:
                self.reply(200, contexts())
            except ClusterError as error:
                self.reply(error.status, {'error': str(error), 'category': error.category})
        elif self.path == '/health':
            self.reply(200, {'status': 'ok'})
        elif self.path in ('/', '/app.js', '/iam.js', '/auth.js', '/signin.js', '/style.css'):
            name = 'index.html' if self.path == '/' else self.path[1:]
            data = (WEB / name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', {'html': 'text/html; charset=utf-8', 'js': 'text/javascript', 'css': 'text/css'}[name.split('.')[-1]])
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.reply(404, {'error': 'Not found'})

    def reply(self, status, obj, headers=None):
        data = json.dumps(obj).encode()
        self.send_response(status)
        for key,value in (headers or {}).items():self.send_header(key,value)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_POST(self):
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        if not auth.allowed_request(self):
            self.reply(403, {'error': 'Only same-origin local requests are accepted.'})
            return
        if self.headers.get_content_type() != 'application/json':
            self.reply(415, {'error': 'Content-Type must be application/json.'})
            return
        if self.path not in ('/api/iam/sync/plan', '/api/iam/sync/execute', '/api/iam/sync/discard', '/api/iam/resolve-context', '/api/iam/jobs', '/api/source/scale/plan', '/api/source/scale/execute', '/api/analyze', '/api/live/analyze', '/api/namespaces', '/api/sync/plan', '/api/sync/execute', '/api/live/jobs', '/api/operations/health', '/api/rollback/plan', '/api/rollback/execute', '/api/sync/discard'):
            self.reply(404, {'error': 'Not found'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 5_000_000:
                self.reply(413, {'error': 'JSON payload must be between 1 byte and 5 MB'})
                return
            body = json.loads(self.rfile.read(length))
            if self.path == '/api/iam/sync/plan':
                self.reply(200, iam.sync_plan(body))
                return
            if self.path == '/api/iam/sync/execute':
                self.reply(200, iam.sync_execute(body))
                return
            if self.path == '/api/iam/sync/discard':
                self.reply(200, iam.discard(body))
                return
            if self.path == '/api/iam/resolve-context':
                self.reply(200, azure_iam.resolve_context(body))
                return
            if self.path == '/api/iam/jobs':
                self.reply(202, iam_jobs.start(body))
                return
            if self.path == '/api/live/jobs':
                self.reply(202, jobs.start(body))
                return
            elif self.path == '/api/operations/health':
                result = operation_health(body)
            elif self.path == '/api/rollback/plan':
                result = rollback_plan(body)
            elif self.path == '/api/rollback/execute':
                result = rollback_execute(body)
            elif self.path == '/api/sync/discard':
                result = discard_plans(body)
            elif self.path == '/api/source/scale/plan':
                result = source_scale_plan(body)
            elif self.path == '/api/source/scale/execute':
                result = source_scale_execute(body)
            elif self.path == '/api/sync/plan':
                result = sync_plan(body)
            elif self.path == '/api/sync/execute':
                result = sync_execute(body)
            elif self.path == '/api/namespaces':
                result = namespaces(body['context'])
            elif self.path == '/api/live/analyze':
                result = live_analyze(body)
            else:
                result = analyze(body['source'], body['target'], body['namespaces'], body.get('old_registry', ''), body.get('new_registry', ''))
            self.reply(200, result)
        except ClusterError as error:
            self.reply(error.status, {'error': str(error), 'category': error.category})
        except json.JSONDecodeError:
            self.reply(400, {'error': 'Request body must contain valid JSON.'})
        except ValueError as error:
            self.reply(400, {'error': str(error)})
        except (OSError, sqlite3.Error):
            self.reply(503, {'error': 'Local operation history could not be accessed. Check the state directory permissions.'})
        except (KeyError, TypeError, AttributeError):
            self.reply(400, {'error': 'Invalid input. Check manifests, contexts and namespace selection.'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8081)
    parser.add_argument('--host', default='127.0.0.1', help='Bind address; use 0.0.0.0 behind a private container Service')
    parser.add_argument('--exclusions-file', type=Path, default=DEFAULT_EXCLUSIONS_FILE)
    args = parser.parse_args()
    try:
        auth.validate_config()
        auth.prepare_kubeconfig()
        load_exclusions(args.exclusions_file)
    except (OSError, ValueError) as error:
        parser.error(f'Cannot initialize application: {error}')
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()

if __name__ == '__main__':
    main()
