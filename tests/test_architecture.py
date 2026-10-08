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


def add_hy2(root):
    row = copy.deepcopy(records(root)[2]); row.update(id=3, protocol='hysteria',
        port=4443, listen='', tag='inbound-4443', remark='My HY2', settings='{"version":2,"clients":[],"future":7}')
    row['stream_settings']=json.dumps({'network':'hysteria','security':'tls',
        'hysteriaSettings':{'version':2,'udpIdleTimeout':60,'future':8},
        'tlsSettings':{'certificates':[{'certificateFile':'/root/cert/main.example/fullchain.pem','keyFile':'key','future':9}],
                       'settings':{'fingerprint':'','future':10},'futureTls':11},
        'finalmask':{'tcp':[],'udp':[{'type':'salamander','settings':{'password':'secret','future':12}}],'future':13},
        'futureStream':14})
    with connection(root/'etc/x-ui/x-ui.db') as db:
        arch.insert(db,'inbounds',row)
        db.execute("INSERT INTO hosts VALUES(3,3,'main.example',4443,'hy2-extra')")
        db.execute("INSERT INTO client_inbounds VALUES(1,3,'')")


class Architecture(unittest.TestCase):
    def test_owned_xhttp_host_sni_repair_preserves_additional_user_host(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                db.executescript("ALTER TABLE hosts ADD COLUMN sni TEXT DEFAULT '';"
                                 "ALTER TABLE hosts ADD COLUMN host_header TEXT DEFAULT '';"
                                 "INSERT INTO hosts VALUES(50,2,'user.example',1111,'user-extra','user.sni','user.host');")
            for attempt in range(2):
                arch.reconcile(root,remember=True);arch.reconcile(root)
                with connection(root/'etc/x-ui/x-ui.db') as db:
                    self.assertEqual(db.execute('SELECT sni,host_header FROM hosts WHERE id=2').fetchone(),
                                     ('main.example','main.example'))
                    self.assertEqual(db.execute('SELECT sni,host_header FROM hosts WHERE id=1').fetchone(),('',''))
                    self.assertEqual(db.execute('SELECT sni,host_header FROM hosts WHERE id=50').fetchone(),
                                     ('user.sni','user.host'))
                self.assertEqual(json.loads(records(root)[2]['stream_settings'])['xhttpSettings']['host'],'main.example')

    def test_subscription_probe_ignores_deleted_disabled_and_custom_only_nodes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);arch.reconcile(root)
            file=root/'etc/x-ui/x-ui.db'
            with connection(file) as db:
                db.executescript("ALTER TABLE clients ADD COLUMN enable INTEGER DEFAULT 1;"
                                 "ALTER TABLE clients ADD COLUMN sub_id TEXT DEFAULT 'owned-sub';"
                                 "INSERT INTO clients VALUES(0,'custom','custom-id',1,'custom-sub');"
                                 "INSERT INTO client_inbounds VALUES(0,99,'');"
                                 "INSERT INTO settings VALUES(2,'subEnable','true');")
            source=(REPO/'lucx-ui-latest.sh').read_text(encoding='utf-8')
            probe=source.split("<<'PY_UPDATE_SUBS'\n",1)[1].split('\nPY_UPDATE_SUBS\n',1)[0]
            probe=probe.replace("Path('/var/lib/lucx-ui-preinstall/architecture.json')",
                                'Path('+repr(str(root/arch.STATE/'architecture.json'))+')')
            def execute():
                result=subprocess.run([sys.executable,'-c',probe,str(file)],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                return result.stdout
            self.assertIn('owned-sub',execute());self.assertNotIn('custom-sub',execute())
            with connection(file) as db:db.execute('UPDATE inbounds SET enable=0 WHERE id IN (1,2)')
            self.assertEqual(execute(),'')
            with connection(file) as db:db.execute('DELETE FROM inbounds WHERE id IN (1,2)')
            arch.reconcile(root)
            self.assertEqual(execute(),'')

    def test_future_nested_sockopt_field_survives_known_option_reset(self):
        self.assertEqual(arch.reset_sockopt({'tcpFastOpen':True,'happyEyeballs':{
            'tryDelayMs':999,'prioritizeIPv6':True,'interleave':2,'maxConcurrentTry':8,'futureNative':7}}),
            {'happyEyeballs':{'futureNative':7}})
    def test_ambiguous_verified_backup_is_not_an_update_blocker(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                duplicate=records(root)[2];duplicate['id']=3;arch.insert(db,'inbounds',duplicate)
            store=root/'var/backups/x-ui';store.mkdir(parents=True)
            archive=store/'lucx-ui-backup-20261008-000000.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:tar.add(root/'etc/x-ui/x-ui.db',arcname='files/etc/x-ui/x-ui.db')
            Path(str(archive)+'.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name)
            self.assertIsNone(arch.archive_snapshot(root,'xhttp',['main.example','cover.example'],'/secret',optional=True))
    def test_unmanaged_optional_null_transport_does_not_block_owned_patch(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                db.execute('UPDATE inbounds SET listen=NULL,enable=0,stream_settings=? WHERE id=99',
                    ('{"network":"xhttp","xhttpSettings":null,"future":7}',))
            before=records(root)[99];arch.reconcile(root)
            self.assertEqual(before,records(root)[99])
            self.assertEqual(json.loads(records(root)[2]['stream_settings'])['xhttpSettings']['mode'],'stream-up')
    def test_native_public_key_formats_and_future_format_do_not_block(self):
        for label in ('Password (PublicKey)','PublicKey','Public key','Password','FutureLabel'):
            with self.subTest(label=label),patch.object(arch.Path,'glob',return_value=iter([Path('/fake/xray')])):
                result=subprocess.CompletedProcess([],0,stdout=label+': '+'B'*43+'\n',stderr='')
                with patch.object(arch.subprocess,'run',return_value=result):
                    self.assertEqual(arch.reality_public_key('A'*43),'B'*43 if label!='FutureLabel' else None)

    def test_owned_protocol_changes_normalize_without_recreating_row(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);arch.reconcile(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:db.execute("UPDATE inbounds SET protocol='tproxy',tag='My tag',remark='My name' WHERE id=2")
            arch.reconcile(root,remember=True);arch.reconcile(root)
            row=records(root)[2]
            self.assertEqual((row['id'],row['protocol'],row['tag'],row['remark']),(2,'vless','My tag','My name'))
    def test_optional_hy2_owned_normalization_and_deleted_not_recreated(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            arch.reconcile(root)
            self.assertNotIn('hy2',json.loads((root/arch.STATE/'architecture.json').read_text())['objects'])
            add_hy2(root);arch.reconcile(root);original=records(root)[3]
            with connection(root/'etc/x-ui/x-ui.db') as db:
                s=json.loads(original['stream_settings']);s['network']='tcp';s['security']='none'
                s['hysteriaSettings'].update(version=1,udpIdleTimeout=0,auth='wrong',masquerade={'type':'proxy'})
                s['tlsSettings'].update(serverName='wrong.example',alpn=['http/1.1'])
                s['tlsSettings']['settings']['fingerprint']='chrome'
                s['finalmask']['udp']=[]
                db.execute("UPDATE inbounds SET listen='127.0.0.1',port=5555,tag='renamed',remark='New name',stream_settings=? WHERE id=3",(json.dumps(s),))
                db.execute("UPDATE hosts SET address='wrong.example',port=9999 WHERE id=3")
            arch.reconcile(root,remember=True);arch.reconcile(root)
            row=records(root)[3];s=json.loads(row['stream_settings'])
            self.assertEqual((row['port'],row['listen'],row['tag'],row['remark']),(4443,'','renamed','New name'))
            self.assertEqual(s['hysteriaSettings'],{'version':2,'udpIdleTimeout':60,'future':8})
            self.assertEqual(s['tlsSettings']['settings'],{'fingerprint':'','future':10,'echConfigList':'','pinnedPeerCertSha256':[],'verifyPeerCertByName':''})
            self.assertEqual(arch.salamander_password(s),'secret');self.assertEqual(s['futureStream'],14)
            before=records(root);arch.reconcile(root);self.assertEqual(before,records(root))
            with connection(root/'etc/x-ui/x-ui.db') as db:
                self.assertEqual(db.execute('SELECT address,port FROM hosts WHERE id=3').fetchone(),('main.example',4443))
                for table in ('hosts','client_inbounds'):db.execute('DELETE FROM '+table+' WHERE inbound_id=3')
                db.execute('DELETE FROM inbounds WHERE id=3')
            arch.reconcile(root,remember=True);arch.reconcile(root)
            self.assertNotIn(3,records(root))

    def test_saved_id_reused_by_custom_inbound_does_not_adopt_or_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);arch.reconcile(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                db.execute('DELETE FROM hosts WHERE inbound_id=2')
                custom=copy.deepcopy(records(root)[99]);custom['id']=2
                db.execute('DELETE FROM inbounds WHERE id=2');arch.insert(db,'inbounds',custom)
            before=records(root);arch.reconcile(root)
            self.assertEqual(before,records(root))

    def test_native_user_hy2_or_nonselected_hy2_never_adopted(self):
        for selected in (False, None):
            with self.subTest(selected=selected),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);fixture(root);add_hy2(root)
                if selected is False:
                    (root/arch.STATE/'hy2-ownership.json').write_text('{"selected":false}')
                else:
                    with connection(root/'etc/x-ui/x-ui.db') as db:
                        db.execute('ALTER TABLE inbounds ADD COLUMN created_at INTEGER')
                        db.execute('UPDATE inbounds SET created_at=123456 WHERE id=3')
                before=records(root)[3];arch.reconcile(root)
                self.assertEqual(before,records(root)[3])
                self.assertNotIn('hy2',json.loads((root/arch.STATE/'architecture.json').read_text())['objects'])
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
                if key not in ('listen','settings','stream_settings'): self.assertEqual(before[1][key], after[1][key])
            for key in before[2]:
                if key not in ('listen','settings','stream_settings'): self.assertEqual(before[2][key], after[2][key])
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

    def test_deleted_managed_inbounds_and_hosts_are_not_recreated(self):
        for row_id in (1, 2):
            with self.subTest(row_id=row_id), tempfile.TemporaryDirectory() as folder:
                root=Path(folder);fixture(root);arch.reconcile(root)
                with connection(root/'etc/x-ui/x-ui.db') as db:
                    for table in ('hosts','client_inbounds','client_traffics'):
                        db.execute('DELETE FROM '+table+' WHERE inbound_id=?',(row_id,))
                    db.execute('DELETE FROM inbounds WHERE id=?',(row_id,))
                before=records(root)
                arch.reconcile(root,remember=True);arch.reconcile(root);arch.reconcile(root)
                self.assertEqual(before,records(root))
                self.assertNotIn(row_id,records(root))

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
                for field in ('tag','remark','enable','up','down'):
                    self.assertEqual(before[identifier][field],after[identifier][field])
            with connection(root/'etc/x-ui/x-ui.db') as db:
                self.assertEqual([(h[0],h[1],h[4]) for h in hosts],[(h[0],h[1],h[4]) for h in db.execute('SELECT * FROM hosts')])

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
            self.assertEqual(set(after),{2,99})
            self.assertEqual(after[2]['tag'],'user-xhttp')
            self.assertEqual(after[2]['listen'],arch.SOCKET+',0666')
            stream=json.loads(after[2]['stream_settings'])
            self.assertEqual(stream['network'],'xhttp')
            self.assertEqual(stream['xhttpSettings']['path'],'/secret')
            self.assertNotIn(1,after)

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
            self.assertEqual(stream['realitySettings']['serverNames'],['cover.example'])
            self.assertEqual(stream['tcpSettings']['header'],{'type':'none','future':7})

    def test_known_xhttp_fields_reset_regardless_of_mode_and_user_values(self):
        for mode in ('packet-up','stream-up','auto','stream-one'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);fixture(root);arch.reconcile(root)
                with connection(root/'etc/x-ui/x-ui.db') as db:
                    stream=json.loads(records(root)[2]['stream_settings'])
                    stream['xhttpSettings'].update({k:'user-changed' for k in arch.XHTTP_FIELDS})
                    stream['xhttpSettings'].update(mode=mode, xmux={'maxConcurrency':'1','maxConnections':4,'future':7},
                        extra={'noGRPCHeader':True,'futureExtra':9},future='keep')
                    stream['sockopt'].update({k:'changed' for k in arch.SOCKOPT_FIELDS})
                    stream['sockopt']['futureSockopt']=42
                    db.execute('UPDATE inbounds SET stream_settings=? WHERE id=2',(json.dumps(stream),))
                arch.reconcile(root)
                stream=json.loads(records(root)[2]['stream_settings'])
                self.assertEqual(stream['xhttpSettings'],{'host':'main.example','path':'/secret','mode':'stream-up','future':'keep',
                    'xmux':{'future':7},'extra':{'futureExtra':9}})
                self.assertEqual(stream['sockopt'],{'futureSockopt':42,'trustedXForwardedFor':['X-Forwarded-For']})

    def test_deleted_legacy_inbound_stays_deleted_even_with_verified_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            store=root/'var/backups/x-ui';store.mkdir(parents=True)
            archive=store/'lucx-ui-backup-20261008-000000.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:tar.add(root/'etc/x-ui/x-ui.db',arcname='files/etc/x-ui/x-ui.db')
            Path(str(archive)+'.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                for table in ('hosts','client_inbounds'):db.execute('DELETE FROM '+table+' WHERE inbound_id=1')
                db.execute('DELETE FROM inbounds WHERE id=1')
            arch.reconcile(root);arch.reconcile(root)
            self.assertNotIn(1,records(root))

    def test_missing_xhttp_host_is_restored_and_new_native_fields_survive(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                stream=json.loads(records(root)[2]['stream_settings'])
                stream['xhttpSettings'].pop('host')
                stream['newNativeOptions']={'something':42}
                db.execute('UPDATE inbounds SET stream_settings=? WHERE id=2',(json.dumps(stream),))
            arch.reconcile(root)
            stream=json.loads(records(root)[2]['stream_settings'])
            self.assertEqual(stream['xhttpSettings']['host'],'main.example')
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
            root=Path(folder);fixture(root);arch.reconcile(root);arch.reconcile(root,remember=True)
            with connection(root/'etc/x-ui/x-ui.db') as db:db.execute("UPDATE clients SET uuid='lost' WHERE id=1")
            before=records(root)
            with self.assertRaisesRegex(RuntimeError,'connection client identity'):arch.reconcile(root)
            self.assertEqual(before,records(root))

    def test_legitimate_client_edits_between_operations_do_not_block_reconcile(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);arch.reconcile(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:db.execute("UPDATE clients SET uuid='user-rotated-key',email='user-renamed' WHERE id=1")
            arch.reconcile(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                self.assertEqual(db.execute('SELECT email,uuid FROM clients WHERE id=1').fetchone(),('user-renamed','user-rotated-key'))

    def test_missing_without_identity_or_backup_does_not_block_other_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:db.execute('DELETE FROM inbounds WHERE id=1')
            arch.reconcile(root)
            self.assertNotIn(1,records(root))
            self.assertEqual(json.loads(records(root)[2]['stream_settings'])['xhttpSettings']['mode'],'stream-up')

    def test_only_original_host_is_normalized_new_user_host_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);arch.reconcile(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                db.execute("INSERT INTO hosts VALUES(50,2,'user.example',1111,'user-extra')")
                db.execute("UPDATE hosts SET address='broken.invalid',port=5000 WHERE id=2")
            arch.reconcile(root,remember=True);arch.reconcile(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                self.assertEqual(db.execute('SELECT address,port,extra FROM hosts WHERE id=2').fetchone(),
                    ('main.example',443,'host-new-field'))
                self.assertEqual(db.execute('SELECT * FROM hosts WHERE id=50').fetchone(),
                    (50,2,'user.example',1111,'user-extra'))
                db.execute('DELETE FROM hosts WHERE id=2')
            arch.reconcile(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:self.assertIsNone(db.execute('SELECT * FROM hosts WHERE id=2').fetchone())

    def test_user_changed_known_sockopt_is_reset_on_owned_inbound_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                s=json.loads(records(root)[2]['stream_settings']);s['sockopt']['mark']=77
                db.execute('UPDATE inbounds SET stream_settings=? WHERE id=2',(json.dumps(s),))
                db.execute('UPDATE inbounds SET stream_settings=? WHERE id=99',(json.dumps(s),))
            custom=records(root)[99]
            arch.reconcile(root)
            self.assertNotIn('mark',json.loads(records(root)[2]['stream_settings'])['sockopt'])
            self.assertEqual(records(root)[99],custom)

    def test_ambiguous_legacy_signature_is_skipped_without_blocking_update(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                duplicate=records(root)[2];duplicate['id']=3;arch.insert(db,'inbounds',duplicate)
            before=records(root)
            arch.reconcile(root)
            self.assertEqual(before[2],records(root)[2]);self.assertEqual(before[3],records(root)[3])

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
