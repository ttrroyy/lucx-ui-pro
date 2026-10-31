"""Migrate both Pro layouts to a private TCP backend without changing identities."""
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from test_architecture import arch, connection, records
from test_one_domain import installation


def socket_installation(root, shared):
    installation(root,True)
    if shared:arch.reconcile(root,unify=True)
    with connection(root/'etc/x-ui/x-ui.db') as db:
        db.execute('UPDATE inbounds SET listen=?,port=0,tag=? WHERE id=2',(arch.SOCKET+',0666',arch.LEGACY_XHTTP_TAG))
    snippet=root/'etc/nginx/snippets/includes.conf'
    snippet.write_text(snippet.read_text().replace('grpc://127.0.0.1:10080','unix:'+arch.SOCKET))
    manifest=root/arch.STATE/'architecture.json'
    saved=json.loads(manifest.read_text());saved['objects']['xhttp']['record']=records(root)[2]
    manifest.write_text(json.dumps(saved))


class XhttpTCP(unittest.TestCase):
    def test_old_three_domain_and_shared_socket_migrate_with_real_ip_and_identity_preserved(self):
        for shared in (False,True):
            with self.subTest(shared=shared),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);socket_installation(root,shared);before=records(root)
                arch.reconcile(root,remember=True);arch.reconcile(root,unify=not shared)
                after=records(root)
                self.assertEqual((after[2]['listen'],after[2]['port']),(arch.XHTTP_LISTEN,arch.XHTTP_PORT))
                for key in ('id','tag','remark','sniffing','up','down','future_column'):
                    self.assertEqual(before[2][key],after[2][key])
                for key,value in json.loads(before[2]['settings']).items():
                    self.assertEqual(value,json.loads(after[2]['settings'])[key])
                self.assertEqual(before[99],after[99])
                stream=json.loads(after[2]['stream_settings'])
                self.assertEqual(stream['xhttpSettings']['mode'],'auto')
                self.assertEqual(stream['xhttpSettings']['path'],'/secret')
                self.assertEqual(stream['sockopt']['trustedXForwardedFor'],['X-Forwarded-For'])
                self.assertEqual(stream['sockopt']['futureSockopt'],42)
                reality=json.loads(after[1]['stream_settings'])
                self.assertEqual(reality['realitySettings']['xver'],1)
                self.assertTrue(reality['tcpSettings']['acceptProxyProtocol'])
                text=(root/'etc/nginx/snippets/includes.conf').read_text()
                self.assertIn('grpc_pass grpc://127.0.0.1:10080;',text)
                self.assertNotIn('grpc_pass unix:',text)
                self.assertIn('grpc_set_header X-Forwarded-For $remote_addr;',text)

    def test_repeat_and_restore_reconcile_keep_tcp_and_existing_tags(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);socket_installation(root,True);arch.reconcile(root)
            before=records(root);text=(root/'etc/nginx/snippets/includes.conf').read_text()
            arch.reconcile(root);arch.reconcile(root,remember=True);arch.reconcile(root)
            self.assertEqual(before,records(root))
            self.assertEqual(text,(root/'etc/nginx/snippets/includes.conf').read_text())
            self.assertEqual(records(root)[2]['tag'],arch.LEGACY_XHTTP_TAG)

    def test_fresh_tcp_signature_without_saved_manifest_is_recognized(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installation(root);arch.reconcile(root,unify=True)
            (root/arch.STATE/'architecture.json').unlink()
            self.assertEqual(records(root)[2]['tag'],arch.TAGS['xhttp'])
            arch.reconcile(root)
            self.assertTrue(json.loads((root/arch.STATE/'architecture.json').read_text())['objects']['xhttp']['present'])

    def test_deleted_owned_inbound_does_not_adopt_or_expose_other_tcp_inbound(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);socket_installation(root,True)
            with connection(root/'etc/x-ui/x-ui.db') as db:
                db.execute('DELETE FROM inbounds WHERE id=2')
                db.execute('DELETE FROM hosts WHERE inbound_id=2')
                db.execute('DELETE FROM client_inbounds WHERE inbound_id=2')
                db.execute('UPDATE inbounds SET listen=?,port=?,stream_settings=? WHERE id=99',
                           (arch.XHTTP_LISTEN,arch.XHTTP_PORT,json.dumps({'network':'xhttp','xhttpSettings':{'path':'/secret','host':'main.example'}})))
            custom=records(root)[99];arch.reconcile(root)
            self.assertEqual(custom,records(root)[99]);self.assertNotIn(2,records(root))
            self.assertNotIn('grpc://127.0.0.1:10080',(root/'etc/nginx/snippets/includes.conf').read_text())

    def test_foreign_tcp_conflict_refuses_before_files_or_database_change_but_udp_is_allowed(self):
        for udp in (False,True):
            with self.subTest(udp=udp),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);socket_installation(root,True)
                with connection(root/'etc/x-ui/x-ui.db') as db:
                    db.execute('UPDATE inbounds SET protocol=?,listen=?,port=? WHERE id=99',('hysteria' if udp else 'vless',arch.XHTTP_LISTEN,arch.XHTTP_PORT))
                before=records(root);text=(root/'etc/nginx/snippets/includes.conf').read_text()
                if udp:arch.reconcile(root)
                else:
                    with self.assertRaisesRegex(RuntimeError,'another enabled inbound'):arch.reconcile(root)
                    self.assertEqual(before,records(root))
                    self.assertEqual(text,(root/'etc/nginx/snippets/includes.conf').read_text())

    def test_live_validation_requires_loopback_tcp_not_public_tcp_or_old_unix_socket(self):
        output=lambda a:'\n'.join('LISTEN 0 128 '+x+' *:*' for x in a)
        addresses=['127.0.0.1:7443','0.0.0.0:443','[::]:443','0.0.0.0:10080']
        with patch.object(arch.subprocess,'check_output',return_value=output(addresses)):
            with self.assertRaisesRegex(RuntimeError,'XHTTP loopback TCP'):arch.wait_listeners((7443,),True,timeout=0)
        with patch.object(arch.subprocess,'check_output',return_value=output(addresses+['127.0.0.1:10080'])):
            arch.wait_listeners((7443,),True,timeout=0)

    def test_real_tcp_port_conflict_is_detected(self):
        try:
            occupied=socket.socket();occupied.bind(('127.0.0.1',0));occupied.listen(1)
        except PermissionError:self.skipTest('Local socket binding unavailable')
        with occupied,patch.object(arch,'XHTTP_PORT',occupied.getsockname()[1]):
            with self.assertRaisesRegex(RuntimeError,'occupied'):arch.check_xhttp_port()


if __name__=='__main__':unittest.main()
