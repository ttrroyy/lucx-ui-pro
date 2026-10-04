"""Closed input must terminate selectors instead of spinning indefinitely."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

class SelectorEOF(unittest.TestCase):
    def test_closed_input_terminates_selectors(self):
        bash = os.environ.get('LUCX_TEST_BASH') or shutil.which('bash')
        if not bash:
            self.skipTest('bash unavailable')
        source = (Path(__file__).resolve().parents[1] / 'lucx-ui-latest.sh').read_text(encoding='utf-8')
        for name in ('choose_xray_dns','choose_extra_inbounds','choose_adguard','choose_rkn_guard'):
            with self.subTest(selector=name), tempfile.TemporaryDirectory() as folder:
                function = re.search(r'^'+name+r'\(\) \{.*?^\}',source,re.M|re.S)
                self.assertIsNotNone(function)
                script=Path(folder)/'eof.sh'
                script.write_text('msg_inf(){ :; }; msg_err(){ :; }; DEPLOY_AGH=1\n'+function[0]+'\n'+name+'\n',encoding='utf-8',newline='\n')
                result=subprocess.run([bash,str(script)],input=b'',capture_output=True,timeout=4)
                self.assertEqual(result.returncode,1)
