from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

repo = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pro_compat', repo / 'assets/compat/pro-compat.py')
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


class ClientLinks(unittest.TestCase):
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
            awg = text.split("<<'PY_LUCX_AWG_COMPAT'\n", 1)[1].split('\nPY_LUCX_AWG_COMPAT', 1)[0]
            self.assertEqual((repo / 'assets/compat/awg-compat.py').read_text(encoding='utf-8').rstrip(), awg)

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
