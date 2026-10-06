"""Sidecar settings saves must retain the normalized client's identity."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest
from contextlib import closing

repo=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('compat',repo/'assets/compat/pro-compat.py')
compat=importlib.util.module_from_spec(spec);spec.loader.exec_module(compat)


class ClientCache(unittest.TestCase):
    def test_stale_settings_cannot_replace_identity_or_secret(self):
        for recursive in (0,1):
            with self.subTest(recursive=recursive), closing(sqlite3.connect(':memory:')) as db:
                db.executescript('''CREATE TABLE inbounds(id INTEGER PRIMARY KEY,protocol TEXT,settings TEXT);
                  CREATE TABLE clients(id INTEGER PRIMARY KEY,email TEXT UNIQUE,enable INTEGER,uuid TEXT,sub_id TEXT,flow TEXT,total_gb INTEGER);
                  CREATE TABLE client_inbounds(client_id INTEGER,inbound_id INTEGER,PRIMARY KEY(client_id,inbound_id));
                  INSERT INTO clients VALUES(1,'client',1,'identity','subscription','xtls-rprx-vision',123);
                  INSERT INTO inbounds VALUES(1,'openflux','{"secret":"inbound-secret"}');
                  INSERT INTO client_inbounds VALUES(1,1);''')
                db.execute(f'PRAGMA recursive_triggers={recursive}')
                compat.sync_clients(db)
                db.execute("UPDATE inbounds SET settings=? WHERE id=1",(json.dumps({'secret':'inbound-secret','clients':[{'email':'stale'}]}),))
                value=json.loads(db.execute('SELECT settings FROM inbounds').fetchone()[0])
                self.assertEqual(value['secret'],'inbound-secret')
                self.assertEqual(value['clients'],[{'email':'client','enable':True,'id':'identity','subId':'subscription','flow':'xtls-rprx-vision','totalGB':123}])
                db.execute('UPDATE clients SET total_gb=456 WHERE id=1')
                self.assertEqual(json.loads(db.execute('SELECT settings FROM inbounds').fetchone()[0])['clients'][0]['totalGB'],456)
                db.execute('DELETE FROM client_inbounds')
                self.assertEqual(json.loads(db.execute('SELECT settings FROM inbounds').fetchone()[0])['clients'],[])
