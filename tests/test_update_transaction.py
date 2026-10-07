import json,os,shutil,sqlite3,subprocess,tempfile,unittest
from pathlib import Path
from test_update_compat import compat,database,repo

class UpdateTransaction(unittest.TestCase):
    def test_only_configured_sidecars_return_and_new_files_win(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            db=database(root);db.close()
            source=root/'old';dest=root/'usr/local/x-ui'
            for name,text in {'x-ui':'old panel','x-ui.sh':'old cli','obsolete.sh':'old script','bin/xray-linux-amd64':'old xray',
                              'bin/qwdtt-linux-amd64':'old qwdtt','bin/csqtt-linux-amd64':'custom csqtt',
                              'bin/openflux-linux-amd64':'unused core','bin/tunnel/qwdtt.conf':'config',
                              'bin/tunnel/qwdtt-data/cookies.json':'state','bin/tunnel/unknown.conf':'unrelated'}.items():
                file=source/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(text)
            (dest/'bin').mkdir(parents=True);(dest/'bin/qwdtt-linux-amd64').write_text('new qwdtt')
            compat.restore_sidecars(root,source,dest)
            self.assertEqual((dest/'bin/qwdtt-linux-amd64').read_text(),'new qwdtt')
            self.assertEqual((dest/'bin/csqtt-linux-amd64').read_text(),'custom csqtt')
            self.assertEqual((dest/'bin/tunnel/qwdtt-data/cookies.json').read_text(),'state')
            for name in ('x-ui','x-ui.sh','obsolete.sh','bin/xray-linux-amd64','bin/openflux-linux-amd64','bin/tunnel/unknown.conf'):
                self.assertFalse((dest/name).exists(),name)

    def test_renewal_transaction_and_shared_restore_helpers(self):
        text=(repo/'lucx-ui-latest.sh').read_text(encoding='utf-8')
        backup=(repo/'assets/backup/lucx-ui-backup.sh').read_text(encoding='utf-8')
        helpers=text[text.index('save_renewal_state() {'):text.index('setup_cron() {')]
        self.assertIn(helpers,backup)
        block=text[text.index('save_renewal_state() {'):text.index('# FIREWALL',text.index('setup_cron() {'))]
        # Never touch a real system in this test.
        block=block.replace('rm -f /usr/local/x-ui/update-geodata.sh', ':')
        bash=shutil.which('bash')
        if os.name=='nt':
            candidate=Path('C:/Users/tttrr/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/git/usr/bin/sh.exe')
            bash=str(candidate) if candidate.exists() else None
        if not bash:self.skipTest('bash unavailable')
        with tempfile.TemporaryDirectory() as folder:
            work=Path(folder).resolve().as_posix()
            if os.name=='nt':work='/'+work[0].lower()+work[2:]
            fixture="""
export PATH=/usr/bin:/bin:$PATH
cd 'WORK' || exit 1
msg_err(){ :; }
cert_enabled=enabled; cert_active=active; cron_enabled=disabled; cron_active=inactive
systemctl(){
 local cmd="$1";shift
 local unit="${@: -1}" key=cert
 [[ "$unit" != cron ]] || key=cron
 case "$cmd" in
 is-enabled) local ref="${key}_enabled"; echo "${!ref}" ;;
 is-active) local ref="${key}_active"; echo "${!ref}" ;;
 enable) eval "${key}_enabled=enabled"; [[ "$1" != --now ]] || eval "${key}_active=active" ;;
 mask) [[ "$fail_mask" != 1 ]] || { fail_mask=0;return 1; };cert_enabled=masked;cert_active=inactive ;;
 unmask) [[ "$cert_enabled" != masked ]] || cert_enabled=disabled ;;
 disable) eval "${key}_enabled=disabled" ;;
 start) eval "${key}_active=active" ;;
 stop) eval "${key}_active=inactive" ;;
 esac
}
crontab(){
 case "$1" in
 -l) [[ -f table ]] && cat table ;;
 -r) rm -f table ;;
 *) [[ "$fail_write" != 1 ]] || { fail_write=0;return 1; };cp "$1" table ;;
 esac
}
""".replace('WORK',work)
            assertions="""
printf '%s\n' '# user comment' '5 2 * * * echo unrelated' '0 4 * * * /usr/local/x-ui/update-geodata.sh' > table
cp table original
fail_mask=1
setup_cron && exit 20
cmp table original || exit 21
[[ "$cert_enabled:$cert_active:$cron_enabled:$cron_active" == enabled:active:disabled:inactive ]] || exit 22
fail_write=1
setup_cron && exit 10
cmp table original || exit 11
[[ "$cert_enabled:$cert_active:$cron_enabled:$cron_active" == enabled:active:disabled:inactive ]] || { echo "STATE=$cert_enabled:$cert_active:$cron_enabled:$cron_active"; exit 12; }
setup_cron || exit 13
grep -q 'echo unrelated' table || exit 14
[[ $(grep -c 'certbot renew' table) == 1 ]] || exit 15
! grep -q update-geodata table || exit 16
setup_cron || exit 17
[[ $(grep -c 'certbot renew' table) == 1 ]] || exit 18
"""
            result=subprocess.run([bash,'-c',fixture+block+assertions],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_update_cron_is_inside_rollback_and_no_blanket_restore(self):
        text=(repo/'lucx-ui-latest.sh').read_text(encoding='utf-8')
        update=text[text.index('update_compatibility()'):text.index('choose_amneziawg()')]
        self.assertNotIn('cp -an',update)
        self.assertIn('run_pro_compat restore-sidecars',update)
        self.assertIn('save_renewal_state "$stage/renewal-before"',update)
        self.assertIn('restore_renewal_state "$stage/renewal-before"',update)
        self.assertLess(update.index('(( failed )) || setup_cron || failed=1'),update.index("msg_err 'Обновление не прошло проверку"))
