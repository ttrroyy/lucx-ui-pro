"""Upstream installer stays silent; diagnostics and exit status are retained."""
import os
import signal
import shlex
from pathlib import Path
import subprocess
import tempfile
import unittest

repo=Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name=='posix','Native Bash installation test')
class RknPlainInstall(unittest.TestCase):
    def invoke(self, exitcode):
        source=(repo/'lucx-ui-latest.sh').read_text()
        function=source.split('run_rkn_guard_installer() {',1)[1].split('\n}\n',1)[0]
        with tempfile.TemporaryDirectory() as folder:
            installer=Path(folder)/'installer.sh'
            installer.write_text('clear\n[[ ! -t 1 ]] || exit 99\n[[ "$1" == install ]] || exit 98\nprintf "STATE: installing\\n"\nexit '+str(exitcode)+'\n')
            log=Path(folder)/'install.log'
            result=subprocess.run(['bash','-c','run_rkn_guard_installer() {'+function+'\n}\nrun_rkn_guard_installer "$1" "$2"','test',str(installer),str(log)],capture_output=True,text=True)
            return result,log.read_text()

    def test_plain_output_without_screen_clear(self):
        result,log=self.invoke(0)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout,'')
        self.assertEqual(result.stderr,'')
        self.assertEqual(log,'STATE: installing\n')

    def test_installer_failure_propagates_with_diagnostics_retained(self):
        result,log=self.invoke(7)
        self.assertEqual(result.returncode,7)
        self.assertEqual(result.stdout,'')
        self.assertEqual(log,'STATE: installing\n')

    def test_final_schedule_prompt_does_not_wait_on_controlling_terminal(self):
        import pty
        import time
        source=(repo/'lucx-ui-latest.sh').read_text()
        function=source.split('run_rkn_guard_installer() {',1)[1].split('\n}\n',1)[0]
        with tempfile.TemporaryDirectory() as folder:
            installer=Path(folder)/'installer.sh'
            installer.write_text('offer_install_schedule() {\n    read -r -p "Schedule? " answer </dev/tty\n}\nprintf "STATE: installed\\n"\n    offer_install_schedule || return 1\nprintf "STATE: complete\\n"\n')
            log=Path(folder)/'install.log'
            pid,fd=pty.fork()
            if pid==0:
                os.execvp('bash',['bash','-c','run_rkn_guard_installer() {'+function+'\n}\nrun_rkn_guard_installer "$1" "$2"','test',str(installer),str(log)])
            status=None
            try:
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    done,status=os.waitpid(pid,os.WNOHANG)
                    if done:break
                    status=None
                    time.sleep(0.05)
                self.assertIsNotNone(status,'Installer is waiting for an invisible /dev/tty prompt')
                self.assertEqual(os.waitstatus_to_exitcode(status),0)
                self.assertEqual(log.read_text(),'STATE: installed\nSTATE: complete\n')
                self.assertIn('read -r -p "Schedule? "',installer.read_text())
            finally:
                if status is None:
                    os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
                os.close(fd)

    def test_Pro_timers_replace_existing_native_schedule_in_both_modes(self):
        source=(repo/'lucx-ui-latest.sh').read_text()
        function=source.split('install_rkn_guard_auto_updates() {',1)[1].split('\n}\n',1)[0]
        for mode in ('1','2'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);units=root/'etc/systemd/system';units.mkdir(parents=True)
                for name in ('rkn-guard-update.timer','rkn-guard-update.service','unrelated.timer'):
                    (units/name).write_text('original')
                config=root/'etc/rkn-guard-update.conf';config.write_text('local=06:00')
                body=function.replace('/usr/local/lib/lucx-ui-pro',str(root/'updates')).replace('/etc/systemd/system',str(units)).replace('/etc/rkn-guard-update.conf',str(config))
                helper=repo/'assets/compat/pro-compat.py'
                script='systemctl() { printf "%s\\n" "$*"; }; run_pro_compat() { python3 '+shlex.quote(str(helper))+' "$1" --root '+shlex.quote(str(root))+'; };\n'
                result=subprocess.run(['bash','-c',script+'install_rkn_guard_auto_updates() {'+body+'\n}\ninstall_rkn_guard_auto_updates "$1"','test',mode],capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertFalse(config.exists())
                self.assertFalse((units/'rkn-guard-update.timer').exists())
                self.assertFalse((units/'rkn-guard-update.service').exists())
                self.assertEqual((units/'unrelated.timer').read_text(),'original')
                self.assertIn('enable --now rkn-guard-list-update.timer',result.stdout)
                self.assertEqual((units/'rkn-guard-self-update.timer').exists(),mode=='2')

    def test_update_repair_removes_only_native_schedule_and_preserves_Pro_timers(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('rkn_compat',repo/'assets/compat/pro-compat.py')
        helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);units=root/'etc/systemd/system';units.mkdir(parents=True)
            for name in ('rkn-guard-update.timer','rkn-guard-update.service','unrelated.timer'):
                (units/name).write_text('original')
            pro=units/'rkn-guard-list-update.timer';pro.write_text('OnActiveSec=15min\n')
            config=root/'etc/rkn-guard-update.conf';config.write_text('local=06:00')
            helper.repair_rkn_timers(root)
            self.assertFalse(config.exists())
            self.assertFalse((units/'rkn-guard-update.timer').exists())
            self.assertFalse((units/'rkn-guard-update.service').exists())
            self.assertEqual(pro.read_text(),'OnActiveSec=15min\n')
            self.assertEqual((units/'unrelated.timer').read_text(),'original')
            helper.repair_rkn_timers(root)

    def test_timer_preserves_configured_base_sources_and_empty_selection(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('rkn_sources_compat',repo/'assets/compat/pro-compat.py')
        helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            runner=root/'usr/local/lib/lucx-ui-pro/rkn-guard-list-update.sh'
            runner.parent.mkdir(parents=True)
            self_runner=runner.with_name('rkn-guard-self-update.sh')
            self_runner.write_text('bash "$tmp"\n')
            runner.write_text('exec rkn-guard update '+chr(92)+'\n  -u https://raw.githubusercontent.com/shadow-netlab/traffic-guard-lists/refs/heads/main/public/skipa.list\n')
            helper.repair_rkn_timers(root)
            updated=runner.read_text()
            helper.repair_rkn_timers(root)
            self.assertEqual(runner.read_text(),updated)
            self.assertEqual(self_runner.read_text(),'bash "$tmp"\nrkn-guard setup-logging\n')
            self.assertIn('C24Be/AS_Network_List',updated)
            config=root/'base-sources'
            body=updated.replace('/etc/rkn-guard-sources.list',str(config))
            stub='rkn-guard(){ if [[ "$2" == --help ]]; then echo --managed-lists; else printf "ARG=%s\\n" "$@"; fi; }; fake_exec(){ rkn-guard "$@"; exit; };\n'
            for text in (' # comment\n  https://example.org/custom # note\n',''):
                config.write_text(text)
                result=subprocess.run(['bash','-c',stub+body.replace('exec rkn-guard','fake_exec')],text=True,capture_output=True,timeout=5)
                self.assertEqual(result.returncode,0,result.stderr)
                custom=result.stdout.split('ARG=update')[1].splitlines()
                self.assertIn('ARG=--managed-lists',custom)
                if text:self.assertIn('ARG=https://example.org/custom',custom)
                else:self.assertNotIn('ARG=-u',custom)


    def test_restore_repairs_only_archived_supported_logging_and_propagates_errors(self):
        source=(repo/'assets/backup/lucx-ui-backup.sh').read_text(encoding='utf-8')
        body=source.split('restore_rkn_logging() {',1)[1].split('\n}\n',1)[0]
        for archived,supported,status in ((False,True,7),(True,False,7),(True,True,0),(True,True,7)):
            with self.subTest(archived=archived,supported=supported,status=status),tempfile.TemporaryDirectory() as folder:
                root=Path(folder)
                if archived:
                    config=root/'files/etc/rsyslog.d/10-iptables-scanners.conf'
                    config.parent.mkdir(parents=True);config.touch()
                stub='rkn-guard(){ if [[ "$1" == --help ]]; then printf "%s" "$HELP_TEXT"; else echo REPAIR > "$CALLED"; echo native-log; return "$NATIVE_RC"; fi; };\n'
                env=dict(os.environ,HELP_TEXT='setup-logging' if supported else 'legacy',NATIVE_RC=str(status),CALLED=str(root/'called'))
                result=subprocess.run(['bash','-c',stub+'restore_rkn_logging() {'+body+'\n}\nrestore_rkn_logging "$1"','test',str(root)],env=env,text=True,capture_output=True,timeout=5)
                should_repair=archived and supported
                self.assertEqual(result.returncode,status if should_repair else 0,result.stderr)
                self.assertEqual((root/'called').exists(),should_repair)
                self.assertEqual(result.stdout,'')
                if should_repair and status:self.assertIn('native-log',result.stderr)

if __name__=='__main__': unittest.main()
