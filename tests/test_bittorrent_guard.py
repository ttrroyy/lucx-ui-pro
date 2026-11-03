"""Optional guard lifecycle must not touch panel services or unrelated firewall rules."""
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SOURCE = (REPO/'lucx-ui-latest.sh').read_text(encoding='utf-8')
BASH = os.environ.get('LUCX_TEST_BASH') or shutil.which('bash')


def function(name):
    return re.search(r'^'+name+r'\(\) [({]\n.*?^[)}]\n', SOURCE, re.M|re.S)[0]


MOCK = r'''#!/usr/bin/env python3
import json,os,pathlib,sys
p=pathlib.Path(os.environ['MOCK_DIR']);args=sys.argv[1:];name=pathlib.Path(sys.argv[0]).name
with (p/'calls').open('a') as f:f.write(name+' '+ ' '.join(args)+'\n')
state=json.loads((p/'state.json').read_text())
if name=='systemctl':
    if args[0]=='is-active' and os.environ.get('MOCK_SERVICE_FAIL')=='1' and args[-1]=='vibehost-guard-tick.timer':sys.exit(1)
    sys.exit(0)
if name=='curl':
    if os.environ.get('MOCK_DOWNLOAD_FAIL')=='1':sys.exit(22)
    output=pathlib.Path(args[args.index('-o')+1])
    output.write_text((p/'provider.sh').read_text());sys.exit(0)
if name.endswith('-save'):
    print('unrelated-rules');sys.exit(0)
if name=='create-guard':
    state['guard']=True;state['scan']=True;state['hooks']={h:1 for h in ('INPUT','OUTPUT','FORWARD','DOCKER-USER')}
else:
    args=[a for a in args if a!='-w'];op=args[0];target=args[1]
    if op=='-C':sys.exit(0 if state['hooks'].get(target,0) else 1)
    if op=='-D':state['hooks'][target]-=1
    elif op=='-S':sys.exit(0 if state['scan' if target=='VH_GUARD_SCAN' else 'guard'] else 1)
    elif op=='-F':assert target in ('VH_GUARD','VH_GUARD_SCAN')
    elif op=='-X':
        assert not any(state['hooks'].values())
        if target=='VH_GUARD_SCAN':assert not state['guard'];state['scan']=False
        else:assert target=='VH_GUARD';state['guard']=False
    else:raise AssertionError('Unexpected firewall mutation')
(p/'state.json').write_text(json.dumps(state))
'''


@unittest.skipUnless(BASH and os.name!='nt','Linux bash lifecycle mocks required')
class BitTorrentGuard(unittest.TestCase):
    def invoke(self, action, *, installed=False, provider_fails=False, download_fails=False, service_fails=False):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'bin').mkdir()
            (root/'run').mkdir();(root/'var/log').mkdir(parents=True)
            (root/'etc/systemd/system').mkdir(parents=True)
            snapshots=root/'var/lib/lucx-ui-preinstall/firewall';snapshots.mkdir(parents=True)
            for family in ('v4','v6'):
                (snapshots/('iptables.'+family)).write_text('*filter\n:VH_GUARD - [0:0]\n:VH_GUARD_SCAN - [0:0]\n:RKN - [0:0]\n-A INPUT -j VH_GUARD\n-A VH_GUARD -m comment --comment vhscan -j VH_GUARD_SCAN\n-A VH_GUARD_SCAN -j DROP\n-A INPUT -j RKN\nCOMMIT\n')
            for name in ('curl','systemctl','iptables','ip6tables','iptables-save','ip6tables-save','create-guard'):
                path=root/'bin'/name;path.write_text(MOCK);path.chmod(0o755)
            original={'guard':installed,'scan':installed,'hooks':{'INPUT':2 if installed else 0,'OUTPUT':int(installed),'FORWARD':int(installed),'DOCKER-USER':int(installed)},'other':['RKN','user-rule']}
            (root/'state.json').write_text(json.dumps(original))
            native=root/'usr/local/lib/vibehost-guard/guard.sh'
            conf=root/'etc/vibehost-guard/agent.conf'
            if installed:
                native.parent.mkdir(parents=True);native.write_text('#!/bin/sh\nexit 0\n');native.chmod(0o755)
                conf.parent.mkdir(parents=True);conf.write_text('fixture-registration\n');conf.chmod(0o600)
                for name in ('vibehost-guard.service','vibehost-guard-dpid.service','vibehost-guard-scan.path','vibehost-guard-scan.service','vibehost-guard-tick.service','vibehost-guard-tick.timer'):
                    (root/'etc/systemd/system'/name).touch()
            provider='''#!/bin/sh
printf '%s' "$#" > "$MOCK_DIR/provider-argc"
mkdir -p "$MOCK_DIR/usr/local/lib/vibehost-guard" "$MOCK_DIR/etc/systemd/system"
printf '#!/bin/sh\\nexit 0\\n' > "$MOCK_DIR/usr/local/lib/vibehost-guard/guard.sh"
chmod 755 "$MOCK_DIR/usr/local/lib/vibehost-guard/guard.sh"
for u in vibehost-guard.service vibehost-guard-tick.service vibehost-guard-tick.timer; do touch "$MOCK_DIR/etc/systemd/system/$u"; done
create-guard
'''+('exit 9\n' if provider_fails else 'exit 0\n')
            (root/'provider.sh').write_text(provider)
            names=('bittorrent_guard_backup','remove_bittorrent_guard','install_bittorrent_guard','uninstall_bittorrent_guard')
            script='msg_inf(){ echo "$1"; }; msg_err(){ echo "$1"; }; msg_ok(){ echo "$1"; };\n'+'\n'.join(function(n) for n in names)
            for prefix in ('/var/backups/lucx-ui-bittorrent-guard','/var/log/lucx-ui-pro-bittorrent-guard.log','/usr/local/lib/vibehost-guard','/etc/vibehost-guard','/etc/systemd/system','/etc/cron.d/vibehost-guard','/run/vibehost-guard.lock','/var/lib/lucx-ui-preinstall'):
                script=script.replace(prefix,str(root)+prefix)
            script='PREINSTALL_STATE_DIR='+shlex.quote(str(snapshots.parent))+'\n'+script
            path=root/'test.sh';path.write_text(script+'\n'+action+'\n')
            env=dict(os.environ,MOCK_DIR=str(root),PATH=str(root/'bin')+os.pathsep+os.environ['PATH'],MOCK_DOWNLOAD_FAIL=str(int(download_fails)),MOCK_SERVICE_FAIL=str(int(service_fails)))
            result=subprocess.run([BASH,str(path)],env=env,capture_output=True,text=True,timeout=15)
            state=json.loads((root/'state.json').read_text())
            state['dpid_unit']=(root/'etc/systemd/system/vibehost-guard-dpid.service').exists()
            state['guard_library_mode']=native.parent.stat().st_mode & 0o777 if native.parent.exists() else None
            state['scan_units']=[(root/'etc/systemd/system'/n).exists() for n in ('vibehost-guard-scan.path','vibehost-guard-scan.service')]
            state['scan_backups']=len(list((root/'var/backups').glob('**/vibehost-guard-scan.path')))
            state['dpid_backups']=len(list((root/'var/backups').glob('**/vibehost-guard-dpid.service')))
            state['snapshots']=[p.read_text() for p in sorted(snapshots.iterdir())]
            calls=(root/'calls').read_text() if (root/'calls').exists() else ''
            self.assertNotRegex(calls,r'systemctl (?:stop|restart|reload|disable) (?:x-ui|nginx)')
            self.assertEqual(state['other'],original['other'])
            return result,state,calls,native.exists(),conf.read_text() if conf.exists() else None,(root/'provider-argc').read_text() if (root/'provider-argc').exists() else None

    def test_install_uses_no_enrollment_code_and_preserves_registration(self):
        result,state,calls,native,conf,argc=self.invoke('install_bittorrent_guard',installed=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(argc,'0');self.assertEqual(conf,'fixture-registration\n')
        self.assertTrue(native);self.assertTrue(state['guard'])
        self.assertEqual(state['guard_library_mode'],0o755)
        self.assertIn('enable --now vibehost-guard.service vibehost-guard-tick.timer',calls)
        self.assertIn('Защита BitTorrent обновлена:',result.stdout)
        self.assertNotIn('Защита BitTorrent установлена:',result.stdout)

    def test_fresh_install_reports_installation(self):
        result,state,_,native,_,argc=self.invoke('install_bittorrent_guard')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(argc,'0');self.assertTrue(native);self.assertTrue(state['guard'])
        self.assertIn('Защита BitTorrent установлена:',result.stdout)
        self.assertNotIn('Защита BitTorrent обновлена:',result.stdout)

    def test_uninstall_is_idempotent_and_removes_duplicate_owned_hooks(self):
        result,state,calls,native,conf,_=self.invoke('uninstall_bittorrent_guard && uninstall_bittorrent_guard',installed=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(state['guard']);self.assertFalse(state['scan']);self.assertFalse(any(state['hooks'].values()))
        self.assertFalse(any(state['scan_units']));self.assertGreater(state['scan_backups'],0)
        self.assertLess(calls.index('systemctl stop vibehost-guard-scan.path'),calls.index('systemctl stop vibehost-guard-dpid.service'))
        self.assertFalse(native);self.assertIsNone(conf)
        self.assertFalse(state['dpid_unit']);self.assertGreater(state['dpid_backups'],0)
        self.assertIn('systemctl stop vibehost-guard-dpid.service',calls)
        self.assertIn('systemctl disable vibehost-guard-dpid.service',calls)
        for snapshot in state['snapshots']:
            self.assertNotIn('VH_GUARD',snapshot);self.assertIn('-A INPUT -j RKN',snapshot)

    def test_panel_uninstall_keeps_iptables_only_when_independent_guard_remains(self):
        start=SOURCE.index('    for package in nginx-full')
        loop=SOURCE[start:SOURCE.index('\n    done',start)+len('\n    done')]
        for installed in (False,True):
            with self.subTest(installed=installed),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);(root/'packages').touch();native=root/'guard.sh'
                if installed:native.touch();native.chmod(0o755)
                body=loop.replace('/usr/local/lib/vibehost-guard/guard.sh',str(native))
                script='PREINSTALL_STATE_DIR='+shlex.quote(str(root))+'\ndir="$PREINSTALL_STATE_DIR"\nmsg_inf(){ :; }; msg_err(){ :; }; dpkg-query(){ echo "install ok installed"; }; apt-get(){ echo "$*" >> "$PREINSTALL_STATE_DIR/calls"; };\n'+body
                result=subprocess.run([BASH,'-c',script],capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,0,result.stderr)
                calls=(root/'calls').read_text()
                self.assertEqual('purge iptables' in calls,not installed)
                self.assertIn('purge nginx',calls)

    def test_download_failure_does_not_change_existing_protection(self):
        result,state,_,native,conf,argc=self.invoke('install_bittorrent_guard',installed=True,download_fails=True)
        self.assertNotEqual(result.returncode,0);self.assertTrue(state['guard'])
        self.assertTrue(native);self.assertEqual(conf,'fixture-registration\n');self.assertIsNone(argc)

    def test_partial_new_install_failure_removes_only_new_guard(self):
        result,state,_,native,_,_=self.invoke('install_bittorrent_guard',provider_fails=True)
        self.assertNotEqual(result.returncode,0);self.assertFalse(native)
        self.assertFalse(state['guard']);self.assertFalse(state['scan']);self.assertFalse(any(state['hooks'].values()))
        self.assertFalse(any(state['scan_units']))

    def test_timer_start_failure_is_not_reported_as_success(self):
        result,state,_,native,_,_=self.invoke('install_bittorrent_guard',service_fails=True)
        self.assertNotEqual(result.returncode,0);self.assertFalse(native);self.assertFalse(state['guard'])

    def test_selector_accepts_yes_and_no_after_invalid_input(self):
        for answer,want in [('bad\n1\n','1'),('2\n','0')]:
            result=subprocess.run([BASH,'-c','msg_inf(){ :; };\n'+function('choose_bittorrent_guard')+'\nchoose_bittorrent_guard && echo RESULT=$DEPLOY_BITTORRENT_GUARD'],input=answer,capture_output=True,text=True,timeout=3)
            self.assertEqual(result.returncode,0);self.assertIn('RESULT='+want,result.stdout)

    def test_component_flags_are_standalone_and_selector_follows_rkn(self):
        self.assertIn('choose_rkn_guard || return 1\n    choose_bittorrent_guard || return 1',SOURCE)
        ownership=SOURCE.index('Component maintenance requires an installation owned by this script.')
        for variable in ('BITTORRENT_GUARD_ONLY','BITTORRENT_GUARD_UNINSTALL'):
            self.assertIn('"$'+variable+'"',SOURCE[SOURCE.index('for _action_value'):SOURCE.index('fetch_release_info()')])
            dispatch=SOURCE.index('if [[ "${'+variable+'}" == y ]]; then')
            self.assertLess(dispatch,ownership)
        backup=(REPO/'assets/backup/lucx-ui-backup.sh').read_text(encoding='utf-8')
        self.assertIn('/usr/local/lib/vibehost-guard',backup);self.assertIn('/etc/vibehost-guard',backup)
        self.assertIn('restore_unit_state vibehost-guard-dpid.service',backup)
        self.assertIn('vibehost-guard.service vibehost-guard-dpid.service vibehost-guard-tick.service',backup)
        self.assertGreater(backup.index('Failed to restore BitTorrent guard rules'),backup.index('UFW state and runtime rules restored'))



@unittest.skipUnless(BASH,'bash required')
class GuardSnapshotCleanup(unittest.TestCase):
    def test_explicit_uninstall_cannot_resurrect_scan_rules_or_remove_foreign_chains(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); snapshots=root/'firewall'; snapshots.mkdir()
            rules='*filter\n:VH_GUARD - [0:0]\n:VH_GUARD_SCAN - [0:0]\n:VH_GUARD_CUSTOM - [0:0]\n-A INPUT -j VH_GUARD\n-A VH_GUARD -m comment --comment vhscan -j VH_GUARD_SCAN\n-A VH_GUARD_SCAN -j DROP\n-A INPUT -j VH_GUARD_CUSTOM\n-A VH_GUARD_CUSTOM -j ACCEPT\nCOMMIT\n'
            for family in (4,6): (snapshots/('iptables.v'+str(family))).write_text(rules)
            script='bittorrent_guard_backup(){ :; }; remove_bittorrent_guard(){ :; }; msg_ok(){ :; }; msg_err(){ :; };\n'+function('uninstall_bittorrent_guard')+'\nuninstall_bittorrent_guard\n'
            env=dict(os.environ,PREINSTALL_STATE_DIR=root.as_posix())
            r=subprocess.run([BASH,'-c',script],env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(r.returncode,0,r.stderr)
            for family in (4,6):
                self.assertEqual((snapshots/('iptables.v'+str(family))).read_text(),'*filter\n:VH_GUARD_CUSTOM - [0:0]\n-A INPUT -j VH_GUARD_CUSTOM\n-A VH_GUARD_CUSTOM -j ACCEPT\nCOMMIT\n')


    def test_restore_preserves_independent_guard_service_state_when_archive_omits_guard(self):
        backup=(REPO/'assets/backup/lucx-ui-backup.sh').read_text(encoding='utf-8')
        a=backup.index('    # An archive without Guard must preserve')
        stop=backup[a:backup.index('    # ── stop running services',a)]
        a=backup.index('    if [[ -x /usr/local/lib/vibehost-guard/guard.sh && -f')
        restore=backup[a:backup.index('    if systemctl is-active --quiet x-ui;',a)]
        for archived in (False,True):
            with self.subTest(archived=archived),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);stage=root/'staging';stage.mkdir()
                native=root/'usr/local/lib/vibehost-guard/guard.sh';native.parent.mkdir(parents=True)
                native.write_text('#!/bin/sh\nexit 0\n');native.chmod(0o755)
                units=root/'etc/systemd/system';units.mkdir(parents=True)
                for unit in ('vibehost-guard.service','vibehost-guard-dpid.service','vibehost-guard-scan.path'):(units/unit).touch()
                if archived:
                    old=stage/'files/usr/local/lib/vibehost-guard/guard.sh';old.parent.mkdir(parents=True)
                    old.write_text('SCAN_CHAIN=VH_GUARD_SCAN\n')
                script='systemctl(){ printf "CTL %s\\n" "$*" >> "$TEST_CALLS"; }; restore_unit_state(){ printf "RESTORE %s\\n" "$*" >> "$TEST_CALLS"; }; die(){ exit 1; };\n'+stop+'\n'+restore
                for prefix in ('/usr/local/lib/vibehost-guard','/etc/systemd/system'):
                    script=script.replace(' '+prefix,' '+root.as_posix()+prefix)
                env=dict(os.environ,staging=stage.as_posix(),TEST_CALLS=(root/'calls').as_posix())
                r=subprocess.run([BASH,'-c',script],env=env,capture_output=True,text=True,timeout=10)
                self.assertEqual(r.returncode,0,r.stderr)
                calls=(root/'calls').read_text()
                self.assertEqual('CTL stop vibehost-guard-tick.timer' in calls,archived)
                self.assertEqual('RESTORE vibehost-guard.service' in calls,archived)
                self.assertEqual('RESTORE vibehost-guard-scan.path' in calls,archived)

if __name__=='__main__':unittest.main()
