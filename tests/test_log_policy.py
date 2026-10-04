import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

repo = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('logs', repo / 'assets/compat/log-policy.py')
logs = importlib.util.module_from_spec(spec); spec.loader.exec_module(logs)


class LogPolicy(unittest.TestCase):
    def test_budget_includes_archives_separately_for_each_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for prefix in ('access','error'):
                for name,size,mtime in [('.log',30,100),('.log.1',40,90),('.log.2.gz',40,80)]:
                    path=root/(prefix+name);path.write_bytes(b'x'*size);os.utime(path,(mtime,mtime))
            other=root/'unrelated.txt';other.write_text('keep')
            logs.prune(root,budget=100,age=1000,now=100)
            for prefix in ('access','error'):
                self.assertEqual(sum(p.stat().st_size for p in root.glob(prefix+'.log*')),70)
                self.assertFalse((root/(prefix+'.log.2.gz')).exists())
                self.assertEqual((root/(prefix+'.log')).stat().st_size,30)
            self.assertEqual(other.read_text(),'keep')

    def test_expired_archives_removed_even_when_logs_are_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name,mtime in [('access.log',1),('access.log.1',1),('access.log.2.gz',95)]:
                path=root/name;path.write_text('record');os.utime(path,(mtime,mtime))
            logs.prune(root,budget=1000,age=10,now=100)
            self.assertTrue((root/'access.log').exists())
            self.assertFalse((root/'access.log.1').exists())
            self.assertTrue((root/'access.log.2.gz').exists())

    def test_install_is_idempotent_and_remove_restores_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);original=root/logs.CONFIGS[0]
            original.parent.mkdir(parents=True);original.write_text('original rotation\n')
            logs.install(root);logs.install(root)
            self.assertIn('maxsize 25M',original.read_text())
            self.assertIn('SystemMaxUse=100M',(root/logs.CONFIGS[1]).read_text())
            self.assertIn('MaxRetentionSec=3day',(root/logs.CONFIGS[1]).read_text())
            logs.remove(root)
            self.assertEqual(original.read_text(),'original rotation\n')
            self.assertFalse((root/logs.CONFIGS[1]).exists())
            self.assertFalse((root/'etc/systemd/system/lucx-log-policy.timer').exists())

    def test_embedded_helpers_match_and_all_lifecycle_hooks_exist(self):
        source=(repo/'assets/compat/log-policy.py').read_text(encoding='utf-8').rstrip()
        for relative in ('lucx-ui-latest.sh','assets/backup/lucx-ui-backup.sh'):
            text=(repo/relative).read_text(encoding='utf-8')
            actual=text.split("<<'PY_LUCX_LOG_POLICY'\n",1)[1].split('\nPY_LUCX_LOG_POLICY',1)[0]
            self.assertEqual(source,actual)
            self.assertIn('run_log_policy install',text)
        self.assertIn('run_log_policy remove',(repo/'lucx-ui-latest.sh').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
