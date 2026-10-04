"""Restored firewall sets must exist before service/UFW startup."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

class RestoreFirewallSets(unittest.TestCase):
    def test_ipset_failure_stops_before_service_start(self):
        bash=os.environ.get('LUCX_TEST_BASH') or shutil.which('bash')
        if not bash:self.skipTest('bash unavailable')
        source=(Path(__file__).resolve().parents[1]/'assets/backup/lucx-ui-backup.sh').read_text(encoding='utf-8')
        start=source.index('    local ipset_archive\n')
        end=source.index('    # ── systemd',start)
        block=source[start:end]
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);saved=root/'sets';saved.write_text('create example hash:net\n')
            block=block.replace('/etc/ipset.conf','"'+saved.as_posix()+'"').replace('/etc/iptables/ipsets','"'+(root/'absent').as_posix()+'"')
            for status in (0,1):
                script=root/'restore.sh';script.write_text('die(){ exit 1; }; ipset(){ echo SETS; return '+str(status)+'; }; main(){\n'+block+'echo SERVICES; }; main\n',encoding='utf-8',newline='\n')
                result=subprocess.run([bash,str(script)],capture_output=True,text=True,timeout=4)
                self.assertEqual(result.returncode,status)
                self.assertIn('SETS',result.stdout)
                self.assertEqual('SERVICES' in result.stdout,status==0)
