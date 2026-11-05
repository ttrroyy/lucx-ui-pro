"""Only HY2 and Telegram are selectable as fresh-install additions."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest

SOURCE = (Path(__file__).resolve().parents[1] / 'lucx-ui-latest.sh').read_text(encoding='utf-8')
BASH = os.environ.get('LUCX_TEST_BASH') or shutil.which('bash')
SELECTOR = re.search(r'^choose_extra_inbounds\(\) \{\n.*?^\}\n', SOURCE, re.M | re.S)[0]


@unittest.skipUnless(BASH, 'bash required')
class InstallComponents(unittest.TestCase):
    def select(self, answers, arch='x86_64'):
        script = 'msg_inf(){ :; }; msg_err(){ echo "$1"; };\n'
        script += 'uname(){ echo ' + arch + '; }; choose_hy2_port(){ hy2_port=42443; };\n'
        script += SELECTOR + '\nchoose_extra_inbounds || exit $?\nprintf "RESULT=%s,%s,%s\\n" "$DEPLOY_HY2" "$DEPLOY_TPROXY" "$hy2_port"\n'
        result = subprocess.run([BASH, '-c', script], input=answers.encode(), capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode()

    def test_hy2_only(self):
        self.assertIn('RESULT=1,,42443', self.select('2\n1\n'))

    def test_telegram_only(self):
        self.assertIn('RESULT=2,1,', self.select('3\n1\n'))

    def test_both(self):
        self.assertIn('RESULT=1,1,42443', self.select('2,3\n1\n'))

    def test_no_additional_inbounds(self):
        self.assertIn('RESULT=2,,', self.select('1\n1\n'))

    def test_removed_choices_and_mixed_none_do_not_deploy(self):
        output = self.select('4\n5\n6\n1,2\n2,5\n1\n1\n')
        self.assertIn('RESULT=2,,', output)
        self.assertIn('Введите 2, 3 или 2,3.', output)

    def test_reselect_discards_previous_selection(self):
        self.assertIn('RESULT=2,,', self.select('2,3\n2\n1\n1\n'))

    def test_telegram_architecture_check_does_not_block_hy2(self):
        output = self.select('3\n2\n1\n', arch='aarch64')
        self.assertIn('доступен только на x86_64', output)
        self.assertIn('RESULT=1,,42443', output)

    def test_unsupported_installers_and_firewall_openings_removed(self):
        self.assertNotRegex(SOURCE, r'DEPLOY_(QWDTT|CSQTT|OPENFLUX)|_insert_one_extra|PY_OPENFLUX_VERSION')
        self.assertNotRegex(SOURCE, r'ufw allow (?:56000|56001|56003|46000)/udp|ufw allow 18445/tcp')
        menu = re.search(r'^components_menu\(\) \{\n.*?^\}\n', SOURCE, re.M | re.S)[0]
        self.assertNotIn('HY2', menu)
        self.assertNotIn('Hysteria', menu)
