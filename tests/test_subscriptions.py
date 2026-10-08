"""HTTP import probes and narrow upgrade of existing managed subscriptions."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

repo = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


renderer = load('renderer', repo / 'assets/clash/clash-sub-server.py')
compat = load('compat', repo / 'assets/compat/pro-compat.py')


class Subscriptions(unittest.TestCase):
    def test_head_matches_get_without_body_for_success_and_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            template = Path(folder) / 'template'
            template.write_text('proxies: []\n# ${SUB_ID} Кириллица\n', encoding='utf-8')
            with patch.object(renderer, 'TEMPLATE', template):
                server = ThreadingHTTPServer(('127.0.0.1', 0), renderer.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    for path, status in [('/health', 200), ('/api/clash?sub_id=client-01', 200),
                                         ('/api/clash', 400), ('/api/clash?sub_id=a%2Fb', 400),
                                         ('/missing', 404)]:
                        self.assert_pair(server.server_port, path, status)
                    template.unlink()
                    self.assert_pair(server.server_port, '/api/clash?sub_id=client-01', 503)
                finally:
                    server.shutdown(); server.server_close(); thread.join()

    def assert_pair(self, port, path, status):
        responses = []
        for method in ('GET', 'HEAD'):
            conn = HTTPConnection('127.0.0.1', port, timeout=3)
            conn.request(method, path)
            response = conn.getresponse()
            responses.append((response.status, dict(response.getheaders()), response.read()))
            conn.close()
        get, head = responses
        self.assertEqual(get[0], status); self.assertEqual(head[0], status)
        self.assertEqual(head[2], b'')
        self.assertEqual(int(head[1]['Content-Length']), len(get[2]))
        for field in ('Content-Type', 'Content-Length', 'Cache-Control', 'Content-Disposition'):
            self.assertEqual(get[1].get(field), head[1].get(field))

    def test_existing_renderer_upgrade_preserves_unit_port_and_template(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            unit = root / 'etc/systemd/system/lucx-clash-sub.service'
            tpl = root / 'var/www/subpage/clash.yaml.tpl'
            script = root / 'usr/local/lib/lucx-ui-pro/clash-sub-server.py'
            for p in (unit, tpl, script): p.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text('ExecStart=/usr/bin/python3 /usr/local/lib/lucx-ui-pro/clash-sub-server.py --port 12345\n# custom\n')
            tpl.write_text('my original template\n')
            script.write_text('old GET-only implementation\n')
            before = (unit.read_bytes(), tpl.read_bytes())
            compat.repair_clash_renderer(root)
            self.assertEqual(script.read_text(), (repo / 'assets/clash/clash-sub-server.py').read_text())
            timestamp = script.stat().st_mtime_ns
            compat.repair_clash_renderer(root)
            self.assertEqual(script.stat().st_mtime_ns, timestamp)
            self.assertEqual(before, (unit.read_bytes(), tpl.read_bytes()))
            unit.write_text('ExecStart=/custom/user-server.py --port 12345\n')
            script.write_text('user replacement')
            compat.repair_clash_renderer(root)
            self.assertEqual(script.read_text(), 'user replacement')

    def test_legacy_yaml_upgrade_keeps_custom_fields_and_other_providers(self):
        original = '''custom-global: keep
proxy-providers:
  sub:
    type: http
    url: https://test.example/__lucx_provider/${SUB_ID}
    path: ./proxy_providers/custom.yaml
    override:
      custom-new: keep
      override-expr:
        - '.custom = "keep"'
    health-check:
      enable: true
  user-provider:
    type: file
    path: ./user.yaml
rules:
  - MATCH,DIRECT
'''
        repaired = compat.repair_clash_template(original)
        for value in ('custom-global: keep', 'custom-new: keep', "'.custom = \"keep\"'",
                      '    path: ./proxy_providers/custom.yaml', '  user-provider:\n    type: file\n    path: ./user.yaml\n',
                      'rules:\n  - MATCH,DIRECT\n'):
            self.assertIn(value, repaired)
        self.assertIn('support-x25519mlkem768', repaired)
        self.assertIn('.["client-fingerprint"]) = "chrome"', repaired)
        self.assertNotIn('global-client-fingerprint:', repaired)
        self.assertEqual(repaired, compat.repair_clash_template(repaired))

    def test_fresh_install_renderer_is_the_same_as_upgrade_renderer(self):
        source = (repo / 'assets/clash/clash-sub-server.py').read_text()
        self.assertEqual(compat.CLASH_RENDERER_SOURCE.lstrip('\n'), source)
        main = (repo / 'lucx-ui-latest.sh').read_text()
        self.assertEqual(main.split("<<'PY_CLASH_SERVER'\n", 1)[1].split('\nPY_CLASH_SERVER\n', 1)[0] + '\n', source)


if __name__ == '__main__':
    unittest.main()
