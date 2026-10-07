"""AWG module installation stays upstream; the initial opt-out is respected."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import io
from types import SimpleNamespace

repo = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('awg_bbr', repo/'assets/compat/awg-bbr.py')
awg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(awg)
spec = importlib.util.spec_from_file_location('compat', repo/'assets/compat/pro-compat.py')
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


class NativeAWG(unittest.TestCase):
    def test_only_bbr_assignments_removed(self):
        text = '#!/bin/bash\nnet.core.default_qdisc = fq\nnet.core.rmem_max = 33554432\nnet.ipv4.tcp_congestion_control = bbr\napply_udp_tunnel_abi_compat compat/compat.h\n'
        expected = '#!/bin/bash\nnet.core.rmem_max = 33554432\napply_udp_tunnel_abi_compat compat/compat.h\n'
        self.assertEqual(awg.strip_bbr(text), expected)
        self.assertEqual(awg.strip_bbr(expected), expected)

    def test_awg_cannot_override_panel_with_comments_or_other_values(self):
        text = '# retained comment\nnet.core.default_qdisc = fq # comment\nnet.ipv4.tcp_congestion_control = bbr # comment\nnet.core.default_qdisc = cake\nnet.ipv4.tcp_congestion_control = cubic\nnet.core.rmem_max = 33554432\n'
        self.assertEqual(awg.strip_bbr(text), '# retained comment\nnet.core.rmem_max = 33554432\n')

    def test_panel_web_disable_and_old_bbr_baseline_survive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root/'etc/sysctl.d/99-bbr-x-ui.conf'
            config.parent.mkdir(parents=True)
            config.write_text('#fq:bbr\nnet.core.default_qdisc = fq\nnet.ipv4.tcp_congestion_control = bbr\n')
            awg.sync_panel_state(root)
            self.assertTrue(config.read_text().startswith('#fq_codel:cubic\n'))
            config.unlink()  # The native web endpoint removes its drop-in.
            awg.sync_panel_state(root)
            self.assertIn('net.ipv4.tcp_congestion_control = cubic', config.read_text())
            self.assertIn('net.core.default_qdisc = fq_codel', config.read_text())
            before = config.read_bytes()
            awg.sync_panel_state(root)
            self.assertEqual(config.read_bytes(), before)

    def test_old_cli_disabled_marker_migrates_without_enabling_bbr(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'etc/sysctl.d').mkdir(parents=True)
            marker = root/'etc/x-ui/.lucx-bbr-restore'
            marker.parent.mkdir(parents=True)
            marker.write_text('fq_codel:cubic\n')
            awg.sync_panel_state(root)
            self.assertIn('net.ipv4.tcp_congestion_control = cubic',
                          (root/'etc/sysctl.d/99-bbr-x-ui.conf').read_text())

    def test_old_wrapper_restored_before_helper_retired(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'installer.sh'
            path.write_text('#!/bin/bash\n# LUCX AWG installer\npython3 /usr/local/lib/lucx-ui-pro/awg-compat.py ready\n')
            original = b'#!/bin/bash\necho original\nnet.ipv4.tcp_congestion_control = bbr\n'
            with patch.object(awg.urllib.request, 'urlopen', return_value=io.BytesIO(original)), patch.object(awg.subprocess, 'run'):
                awg.prepare_installer(path, '3.9.0-lucx.280')
            self.assertEqual(path.read_text(), '#!/bin/bash\necho original\n')

    def test_cli_wrapper_removed_without_changing_other_functions(self):
        text = 'install_awg_module() {\n    /usr/local/sbin/lucx-awg-sysctl-guard || return 1\n    bash "$script" "$@"\n    local rc=$?\n    /usr/local/sbin/lucx-awg-sysctl-guard >/dev/null 2>&1 || true\n    return $rc\n}\nuninstall_awg_module() { echo uninstall; }\n'
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cli';path.write_text(text)
            awg.clean_cli(path)
            self.assertEqual(path.read_text(), 'install_awg_module() {\n    bash "$script" "$@"\n}\nuninstall_awg_module() { echo uninstall; }\n')

    def test_cleanup_retires_only_pro_and_partial_same_pin(self):
        calls=[]
        def run(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(stdout=('amneziawg/abc-lucxpro280, kernel: built\n'
                                           'amneziawg/abc, kernel: installed\n'
                                           'amneziawg/other-native, kernel: installed\n') if args[1]=='status' else '')
        with patch.object(awg.subprocess,'run',side_effect=run), patch.object(awg,'Path') as path:
            path.return_value.glob.return_value=[]
            awg.cleanup_legacy_modules()
            path.return_value.unlink.assert_called_once_with(missing_ok=True)
        removed=[args[5] for args in calls if args[1]=='remove']
        self.assertEqual(removed,['abc','abc-lucxpro280'])

    def test_initial_selector_and_install_branch(self):
        bash=os.environ.get('LUCX_TEST_BASH') or shutil.which('bash')
        if not bash:
            self.skipTest('bash unavailable')
        text=(repo/'lucx-ui-latest.sh').read_text(encoding='utf-8')
        a=text.index('choose_amneziawg() {');b=text.index('\n}\n',a)+3
        selector=text[a:b]
        a=text.index('    if [[ "${DEPLOY_AWG}" == "y" ]]; then\n        install_awg_kernel')
        b=text.index('\n    fi',a)+7
        branch=text[a:b]
        for choice in ('y','n'):
            code='set -e\nmsg_inf(){ :; }; install_awg_kernel(){ echo INSTALLED; };\n'+selector+'\nDEPLOY_AWG='+choice+'\nchoose_amneziawg\n'+branch
            result=subprocess.run([bash,'-c',code],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual('INSTALLED' in result.stdout,choice=='y')

    def test_update_preserves_native_call_and_opt_out(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'update.sh'
            path.write_text('XUI_UPDATE_TAG=x\n    config_after_update\n        bash "${awg_installer}" || echo AWG_FAILED\n')
            compat.adapt_updater(path)
            result=path.read_text()
            self.assertIn('LUCX_PRO_AWG_ENABLED',result)
            self.assertIn('bash "${awg_installer}"',result)
            self.assertNotIn('--no-kernel-upgrade',result)
            self.assertIn('exit 1;',result)

    def test_update_opt_out_and_native_failure(self):
        bash=os.environ.get('LUCX_TEST_BASH') or shutil.which('bash')
        if not bash:
            self.skipTest('bash unavailable')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'update.sh'
            path.write_text('XUI_UPDATE_TAG=x\n    config_after_update\n        bash "${awg_installer}" || echo AWG_FAILED\n')
            compat.adapt_updater(path)
            result=path.read_text().split('        if [[',1)[1]
            result='        if [['+result
            result=result.replace('/usr/local/sbin/lucx-awg-sysctl-guard','bbr_guard')
            for enabled,failed in ((0,0),(1,0),(1,1)):
                code=('bbr_guard(){ echo GUARDED; }; bash(){ echo INSTALLED; return '+str(failed)+'; };\n'
                      'LUCX_PRO_AWG_ENABLED='+str(enabled)+'\n'+result)
                run=subprocess.run([bash,'-c',code],capture_output=True,text=True)
                self.assertEqual('INSTALLED' in run.stdout,bool(enabled))
                self.assertEqual('GUARDED' in run.stdout,bool(enabled))
                self.assertEqual(run.returncode,1 if enabled and failed else 0)
