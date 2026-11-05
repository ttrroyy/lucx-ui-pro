"""Component selection validates all numbers before changing the installation."""
import os
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE = (Path(__file__).resolve().parents[1] / 'lucx-ui-latest.sh').read_text(encoding='utf-8')
BASH = os.environ.get('LUCX_TEST_BASH') or shutil.which('bash')
MENU = re.search(r'^components_menu\(\) \{\n.*?^\}\n', SOURCE, re.M | re.S)[0]


@unittest.skipUnless(BASH, 'bash required')
class ComponentsMenu(unittest.TestCase):
    def invoke(self, answers, owned=True, fail=''):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            if owned:
                (root / 'owned-by-lucx-ui-pro').touch()
            functions = ('install_adguard', 'uninstall_adguard', 'print_adguard_results',
                         'install_rkn_guard', 'uninstall_rkn_guard',
                         'install_bittorrent_guard', 'uninstall_bittorrent_guard',
                         'install_tg_web_proxy', 'uninstall_tg_web_proxy')
            script = 'PREINSTALL_STATE_DIR=' + shlex.quote(root.as_posix()) + '\n'
            script += 'msg_inf(){ :; }; msg_err(){ echo "$1"; }; choose_rkn_guard(){ DEPLOY_RKN=1; };\n'
            script += 'show_components(){ echo COMPONENT_STATUS; };\n'
            for name in functions:
                script += name + '(){ echo CALL:' + name + '; return ' + ('9' if name == fail else '0') + '; };\n'
            script += MENU + '\ncomponents_menu\n'
            result = subprocess.run([BASH, '-c', script], input=answers.encode(),
                                    capture_output=True, timeout=10)
            result.stdout = result.stdout.decode()
            result.stderr = result.stderr.decode()
            return result

    def calls(self, result):
        return re.findall(r'CALL:(\w+)', result.stdout)

    def test_install_multiple_deduplicates_and_reuses_existing_installers(self):
        result = self.invoke('1\n1 3 1\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(result), ['install_adguard', 'print_adguard_results',
                                              'install_bittorrent_guard'])

    def test_reinstall_uses_current_component_semantics(self):
        result = self.invoke('2\n4 2\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(result), ['install_tg_web_proxy', 'install_rkn_guard'])

    def test_remove_multiple(self):
        result = self.invoke('3\n2 4\n')
        self.assertEqual(self.calls(result), ['uninstall_rkn_guard', 'uninstall_tg_web_proxy'])

    def test_invalid_selection_never_partially_executes(self):
        result = self.invoke('3\n1 99\n0\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(result), [])

    def test_unowned_installation_rejected_before_any_component_action(self):
        result = self.invoke('1\n3 1\n', owned=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(result), [])

    def test_standalone_guard_still_available(self):
        result = self.invoke('1\n3\n', owned=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(result), ['install_bittorrent_guard'])

    def test_component_failure_stops_remaining_actions(self):
        result = self.invoke('3\n1 4\n', fail='uninstall_adguard')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(result), ['uninstall_adguard'])

    def test_cancelled_adguard_warning_stops_batch_without_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'owned-by-lucx-ui-pro').touch()
            body = 'PREINSTALL_STATE_DIR=' + shlex.quote(root.as_posix()) + '\n'
            body += 'show_components(){ :; }; msg_inf(){ :; }; msg_err(){ :; }; uninstall_adguard(){ return 2; }; uninstall_tg_web_proxy(){ echo UNEXPECTED_TG_REMOVAL; };\n'
            result = subprocess.run([BASH, '-c', body + MENU + '\ncomponents_menu'],
                                    input=b'3\n1 4\n', capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(b'UNEXPECTED_TG_REMOVAL', result.stdout)

    def test_menu_excluded_from_full_reinstall_and_domain_questions(self):
        function = re.search(r'^is_full_install_request\(\) \{\n.*?^\}\n', SOURCE, re.M | re.S)[0]
        self.assertIn('"${COMPONENTS_ONLY}" != "y"', function)
        self.assertIn('if is_full_install_request; then\n    choose_auto_domains', SOURCE)

    def test_cancel_action_does_not_change_components(self):
        result = self.invoke('0\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(result), [])

    def test_comma_separated_selection(self):
        result = self.invoke('3\n1, 3,1\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(result), ['uninstall_adguard', 'uninstall_bittorrent_guard'])

    def test_status_shown_before_action_and_after_success(self):
        result = self.invoke('1\n3\n')
        self.assertEqual(result.stdout.count('COMPONENT_STATUS'), 2)

    def test_action_labels_and_invalid_input_are_explicit(self):
        for label in ('Отмена', 'Установить', 'Переустановить', 'Удалить'):
            self.assertIn(label, MENU)
        result = self.invoke('9\n1\n3\n')
        self.assertIn('Введите номер действия от 0 до 3.', result.stdout)
        self.assertEqual(self.calls(result), ['install_bittorrent_guard'])


@unittest.skipUnless(BASH, 'bash required')
class ComponentStatus(unittest.TestCase):
    def status(self, component, installed, active):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            if installed and component == 1:
                (root / 'opt/AdGuardHome').mkdir(parents=True)
            if installed and component == 3:
                path = root / 'usr/local/lib/vibehost-guard/guard.sh'
                path.parent.mkdir(parents=True); path.touch()
            function = re.search(r'^component_status\(\) \{\n.*?^\}\n', SOURCE, re.M | re.S)[0]
            for prefix in ('/opt/AdGuardHome', '/usr/local/lib/vibehost-guard/guard.sh'):
                function = function.replace(prefix, root.as_posix() + prefix)
            code = 'systemctl(){ return ' + ('0' if active else '1') + '; };\n'
            code += 'iptables(){ return ' + ('0' if active else '1') + '; };\n'
            if component == 2:
                code += 'command(){ if [[ \"$1 $2\" == \"-v rkn-guard\" ]]; then return ' + ('0' if installed else '1') + '; else builtin command \"$@\"; fi; };\n'
            code += function + '\ncomponent_status ' + str(component)
            result = subprocess.run([BASH, '-c', code], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.decode().strip()

    def test_missing_components(self):
        for component in (1, 2, 3):
            with self.subTest(component=component):
                self.assertEqual(self.status(component, False, False), 'Не установлен')

    def test_installed_inactive_components(self):
        for component in (1, 2, 3):
            with self.subTest(component=component):
                self.assertEqual(self.status(component, True, False), 'Установлен, неактивен')

    def test_active_components(self):
        for component in (1, 2, 3):
            with self.subTest(component=component):
                self.assertEqual(self.status(component, True, True), 'Активен')


class TelegramComponentStatus(unittest.TestCase):
    def status(self, enabled, *, renamed=False, reused=False, listener=True):
        code = re.search(r"<<'PY_COMPONENT_TG_STATUS'\n(.*?)\nPY_COMPONENT_TG_STATUS", SOURCE, re.S)[1]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); database = root / 'db'; manifest = root / 'architecture.json'
            secret = 'fixture-secret'
            manifest.write_text(json.dumps({'telegram': {'id': 1, 'secret_sha256': hashlib.sha256(secret.encode()).hexdigest()}}))
            with contextlib.closing(sqlite3.connect(database)) as db, db:
                db.execute('CREATE TABLE inbounds(id INTEGER,enable INTEGER,protocol TEXT,tag TEXT,settings TEXT)')
                db.execute('INSERT INTO inbounds VALUES(1,?,?,?,?)', (enabled, 'tproxy',
                    'custom' if renamed else 'inbound-tproxy', json.dumps({'secret': 'other' if reused else secret})))
            out = io.StringIO()
            connection = contextlib.nullcontext()
            with patch('sys.argv', ['helper', str(database), str(manifest)]), contextlib.redirect_stdout(out), \
                    patch('socket.create_connection', return_value=connection,
                          side_effect=None if listener else OSError('fixture listener absent')):
                exec(compile(code, 'tg-component-status', 'exec'), {})
            return out.getvalue().strip()

    def test_disabled_telegram_is_not_reported_active(self):
        self.assertEqual(self.status(False), 'Отключён в панели')

    def test_renamed_owned_telegram_is_detected(self):
        self.assertEqual(self.status(True, renamed=True), 'Активен')

    def test_reused_id_does_not_adopt_foreign_telegram(self):
        self.assertEqual(self.status(True, renamed=True, reused=True), 'Не установлен')

    def test_enabled_telegram_without_listener_is_not_reported_active(self):
        self.assertEqual(self.status(True, listener=False), 'Установлен, неактивен')
