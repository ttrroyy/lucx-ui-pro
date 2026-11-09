"""New native service permissions stay authoritative through updater adaptation."""
from pathlib import Path
import os,shutil,subprocess,tempfile,unittest
from test_update_compat import compat

class NativeReleaseContract(unittest.TestCase):
    def test_verified_staging_avoids_network_and_retains_checksum(self):
        bash = os.environ.get('LUCX_TEST_BASH') or shutil.which('bash')
        if not bash:
            self.skipTest('bash unavailable')
        native = '''XUI_UPDATE_TAG=x
prepare() {
    config_after_update
        bash "${awg_installer}" || echo failed
}
    require_repo_files "${script_ref}" "${required_files[@]}"
    ${curl_bin} -fLRo ${xui_folder}-linux-$(arch).tar.gz https://github.com/AlexeyLCP/lucx-ui/releases/download/${tag_version}/x-ui-linux-$(arch).tar.gz 2> /dev/null
archive="${xui_folder}-linux-$(arch).tar.gz"
    sidecar_code=$(${curl_bin} -sL --retry 3 --retry-delay 3 --connect-timeout 15 --max-time 60 -o "${archive}.sha256" -w '%{http_code}' "https://github.com/AlexeyLCP/lucx-ui/releases/download/${tag_version}/x-ui-linux-$(arch).tar.gz.sha256" 2> /dev/null)
    ${curl_bin} -fLRo "${xui_script_temp}" "https://raw.githubusercontent.com/AlexeyLCP/lucx-ui/${script_ref}/x-ui.sh" > /dev/null 2>&1
[[ "$sidecar_code" == 200 ]]
sha256sum -c "$archive.sha256"
cmp "$LUCX_PRO_PANEL_STAGE/x-ui.cli" "$xui_script_temp"
'''
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'panel.tar.gz').write_bytes(b'verified archive fixture')
            (root/'x-ui.cli').write_text('#!/bin/bash\n')
            path = root/'update.sh'
            path.write_text(native)
            compat.adapt_updater(path)
            # All network calls fail, so a successful run must use staging.
            harness = '''set -e
LUCX_PRO_PANEL_STAGE="$PWD"
xui_folder=x-ui
xui_script_temp=cli
release=debian
curl_bin=network_forbidden
network_forbidden(){ return 99; }
require_repo_files(){ return 98; }
arch(){ echo amd64; }
sha256sum panel.tar.gz | sed 's/panel.tar.gz/x-ui-linux-amd64.tar.gz/' > panel.sha256
source update.sh
'''
            (root/'test.sh').write_text(harness, newline='\n')
            result = subprocess.run([bash, 'test.sh'], cwd=root, text=True,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_native_caps_and_future_fields_are_preserved(self):
        native=('ReadWritePaths=-/etc/x-ui -/etc/amnezia -/etc/ufw -/etc/default/ufw\n'
                'CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW CAP_SETUID CAP_SETGID CAP_CHOWN CAP_KILL\n'
                'FuturePanelDirective=leave-intact\n')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'update.sh'
            path.write_text(native+'XUI_UPDATE_TAG=x\n    config_after_update\n        bash "${awg_installer}" || echo failed\n')
            compat.adapt_updater(path)
            self.assertTrue(path.read_text().startswith(native))
