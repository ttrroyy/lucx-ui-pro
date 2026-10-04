"""TG maintenance follows the managed domain if the UI changes its tag."""
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

class TProxyOwnership(unittest.TestCase):
    def test_renamed_owned_tag_removed_without_touching_other_domains_ports(self):
        source=(Path(__file__).resolve().parents[1]/'lucx-ui-latest.sh').read_text(encoding='utf-8')
        code=re.search(r"<<'PY_TG_DELETE'\n(.*?)\nPY_TG_DELETE",source,re.S)[1]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db'
            with closing(sqlite3.connect(path)) as db:
                db.executescript('''CREATE TABLE inbounds(id INTEGER,protocol TEXT,tag TEXT,listen TEXT,port INTEGER,settings TEXT);
                CREATE TABLE client_inbounds(client_id INTEGER,inbound_id INTEGER);
                INSERT INTO inbounds VALUES(1,'tproxy','inbound-tproxy','127.0.0.1',11443,'{"hostname":"owned.example"}'),
                (2,'tproxy','inbound-2','127.0.0.1',11443,'{"hostname":"owned.example"}'),
                (3,'tproxy','custom','127.0.0.1',11443,'{"hostname":"other.example"}'),
                (4,'tproxy','custom-port','127.0.0.1',12443,'{"hostname":"owned.example"}');
                INSERT INTO client_inbounds VALUES(1,1),(1,2),(1,3),(1,4);''')
            namespace={}
            with patch.object(sys,'argv',['helper',str(path),'owned.example']):exec(compile(code,'tg-delete','exec'),namespace)
            namespace['con'].close()
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT id FROM inbounds ORDER BY id').fetchall(),[(3,),(4,)])
                self.assertEqual(db.execute('SELECT inbound_id FROM client_inbounds ORDER BY inbound_id').fetchall(),[(3,),(4,)])
