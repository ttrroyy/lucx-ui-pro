"""HY2 uses UDP: an occupied TCP listener must not reject the same port."""
import os,re,shutil,socket,subprocess,tempfile,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SOURCE=(ROOT/'lucx-ui-latest.sh').read_text(encoding='utf-8')

class Hy2PortSelection(unittest.TestCase):
    def test_latest_linux_release_skips_windows_only_and_prereleases(self):
        import json,sys
        body=re.search(r"<<'PY_LINUX_RELEASE'\n(.*?)\nPY_LINUX_RELEASE",SOURCE,re.S)[1]
        def release(tag, assets, **flags):
            names=assets+[a+'.sha256' for a in assets if a.endswith('.tar.gz')]
            return dict(tag_name=tag, assets=[dict(name=a,size=123) for a in names], **flags)
        fixtures=[release('v3.11.0-lucx.355',['x-ui-windows-amd64.zip']),
                  release('v3.11.0-lucx.354',['x-ui-linux-amd64.tar.gz'],prerelease=True),
                  release('v3.11.0-lucx.353',['x-ui-linux-arm64.tar.gz']),
                  release('v3.11.0-lucx.352',['x-ui-linux-amd64.tar.gz'])]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'releases.json'
            path.write_text(json.dumps(fixtures))
            subprocess.run([sys.executable,'-c',body,str(path),'amd64'],check=True,capture_output=True)
            self.assertEqual(json.loads(path.read_text())['tag_name'],'v3.11.0-lucx.352')
            path.write_text(json.dumps(fixtures[:1]))
            result=subprocess.run([sys.executable,'-c',body,str(path),'amd64'],capture_output=True)
            self.assertNotEqual(result.returncode,0)

    def select(self, values, env='', stub=''):
        bash=os.environ.get('LUCX_TEST_BASH') or shutil.which('bash')
        if not bash:self.skipTest('bash unavailable')
        function=re.search(r'^choose_hy2_port\(\) \{.*?^\}',SOURCE,re.M|re.S)[0]
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'select.sh'
            p.write_text('msg_inf(){ :; }; msg_err(){ :; };\n'+env+'\n'+stub+'\n'+function+'\nchoose_hy2_port || exit $?\nprintf "SELECTED=%s\\n" "$hy2_port"\n',newline='\n')
            result=subprocess.run([bash,str(p)],input=values,text=True,capture_output=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            return result.stdout

    def test_tcp_443_does_not_reserve_udp_443(self):
        result=self.select('443\n',stub='ss(){ [[ "$1" == -Hlnu ]] || { echo wrong-protocol; }; }')
        self.assertIn('SELECTED=443',result)
        self.assertNotIn('SELECTED=44443',result)

    def test_removed_sidecar_ports_are_available_when_no_udp_listener_exists(self):
        for port in (56000, 56001, 56003, 46000):
            with self.subTest(port=port):
                self.assertIn('SELECTED='+str(port),self.select(str(port)+'\n',stub='ss(){ :; }'))

    def test_real_udp_listener_rejected_tcp_listener_accepted(self):
        if not shutil.which('ss'):self.skipTest('Linux ss unavailable')
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as udp, socket.socket(socket.AF_INET,socket.SOCK_STREAM) as tcp:
            udp.bind(('127.0.0.1',0));tcp.bind(('127.0.0.1',0));tcp.listen()
            self.assertIn('SELECTED='+str(tcp.getsockname()[1]),self.select(str(udp.getsockname()[1])+'\n'+str(tcp.getsockname()[1])+'\n'))

    def test_fresh_release_ownership_and_native_service_account(self):
        section=SOURCE[SOURCE.index('install_panel() {'):SOURCE.index('setup_fail2ban() {')]
        self.assertIn('tar --no-same-owner -zxvf',section)
        self.assertIn('id lucx-mtproxy',section)
        self.assertIn('useradd -r -M -N -s /usr/sbin/nologin lucx-mtproxy',section)

    def test_release_404_stops_before_reinstall_removes_existing_panel(self):
        bash=os.environ.get('LUCX_TEST_BASH') or shutil.which('bash')
        if not bash:self.skipTest('bash unavailable')
        start=SOURCE.index('_arch() {')
        end=SOURCE.index('# This is the first installation question.',start)
        script=r"""
is_full_install_request() { return 0; }
existing_lucx_install_detected() { return 0; }
confirm_reinstall() { return 0; }
msg_err() { printf '%s\n' "$*" >&2; }
uninstall_xui() { echo UNINSTALL_RAN; }
curl() { echo 'curl: HTTP 404' >&2; return 22; }
PANEL_VERSION=v3.11.0-lucx.99999
PREINSTALL_STATE_DIR=$1
"""+SOURCE[start:end]
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'owned-by-lucx-ui-pro').touch()
            result=subprocess.run([bash,'-c',script,'test',folder],
                                  capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,1)
        self.assertIn('HTTP 404',result.stderr)
        self.assertIn('/v3.11.0-lucx.99999/x-ui-linux-',result.stderr)
        self.assertNotIn('UNINSTALL_RAN',result.stdout)

    def test_certificate_request_does_not_expand_unrelated_ip_certificate(self):
        bash=os.environ.get('LUCX_TEST_BASH') or shutil.which('bash')
        if not bash:self.skipTest('bash unavailable')
        function=re.search(r'^ensure_site_certificate\(\) \{.*?^\}',SOURCE,re.M|re.S)[0]
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'cert.sh'
            p.write_text('certbot(){ printf "ARG=%s\\n" "$@"; return 1; };\n'+function+'\nensure_site_certificate cert-request-test.invalid\n',newline='\n')
            result=subprocess.run([bash,str(p)],capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,1)
            self.assertIn('ARG=--cert-name\nARG=cert-request-test.invalid',result.stdout)
            self.assertNotIn('ARG=--expand',result.stdout)

    def test_update_staging_does_not_keep_ci_owner(self):
        import io,sys,tarfile
        if os.name != 'posix':self.skipTest('POSIX ownership check')
        body=re.search(r"<<'PY_UPDATE_EXTRACT'\n(.*?)\nPY_UPDATE_EXTRACT",SOURCE,re.S)[1]
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);archive=root/'release.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:
                info=tarfile.TarInfo('x-ui/bin/test-sidecar');info.uid=info.gid=1001;info.uname=info.gname='runner';info.mode=0o755;info.size=4
                tar.addfile(info,io.BytesIO(b'test'))
            subprocess.run([sys.executable,'-c',body,str(archive),str(root/'out')],check=True,capture_output=True)
            self.assertEqual((root/'out/x-ui/bin/test-sidecar').stat().st_uid,os.getuid())
