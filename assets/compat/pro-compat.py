#!/usr/bin/env python3
"""Local, idempotent Pro migrations. Never resets panel accounts or ports."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

REVISION = '2026.10.08-287.4'
# Generated from assets/clash/clash-sub-server.py by sync-embedded.py.
CLASH_RENDERER_SOURCE = r'''
#!/usr/bin/env python3
"""Local per-client YAML renderer, with identical GET/HEAD response headers."""
import argparse
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

TEMPLATE = Path('/var/www/subpage/clash.yaml.tpl')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.render()

    def do_HEAD(self):
        self.render()

    def render(self):
        parsed = urlsplit(self.path)
        if parsed.path == '/health':
            return self.reply(200, b'ok', 'text/plain')
        if parsed.path != '/api/clash':
            return self.reply(404, b'not found', 'text/plain')
        sub_id = parse_qs(parsed.query).get('sub_id', [''])[0]
        if not re.fullmatch(r'[A-Za-z0-9._~-]{1,256}', sub_id):
            return self.reply(400, b'invalid subscription id', 'text/plain')
        try:
            body = TEMPLATE.read_text(encoding='utf-8').replace('${SUB_ID}', sub_id).encode('utf-8')
        except OSError:
            return self.reply(503, b'template unavailable', 'text/plain')
        self.reply(200, body, 'text/yaml; charset=utf-8')

    def reply(self, status, body, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if status == 200 and content_type.startswith('text/yaml'):
            self.send_header('Content-Disposition', 'attachment; filename="clash.yaml"')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        # nginx already logs requests; do not duplicate secret subscription IDs
        # in the renderer's system journal.
        pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
'''
PROTOCOLS = "('qwdtt','csqtt','tproxy','olcrtc','openflux')"


def client_sync_sql(client_columns=(), link_columns=('client_id', 'inbound_id')):
    # Rebuild from normalized records, not an email snapshot of a deleted row.
    fields = ["'email',c.email", "'enable',json(CASE WHEN c.enable THEN 'true' ELSE 'false' END)"]
    # A settings save still feeds these cached clients through SyncInbound.
    # Preserve the normalized identity, limits and credentials in that cache.
    mapping = {'uuid':'id','sub_id':'subId','password':'password','auth':'auth',
               'flow':'flow','security':'security','wg_private_key':'privateKey',
               'wg_public_key':'publicKey','wg_pre_shared_key':'preSharedKey',
               'wg_keep_alive':'keepAlive','wg_forwarded_ports':'forwardedPorts',
               'secret':'secret','ad_tag':'adTag','limit_ip':'limitIp',
               'total_gb':'totalGB','expiry_time':'expiryTime','tg_id':'tgId',
               'group_name':'group','comment':'comment','reset':'reset',
               'reset_day':'resetDay','reset_weekday':'resetWeekday','reset_max':'resetMax',
               'traffic_reset':'trafficReset','traffic_reset_day':'trafficResetDay',
               'created_at':'created_at','updated_at':'updated_at'}
    for column, key in mapping.items():
        if column in client_columns:
            fields.append(f"'{key}',c.\"{column}\"")
    if 'reverse' in client_columns:
        fields.append("'reverse',json(CASE WHEN json_valid(c.reverse) THEN c.reverse ELSE 'null' END)")
    if 'wg_allowed_ips' in client_columns:
        fields.append("""'allowedIPs',json(CASE WHEN COALESCE(c.wg_allowed_ips,'')='' THEN '[]'
          ELSE '[' || replace(json_quote(c.wg_allowed_ips),',','","') || ']' END)""")
    client_json = 'json_object(' + ','.join(fields) + ')'
    rebuild = """UPDATE inbounds SET settings = json_set(
      CASE WHEN json_valid(settings) THEN settings ELSE '{}' END, '$.clients',
      json(COALESCE((SELECT json_group_array(%s)
        FROM clients c JOIN client_inbounds ci ON ci.client_id=c.id
        WHERE ci.inbound_id=inbounds.id AND c.email != ''), '[]')))
      WHERE protocol IN %s""" % (client_json, PROTOCOLS)
    sql = 'BEGIN IMMEDIATE;\n'
    for name in ('ins', 'del', 'link_update', 'client_update', 'client_delete', 'inbound_delete', 'rename_merge', 'inbound_update'):
        sql += f'DROP TRIGGER IF EXISTS lucx_shareonly_clients_{name};\n'
    # Old Pro component removals and old archives can leave dangling links.
    sql += ('DELETE FROM client_inbounds WHERE client_id NOT IN (SELECT id FROM clients) '
            'OR inbound_id NOT IN (SELECT id FROM inbounds);\n')
    sql += rebuild + ';\n'
    # LucX 280's share-only UpdateInboundClient creates a second normalized
    # record before Update() renames the original. Merge that transient row
    # only at the final rename, with a matching stable identity AND nonempty
    # share-only memberships contained in the original (also filtered edits).
    # Ordinary duplicate emails still fail.
    if {'uuid', 'sub_id'} <= set(client_columns):
        assignments = ','.join('"'+c+'"=(SELECT "'+c+'" FROM clients WHERE email=NEW.email)'
                               for c in client_columns if c not in ('id', 'email'))
        link_names = ','.join('"'+c+'"' for c in link_columns)
        link_values = ','.join('OLD.id' if c == 'client_id' else '"'+c+'"' for c in link_columns)
        sql += f'''CREATE TRIGGER lucx_shareonly_clients_rename_merge
          BEFORE UPDATE OF email ON clients
          WHEN NEW.email != OLD.email AND EXISTS (
            SELECT 1 FROM clients d WHERE d.email=NEW.email AND d.id!=OLD.id
              AND ((d.sub_id!='' AND d.sub_id=OLD.sub_id) OR (d.uuid!='' AND d.uuid=OLD.uuid))
              AND EXISTS (SELECT 1 FROM client_inbounds WHERE client_id=OLD.id)
              AND EXISTS (SELECT 1 FROM client_inbounds WHERE client_id=d.id)
              AND NOT EXISTS (
                SELECT 1 FROM client_inbounds ci JOIN inbounds i ON i.id=ci.inbound_id
                WHERE ci.client_id=OLD.id AND i.protocol NOT IN {PROTOCOLS})
              AND EXISTS (SELECT 1 FROM client_inbounds a JOIN client_inbounds b
                          ON a.inbound_id=b.inbound_id WHERE a.client_id=OLD.id AND b.client_id=d.id)
              AND NOT EXISTS (SELECT ci.inbound_id FROM client_inbounds ci JOIN inbounds i ON i.id=ci.inbound_id
                              WHERE ci.client_id=d.id AND i.protocol IN {PROTOCOLS}
                              EXCEPT SELECT inbound_id FROM client_inbounds WHERE client_id=OLD.id))
          BEGIN
            UPDATE clients SET {assignments} WHERE id=OLD.id;
            INSERT OR IGNORE INTO client_inbounds ({link_names})
              SELECT {link_values} FROM client_inbounds WHERE client_id=(SELECT id FROM clients WHERE email=NEW.email);
            DELETE FROM client_inbounds WHERE client_id=(SELECT id FROM clients WHERE email=NEW.email);
            DELETE FROM clients WHERE email=NEW.email AND id!=OLD.id;
          END;\n'''
    for name, event, predicate in (
        ('ins', 'AFTER INSERT ON client_inbounds', 'id=NEW.inbound_id'),
        ('del', 'AFTER DELETE ON client_inbounds', 'id=OLD.inbound_id'),
        ('link_update', 'AFTER UPDATE OF client_id,inbound_id ON client_inbounds',
         'id IN (OLD.inbound_id,NEW.inbound_id)'),
        ('client_update', 'AFTER UPDATE ON clients',
         'id IN (SELECT inbound_id FROM client_inbounds WHERE client_id=NEW.id)'),
    ):
        sql += (f'CREATE TRIGGER lucx_shareonly_clients_{name} {event} BEGIN\n'
                + rebuild + ' AND ' + predicate + ';\nEND;\n')
    # A sidecar settings save can replace our cache with stale form clients.
    # Reconcile from normalized memberships only when the cache differs; this
    # predicate also terminates recursion when recursive_triggers is enabled.
    normalized = """COALESCE((SELECT json_group_array(%s)
      FROM clients c JOIN client_inbounds ci ON ci.client_id=c.id
      WHERE ci.inbound_id=NEW.id AND c.email!=''),'[]')""" % client_json
    sql += (f'CREATE TRIGGER lucx_shareonly_clients_inbound_update AFTER UPDATE OF settings ON inbounds '
            f'WHEN NEW.protocol IN {PROTOCOLS} AND json_valid(NEW.settings) '
            f"AND COALESCE(json_extract(NEW.settings,'$.clients'),'') != {normalized} BEGIN\n"
            + rebuild + ' AND id=NEW.id;\nEND;\n')
    # BEFORE preserves the email until join-delete triggers finish; unrelated
    # inbounds/clients are never removed. Handles direct SQL component removal too.
    sql += '''CREATE TRIGGER lucx_shareonly_clients_client_delete BEFORE DELETE ON clients
      BEGIN DELETE FROM client_inbounds WHERE client_id=OLD.id; END;
      CREATE TRIGGER lucx_shareonly_clients_inbound_delete BEFORE DELETE ON inbounds
      BEGIN DELETE FROM client_inbounds WHERE inbound_id=OLD.id; END;
      COMMIT;'''
    return sql


def sync_clients(db):
    required = {'clients': {'id', 'email', 'enable'},
                'client_inbounds': {'client_id', 'inbound_id'},
                'inbounds': {'id', 'protocol', 'settings'}}
    for table, columns in required.items():
        existing = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        if not columns <= existing:
            raise RuntimeError(f'Unsupported panel schema: {table}; no migration performed')
    db.executescript(client_sync_sql([row[1] for row in db.execute('PRAGMA table_info(clients)')],
                                    [row[1] for row in db.execute('PRAGMA table_info(client_inbounds)')]))


def detach_pro_triggers(db):
    # Never touch triggers belonging to the panel or another application.
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        if re.fullmatch(r'lucx_shareonly_clients_(ins|del|link_update|client_update|client_delete|inbound_delete|rename_merge|inbound_update)', name):
            db.execute('DROP TRIGGER "' + name + '"')
    db.commit()


def probe_clients(db):
    """Exercise Pro triggers against an isolated copy of the real migrated DB."""
    with closing(sqlite3.connect(':memory:')) as copy:
        db.backup(copy)
        sync_clients(copy)
        shares = copy.execute("SELECT id FROM inbounds WHERE protocol IN " + PROTOCOLS).fetchall()
        for (inbound,) in shares:
            members = copy.execute('SELECT c.id,c.email,c.enable FROM clients c JOIN client_inbounds ci '
                                   'ON ci.client_id=c.id WHERE ci.inbound_id=?', (inbound,)).fetchall()
            def assert_sync():
                raw = copy.execute('SELECT settings FROM inbounds WHERE id=?', (inbound,)).fetchone()[0]
                actual = sorted((c['email'], c['enable']) for c in json.loads(raw)['clients'])
                expected = sorted((email, bool(enabled)) for _, email, enabled in members if email)
                if actual != expected:
                    raise RuntimeError('Client synchronization probe failed')
            assert_sync()
            # Bound the mutation probes for large installations; the complete
            # membership list above is still compared for every shared inbound.
            for client, email, enabled in members[:3]:
                copy.execute('SAVEPOINT probe')
                replacement = '__lucx_compat_probe_' + str(client)
                while copy.execute('SELECT 1 FROM clients WHERE email=?', (replacement,)).fetchone():
                    replacement += '_'
                copy.execute('UPDATE clients SET email=? WHERE id=?', (replacement, client))
                raw = json.loads(copy.execute('SELECT settings FROM inbounds WHERE id=?', (inbound,)).fetchone()[0])
                if not any(c['email'] == replacement for c in raw['clients']):
                    raise RuntimeError('Client rename probe failed')
                copy.execute('ROLLBACK TO probe'); copy.execute('RELEASE probe')
                copy.execute('SAVEPOINT probe')
                copy.execute('UPDATE clients SET enable=? WHERE id=?', (not enabled, client))
                raw = json.loads(copy.execute('SELECT settings FROM inbounds WHERE id=?', (inbound,)).fetchone()[0])
                if email and next(c['enable'] for c in raw['clients'] if c['email'] == email) != bool(not enabled):
                    raise RuntimeError('Client enable probe failed')
                copy.execute('DELETE FROM client_inbounds WHERE client_id=? AND inbound_id=?', (client, inbound))
                raw = json.loads(copy.execute('SELECT settings FROM inbounds WHERE id=?', (inbound,)).fetchone()[0])
                if any(c['email'] == email for c in raw['clients']):
                    raise RuntimeError('Client detach probe failed')
                copy.execute('ROLLBACK TO probe'); copy.execute('RELEASE probe')
                copy.execute('SAVEPOINT probe')
                link = copy.execute('SELECT * FROM client_inbounds WHERE client_id=? AND inbound_id=?', (client, inbound)).fetchone()
                copy.execute('DELETE FROM client_inbounds WHERE client_id=? AND inbound_id=?', (client, inbound))
                placeholders = ','.join('?' for _ in link)
                copy.execute('INSERT INTO client_inbounds VALUES(' + placeholders + ')', link)
                raw = json.loads(copy.execute('SELECT settings FROM inbounds WHERE id=?', (inbound,)).fetchone()[0])
                if email and not any(c['email'] == email for c in raw['clients']):
                    raise RuntimeError('Client attach probe failed')
                copy.execute('ROLLBACK TO probe'); copy.execute('RELEASE probe')
                copy.execute('SAVEPOINT probe')
                copy.execute('DELETE FROM clients WHERE id=?', (client,))
                if copy.execute('SELECT 1 FROM client_inbounds WHERE client_id=?', (client,)).fetchone():
                    raise RuntimeError('Client delete probe failed')
                copy.execute('ROLLBACK TO probe'); copy.execute('RELEASE probe')
            copy.execute('SAVEPOINT probe')
            copy.execute('DELETE FROM inbounds WHERE id=?', (inbound,))
            if copy.execute('SELECT 1 FROM client_inbounds WHERE inbound_id=?', (inbound,)).fetchone():
                raise RuntimeError('Inbound delete probe failed')
            copy.execute('ROLLBACK TO probe'); copy.execute('RELEASE probe')
        if copy.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Compatibility probe damaged its database copy')
    print('Client links: compatibility probes passed on a database copy.')


def meaningful_json(value):
    if isinstance(value, dict):
        return {key: meaningful_json(item) for key, item in value.items() if key != 'updated_at'}
    if isinstance(value, list):
        return [meaningful_json(item) for item in value]
    return value


def protected_state(root, update_only=False):
    managed_ids = set()
    manifest = root / 'var/lib/lucx-ui-preinstall/architecture.json'
    if update_only and manifest.is_file():
        managed_ids = {obj['record']['id'] for obj in json.loads(manifest.read_text()).get('objects', {}).values()
                       if obj.get('present', True) and obj.get('record')}
    with closing(sqlite3.connect((root / 'etc/x-ui/x-ui.db').as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Panel database integrity check failed')
        db.row_factory = sqlite3.Row
        tables = {}
        for table, fields in (
            ('clients', ('id','email','uuid','sub_id','enable','total','total_gb','expiry_time','limit_ip',
                         'password','auth','flow','security','reverse','wg_private_key','wg_public_key',
                         'wg_allowed_ips','wg_pre_shared_key','wg_keep_alive','wg_forwarded_ports',
                         'secret','ad_tag','limit_hwid','tg_id','group_name','comment','reset','reset_day',
                         'reset_weekday','reset_max','traffic_reset','traffic_reset_day')),
            ('inbounds', ('id','protocol','port','listen','enable','tag','settings','stream_settings','sniffing','allocate')),
            ('users', ('id','username','password')),
            ('client_inbounds', ('client_id','inbound_id','flow_override')),
        ):
            if update_only and table == 'users':
                continue
            columns = {r[1] for r in db.execute('PRAGMA table_info(' + table + ')')}
            required = {'clients': {'id','email','enable'}, 'inbounds': {'id','protocol','settings'},
                        'users': {'id','username','password'}, 'client_inbounds': {'client_id','inbound_id'}}[table]
            if not required <= columns:
                raise RuntimeError('Unsupported migrated schema: ' + table)
            selected = [f for f in fields if f in columns]
            query = 'SELECT ' + ','.join('"'+f+'"' for f in selected) + ' FROM ' + table
            if table == 'client_inbounds':
                query += ' WHERE client_id IN (SELECT id FROM clients) AND inbound_id IN (SELECT id FROM inbounds)'
            rows = [dict(row) for row in db.execute(query)]
            types = {r[1]: r[2].upper() for r in db.execute('PRAGMA table_info(' + table + ')')}
            for row in rows:
                for key, value in list(row.items()):
                    if types.get(key) == 'TEXT' and value is None:
                        row[key] = ''
                    if key == 'wg_keep_alive':
                        row[key] = str(row[key])
            if table == 'inbounds':
                if update_only:
                    rows = [row for row in rows if row['id'] not in managed_ids
                            and row['protocol'] in ('qwdtt','csqtt','tproxy','olcrtc','openflux')]
                for row in rows:
                    settings = json.loads(row['settings'])
                    if not isinstance(settings, dict):
                        raise RuntimeError('Unknown inbound settings layout')
                    if row['protocol'] in ('qwdtt','csqtt','tproxy','olcrtc','openflux'):
                        settings.pop('clients', None)  # Derived from normalized memberships.
                    row['settings'] = meaningful_json(settings)
                    for key in ('stream_settings','sniffing','allocate'):
                        if row.get(key):
                            row[key] = meaningful_json(json.loads(row[key]))
            tables[table] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
        settings = dict(db.execute('SELECT key,value FROM settings ORDER BY id'))
        keys = ('webPort','webListen','webDomain','webBasePath','webCertFile','webKeyFile',
                'subEnable','subPort','subPath','subClashPath','subDomain','subCertFile','subKeyFile',
                'xrayTemplateConfig','timeLocation','twoFactorEnabled','twoFactorSecret')
        saved_settings = {k: settings[k] for k in keys if k in settings}
    files = {}
    for folder in ('var/www',):
        for file in (root / folder).rglob('*'):
            if file.is_file() and file.name != 'clash.yaml.tpl':
                files[file.relative_to(root).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
    for relative in ('opt/AdGuardHome/AdGuardHome.yaml', 'etc/default/ufw'):
        file = root / relative
        if file.is_file():
            files[relative] = hashlib.sha256(file.read_bytes()).hexdigest()
    for key in ('webCertFile','webKeyFile','subCertFile','subKeyFile'):
        if settings.get(key):
            file = root / settings[key].lstrip('/')
            if not file.is_file():
                raise RuntimeError('Configured certificate/key is missing')
            files[file.relative_to(root).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
    if update_only:
        # Protect only identities and memberships used by Pro client triggers.
        # Native protocol settings and client fields may migrate upstream.
        tables['clients'] = [{'id': r['id'], 'email': r['email']} for r in tables['clients']]
        tables['inbounds'] = [{'id': r['id'], 'protocol': r['protocol']} for r in tables['inbounds']]
        share_ids = {r['id'] for r in tables['inbounds']}
        tables['client_inbounds'] = [{'client_id': r['client_id'], 'inbound_id': r['inbound_id']}
                                    for r in tables['client_inbounds'] if r['inbound_id'] in share_ids]
        tables.pop('users', None)
        saved_settings = {k:v for k,v in saved_settings.items() if k.startswith(('web','sub'))}
        for rows in tables.values():
            rows.sort(key=lambda row: json.dumps(row, sort_keys=True))
    return {'tables': tables, 'settings': saved_settings, 'files': files, 'update_only': update_only}


def compare_existing_columns(previous, current):
    # New panel releases add columns with defaults. Protect every pre-existing
    # value and row; new schema fields have no prior value to compare against.
    for table, rows in previous['tables'].items():
        if not rows:
            continue
        fields = set().union(*(row.keys() for row in rows))
        current['tables'][table] = sorted(
            ({key: value for key, value in row.items() if key in fields}
             for row in current['tables'].get(table, [])),
            key=lambda row: json.dumps(row, sort_keys=True))


def preserve_client_flow(root, state):
    """Restore only global flow clobbered by native SyncInbound; reject other loss."""
    previous = json.loads(state.read_text(encoding='utf-8'))
    current = protected_state(root)
    compare_existing_columns(previous, current)
    old = {row['id']: row for row in previous['tables']['clients']}
    new = {row['id']: row for row in current['tables']['clients']}
    if old.keys() != new.keys():
        raise RuntimeError('Migration changed client IDs; flow repair refused')
    changes = []
    for client_id, row in old.items():
        fields = [key for key, value in row.items() if new[client_id].get(key) != value]
        if fields:
            if fields != ['flow'] or row['flow'] not in ('', 'xtls-rprx-vision', 'xtls-rprx-vision-udp443'):
                raise RuntimeError('Migration changed protected client fields: ' + ','.join(fields))
            changes.append((row['flow'], client_id))
            new[client_id]['flow'] = row['flow']
    current['tables']['clients'] = sorted(new.values(), key=lambda row: json.dumps(row, sort_keys=True))
    for section in ('tables', 'settings', 'files'):
        for name, value in previous[section].items():
            if current[section].get(name) != value:
                raise RuntimeError('Migration changed protected ' + section + ': ' + name + '; flow repair refused')
    if changes:
        with closing(sqlite3.connect(root / 'etc/x-ui/x-ui.db', timeout=30)) as db:
            with db:
                db.executemany('UPDATE clients SET flow=? WHERE id=?', changes)
        print('Native migration: preserved global flow for ' + str(len(changes)) + ' client(s).')


def verify_state(root, state, before_repair=False):
    previous = json.loads(state.read_text(encoding='utf-8'))
    current = protected_state(root, previous.get('update_only', False))
    compare_existing_columns(previous, current)
    for section in ('tables','settings','files'):
        for name, value in previous[section].items():
            if current[section].get(name) != value:
                # Never log credential values or entire client records.
                detail = ''
                if section == 'tables' and name in ('inbounds', 'clients', 'users'):
                    old = {r['id']: r for r in value}
                    new = {r['id']: r for r in current[section].get(name, [])}
                    changes = []
                    for row_id, row in old.items():
                        fields = [k for k, v in row.items() if new.get(row_id, {}).get(k) != v]
                        if fields: changes.append(str(row_id) + ':' + ','.join(fields))
                    added = sorted(new.keys() - old.keys())
                    removed = sorted(old.keys() - new.keys())
                    if added: changes.append('added IDs:' + ','.join(map(str, added)))
                    if removed: changes.append('removed IDs:' + ','.join(map(str, removed)))
                    detail = ' (' + '; '.join(changes) + ')'
                raise RuntimeError('Update changed protected ' + section + ': ' + name + detail)
    with closing(sqlite3.connect((root / 'etc/x-ui/x-ui.db').as_uri() + '?mode=ro', uri=True)) as db:
        dangling = db.execute('SELECT COUNT(*) FROM client_inbounds WHERE client_id NOT IN '
                              '(SELECT id FROM clients) OR inbound_id NOT IN (SELECT id FROM inbounds)').fetchone()[0]
        if previous.get('update_only'):
            dangling = db.execute('SELECT COUNT(*) FROM client_inbounds ci JOIN inbounds i ON i.id=ci.inbound_id '
                                  'WHERE i.protocol IN ' + PROTOCOLS + ' AND ci.client_id NOT IN (SELECT id FROM clients)').fetchone()[0]
        if dangling and not before_repair:
            raise RuntimeError('Update left orphaned client memberships')
    print('Protected clients, memberships, accounts, settings and files: preserved.')


def restore_sidecars(root, source, destination):
    """Fill absent files only for Pro-supported, configured sidecar protocols."""
    import shutil
    with closing(sqlite3.connect(root / 'etc/x-ui/x-ui.db')) as db:
        protocols = {r[0] for r in db.execute('SELECT DISTINCT protocol FROM inbounds')}
    prefixes = protocols & {'qwdtt', 'csqtt', 'tproxy', 'olcrtc', 'openflux'}
    if 'tproxy' in prefixes:
        prefixes.add('mtproxy')
    def missing(src, dst):
        if src.is_symlink() or dst.is_symlink():
            return
        if src.is_dir():
            if dst.exists() and not dst.is_dir():
                return
            dst.mkdir(parents=True, exist_ok=True)
            for child in src.iterdir():
                missing(child, dst / child.name)
        elif src.is_file() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    bins = source / 'bin'
    if bins.is_symlink() or not bins.is_dir() or (destination / 'bin').is_symlink():
        return
    for file in bins.iterdir():
        if file.is_file() and any(re.fullmatch(re.escape(core) + r'-linux-(?:amd64|arm64|arm|arm32|armv[567]|386)', file.name) for core in prefixes):
            missing(file, destination / 'bin' / file.name)
    tunnel = bins / 'tunnel'
    if tunnel.is_dir() and not tunnel.is_symlink() and not (destination / 'bin/tunnel').is_symlink():
        for file in tunnel.iterdir():
            if any(file.name.startswith(core + '-') or file.name.startswith(core + '.') for core in prefixes):
                missing(file, destination / 'bin/tunnel' / file.name)


def adapt_updater(path):
    """Keep the author's updater; suppress its setup wizard; retain native AWG."""
    text = path.read_text(encoding='utf-8')
    config = '    config_after_update\n'
    awg = '        bash "${awg_installer}" ||'
    if text.count(config) != 1 or text.count(awg) != 1 or 'XUI_UPDATE_TAG' not in text:
        raise RuntimeError('Native updater contract changed; panel has not been modified')
    text = text.replace(config, '    "${xui_folder}/x-ui" migrate || exit 1\n', 1)
    # Startup also migrates the schema. Keep it stopped until the explicit
    # migration/protection checks complete to avoid concurrent ALTER TABLE.
    for command in ('systemctl start x-ui', 'rc-service x-ui start'):
        line = '        ' + command + ' > /dev/null 2>&1\n'
        if text.count(line) > 1:
            raise RuntimeError('Native updater start contract changed')
        text = text.replace(line, '        : # Pro starts panel after migration checks\n')

    # Respect installations whose initial selector skipped AWG. When enabled,
    # execute the author's original call with only the BBR assignments removed.
    pattern = r'(?m)^        bash "\$\{awg_installer\}" \|\|[^\n]*\n'
    def native_awg(match):
        return ('        if [[ "${LUCX_PRO_AWG_ENABLED:-1}" == 1 ]]; then\n'
                '            /usr/local/sbin/lucx-awg-sysctl-guard || exit 1\n'
                + match.group(0).replace(' ||', ' || {', 1).rstrip('\n') + '; exit 1; }\n'
                + '        fi\n')
    text, count = re.subn(pattern, native_awg, text)
    if count != 1:
        raise RuntimeError('Native AWG update contract changed')
    path.write_text(text, encoding='utf-8', newline='\n')


def provider_route(settings):
    prefix = '/' + settings.get('subClashPath', '/mihomo/').strip('/') + '/'
    if not re.fullmatch(r'/[A-Za-z0-9_/-]+/', prefix) or '//' in prefix:
        raise RuntimeError('Unsupported native Clash path')
    port = int(settings.get('subPort', 2096))
    if not 1 <= port <= 65535:
        raise RuntimeError('Invalid subscription port')
    scheme = 'https' if settings.get('subCertFile') and settings.get('subKeyFile') else 'http'
    return f'''    # LUCX PRO native provider BEGIN
    location ~ ^/__lucx_provider/(?<lucx_provider_id>[^/]+)/?$ {{
        if ($hack = 1) {{ return 404; }}
        rewrite ^ {prefix}$lucx_provider_id break;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_redirect off;
        proxy_pass {scheme}://127.0.0.1:{port};
    }}
    # LUCX PRO native provider END
'''


def repair_nginx(root, settings):
    snippet = root / 'etc/nginx/snippets/includes.conf'
    template = root / 'var/www/subpage/clash.yaml.tpl'
    if template.is_file():
        text = template.read_text(encoding='utf-8')
        original_template = text
        text = repair_clash_template(text)
        text, count = re.subn(
            r'(?m)^(    url: https://[^/\s]+)/[^/\s]+/\$\{SUB_ID\}\?provider=1$',
            r'\1/__lucx_provider/${SUB_ID}', text)
        if not count and '/__lucx_provider/${SUB_ID}' not in text:
            raise RuntimeError('Unknown Clash provider URL; refusing to guess')
        if not snippet.is_file():
            raise RuntimeError('Clash nginx snippet is missing')
        route = provider_route(settings)
        original = snippet.read_text(encoding='utf-8')
        marker = r'    # LUCX PRO native provider BEGIN\n.*?    # LUCX PRO native provider END\n'
        existing = re.search(marker, original, re.S)
        if existing:
            block = existing[0]
            # Patch only the managed destination/path, retaining extra headers,
            # timeouts and unknown user directives inside our provider location.
            for directive in ('rewrite', 'proxy_pass'):
                target = re.search(r'(?m)^        ' + directive + r' [^\n]+$', route)[0]
                block, count = re.subn(r'(?m)^        ' + directive + r' [^\n]+$', lambda _: target, block)
                if not count:
                    closing = block.rfind('\n    }')
                    if closing == -1: raise RuntimeError('Managed provider location is incomplete')
                    block = block[:closing] + '\n' + target + block[closing:]
            repaired = original[:existing.start()] + block + original[existing.end():]
        else:
            repaired = route + original
        if repaired != original:
            snippet.write_text(repaired, encoding='utf-8')
        if text != original_template:
            template.write_text(text, encoding='utf-8')
    # Match only the saved panel port; Xray inbound HTTP proxies remain HTTP.
    panel_port = int(settings.get('webPort', 54321))
    scheme = 'https' if settings.get('webCertFile') and settings.get('webKeyFile') else 'http'
    domain_file = root / 'var/lib/lucx-ui-preinstall/domains'
    managed_domains = domain_file.read_text().splitlines()[:2] if domain_file.is_file() else []
    identity = root / 'var/lib/lucx-ui-preinstall/architecture.json'
    if identity.is_file():
        names = json.loads(identity.read_text(encoding='utf-8')).get('nginx_files', {}).values()
        managed_files = [root / name for name in names
                         if name.startswith('etc/nginx/sites-available/') and '..' not in Path(name).parts]
    else:
        managed_files = [root / 'etc/nginx/sites-available' / domain for domain in managed_domains]
    for path in managed_files:
        if not path.is_file():
            continue
        candidate = path.read_text(encoding='utf-8')
        if not any(domain in [name for value in re.findall(r'\bserver_name\s+([^;]+);', candidate)
                              for name in value.split()]
                   and f'/etc/letsencrypt/live/{domain}/fullchain.pem' in candidate
                   and 'include /etc/nginx/snippets/includes.conf;' in candidate
                   for domain in managed_domains):
            continue
        text = path.read_text(encoding='utf-8')
        repaired = re.sub(r'proxy_pass https?://127\.0\.0\.1:' + str(panel_port) + r'(?=[/;])',
                          f'proxy_pass {scheme}://127.0.0.1:{panel_port}', text)
        if repaired != text:
            path.write_text(repaired, encoding='utf-8')


def repair_clash_renderer(root):
    # This dedicated service/file is owned by Pro. Keep its port, unit and
    # template unchanged; only replace the implementation that lacked HEAD.
    renderer = root / 'usr/local/lib/lucx-ui-pro/clash-sub-server.py'
    unit = root / 'etc/systemd/system/lucx-clash-sub.service'
    if not unit.is_file() or not (root / 'var/www/subpage/clash.yaml.tpl').is_file():
        return
    if '/usr/local/lib/lucx-ui-pro/clash-sub-server.py' not in unit.read_text(encoding='utf-8'):
        return
    source = CLASH_RENDERER_SOURCE.lstrip('\n')
    if not renderer.is_file() or renderer.read_text(encoding='utf-8') != source:
        renderer.parent.mkdir(parents=True, exist_ok=True)
        renderer.write_text(source, encoding='utf-8', newline='\n')
        renderer.chmod(0o755)


def repair_clash_template(text):
    # Only our named HTTP provider needs these overrides. Never change native
    # panel inbounds/hosts or fingerprints for TLS/XHTTP or other providers.
    match = re.search(r'(?m)^proxy-providers:\n  sub:\n((?:    [^\n]*\n|[ \t]*\n)*)', text)
    if not match or not re.search(r'(?m)^    type: http\s*$', match[1]):
        return text
    body = match[1]
    if not re.search(r'(?m)^    proxy:', body):
        body = body.replace('    type: http\n', '    type: http\n    proxy: DIRECT\n', 1)
    if '    path: ./proxy_providers/base64.yml\n' in body:
        url = re.search(r'(?m)^    url: https://([^/\s]+)/([^/\s]+)/\$\{SUB_ID\}', body)
        if url:
            body = body.replace('    path: ./proxy_providers/base64.yml\n',
                                f'    path: ./proxy_providers/{url[1]}_{url[2]}_${{SUB_ID}}.yaml\n')
    if '    override:\n' not in body:
        # An unfamiliar inline override belongs to the user; do not replace it.
        if re.search(r'(?m)^    override:', body):
            return text[:match.start(1)] + body + text[match.end(1):]
        body += '    override:\n'
    if '      override-expr:\n' not in body:
        if re.search(r'(?m)^      override-expr:', body):
            return text[:match.start(1)] + body + text[match.end(1):]
        body = body.replace('    override:\n', '    override:\n      override-expr:\n', 1)
    # Mihomo 1.19.32's Firefox preset cannot negotiate new Xray REALITY.
    # Limit Chrome to REALITY; ordinary TLS/XHTTP retains its native Firefox.
    expressions = (
        '(select(.type == "vless" and .["reality-opts"] != null) | .["client-fingerprint"]) = "chrome"',
        '(select(.type == "vless" and .["reality-opts"] != null) | .["reality-opts"]["support-x25519mlkem768"]) = true',
    )
    for expression in reversed(expressions):
        if expression not in body:
            body = body.replace('      override-expr:\n',
                                "      override-expr:\n        - '" + expression + "'\n", 1)
    return text[:match.start(1)] + body + text[match.end(1):]


def remove_forwarding_override(root):
    path = root / 'etc/sysctl.d/99-lucx-ui-forwarding.conf'
    if not path.is_file():
        return
    text = path.read_text(encoding='utf-8')
    text = re.sub(r'(?m)^\s*net\.ipv4\.ip_forward\s*=\s*1\s*(?:#.*)?\n?', '', text)
    if any(line.strip() and not line.lstrip().startswith('#') for line in text.splitlines()):
        path.write_text(text, encoding='utf-8')
    else:
        path.unlink()
    # Do not write ip_forward=0: active panel tunnels own runtime forwarding.


def repair_rkn_timers(root):
    for name, delay in (('list', '15min'), ('self', '30min')):
        path = root / f'etc/systemd/system/rkn-guard-{name}-update.timer'
        if path.is_file():
            text = path.read_text(encoding='utf-8')
            patched = re.sub(r'(?m)^OnBootSec=' + delay + r'$', 'OnActiveSec=' + delay, text)
            if patched != text:
                path.write_text(patched, encoding='utf-8')

def migrate(root, clients_only=False):
    with closing(sqlite3.connect(root / 'etc/x-ui/x-ui.db', timeout=30)) as db, db:
        sync_clients(db)
        if clients_only:
            return
        settings = dict(db.execute('SELECT key,value FROM settings ORDER BY id'))
        repair_nginx(root, settings)
        repair_clash_renderer(root)
        db.commit()
    remove_forwarding_override(root)
    repair_rkn_timers(root)
    state = root / 'var/lib/lucx-ui-preinstall'
    if (state / 'owned-by-lucx-ui-pro').is_file():
        (state / 'compat-revision').write_text(REVISION + '\n', encoding='utf-8')


def inspect(root):
    path = root / 'etc/x-ui/x-ui.db'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        print('Инбаунды:', ', '.join(f'{p}={n}' for p, n in db.execute(
            'SELECT protocol,COUNT(*) FROM inbounds GROUP BY protocol')))
        for row_id, protocol, raw in db.execute(
                "SELECT id,protocol,settings FROM inbounds WHERE protocol IN ('csqtt','qwdtt','tproxy','amneziawg')"):
            data = json.loads(raw)
            config = data.get('server', data) if protocol == 'amneziawg' else data
            default = protocol == 'qwdtt'
            route = config.get('routeThroughXray', default)
            change = ' (сохраняется)'
            print(f'{protocol} #{row_id}: routeThroughXray={route}{change}')
        triggers = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'lucx_shareonly_%'")]
        print('Синхронизация клиентов:', ', '.join(triggers) or 'отсутствует')
        dangling = db.execute('SELECT COUNT(*) FROM client_inbounds WHERE client_id NOT IN '
                              '(SELECT id FROM clients) OR inbound_id NOT IN (SELECT id FROM inbounds)').fetchone()[0]
        print('Осиротевшие связи:', dangling)
    for name, relative in (
        ('AWG BBR guard', 'usr/local/sbin/lucx-awg-sysctl-guard'),
        ('Clash renderer', 'etc/systemd/system/lucx-clash-sub.service'),
        ('AdGuard', 'opt/AdGuardHome'), ('RKN guard', 'usr/local/bin/rkn-guard'),
        ('BBR панели', 'etc/sysctl.d/99-bbr-x-ui.conf'),
        ('Pro ip_forward override', 'etc/sysctl.d/99-lucx-ui-forwarding.conf'),
        ('Сайт заглушка', 'var/lib/lucx-ui-preinstall/cover-generator.json'),
    ):
        print(f'{name}: {"есть" if (root / relative).exists() else "нет"}')
    print('Правки: связи клиентов, native Clash provider, TLS панели, исключение BBR из AWG, таймеры RKN.')
    print('UFW allow routed сохраняется; правила CSQTT обслуживает панель.')
    print('Аккаунты, порты, DNS и содержимое сайта сохраняются.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('report', 'clients', 'apply', 'firewall',
                                         'restore-sidecars', 'capture', 'capture-update', 'preserve-flow', 'verify', 'probe', 'prepare-update', 'adapt-updater'))
    parser.add_argument('--root', type=Path, default=Path('/'))
    parser.add_argument('--state', type=Path)
    parser.add_argument('--path', type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--before-repair', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == 'restore-sidecars':
        restore_sidecars(root, args.source, root / 'usr/local/x-ui')
    elif args.action == 'adapt-updater':
        adapt_updater(args.path)
    elif args.action in ('capture', 'capture-update'):
        args.state.write_text(json.dumps(protected_state(root, args.action == 'capture-update'), ensure_ascii=False), encoding='utf-8')
        args.state.chmod(0o600)
    elif args.action == 'preserve-flow':
        preserve_client_flow(root, args.state)
    elif args.action == 'verify':
        verify_state(root, args.state, args.before_repair)
    elif args.action in ('probe', 'prepare-update'):
        with closing(sqlite3.connect(root / 'etc/x-ui/x-ui.db', timeout=30)) as db:
            if args.action == 'probe':
                probe_clients(db)
            else:
                detach_pro_triggers(db)
    elif args.action == 'firewall':
        remove_forwarding_override(root)
    elif args.action == 'report':
        inspect(root)
    else:
        migrate(root, args.action == 'clients')


if __name__ == '__main__':
    main()
