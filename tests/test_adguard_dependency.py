"""Do not remove the managed DNS endpoint while Xray still depends on it."""
from contextlib import closing, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = (Path(__file__).resolve().parents[1] / 'lucx-ui-latest.sh').read_text(encoding='utf-8')
CODE = re.search(r"<<'PY_ADGUARD_DNS_DEPENDENCY'\n(.*?)\nPY_ADGUARD_DNS_DEPENDENCY", SOURCE, re.S)[1]


class AdGuardDependency(unittest.TestCase):
    def inspect(self, config):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); database = root / 'db'; domains = root / 'domains'
            domains.write_text('panel.example\npanel.example\n')
            with closing(sqlite3.connect(database)) as db, db:
                db.execute('CREATE TABLE settings(id INTEGER PRIMARY KEY,key TEXT,value TEXT)')
                db.execute('INSERT INTO settings(key,value) VALUES(?,?)', ('xrayTemplateConfig', json.dumps(config)))
            output = io.StringIO()
            with patch.object(sys, 'argv', ['helper', str(database), str(domains)]), redirect_stdout(output):
                exec(compile(CODE, 'adguard-dependency', 'exec'), {})
            return output.getvalue().strip()

    def test_self_hosted_dns_and_legacy_wrappers_detected(self):
        value = {'dns': {'servers': [{'address': 'https://panel.example/dns-query'}]}}
        for config in (value, json.dumps(value), {'xraySetting': json.dumps(value)}):
            with self.subTest(config=config):
                self.assertEqual(self.inspect(config), 'dependent')

    def test_external_dns_and_disabled_dns_do_not_block_removal(self):
        for config in ({}, {'dns': None}, {'dns': {'servers': None}},
                       {'dns': {'servers': ['https://dns.example/dns-query', 'localhost']}}):
            with self.subTest(config=config):
                self.assertEqual(self.inspect(config), 'clear')

    def test_only_actual_managed_https_endpoint_counts(self):
        for address, expected in [('https://panel.example:443/dns-query/', 'dependent'),
                                  ('https://panel.example:8443/dns-query', 'clear'),
                                  ('https://panel.example/unrelated', 'clear'),
                                  ('http://panel.example/dns-query', 'clear')]:
            with self.subTest(address=address):
                self.assertEqual(self.inspect({'dns': {'servers': [address]}}), expected)

    def test_full_panel_uninstall_bypasses_no_longer_needed_dependency(self):
        self.assertIn('uninstall_adguard quiet || msg_err', SOURCE)
        function = re.search(r'^uninstall_adguard\(\) \{\n.*?^\}\n', SOURCE, re.M | re.S)[0]
        self.assertLess(function.index('PY_ADGUARD_DNS_DEPENDENCY'), function.index('systemctl stop AdGuardHome'))


@unittest.skipUnless(os.environ.get('LUCX_TEST_BASH') or shutil.which('bash'), 'bash required')
class AdGuardRemovalConfirmation(unittest.TestCase):
    def invoke(self, answer, quiet=False):
        bash = os.environ.get('LUCX_TEST_BASH') or shutil.which('bash')
        function = re.search(r'^uninstall_adguard\(\) \{\n.*?^\}\n', SOURCE, re.M | re.S)[0]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'opt/AdGuardHome').mkdir(parents=True); (root / 'db').touch()
            for prefix in ('/opt/AdGuardHome', '/etc/nginx', '/root/.lucx-adguard-info'):
                function = function.replace(prefix, root.as_posix() + prefix)
            script = 'XUIDB=' + shlex.quote((root / 'db').as_posix()) + '; PREINSTALL_STATE_DIR=' + shlex.quote(root.as_posix()) + '\n'
            script += 'msg_inf(){ echo "$1"; }; msg_err(){ echo "$1"; }; msg_ok(){ :; };\n'
            script += 'python3(){ cat >/dev/null; echo dependent; }; systemctl(){ echo SERVICE_CALL; }; rm(){ echo REMOVE_CALL; }; nginx(){ :; };\n'
            script += function + '\nuninstall_adguard' + (' quiet' if quiet else '') + '\n'
            return subprocess.run([bash, '-c', script], input=answer.encode(), capture_output=True, timeout=10)

    def test_warning_can_be_explicitly_confirmed(self):
        result = self.invoke('1\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Предупреждение'.encode(), result.stdout)
        self.assertIn(b'REMOVE_CALL', result.stdout)

    def test_cancel_keeps_adguard_and_services_untouched(self):
        result = self.invoke('0\n')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn(b'REMOVE_CALL', result.stdout)
        self.assertNotIn(b'SERVICE_CALL', result.stdout)

    def test_full_uninstall_needs_no_dns_warning_confirmation(self):
        result = self.invoke('', quiet=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Предупреждение'.encode(), result.stdout)
        self.assertIn(b'REMOVE_CALL', result.stdout)
