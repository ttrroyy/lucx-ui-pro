import contextlib,io,json,re,sqlite3,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from test_update_compat import compat

repo=Path(__file__).resolve().parents[1]
script=(repo/'lucx-ui-latest.sh').read_text(encoding='utf-8')
section=script[script.index('insert_extra_inbound()'):script.index('choose_adguard()')]
body=re.search(r"<<'PY'\n(.*?)\nPY",section,re.S)[1]

class OpenFluxInstall(unittest.TestCase):
    def test_install_and_client_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'panel.db';db=sqlite3.connect(path)
            db.executescript("""CREATE TABLE clients(id INTEGER PRIMARY KEY,email TEXT UNIQUE,enable INTEGER);
CREATE TABLE client_inbounds(client_id INTEGER,inbound_id INTEGER,PRIMARY KEY(client_id,inbound_id));
CREATE TABLE inbounds(id INTEGER PRIMARY KEY,user_id INTEGER,up INTEGER,down INTEGER,total INTEGER,remark TEXT,enable INTEGER,expiry_time INTEGER,listen TEXT,port INTEGER,protocol TEXT,settings TEXT,stream_settings TEXT,tag TEXT,sniffing TEXT);""")
            args=['-',str(path),'openflux','OpenFlux','18445','','203.0.113.10','unused','0123456789abcdef01234567']
            with patch.object(sys,'argv',args),patch('socket.socket') as sock,contextlib.redirect_stdout(io.StringIO()):
                exec(compile(body,'installer','exec'),{})
            row=db.execute('SELECT id,remark,port,settings FROM inbounds').fetchone()
            self.assertEqual(row[1:3],('OpenFlux',18445))
            cfg=json.loads(row[3]);self.assertEqual(cfg['shareHost'],'203.0.113.10')
            self.assertTrue(all(cfg[k]=='' for k in ('yandexUrl','mailruUrl','cupsUrl')))
            compat.sync_clients(db)
            db.execute("INSERT INTO clients VALUES(1,'client',1)");db.execute('INSERT INTO client_inbounds VALUES(1,?)',(row[0],))
            self.assertEqual(json.loads(db.execute('SELECT settings FROM inbounds').fetchone()[0])['clients'][0]['email'],'client')
            db.execute('DELETE FROM client_inbounds');self.assertEqual(json.loads(db.execute('SELECT settings FROM inbounds').fetchone()[0])['clients'],[])
            db.execute('INSERT INTO client_inbounds VALUES(1,?)',(row[0],));db.execute('DELETE FROM clients')
            self.assertEqual(db.execute('SELECT count(*) FROM client_inbounds').fetchone()[0],0)
            db.execute("INSERT INTO clients VALUES(2,'other',1)");db.execute('INSERT INTO client_inbounds VALUES(2,?)',(row[0],));db.execute('DELETE FROM inbounds')
            self.assertEqual(db.execute('SELECT count(*) FROM client_inbounds').fetchone()[0],0)
            db.close()

    def test_selector_and_firewall_wiring(self):
        self.assertIn("echo '6 - OpenFlux'",script)
        self.assertIn('6) has_valid=1; want_openflux=1',script)
        self.assertIn('_insert_one_extra openflux OpenFlux 18445',script)
        self.assertIn('ufw allow 18445/tcp || return 1',script)
