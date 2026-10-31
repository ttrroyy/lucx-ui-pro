"""One-hostname migration retains identities and prunes only unused owned certificates."""
import copy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import re
import sys
import shutil
import subprocess

from test_architecture import arch, connection, fixture, records


def installation(root, telegram=False):
    fixture(root)
    stream = root / 'etc/nginx/stream-enabled/stream.conf'
    stream.parent.mkdir()
    stream.write_text('''map $ssl_preread_server_name $sni_name {
    hostnames;
    cover.example xray;
    main.example www;
    tg.example tproxy;
    user.example custom;
    default xray;
}
upstream xray { server 127.0.0.1:8443; }
upstream www { server 127.0.0.1:7443; }
upstream tproxy { server 127.0.0.1:11443; }
server { listen 443; proxy_protocol on; proxy_pass $sni_name; ssl_preread on; }
''')
    (root / 'etc/nginx/sites-available/80.conf').write_text('server { listen 80; server_name main.example cover.example tg.example user.example; }')
    snippet = root / 'etc/nginx/snippets/includes.conf'
    snippet.write_text(snippet.read_text() + '\nlocation / { try_files $uri $uri/ =404; }\n')
    state = root / arch.STATE
    (state / 'new-certificates').write_text('cover.example\ntg.example\n')
    for domain in ('main.example', 'cover.example', 'tg.example', 'unrelated.example'):
        for folder in ('live', 'archive'):
            path = root / 'etc/letsencrypt' / folder / domain
            path.mkdir(parents=True)
            (path / 'fullchain.pem').write_text(domain)
        renewal = root / 'etc/letsencrypt/renewal' / (domain + '.conf')
        renewal.parent.mkdir(exist_ok=True)
        renewal.write_text('cert = /etc/letsencrypt/live/' + domain + '/fullchain.pem')
        (root / 'root/cert' / domain).mkdir(parents=True)
    if telegram:
        (state / 'domains').write_text('main.example\ncover.example\ntg.example\n')
        (root / 'root/.lucx-tg-web-proxy-info').write_text('TG_WEB_PROXY_DOMAIN=tg.example\nTG_WEB_PROXY_SECRET=preserved\n')
        settings = {'hostname': 'tg.example', 'secret': 'preserved', 'clients': [{'id': 'uuid-one'}],
                    'behindCover': False, 'externalTLS': False, 'carrierMode': 'websocket',
                    'certFile': '/root/cert/tg.example/fullchain.pem', 'keyFile': '/root/cert/tg.example/privkey.pem',
                    'siteDir': '/var/www/html', 'future': {'keep': True}}
        with connection(root / 'etc/x-ui/x-ui.db') as db:
            row = copy.deepcopy(records(root)[2])
            row.update(id=3, protocol='tproxy', port=11443, listen='127.0.0.1', tag='inbound-tproxy',
                       remark='My Telegram', settings=json.dumps(settings), stream_settings='{}')
            arch.insert(db, 'inbounds', row)
            db.execute("INSERT INTO client_inbounds VALUES(1,3,'')")
    arch.reconcile(root, remember=True)


class OneDomain(unittest.TestCase):
    def test_install_and_telegram_use_only_the_panel_domain_without_extra_prompts(self):
        bash=shutil.which('bash')
        if not bash:self.skipTest('bash unavailable')
        source=(Path(__file__).resolve().parents[1]/'lucx-ui-latest.sh').read_text(encoding='utf-8')
        functions='\n'.join(re.search(r'^'+name+r'\(\) \{.*?^\}',source,re.M|re.S)[0]
                            for name in ('validate_domains','choose_webproxy_domain'))
        stubs='''msg_inf(){ :; }; msg_err(){ :; }; get_server_ip(){ IP4=192.0.2.10; }
domain_a_ok(){ return 0; }; generate_auto_domain(){ AUTO_DOMAIN_RESULT=auto.example; }
prompt_domain_a_record(){ echo DOMAIN_PROMPT; DOMAIN_INPUT=manual.example; }
IP4_REGEX='^[0-9.]+$'; DEPLOY_TPROXY=1
'''
        for auto,domain,expected,prompts in [('n','','manual.example',1),('y','','auto.example',0),('n','panel.example','panel.example',0)]:
            with self.subTest(auto=auto,domain=domain):
                script=stubs+functions+f'\nAUTODOMAIN={auto}; domain={domain}; validate_domains && choose_webproxy_domain && choose_webproxy_domain\n'
                script+=f'[[ "$domain" == {expected} && "$reality_domain" == "$domain" && "$webproxy_domain" == "$domain" ]]\n'
                result=subprocess.run([bash,'-s'],input=script,text=True,capture_output=True,timeout=5)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(result.stdout.count('DOMAIN_PROMPT'),prompts)

    def test_ordinary_update_selects_mandatory_migration_only_once(self):
        source=(Path(__file__).resolve().parents[1]/'lucx-ui-latest.sh').read_text(encoding='utf-8')
        code=re.search(r"<<'PY_UPDATE_ONE_DOMAIN'\n(.*?)\nPY_UPDATE_ONE_DOMAIN",source,re.S)[1]
        self.assertNotIn('-one-domain)',source)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installation(root)
            path=root/arch.STATE/'domains'
            for expected in ('y\n',''):
                output=io.StringIO()
                with patch.object(sys,'argv',['check',str(path)]),contextlib.redirect_stdout(output):exec(compile(code,'migration-decision','exec'),{})
                self.assertEqual(output.getvalue(),expected)
                if expected:arch.reconcile(root,unify=True)
            path.write_text('main.example\nmain.example\ntg.example\n')
            output=io.StringIO()
            with patch.object(sys,'argv',['check',str(path)]),contextlib.redirect_stdout(output):exec(compile(code,'migration-decision','exec'),{})
            self.assertEqual(output.getvalue(),'y\n')

    def test_fresh_single_domain_needs_no_legacy_cover_listener(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installation(root)
            (root/arch.STATE/'architecture.json').unlink()
            (root/arch.STATE/'domains').write_text('main.example\nmain.example\n')
            (root/'etc/nginx/sites-available/cover.example').unlink()
            f=root/'etc/nginx/stream-enabled/stream.conf'
            f.write_text(f.read_text().replace('cover.example xray;','').replace('main.example www;','main.example xray;'))
            with connection(root/'etc/x-ui/x-ui.db') as db:
                stream=json.loads(records(root)[1]['stream_settings'])
                stream['realitySettings'].update(serverNames=['main.example'],target='127.0.0.1:7443',xver=1)
                db.execute('UPDATE inbounds SET stream_settings=? WHERE id=1',(json.dumps(stream),))
            arch.reconcile(root)
            self.assertEqual(arch.layout(root)['xhttp'],arch.layout(root)['reality'])
            self.assertNotIn('9443',(root/'etc/nginx/sites-available/main.example').read_text())

    def test_migration_prints_actual_owned_telegram_link_only_if_enabled_and_present(self):
        source=(Path(__file__).resolve().parents[1]/'lucx-ui-latest.sh').read_text(encoding='utf-8')
        code=re.search(r"<<'PY_UPDATE_TELEGRAM_LINK'\n(.*?)\nPY_UPDATE_TELEGRAM_LINK",source,re.S)[1]
        self.assertRegex(source,r'if \[\[ "\$ONE_DOMAIN" == y \]\]; then\n[^\n]*\n\s*python3[^\n]*PY_UPDATE_TELEGRAM_LINK')
        for state in ('enabled','disabled','deleted','different-secret','no-telegram'):
            with self.subTest(state=state),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);installation(root,True);arch.reconcile(root,unify=True)
                manifest=root/arch.STATE/'architecture.json'
                with connection(root/'etc/x-ui/x-ui.db') as db:
                    if state=='disabled':db.execute('UPDATE inbounds SET enable=0 WHERE id=3')
                    if state=='deleted':db.execute('DELETE FROM inbounds WHERE id=3')
                    if state=='different-secret':
                        settings=json.loads(records(root)[3]['settings']);settings['secret']='another'
                        db.execute('UPDATE inbounds SET settings=? WHERE id=3',(json.dumps(settings),))
                if state=='no-telegram':
                    saved=json.loads(manifest.read_text());saved['telegram']={};manifest.write_text(json.dumps(saved))
                output=io.StringIO()
                with patch.object(sys,'argv',['link',str(root/'etc/x-ui/x-ui.db'),str(manifest)]),contextlib.redirect_stdout(output):exec(compile(code,'migration-link','exec'),{})
                self.assertEqual(output.getvalue(),'Telegram WEB-proxy: https://t.me/webproxy?server=main.example&secret=preserved\n' if state=='enabled' else '')

    def test_unification_preserves_data_and_header_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installation(root, True)
            (root/'root/.lucx-tg-web-proxy-info').write_text('TG_WEB_PROXY_DOMAIN=tg.example\nTG_WEB_PROXY_SECRET=outdated-info\n')
            before=records(root)
            arch.reconcile(root, unify=True)
            after=records(root)
            self.assertEqual(before[99], after[99])
            for row_id in (1, 2, 3):
                for field in ('id','tag','remark','sniffing','up','down','future_column'):
                    self.assertEqual(before[row_id][field],after[row_id][field])
            reality=json.loads(after[1]['stream_settings'])['realitySettings']
            self.assertEqual((reality['target'],reality['xver'],reality['serverNames']),('127.0.0.1:7443',1,['main.example']))
            for key in ('privateKey','shortIds','future'):
                self.assertEqual(reality[key],json.loads(before[1]['stream_settings'])['realitySettings'][key])
            xhttp=json.loads(after[2]['stream_settings'])
            self.assertEqual(xhttp['xhttpSettings']['mode'],'auto')
            self.assertEqual(xhttp['sockopt']['trustedXForwardedFor'],['X-Forwarded-For'])
            telegram=json.loads(after[3]['settings'])
            original=json.loads(before[3]['settings'])
            for key in ('secret','clients','carrierMode','siteDir','future'):self.assertEqual(telegram[key],original[key])
            self.assertTrue(telegram['behindCover']);self.assertFalse(telegram['externalTLS'])
            self.assertEqual(telegram['hostname'],'main.example')
            self.assertIn('TG_WEB_PROXY_SECRET=preserved',(root/'root/.lucx-tg-web-proxy-info').read_text())
            stream=(root/'etc/nginx/stream-enabled/stream.conf').read_text()
            self.assertIn('main.example xray;',stream);self.assertIn('user.example custom;',stream)
            self.assertNotIn('tg.example tproxy;',stream)
            snippet=(root/'etc/nginx/snippets/includes.conf').read_text()
            self.assertIn('proxy_pass http://127.0.0.1:24014;',snippet)
            self.assertIn('proxy_request_buffering off;',snippet)
            self.assertIn('grpc_set_header X-Forwarded-For $remote_addr;',snippet)
            self.assertEqual(arch.layout(root)['reality'],arch.layout(root)['xhttp'])
            with connection(root/'etc/x-ui/x-ui.db') as db:
                self.assertEqual(db.execute('SELECT uuid FROM clients').fetchall(),[('uuid-one',)])
                self.assertEqual(db.execute('SELECT inbound_id FROM client_inbounds ORDER BY inbound_id').fetchall(),[(1,),(2,),(3,)])

    def test_managed_host_sni_lists_cannot_reintroduce_retired_domains(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installation(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                db.executescript("ALTER TABLE hosts ADD COLUMN snis TEXT; ALTER TABLE hosts ADD COLUMN sni_shared_random INTEGER;")
                db.execute('UPDATE hosts SET snis=?,sni_shared_random=1',(json.dumps(['cover.example','tg.example']),))
                db.execute("INSERT INTO hosts VALUES(99,1,'user.example',443,'user-field','[\"user.example\"]',1)")
            arch.reconcile(root,unify=True)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                self.assertEqual(db.execute('SELECT snis,sni_shared_random FROM hosts WHERE id<99 ORDER BY id').fetchall(),[('[]',0),('[]',0)])
                self.assertEqual(db.execute('SELECT snis,sni_shared_random FROM hosts WHERE id=99').fetchone(),('["user.example"]',1))

    def test_repeat_and_restore_reconcile_are_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installation(root,True);arch.reconcile(root,unify=True)
            def snapshot():return {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            first=snapshot();arch.reconcile(root);self.assertEqual(first,snapshot())
            arch.reconcile(root,unify=True);self.assertEqual(first,snapshot())

    def test_deleted_or_disabled_reality_does_not_break_https_or_get_recreated(self):
        for deleted in (False,True):
            with self.subTest(deleted=deleted),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);installation(root)
                with connection(root/'etc/x-ui/x-ui.db') as db:
                    db.execute('DELETE FROM inbounds WHERE id=1' if deleted else 'UPDATE inbounds SET enable=0 WHERE id=1')
                arch.reconcile(root,unify=True)
                self.assertIn('main.example www;',(root/'etc/nginx/stream-enabled/stream.conf').read_text())
                self.assertEqual(1 not in records(root),deleted)

    def test_removed_telegram_restores_static_root_without_recreating_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installation(root,True);arch.reconcile(root,unify=True)
            with connection(root/'etc/x-ui/x-ui.db') as db:db.execute('DELETE FROM inbounds WHERE id=3')
            arch.reconcile(root)
            self.assertNotIn('LUCX PRO Telegram relay',(root/'etc/nginx/snippets/includes.conf').read_text())
            self.assertNotIn(3,records(root))

    def test_custom_root_and_external_telegram_fail_before_files_change(self):
        for external in (False,True,'mtproto-zig'):
            with self.subTest(external=external),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);installation(root,True)
                if external:
                    with connection(root/'etc/x-ui/x-ui.db') as db:
                        settings=json.loads(records(root)[3]['settings'])
                        settings['backend' if isinstance(external,str) else 'externalTLS']=external
                        db.execute('UPDATE inbounds SET settings=? WHERE id=3',(json.dumps(settings),))
                else:
                    f=root/'etc/nginx/snippets/includes.conf';f.write_text(f.read_text().replace('try_files $uri $uri/ =404;','proxy_pass http://user;'))
                before=records(root)
                with self.assertRaises(RuntimeError):arch.reconcile(root,unify=True)
                self.assertEqual(before,records(root))
                self.assertEqual((root/arch.STATE/'domains').read_text(),'main.example\ncover.example\ntg.example\n')

    def test_reused_telegram_port_and_domain_with_another_secret_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installation(root,True)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                settings=json.loads(records(root)[3]['settings']);settings['secret']='another-owner'
                db.execute('UPDATE inbounds SET tag=?,settings=? WHERE id=3',('custom-tg',json.dumps(settings)))
            before=records(root)[3]
            arch.reconcile(root,unify=True)
            self.assertEqual(records(root)[3],before)
            self.assertIn('tg.example tproxy;',(root/'etc/nginx/stream-enabled/stream.conf').read_text())
            arch.prune_certificates(root)
            self.assertTrue((root/'etc/letsencrypt/live/tg.example').is_dir())

    def test_cleanup_preserves_unrelated_and_referenced_certificates(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installation(root,True);arch.reconcile(root,unify=True)
            f=root/'etc/systemd/system/custom.service';f.parent.mkdir(parents=True)
            f.write_text('Environment=CERT_DIR=/etc/letsencrypt/live/cover.example')
            arch.prune_certificates(root)
            for domain in ('main.example','cover.example','unrelated.example'):
                self.assertTrue((root/'etc/letsencrypt/live'/domain).exists())
            self.assertFalse((root/'etc/letsencrypt/live/tg.example').exists())
            f.unlink();arch.prune_certificates(root)
            self.assertFalse((root/'etc/letsencrypt/live/cover.example').exists())
            arch.prune_certificates(root)

    def test_missing_creation_proof_retains_retired_certificate(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installation(root);arch.reconcile(root,unify=True)
            (root/arch.STATE/'new-certificates').unlink();arch.prune_certificates(root)
            self.assertTrue((root/'etc/letsencrypt/live/cover.example').is_dir())

    def test_large_unrelated_configuration_retains_certificates_without_blocking_update(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installation(root);arch.reconcile(root,unify=True)
            (root/'root/large-user.conf').write_bytes(b'x'*(16*1024*1024+1))
            arch.prune_certificates(root)
            self.assertTrue((root/'etc/letsencrypt/live/cover.example').is_dir())

    def test_external_service_config_and_certificate_alias_are_dependencies(self):
        for alias in (False,True):
            with self.subTest(alias=alias),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);installation(root);arch.reconcile(root,unify=True)
                external=root/'etc/custom-service';external.mkdir()
                if alias:
                    try:(external/'cert.pem').symlink_to(root/'etc/letsencrypt/live/cover.example/fullchain.pem')
                    except OSError:self.skipTest('File symlinks unavailable')
                else:(external/'tls.toml').write_bytes(b'\xff\ncert="/etc/letsencrypt/live/cover.example/fullchain.pem"')
                arch.prune_certificates(root)
                self.assertTrue((root/'etc/letsencrypt/live/cover.example').is_dir())

    def test_redirected_certificate_tree_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installation(root);arch.reconcile(root,unify=True)
            target=root/'etc/letsencrypt/live/cover.example'
            import shutil
            shutil.rmtree(target)
            try:target.symlink_to(root/'etc/letsencrypt/live/unrelated.example',target_is_directory=True)
            except OSError:self.skipTest('Directory symlinks unavailable')
            arch.prune_certificates(root)
            self.assertTrue(target.is_symlink())
            self.assertTrue((root/'etc/letsencrypt/live/unrelated.example/fullchain.pem').is_file())


if __name__=='__main__':unittest.main()
