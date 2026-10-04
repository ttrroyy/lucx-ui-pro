"""Compatibility checks must fail safely without mutating live client records."""
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
spec = importlib.util.spec_from_file_location('compat', repo / 'assets/compat/pro-compat.py')
compat = importlib.util.module_from_spec(spec); spec.loader.exec_module(compat)


def database(root):
    path = root / 'etc/x-ui/x-ui.db'; path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.executescript('''CREATE TABLE clients(id INTEGER PRIMARY KEY,email TEXT UNIQUE,enable INTEGER,uuid TEXT,sub_id TEXT);
      CREATE TABLE inbounds(id INTEGER PRIMARY KEY,protocol TEXT,port INTEGER,settings TEXT);
      CREATE TABLE client_inbounds(client_id INTEGER,inbound_id INTEGER,PRIMARY KEY(client_id,inbound_id));
      CREATE TABLE settings(id INTEGER PRIMARY KEY,key TEXT,value TEXT);
      CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,password TEXT);
      INSERT INTO users VALUES(1,'admin','hash');
      INSERT INTO clients VALUES(1,'client',1,'uuid','sub');
      INSERT INTO inbounds VALUES(1,'qwdtt',1234,'{}'),(2,'csqtt',1235,'{"routeThroughXray":true}'),(3,'tproxy',1236,'{}');
      INSERT INTO client_inbounds VALUES(1,1),(1,2),(1,3);
      INSERT INTO settings VALUES(1,'webPort','54321'),(2,'webBasePath','/panel/');''')
    return db


class UpdateCompatibility(unittest.TestCase):
    def test_updater_defers_service_start_until_after_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'update.sh'
            path.write_text('XUI_UPDATE_TAG=x\n        systemctl start x-ui > /dev/null 2>&1\n        rc-service x-ui start > /dev/null 2>&1\n    config_after_update\n        bash "${awg_installer}" || true\n')
            compat.adapt_updater(path)
            result=path.read_text()
            self.assertNotIn('systemctl start x-ui',result)
            self.assertNotIn('rc-service x-ui start',result)
            self.assertIn('"${xui_folder}/x-ui" migrate || exit 1',result)

    def test_new_columns_do_not_mask_existing_data_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with closing(database(root)) as db:
                state=root/'state.json';state.write_text(json.dumps(compat.protected_state(root)))
                db.execute("ALTER TABLE clients ADD COLUMN password TEXT DEFAULT ''");db.commit()
                compat.preserve_client_flow(root,state);compat.verify_state(root,state)
                db.execute("UPDATE clients SET uuid='lost'");db.commit()
                with self.assertRaises(RuntimeError):compat.preserve_client_flow(root,state)
                with self.assertRaises(RuntimeError):compat.verify_state(root,state)

    def test_native_global_flow_clobber_is_restored_but_other_loss_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with closing(database(root)) as db:
                db.execute("ALTER TABLE clients ADD COLUMN flow TEXT DEFAULT ''")
                db.execute("UPDATE clients SET flow='xtls-rprx-vision'"); db.commit()
                state = root/'state.json'; state.write_text(json.dumps(compat.protected_state(root)))
                db.execute("UPDATE clients SET flow=''"); db.commit()
                compat.preserve_client_flow(root,state)
                compat.verify_state(root,state)
                db.execute("UPDATE clients SET flow='',sub_id='lost'"); db.commit()
                with self.assertRaisesRegex(RuntimeError,'protected client fields'):
                    compat.preserve_client_flow(root,state)
                self.assertEqual(db.execute('SELECT flow FROM clients').fetchone()[0],'')

    def test_real_schema_probe_preserves_live_database(self):
        with tempfile.TemporaryDirectory() as folder:
            with closing(database(Path(folder))) as db:
                before = '\n'.join(db.iterdump())
                compat.probe_clients(db)
                self.assertEqual(before, '\n'.join(db.iterdump()))
                db.execute('ALTER TABLE clients RENAME COLUMN email TO changed_email'); db.commit()
                with self.assertRaisesRegex(RuntimeError,'Unsupported panel schema'):
                    compat.probe_clients(db)

    def test_preservation_detects_data_and_file_loss(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with closing(database(root)) as db:
                site=root/'var/www/site/index.html';site.parent.mkdir(parents=True);site.write_text('site')
                state=root/'state.json';state.write_text(json.dumps(compat.protected_state(root)))
                # Announced repairs may rebuild JSON clients/change CSQTT routing.
                compat.sync_clients(db)
                db.execute("UPDATE inbounds SET settings=json_set(settings,'$.routeThroughXray',json('false')) WHERE protocol='csqtt'");db.commit()
                compat.verify_state(root,state)
                site.write_text('lost')
                with self.assertRaisesRegex(RuntimeError,'protected files'):
                    compat.verify_state(root,state)
                site.write_text('site')
                db.execute("UPDATE clients SET sub_id='lost' WHERE id=1");db.commit()
                with self.assertRaisesRegex(RuntimeError,'protected tables: clients'):
                    compat.verify_state(root,state)

    def test_json_timestamps_are_not_configuration_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with closing(database(root)) as db:
                db.execute("INSERT INTO inbounds VALUES(4,'vless',443,?)", (json.dumps({'clients':[{'email':'client','id':'uuid','updated_at':1}],'decryption':'none'}),))
                db.commit()
                state=root/'state.json';state.write_text(json.dumps(compat.protected_state(root)))
                db.execute("UPDATE inbounds SET settings=? WHERE id=4", (json.dumps({'decryption':'none','clients':[{'id':'uuid','updated_at':2,'email':'client'}]}),));db.commit()
                compat.verify_state(root,state)
                db.execute("UPDATE inbounds SET settings=? WHERE id=4", (json.dumps({'decryption':'none','clients':[{'id':'changed','updated_at':2,'email':'client'}]}),));db.commit()
                with self.assertRaisesRegex(RuntimeError,'protected tables: inbounds'):
                    compat.verify_state(root,state)

    def test_detach_preserves_upstream_triggers(self):
        with tempfile.TemporaryDirectory() as folder:
            with closing(database(Path(folder))) as db:
                compat.sync_clients(db)
                db.execute('CREATE TRIGGER author_trigger AFTER UPDATE ON clients BEGIN SELECT 1; END;')
                compat.detach_pro_triggers(db)
                self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall(), [('author_trigger',)])

    def test_updater_contract_is_behavior_based_not_version_based(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'update.sh'
            original='XUI_UPDATE_TAG=x\n    config_after_update\n        bash "${awg_installer}" || echo fail\n'
            path.write_text(original)
            compat.adapt_updater(path)
            self.assertIn('"${xui_folder}/x-ui" migrate || exit 1',path.read_text())
            self.assertNotIn('bash "${awg_installer}"',path.read_text())
            changed=original.replace('config_after_update','new_setup_function')
            path.write_text(changed)
            with self.assertRaisesRegex(RuntimeError,'contract changed'):
                compat.adapt_updater(path)
            self.assertEqual(path.read_text(),changed)

    def test_future_awg_skips_legacy_core_patch(self):
        spec=importlib.util.spec_from_file_location('awg',repo/'assets/compat/awg-compat.py')
        awg=importlib.util.module_from_spec(spec);spec.loader.exec_module(awg)
        with patch.object(awg,'run',return_value=SimpleNamespace(returncode=0,stdout='3.10.0-lucx.281\n')):
            self.assertFalse(awg.legacy_panel())
        with patch.object(awg,'run',return_value=SimpleNamespace(returncode=0,stdout='3.9.0-lucx.280\n')):
            self.assertTrue(awg.legacy_panel())


if __name__ == '__main__':
    unittest.main()
