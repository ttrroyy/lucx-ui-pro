from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

repo = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pro_compat', repo / 'assets/compat/pro-compat.py')
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


class ClientLinks(unittest.TestCase):
    def test_mixed_rename_moves_normal_protocol_links_back_to_original(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.executescript('''CREATE TABLE inbounds(id INTEGER PRIMARY KEY,protocol TEXT,settings TEXT);
              CREATE TABLE clients(id INTEGER PRIMARY KEY,email TEXT UNIQUE,enable INTEGER,uuid TEXT,sub_id TEXT);
              CREATE TABLE client_inbounds(client_id INTEGER,inbound_id INTEGER,flow_override TEXT,created_at INTEGER,PRIMARY KEY(client_id,inbound_id));
              INSERT INTO clients VALUES(1,'old',1,'uuid','sub');
              INSERT INTO inbounds VALUES(1,'qwdtt','{}'),(2,'csqtt','{}'),(3,'tproxy','{}'),(4,'vless','{"clients":[{"email":"new","id":"uuid"}]}');
              INSERT INTO client_inbounds VALUES(1,1,'',10),(1,2,'',10),(1,3,'',10);''')
            compat.sync_clients(db)
            db.execute("INSERT INTO clients VALUES(2,'new',0,'uuid','sub')")
            db.executemany('INSERT INTO client_inbounds VALUES(2,?,?,20)',[(1,''),(2,''),(3,''),(4,'xtls-rprx-vision')])
            db.execute("UPDATE clients SET email='new' WHERE id=1")
            self.assertEqual(db.execute('SELECT id,email,enable FROM clients').fetchall(),[(1,'new',0)])
            self.assertEqual(db.execute('SELECT client_id,flow_override,created_at FROM client_inbounds WHERE inbound_id=4').fetchone(),(1,'xtls-rprx-vision',20))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM client_inbounds WHERE client_id=1').fetchone()[0],4)

    def test_rkn_timer_reinstall_and_restore_reschedule(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);units=root/'etc/systemd/system';units.mkdir(parents=True)
            for name,delay,interval in [('list','15min','6h'),('self','30min','1d')]:
                path=units/f'rkn-guard-{name}-update.timer'
                path.write_text(f'[Timer]\nOnBootSec={delay}\nOnUnitActiveSec={interval}\nRandomizedDelaySec=20min\n')
            custom=units/'unrelated.timer';custom.write_text('OnBootSec=15min\n')
            compat.repair_rkn_timers(root);compat.repair_rkn_timers(root)
            for name,delay in [('list','15min'),('self','30min')]:
                text=(units/f'rkn-guard-{name}-update.timer').read_text()
                self.assertIn('OnActiveSec='+delay,text);self.assertNotIn('OnBootSec=',text)
            self.assertEqual(custom.read_text(),'OnBootSec=15min\n')

    def test_panel_share_only_rename_sequence_keeps_original_identity(self):
        for recursive in (0, 1):
            with self.subTest(recursive=recursive), closing(sqlite3.connect(':memory:')) as db:
                db.executescript('''CREATE TABLE inbounds(id INTEGER PRIMARY KEY,protocol TEXT,settings TEXT);
                  CREATE TABLE clients(id INTEGER PRIMARY KEY,email TEXT UNIQUE,enable INTEGER,uuid TEXT,sub_id TEXT,total_gb INTEGER);
                  CREATE TABLE client_inbounds(client_id INTEGER,inbound_id INTEGER,PRIMARY KEY(client_id,inbound_id));
                  INSERT INTO clients VALUES(1,'old',1,'uuid','sub',100);
                  INSERT INTO inbounds VALUES(1,'qwdtt','{}'),(2,'csqtt','{}'),(3,'tproxy','{}');
                  INSERT INTO client_inbounds VALUES(1,1),(1,2),(1,3);''')
                db.execute(f'PRAGMA recursive_triggers={recursive}')
                compat.sync_clients(db)
                # Actual panel fanout inserts by new email, links every inbound,
                # then renames the original row by its original primary key.
                db.execute("INSERT INTO clients VALUES(2,'renamed',0,'uuid','sub',200)")
                for inbound_id in (1, 2, 3):
                    db.execute('INSERT INTO client_inbounds VALUES(2,?)', (inbound_id,))
                db.execute("UPDATE clients SET email='renamed' WHERE id=1 AND email='old'")
                self.assertEqual(db.execute('SELECT * FROM clients').fetchall(), [(1,'renamed',0,'uuid','sub',200)])
                self.assertEqual(db.execute('SELECT client_id,inbound_id FROM client_inbounds').fetchall(),[(1,1),(1,2),(1,3)])
                for (settings,) in db.execute('SELECT settings FROM inbounds'):
                    self.assertEqual(json.loads(settings)['clients'],[{'email':'renamed','enable':False,'id':'uuid','subId':'sub','totalGB':200}])
                # A different identity must never be silently merged.
                db.execute("INSERT INTO clients VALUES(3,'occupied',1,'other','other',300)")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE clients SET email='occupied' WHERE id=1")
                db.execute("INSERT INTO clients VALUES(4,'partial',1,'uuid','sub',300)")
                db.execute('INSERT INTO client_inbounds VALUES(4,1)')
                db.execute("UPDATE clients SET email='partial' WHERE id=1")
                self.assertEqual(db.execute('SELECT inbound_id FROM client_inbounds WHERE client_id=1').fetchall(),[(1,),(2,),(3,)])
                self.assertIsNone(db.execute('SELECT id FROM clients WHERE id=4').fetchone())
                db.execute("INSERT INTO inbounds VALUES(4,'vless','{}')")
                db.execute('INSERT INTO client_inbounds VALUES(1,4)')
                db.execute("INSERT INTO clients VALUES(5,'mixed',1,'uuid','sub',300)")
                db.execute('INSERT INTO client_inbounds VALUES(5,1)')
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE clients SET email='mixed' WHERE id=1")

    def test_backfill_rename_detach_and_delete_both_directions(self):
        for recursive in (0, 1):
            with self.subTest(recursive=recursive), closing(sqlite3.connect(':memory:')) as db:
                db.executescript('''CREATE TABLE inbounds(id INTEGER PRIMARY KEY,protocol TEXT,settings TEXT);
                  CREATE TABLE clients(id INTEGER PRIMARY KEY,email TEXT,enable INTEGER);
                  CREATE TABLE client_inbounds(client_id INTEGER,inbound_id INTEGER,PRIMARY KEY(client_id,inbound_id));''')
                db.execute(f'PRAGMA recursive_triggers={recursive}')
                for row_id, protocol in enumerate(('qwdtt', 'csqtt', 'tproxy', 'vless'), 1):
                    db.execute('INSERT INTO inbounds VALUES(?,?,?)', (row_id, protocol, '{"clients":[],"password":"keep"}'))
                db.execute("INSERT INTO clients VALUES(1,'old',1)")
                db.execute("INSERT INTO clients VALUES(2,'other',1)")
                for row_id in (1, 2, 3):
                    db.execute('INSERT INTO client_inbounds VALUES(1,?)', (row_id,))
                db.execute('INSERT INTO client_inbounds VALUES(999,1)')
                db.commit()
                compat.sync_clients(db)

                def snapshot(row_id):
                    return json.loads(db.execute('SELECT settings FROM inbounds WHERE id=?', (row_id,)).fetchone()[0])

                self.assertEqual(db.execute('SELECT COUNT(*) FROM client_inbounds').fetchone()[0], 3)
                for row_id in (1, 2, 3):
                    self.assertEqual(snapshot(row_id)['clients'], [{'email': 'old', 'enable': True}])
                    self.assertEqual(snapshot(row_id)['password'], 'keep')
                    db.execute('INSERT INTO client_inbounds VALUES(2,?)', (row_id,))
                db.execute("UPDATE clients SET email='new',enable=0 WHERE id=1")
                self.assertIn({'email': 'new', 'enable': False}, snapshot(2)['clients'])
                db.execute('DELETE FROM client_inbounds WHERE client_id=1 AND inbound_id=2')
                self.assertEqual(snapshot(2)['clients'], [{'email': 'other', 'enable': True}])
                db.execute('DELETE FROM clients WHERE id=1')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM client_inbounds WHERE client_id=1').fetchone()[0], 0)
                self.assertEqual(snapshot(1)['clients'], [{'email': 'other', 'enable': True}])
                db.execute('DELETE FROM inbounds WHERE id=3')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM client_inbounds WHERE inbound_id=3').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM clients').fetchone()[0], 1)
                db.commit()
                compat.sync_clients(db)
                self.assertEqual(snapshot(1)['clients'], [{'email': 'other', 'enable': True}])
                self.assertEqual(snapshot(4)['clients'], [])

    def test_embedded_helpers_match_source(self):
        expected = (repo / 'assets/compat/pro-compat.py').read_text(encoding='utf-8').rstrip()
        for relative in ('lucx-ui-latest.sh', 'assets/backup/lucx-ui-backup.sh'):
            text = (repo / relative).read_text(encoding='utf-8')
            actual = text.split("<<'PY_PRO_COMPAT'\n", 1)[1].split('\nPY_PRO_COMPAT', 1)[0]
            self.assertEqual(expected, actual)
            awg = text.split("<<'PY_LUCX_AWG_BBR'\n", 1)[1].split('\nPY_LUCX_AWG_BBR', 1)[0]
            self.assertEqual((repo / 'assets/compat/awg-bbr.py').read_text(encoding='utf-8').rstrip(), awg)

    def test_native_provider_bypasses_renderer_and_forwarding_retirement(self):
        route = compat.provider_route({'subClashPath': '/mihomo/', 'subPort': '2096',
                                       'subCertFile': '/cert', 'subKeyFile': '/key'})
        self.assertIn('rewrite ^ /mihomo/$lucx_provider_id break;', route)
        self.assertIn('proxy_pass https://127.0.0.1:2096;', route)
        self.assertNotIn('/__lucx_clash', route)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'etc/sysctl.d/99-lucx-ui-forwarding.conf'
            path.parent.mkdir(parents=True)
            path.write_text('net.ipv4.ip_forward=1\nnet.ipv4.conf.all.rp_filter=2\n', encoding='utf-8')
            compat.remove_forwarding_override(root)
            self.assertEqual(path.read_text(), 'net.ipv4.conf.all.rp_filter=2\n')
            compat.remove_forwarding_override(root)
            self.assertEqual(path.read_text(), 'net.ipv4.conf.all.rp_filter=2\n')


if __name__ == '__main__':
    unittest.main()
