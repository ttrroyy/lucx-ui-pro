#!/usr/bin/env python3
"""Local per-client YAML renderer, with identical GET/HEAD response headers."""
import argparse
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

TEMPLATE = Path('/var/www/subpage/clash.yaml.tpl')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.render()

    def do_HEAD(self):
        self.render()

    def render(self):
        parsed = urlsplit(self.path)
        if parsed.path == '/health':
            return self.reply(200, b'ok', 'text/plain')
        if parsed.path != '/api/clash':
            return self.reply(404, b'not found', 'text/plain')
        sub_id = parse_qs(parsed.query).get('sub_id', [''])[0]
        if not re.fullmatch(r'[A-Za-z0-9._~-]{1,256}', sub_id):
            return self.reply(400, b'invalid subscription id', 'text/plain')
        try:
            body = TEMPLATE.read_text(encoding='utf-8').replace('${SUB_ID}', sub_id).encode('utf-8')
        except OSError:
            return self.reply(503, b'template unavailable', 'text/plain')
        self.reply(200, body, 'text/yaml; charset=utf-8')

    def reply(self, status, body, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if status == 200 and content_type.startswith('text/yaml'):
            self.send_header('Content-Disposition', 'attachment; filename="clash.yaml"')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        # nginx already logs requests; do not duplicate secret subscription IDs
        # in the renderer's system journal.
        pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
