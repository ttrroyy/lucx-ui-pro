"""Managed reconciliation preserves identities and every unrelated field."""
import copy
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('architecture', REPO / 'assets/compat/architecture.py')
arch = importlib.util.module_from_spec(spec); spec.loader.exec_module(arch)


@contextmanager
def connection(path):
    db = sqlite3.connect(path)
    try:
        with db: yield db
    finally: db.close()


def fixture(root):
    state = root / arch.STATE; state.mkdir(parents=True)
    (state / 'owned-by-lucx-ui-pro').touch()
    (state / 'domains').write_text('main.example\ncover.example\n')
    sites = root / 'etc/nginx/sites-available'; sites.mkdir(parents=True)
    for domain, port in (('main.example', 7443), ('cover.example', 9443)):
        (sites / domain).write_text(f'''# custom prefix
server {{
    server_name {domain};
    listen {port} ssl;
    listen [::]:{port} ssl;
    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    include /etc/nginx/snippets/includes.conf;
    location /custom {{ add_header X-User "{{braces}}"; return 200; }}
}}
server {{ server_name custom.example; listen 8888; }}
server {{ server_name {domain}; listen 8999; }}
''')
    (sites / 'user.conf').write_text('server { listen 7443; server_name user.example; }')
    snippet = root / 'etc/nginx/snippets/includes.conf'; snippet.parent.mkdir()
    snippet.write_text('''# XHTTP
    location /secret {
        grpc_pass grpc://unix:/dev/shm/uds2023.sock;
        grpc_buffer_size 16k;
        grpc_socket_keepalive on;
        grpc_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        add_header X-User "custom";
    }
    location /user { return 200; }
''')
    file = root / 'etc/x-ui/x-ui.db'; file.parent.mkdir()
    db = sqlite3.connect(file)
    db.executescript('''CREATE TABLE inbounds(id INTEGER PRIMARY KEY,protocol TEXT,port INTEGER,listen TEXT,tag TEXT,remark TEXT,
        enable INTEGER,settings TEXT,stream_settings TEXT,sniffing TEXT,up INTEGER,down INTEGER,future_column TEXT);
        CREATE TABLE clients(id INTEGER PRIMARY KEY,email TEXT,uuid TEXT);
        CREATE TABLE client_inbounds(client_id INTEGER,inbound_id INTEGER,flow_override TEXT,PRIMARY KEY(client_id,inbound_id));
        CREATE TABLE hosts(id INTEGER PRIMARY KEY,inbound_id INTEGER,address TEXT,port INTEGER,extra TEXT);
        CREATE TABLE client_traffics(id INTEGER PRIMARY KEY,inbound_id INTEGER,email TEXT UNIQUE,up INTEGER,down INTEGER);
        CREATE TABLE settings(id INTEGER PRIMARY KEY,key TEXT,value TEXT);
        INSERT INTO clients VALUES(1,'one','uuid-one');
        INSERT INTO client_inbounds VALUES(1,1,'vision'),(1,2,'');
        INSERT INTO hosts VALUES(1,1,'main.example',443,'host-new-field'),(2,2,'main.example',443,'host-new-field');
        INSERT INTO client_traffics VALUES(1,1,'one',1234,5678);
        INSERT INTO settings VALUES(1,'xrayTemplateConfig','{"outbounds":[{"user":true}],"routing":{"rules":[{"user":true}]}}');''')
    for kind, row_id in (('reality', 1), ('xhttp', 2)):
        stream = {'future': {'native': True}}
        if kind == 'reality':
            stream.update(network='tcp', security='reality', realitySettings={'target':'127.0.0.1:9443',
                'serverNames':['cover.example'], 'privateKey':'private', 'shortIds':['abc'], 'future':'keep','xver':0},
                tcpSettings={'acceptProxyProtocol':True,'header':{'type':'none'}})
        else:
            stream.update(network='xhttp', security='none', xhttpSettings={'host':'main.example','path':'/secret',
                'mode':'packet-up', 'future':'keep'}, sockopt=dict(arch.LEGACY_SOCKOPT, futureSockopt=42))
        db.execute('INSERT INTO inbounds VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (row_id,'vless',8443 if kind=='reality' else 0,
            '' if kind=='reality' else arch.SOCKET+',0666', arch.TAGS[kind],kind,1,
            json.dumps({'clients':[{'email':'one','id':'uuid-one'}],'futureSettings':True}), json.dumps(stream),
            '{"unknownSniffing":true}',12,34,'native-column'))
    db.execute('INSERT INTO inbounds VALUES(99,\'vless\',12345,\'\',\'user\',\'custom\',1,\'{}\',\'{"network":"tcp"}\',\'{}\',9,8,\'custom-new\')')
    db.commit(); db.close()


def records(root):
    with connection(root / 'etc/x-ui/x-ui.db') as db:
        db.row_factory = sqlite3.Row
        return {r['id']: dict(r) for r in db.execute('SELECT * FROM inbounds')}


class Architecture(unittest.TestCase):
    def test_async_xray_startup_waits_and_ignores_other_user_listeners(self):
        addresses=['127.0.0.1:7443','127.0.0.1:9443','0.0.0.0:443','[::]:443','127.0.0.2:7443']
        output=lambda items:'\n'.join('LISTEN 0 128 '+item+' *:*' for item in items)
        with patch.object(arch.subprocess,'check_output',side_effect=[output(addresses),output(addresses+['127.0.0.1:8443']),arch.SOCKET]),patch.object(arch.time,'sleep') as sleep:
            arch.wait_listeners((7443,9443,8443),True)
            sleep.assert_called_once_with(0.5)

    def test_missing_required_listener_fails_with_bounded_wait(self):
        with patch.object(arch.subprocess,'check_output',return_value=''),patch.object(arch.time,'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError,'listener 8443 is missing'):
                arch.wait_listeners((8443,),False,timeout=0)
            sleep.assert_not_called()

    def test_patch_preserves_all_unowned_fields_and_custom_objects(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); fixture(root); before = records(root)
            with connection(root / 'etc/x-ui/x-ui.db') as db:
                before_other = {t:list(db.execute('SELECT * FROM '+t)) for t in ('clients','client_inbounds','hosts','settings','client_traffics')}
            user_nginx = (root/'etc/nginx/sites-available/user.conf').read_bytes()
            arch.reconcile(root)
            after = records(root)
            self.assertEqual(before[99], after[99])
            for key in before[1]:
                if key != 'listen': self.assertEqual(before[1][key], after[1][key])
            for key in before[2]:
                if key not in ('listen','stream_settings'): self.assertEqual(before[2][key], after[2][key])
            stream = json.loads(after[2]['stream_settings'])
            self.assertEqual(stream['xhttpSettings']['mode'],'stream-up')
            self.assertEqual(stream['sockopt'], {'futureSockopt':42,'trustedXForwardedFor':['X-Forwarded-For']})
            self.assertEqual(stream['future'], {'native':True})
            self.assertEqual(stream['xhttpSettings']['future'],'keep')
            with connection(root/'etc/x-ui/x-ui.db') as db:
                for table, values in before_other.items(): self.assertEqual(values,list(db.execute('SELECT * FROM '+table)))
            self.assertEqual(user_nginx,(root/'etc/nginx/sites-available/user.conf').read_bytes())
            for domain, port in (('main.example',7443),('cover.example',9443)):
                content = (root/'etc/nginx/sites-available'/domain).read_text()
                self.assertIn(f'listen 127.0.0.1:{port} ssl' + (' proxy_protocol' if port == 7443 else '') + ';',content)
                self.assertNotIn('[::]',content)
                self.assertIn('location /custom { add_header X-User "{braces}"; return 200; }',content)
                self.assertIn('server { server_name custom.example; listen 8888; }',content)
            content = (root/'etc/nginx/snippets/includes.conf').read_text()
            self.assertIn('location ^~ /secret/',content)
            self.assertIn('grpc_set_header X-Forwarded-For $remote_addr;',content)
            self.assertIn('add_header X-User "custom";',content)
            self.assertIn('location /user { return 200; }',content)

    def test_idempotency_including_files_and_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);arch.reconcile(root)
            before=records(root)
            files={f.relative_to(root):f.read_bytes() for f in root.rglob('*') if f.is_file() and f.suffix!='db'}
            arch.reconcile(root)
            self.assertEqual(before,records(root))
            self.assertEqual(files,{f.relative_to(root):f.read_bytes() for f in root.rglob('*') if f.is_file() and f.suffix!='db'})

    def test_restore_each_deleted_managed_inbound_retains_keys_clients_hosts(self):
        for kind,row_id in (('reality',1),('xhttp',2)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                root=Path(folder);fixture(root);arch.reconcile(root);before=records(root)
                with connection(root/'etc/x-ui/x-ui.db') as db:
                    db.execute('DELETE FROM inbounds WHERE id=?',(row_id,))
                    db.execute('DELETE FROM hosts WHERE inbound_id=?',(row_id,))
                    db.execute('DELETE FROM client_inbounds WHERE inbound_id=?',(row_id,))
                    db.execute('DELETE FROM client_traffics WHERE inbound_id=?',(row_id,))
                arch.reconcile(root);arch.reconcile(root)
                self.assertEqual(before,records(root))
                with connection(root/'etc/x-ui/x-ui.db') as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM client_inbounds').fetchone()[0],2)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM hosts').fetchone()[0],2)
                    self.assertEqual(db.execute('SELECT up,down FROM client_traffics WHERE email="one"').fetchone(),(1234,5678))

    def test_saved_id_allows_tag_change_without_replacing_user_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);arch.reconcile(root)
            with connection(root/'etc/x-ui/x-ui.db') as db: db.execute("UPDATE inbounds SET tag='user' WHERE id=1")
            before=records(root)
            arch.reconcile(root)
            self.assertEqual(before,records(root))

    def test_legacy_renamed_inbounds_and_hosts_keep_identity_without_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                db.execute("UPDATE inbounds SET tag='renamed-' || id,remark='My name ' || id WHERE id IN (1,2)")
                db.execute("UPDATE hosts SET address='custom.example',extra='custom host options'")
                hosts=list(db.execute('SELECT * FROM hosts'))
            before=records(root)
            arch.reconcile(root,remember=True);arch.reconcile(root);arch.reconcile(root)
            after=records(root)
            self.assertEqual(set(before),set(after))
            for identifier in (1,2):
                for field in ('tag','remark','settings','enable','up','down'):
                    self.assertEqual(before[identifier][field],after[identifier][field])
            with connection(root/'etc/x-ui/x-ui.db') as db:
                self.assertEqual(hosts,list(db.execute('SELECT * FROM hosts')))

    def test_saved_identity_survives_renaming_and_changed_bind(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);arch.reconcile(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                db.execute("UPDATE inbounds SET tag='renamed-' || id,remark='Changed',listen='127.0.0.1',port=5000 WHERE id IN (1,2)")
            arch.reconcile(root,remember=True);arch.reconcile(root)
            after=records(root)
            self.assertEqual(set(after),{1,2,99})
            self.assertEqual(after[1]['port'],8443)
            self.assertEqual(after[2]['port'],0)
            self.assertEqual(after[2]['listen'],arch.SOCKET+',0666')

    def test_saved_host_links_identify_edited_transport_and_missing_reality(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);arch.reconcile(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                stream=json.loads(records(root)[2]['stream_settings'])
                stream.update(network='tcp',security='none');stream['xhttpSettings']['path']='/user-edited'
                db.execute("UPDATE inbounds SET tag='user-xhttp',remark='User changed',listen='127.0.0.1',port=5000,stream_settings=? WHERE id=2",(json.dumps(stream),))
                for table in ('hosts','client_inbounds'):db.execute('DELETE FROM '+table+' WHERE inbound_id=1')
                db.execute('DELETE FROM inbounds WHERE id=1')
            arch.reconcile(root,remember=True);arch.reconcile(root);arch.reconcile(root)
            after=records(root)
            self.assertEqual(set(after),{1,2,99})
            self.assertEqual(after[2]['tag'],'user-xhttp')
            self.assertEqual(after[2]['listen'],arch.SOCKET+',0666')
            stream=json.loads(after[2]['stream_settings'])
            self.assertEqual(stream['network'],'xhttp')
            self.assertEqual(stream['xhttpSettings']['path'],'/secret')
            self.assertEqual(json.loads(after[1]['stream_settings'])['realitySettings']['privateKey'],'private')

    def test_verified_backup_identifies_legacy_edited_transport_by_host_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            store=root/'var/backups/x-ui';store.mkdir(parents=True)
            archive=store/'lucx-ui-backup-20261008-000000.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:tar.add(root/'etc/x-ui/x-ui.db',arcname='files/etc/x-ui/x-ui.db')
            Path(str(archive)+'.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                stream=json.loads(records(root)[2]['stream_settings']);stream['xhttpSettings']['path']='/changed'
                db.execute("UPDATE inbounds SET tag='renamed',listen='127.0.0.1',port=5000,stream_settings=? WHERE id=2",(json.dumps(stream),))
            arch.reconcile(root,remember=True);arch.reconcile(root)
            after=records(root)
            self.assertEqual(set(after),{1,2,99})
            self.assertEqual(after[2]['tag'],'renamed')
            self.assertEqual(after[2]['listen'],arch.SOCKET+',0666')

    def test_heavily_edited_reality_owned_fields_and_cleared_keys_recovered(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);arch.reconcile(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                stream=json.loads(records(root)[1]['stream_settings'])
                stream.update(network='ws',security='tls',tcpSettings={'acceptProxyProtocol':False,'header':{'type':'http','future':7}})
                stream['realitySettings'].update(target='127.0.0.1:1234',serverNames=['custom.example'],privateKey='',shortIds=[])
                db.execute("UPDATE inbounds SET tag='edited-reality',remark='Custom name',port=5001,listen='',stream_settings=? WHERE id=1",(json.dumps(stream),))
            arch.reconcile(root,remember=True);arch.reconcile(root);arch.reconcile(root)
            row=records(root)[1];stream=json.loads(row['stream_settings'])
            self.assertEqual(row['tag'],'edited-reality')
            self.assertEqual(row['remark'],'Custom name')
            self.assertEqual(row['listen'],'127.0.0.1')
            self.assertEqual(row['port'],8443)
            self.assertEqual(stream['network'],'tcp')
            self.assertEqual(stream['security'],'reality')
            self.assertEqual(stream['realitySettings']['privateKey'],'private')
            self.assertEqual(stream['realitySettings']['shortIds'],['abc'])
            self.assertEqual(stream['realitySettings']['target'],'127.0.0.1:9443')
            self.assertEqual(stream['realitySettings']['serverNames'],['custom.example','cover.example'])
            self.assertEqual(stream['tcpSettings']['header'],{'type':'none','future':7})

    def test_wrong_xhttp_host_fixed_but_valid_nginx_alias_and_empty_host_preserved(self):
        for host,expected in (('wrong.invalid','main.example'),('alias.example','alias.example'),('','')):
            with self.subTest(host=host),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);fixture(root)
                file=root/'etc/nginx/sites-available/main.example'
                file.write_text(file.read_text().replace('server_name main.example;', 'server_name main.example alias.example;'))
                with connection(root/'etc/x-ui/x-ui.db') as db:
                    stream=json.loads(records(root)[2]['stream_settings']);stream['xhttpSettings']['host']=host
                    db.execute('UPDATE inbounds SET stream_settings=? WHERE id=2',(json.dumps(stream),))
                arch.reconcile(root)
                self.assertEqual(json.loads(records(root)[2]['stream_settings'])['xhttpSettings']['host'],expected)
                self.assertIn('server_name main.example alias.example;',file.read_text())

    def test_renamed_deleted_legacy_inbound_recovers_from_verified_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                db.execute("UPDATE inbounds SET tag='renamed-' || id,remark='User name' WHERE id IN (1,2)")
            before=records(root)
            store=root/'var/backups/x-ui';store.mkdir(parents=True)
            archive=store/'lucx-ui-backup-20261008-000000.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:tar.add(root/'etc/x-ui/x-ui.db',arcname='files/etc/x-ui/x-ui.db')
            Path(str(archive)+'.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                for table in ('hosts','client_inbounds'):db.execute('DELETE FROM '+table+' WHERE inbound_id=1')
                db.execute('DELETE FROM inbounds WHERE id=1')
            arch.reconcile(root)
            for field in ('tag','remark','settings','stream_settings'):
                self.assertEqual(before[1][field],records(root)[1][field])

    def test_removed_native_host_and_new_security_fields_do_not_block_patch(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                stream=json.loads(records(root)[2]['stream_settings'])
                stream['xhttpSettings'].pop('host')
                stream['newNativeOptions']={'something':42}
                db.execute('UPDATE inbounds SET stream_settings=? WHERE id=2',(json.dumps(stream),))
            arch.reconcile(root)
            stream=json.loads(records(root)[2]['stream_settings'])
            self.assertNotIn('host',stream['xhttpSettings'])
            self.assertEqual(stream['newNativeOptions'],{'something':42})

    def test_missing_managed_transport_options_repaired_unknown_values_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);arch.reconcile(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                stream=json.loads(records(root)[2]['stream_settings'])
                stream['xhttpSettings'].pop('mode');stream['xhttpSettings'].pop('path')
                stream['nativeAddedAfterUpdate']={'nested':[1,2,3]}
                db.execute('UPDATE inbounds SET stream_settings=?,port=5000 WHERE id=2',(json.dumps(stream),))
            arch.reconcile(root)
            row=records(root)[2];stream=json.loads(row['stream_settings'])
            self.assertEqual(row['port'],0)
            self.assertEqual(stream['xhttpSettings']['path'],'/secret')
            self.assertEqual(stream['xhttpSettings']['mode'],'stream-up')
            self.assertEqual(stream['nativeAddedAfterUpdate'],{'nested':[1,2,3]})

    def test_connection_identity_loss_detected_but_new_native_fields_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);arch.reconcile(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:db.execute("UPDATE clients SET uuid='lost' WHERE id=1")
            before=records(root)
            with self.assertRaisesRegex(RuntimeError,'connection client identity'):arch.reconcile(root)
            self.assertEqual(before,records(root))

    def test_missing_without_identity_or_verified_backup_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:db.execute('DELETE FROM inbounds WHERE id=1')
            before=records(root)
            with self.assertRaisesRegex(RuntimeError,'no verified recovery record'):arch.reconcile(root)
            self.assertEqual(before,records(root))

    def test_legacy_deleted_record_recovered_from_verified_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            store=root/'var/backups/x-ui';store.mkdir(parents=True)
            archive=store/'lucx-ui-backup-20261007-000000.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:tar.add(root/'etc/x-ui/x-ui.db',arcname='etc/x-ui/x-ui.db')
            Path(str(archive)+'.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                for t in ('hosts','client_inbounds'):db.execute('DELETE FROM '+t+' WHERE inbound_id=1')
                db.execute('DELETE FROM inbounds WHERE id=1')
            arch.reconcile(root)
            self.assertEqual(json.loads(records(root)[1]['stream_settings'])['realitySettings']['privateKey'],'private')

    def test_user_sockopt_override_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                row=records(root)[2];s=json.loads(row['stream_settings']);s['sockopt']['mark']=77
                db.execute('UPDATE inbounds SET stream_settings=? WHERE id=2',(json.dumps(s),))
            arch.reconcile(root)
            self.assertEqual(json.loads(records(root)[2]['stream_settings'])['sockopt']['mark'],77)

    def test_duplicate_identity_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                duplicate=records(root)[2];duplicate['id']=3;arch.insert(db,'inbounds',duplicate)
            with self.assertRaisesRegex(RuntimeError,'Duplicate managed'):arch.reconcile(root)

    def test_provider_patch_preserves_extra_directives_and_unmanaged_vhost(self):
        spec=importlib.util.spec_from_file_location('compat_provider',REPO/'assets/compat/pro-compat.py')
        compat=importlib.util.module_from_spec(spec);spec.loader.exec_module(compat)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);arch.reconcile(root)
            snippet=root/'etc/nginx/snippets/includes.conf'
            snippet.write_text(compat.provider_route({'subCertFile':'cert','subKeyFile':'key'})
                              .replace('        proxy_redirect off;', '        proxy_redirect off;\n        proxy_read_timeout 2h;\n        add_header X-User-Provider "keep";')+snippet.read_text())
            template=root/'var/www/subpage/clash.yaml.tpl';template.parent.mkdir(parents=True)
            template.write_text('    url: https://main.example/__lucx_provider/${SUB_ID}\n')
            user=root/'etc/nginx/sites-available/user.conf'
            user.write_text('server { listen 127.0.0.1:9000; location / { proxy_pass https://127.0.0.1:54321; } }')
            before=user.read_text()
            compat.repair_nginx(root,{'webPort':'54321','subPort':'2096','subClashPath':'/new-provider/'})
            text=snippet.read_text()
            self.assertIn('rewrite ^ /new-provider/$lucx_provider_id break;',text)
            self.assertIn('proxy_pass http://127.0.0.1:2096;',text)
            self.assertIn('proxy_read_timeout 2h;',text)
            self.assertIn('add_header X-User-Provider "keep";',text)
            self.assertEqual(user.read_text(),before)

    def test_user_timeout_and_same_domain_other_server_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            file=root/'etc/nginx/snippets/includes.conf'
            file.write_text(file.read_text().replace('        grpc_buffer_size 16k;', '        grpc_read_timeout 2h;\n        client_body_timeout 3h;\n        grpc_buffer_size 16k;'))
            arch.reconcile(root)
            self.assertIn('grpc_read_timeout 2h;',file.read_text())
            self.assertIn('client_body_timeout 3h;',file.read_text())

    def test_same_version_gate_applies_before_update_or_repair_selector(self):
        script=(REPO/'lucx-ui-latest.sh').read_text(encoding='utf-8')
        update=script.split('update_compatibility() (',1)[1]
        gate=update.split("<<'PY_MIN_UPDATE'\n",1)[1].split('\nPY_MIN_UPDATE\n',1)[0]
        self.assertLess(update.index('PY_MIN_UPDATE'),update.index('while true; do'))
        for version,accepted in (('v3.9.0-lucx.285',False),('v3.9.0-lucx.286',True),('v3.9.0-lucx.287',True)):
            result=subprocess.run([sys.executable,'-c',gate,version],capture_output=True)
            self.assertEqual(result.returncode==0,accepted)

    def test_selector_update_repair_latest_invalid_input_and_eof(self):
        bash=shutil.which('bash')
        if not bash:self.skipTest('bash unavailable')
        source=(REPO/'lucx-ui-latest.sh').read_text(encoding='utf-8').split('update_compatibility() (',1)[1]
        menu=source[source.index('    while true; do'):source.index('    if [[ "${choice// /}" == 1 ]]; then')]
        for installed,stdin,expected in (('286','1\n','ACTION=1'),('286','2\n','ACTION=2'),
                ('287','1\n','ACTION=2'),('287','bad\n1\n','ACTION=2'),('287','','')):
            script='msg_err(){ :; };msg_inf(){ :; }; installed=v3.9.0-lucx.'+installed+'; target=v3.9.0-lucx.287;latest=$target\nf(){\n'+menu+'\necho ACTION=$choice;\n};f\n'
            result=subprocess.run([bash,'-c',script],input=stdin,text=True,capture_output=True,timeout=3)
            self.assertEqual(result.returncode,0)
            if expected:self.assertIn(expected,result.stdout)
            else:self.assertNotIn('ACTION=',result.stdout)
            if installed=='287':self.assertNotIn('Обновить панель до',result.stdout)

    def test_embedded_helpers_match_source(self):
        source=(REPO/'assets/compat/architecture.py').read_text().rstrip()
        for name in ('lucx-ui-latest.sh','assets/backup/lucx-ui-backup.sh'):
            script=(REPO/name).read_text(encoding='utf-8')
            embedded=script.split("<<'PY_LUCX_ARCHITECTURE'\n",1)[1].split('\nPY_LUCX_ARCHITECTURE\n',1)[0]
            self.assertEqual(source,embedded)


if __name__=='__main__':unittest.main()
