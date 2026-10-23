"""Loopback-only browser surface. Previews are inert text, never email HTML."""
from __future__ import annotations

import hmac
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

from .review import Store, counts

CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; font-src 'none'; frame-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


def make_server(root, host='127.0.0.1', port=8765):
    os.umask(0o077)
    store = Store(root)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Query strings can contain sensitive review filters.

        def setup(self):
            super().setup()
            self.connection.settimeout(120)

        def headers_checked(self, mutate=False):
            authority = self.headers.get('Host', '')
            valid = {f'localhost:{self.server.server_port}', f'127.0.0.1:{self.server.server_port}'}
            if authority not in valid:
                raise ValueError('Only localhost access is supported')
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + authority:
                raise ValueError('Cross-origin access is blocked')
            # Following a link may mark the initial document navigation cross-site.
            # Allow only the inert app shell; API access still requires same-site requests.
            app_navigation = (
                self.command == 'GET' and self.path == '/'
                and self.headers.get('Sec-Fetch-Mode') == 'navigate'
                and self.headers.get('Sec-Fetch-Dest') == 'document'
            )
            if self.headers.get('Sec-Fetch-Site') == 'cross-site' and not app_navigation:
                raise ValueError('Cross-site access is blocked')
            if mutate and not hmac.compare_digest(self.headers.get('X-Review-Token', ''), token):
                raise ValueError('Session token missing; reload the page')
            if self.headers.get('Transfer-Encoding'):
                raise ValueError('Chunked requests are not supported')

        def response(self, data, status=200, content_type='application/json; charset=utf-8', attachment=False):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Security-Policy', CSP)
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
            if attachment:
                self.send_header('Content-Disposition', 'attachment; filename="review-volumes.zip"')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                self.headers_checked()
                url = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path in ('/', '/app.js', '/style.css'):
                    name, ctype = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}[url.path]
                    return self.response(files('pst2pdf').joinpath('web', name).read_bytes(), content_type=ctype + '; charset=utf-8')
                if url.path == '/api/session':
                    return self.response(dict(token=token, projects=store.projects()))
                if url.path == '/api/messages':
                    return self.response(store.listing(query['project'], json.loads(query.get('filters', '{}')), query.get('page', 1)))
                if url.path == '/api/message':
                    doc = store.get(query['project'])
                    item = next((i for i in doc['items'] if i['id'] == query['id']), None)
                    if item is None:
                        raise ValueError('Message not found')
                    return self.response(item)
                if url.path == '/api/jobs':
                    return self.response(store.jobs(query['project']))
                if url.path == '/api/package':
                    job = store.job(query['id'])
                    if job['status'] != 'completed' or job['mode'] != 'review':
                        raise ValueError('Review volumes are not available')
                    path = store.runs / job['id'] / 'review-volumes.zip'
                    # Stream potentially large ZIPs, rather than retaining a complete volume in RAM.
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/zip')
                    self.send_header('Content-Length', str(path.stat().st_size))
                    self.send_header('Content-Disposition', 'attachment; filename="review-volumes.zip"')
                    self.send_header('Content-Security-Policy', CSP)
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    import shutil
                    with path.open('rb') as fh:
                        shutil.copyfileobj(fh, self.wfile)
                    return
                self.response(dict(error='Not found'), 404)
            except (ValueError, KeyError, OSError) as exc:
                self.response(dict(error=str(exc)), 400)
            except Exception:
                self.response(dict(error='Local request failed; restart the interface and retry'), 500)

        def do_POST(self):
            try:
                self.headers_checked(mutate=True)
                url = urlsplit(self.path)
                length = int(self.headers.get('Content-Length', '-1'))
                if url.path == '/api/upload':
                    query = parse_qs(url.query)
                    return self.response(store.upload(query['name'][0], self.rfile, length))
                if not 0 <= length <= 5 * 1024**2:
                    raise ValueError('Invalid request size')
                if self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('JSON content type required')
                data = json.loads(self.rfile.read(length))
                if url.path == '/api/import':
                    doc = store.import_archives(data['uploads'], data.get('timezone', 'America/Chicago'))
                    return self.response(dict(id=doc['id'], counts=counts(doc)))
                if url.path == '/api/open':
                    doc = store.get(data['project'], validate=True)
                    return self.response(dict(id=doc['id']))
                if url.path == '/api/decide':
                    return self.response(store.change(data['project'], data['revision'], data.get('include'), data.get('reason', ''),
                                                      ids=data.get('ids'), filters=data.get('filters')))
                if url.path == '/api/undo':
                    return self.response(store.change(data['project'], data['revision'], undo=True))
                if url.path == '/api/generate':
                    return self.response(store.enqueue(data['project'], data['revision'], data['settings']))
                self.response(dict(error='Not found'), 404)
            except (ValueError, KeyError, OSError) as exc:
                self.response(dict(error=str(exc)), 400)
            except Exception:
                self.response(dict(error='Local request failed; no generation was accepted'), 500)

    return ThreadingHTTPServer((host, port), Handler)


def serve(root, host='127.0.0.1', port=8765):
    server = make_server(root, host, port)
    print(f'Local review interface listening on port {port}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
