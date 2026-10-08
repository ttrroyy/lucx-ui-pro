#!/usr/bin/env python3
"""Reconcile only the XHTTP/REALITY fields and nginx blocks owned by Pro.

The root-only manifest retains identities/keys for recovery, never templates
existing user records. Unknown fields survive both DB and nginx patches.
"""
import argparse
from contextlib import closing
import copy
import fnmatch
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tarfile
import tempfile
import time

STATE = 'var/lib/lucx-ui-preinstall'
SOCKET = '/dev/shm/uds2023.sock'
TAGS = {'xhttp': 'inbound-/dev/shm/uds2023.sock,0666:0|', 'reality': 'inbound-8443'}
LEGACY_SOCKOPT = {
    'acceptProxyProtocol': False, 'tcpFastOpen': True, 'mark': 0,
    'tproxy': 'off', 'tcpMptcp': True, 'tcpNoDelay': True,
    'domainStrategy': 'UseIP', 'tcpMaxSeg': 1440, 'dialerProxy': '',
    'tcpKeepAliveInterval': 0, 'tcpKeepAliveIdle': 300,
    'tcpUserTimeout': 10000, 'tcpcongestion': 'bbr', 'V6Only': False,
    'tcpWindowClamp': 600, 'interface': '',
}


def atomic(path, text, private=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                     dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write(text)
    temporary.chmod(0o600 if private else (path.stat().st_mode & 0o777 if path.exists() else 0o644))
    temporary.replace(path)


def rows(db, table):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
    return [dict(row) for row in db.execute('SELECT * FROM "' + table + '"')]


def server_names(text):
    return [name for value in re.findall(r'\bserver_name\s+([^;]+);', text) for name in value.split()]


def layout(root):
    """Saved domains plus certificate/include signatures; never port alone."""
    domains = (root / STATE / 'domains').read_text().splitlines()
    if len(domains) < 2 or any(not re.fullmatch(r'[A-Za-z0-9.-]+', d) for d in domains[:2]):
        raise RuntimeError('Managed domains are missing or invalid')
    result = {}
    manifest_file = root / STATE / 'architecture.json'
    known_files = json.loads(manifest_file.read_text()).get('nginx_files', {}) if manifest_file.is_file() else {}
    for kind, domain in zip(('xhttp', 'reality'), domains):
        candidates = []
        for path in (root / 'etc/nginx/sites-available').iterdir():
            if not path.is_file():
                continue
            text = path.read_text(encoding='utf-8')
            if (domain in server_names(text)
                    and f'/etc/letsencrypt/live/{domain}/fullchain.pem' in text
                    and 'include /etc/nginx/snippets/includes.conf;' in text
                    and re.search(r'\blisten\s+(?:127\.0\.0\.1:|0\.0\.0\.0:|\[::\]:)?'
                                  + ('7443' if kind == 'xhttp' else '9443') + r'(?=\s|;)', text)):
                candidates.append(path)
        known = root / known_files[kind] if kind in known_files else root / 'etc/nginx/sites-available' / domain
        if known in candidates: candidates = [known]
        if len(candidates) != 1:
            raise RuntimeError(f'Ambiguous or missing managed {kind} nginx vhost')
        result[kind] = candidates[0]
    return result


def block_spans(text):
    """Brace scanner respects quoted regexes, escapes, comments and variables."""
    stack, blocks = [], []
    quote, escaped, comment, variable = None, False, False, False
    start = 0
    for index, char in enumerate(text):
        if comment:
            if char == '\n': comment = False
            continue
        if escaped:
            escaped = False
            continue
        if char == '\\':
            escaped = True
            continue
        if quote:
            if char == quote: quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        if char == '#':
            comment = True
            continue
        if char == '{' and index and text[index-1] == '$':
            variable = True
            continue
        if variable:
            if char == '}': variable = False
            continue
        if char == '{':
            stack.append((start, index, len(stack)))
            start = index + 1
        elif char == '}':
            if not stack: raise RuntimeError('Unbalanced nginx braces')
            a, b, depth = stack.pop()
            blocks.append((a, b, index, depth))
            start = index + 1
        elif char == ';':
            start = index + 1
    if stack or quote: raise RuntimeError('Unbalanced nginx configuration')
    return blocks


def patch_vhost(text, domain, port):
    matched = []
    for a, b, c, depth in block_spans(text):
        body = text[b+1:c]
        if (re.search(r'\bserver\s*$', text[a:b])
                and domain in server_names(body)
                and 'include /etc/nginx/snippets/includes.conf;' in body
                and f'/etc/letsencrypt/live/{domain}/fullchain.pem' in body
                and re.search(r'\blisten\s+(?:127\.0\.0\.1:|0\.0\.0\.0:|\[::\]:)?' + str(port) + r'(?=\s|;)', body)):
            matched.append((b, c))
    if len(matched) != 1: raise RuntimeError('Ambiguous managed nginx server block')
    b, c = matched[0]
    body = text[b+1:c]
    # Only server-level listen directives, not nested user locations.
    nested = [(a, z) for a, _, z, depth in block_spans(body) if depth == 0]
    pattern = r'(?m)^[ \t]*listen\s+(?P<addr>(?:127\.0\.0\.1:|0\.0\.0\.0:|\[::\]:)?' + str(port) + r')(?P<flags>\s+[^;\n]*)?;[^\n]*\n?'
    count = 0
    def listen(match):
        nonlocal count
        if any(a <= match.start() < z for a, z in nested): return match.group(0)
        if match.group('addr').startswith('[::]'): return ''
        count += 1
        flags = (match.group('flags') or '').split()
        if 'ssl' not in flags: flags.append('ssl')
        if port == 7443 and 'proxy_protocol' not in flags: flags.append('proxy_protocol')
        if port == 9443: flags = [flag for flag in flags if flag != 'proxy_protocol']
        return '    listen 127.0.0.1:' + str(port) + ' ' + ' '.join(flags) + ';\n'
    body = re.sub(pattern, listen, body)
    if count != 1: raise RuntimeError('Unsupported/duplicate internal nginx listener')
    return text[:b+1] + body + text[c:]


def patch_location(text, path):
    if not re.fullmatch(r'/[A-Za-z0-9_/-]+', path) or '//' in path:
        raise RuntimeError('Unsupported managed XHTTP path')
    candidates = []
    for a, b, c, depth in block_spans(text):
        header = re.sub(r'(?m)#.*$', '', text[a:b]).strip()
        if header.startswith('location ') and SOCKET in text[b+1:c]:
            if not re.fullmatch(r'location\s+(?:\^~\s+)?' + re.escape(path.rstrip('/')) + r'/?', header):
                raise RuntimeError('XHTTP socket is referenced by an unrecognized location')
            candidates.append((a, b, c))
    if len(candidates) != 1: raise RuntimeError('Missing/duplicate managed XHTTP nginx location')
    a, b, c = candidates[0]
    body = text[b+1:c].rstrip() + '\n'
    directives = {
        'client_max_body_size': '0', 'client_body_timeout': '1h',
        'grpc_read_timeout': '1h', 'grpc_send_timeout': '1h',
        'grpc_pass': 'unix:' + SOCKET,
        'grpc_set_header Connection': '""', 'grpc_set_header Host': '$host',
        'grpc_set_header X-Real-IP': '$remote_addr',
        'grpc_set_header X-Forwarded-For': '$remote_addr',
    }
    def replace_top(body, pattern, replacement):
        nested = [(b, c) for _, b, c, depth in block_spans(body) if depth == 0]
        count = 0
        def replace(match):
            nonlocal count
            if any(b < match.start() < c for b, c in nested): return match.group(0)
            count += 1
            return replacement(match) if callable(replacement) else replacement
        return re.sub(pattern, replace, body), count
    for directive, value in directives.items():
        pattern = r'(?m)^[ \t]*' + r'\s+'.join(map(re.escape, directive.split())) + r'\s+[^;\n]*;[^\n]*\n?'
        replacement = '        ' + directive + ' ' + value + ';\n'
        if directive in ('client_body_timeout', 'grpc_read_timeout', 'grpc_send_timeout'):
            # Existing explicit timeouts are valid user choices. Add the xPROMS
            # default only when absent; they do not define transport/ownership.
            replacement = lambda match: match.group(0)
        body, count = replace_top(body, pattern, replacement)
        if count > 1: raise RuntimeError('Duplicate managed nginx directive: ' + directive)
        if not count: body += '        ' + directive + ' ' + value + ';\n'
    # Retire only exact legacy template values. Preserve unrelated/user additions.
    for directive, value in (
            ('grpc_buffer_size', '16k'), ('grpc_socket_keepalive', 'on'),
            ('grpc_set_header X-Forwarded-Proto', '$scheme'),
            ('grpc_set_header X-Forwarded-Port', '$server_port'),
            ('grpc_set_header X-Forwarded-Host', '$host')):
        body, _ = replace_top(body, r'(?m)^[ \t]*' + r'\s+'.join(map(re.escape, directive.split())) + r'\s+' + re.escape(value) + r';[^\n]*\n?', '')
    header = text[a:b]
    # Keep preceding comments/whitespace, replace the location declaration only.
    header = re.sub(r'location\s+(?:\^~\s+)?\S+\s*$', 'location ^~ ' + path.rstrip('/') + '/ ', header)
    return text[:a] + header + '{' + body.rstrip() + '\n    ' + text[c:]


def proof(row, kind, domains, xhttp_path, saved_id=None, saved_record=None, host_link=False):
    try:
        stream = json.loads(row['stream_settings'])
        if row['protocol'] != 'vless': return False
        if row['id'] == saved_id and host_link:
            return True  # Stable host IDs survive names/transport option edits.
        if row['id'] == saved_id and row['tag'] == TAGS[kind]:
            return True  # Persisted ownership survives native transport option changes.
        if row['id'] == saved_id and saved_record:
            previous = json.loads(saved_record['stream_settings'])
            if kind == 'reality':
                key = previous.get('realitySettings', {}).get('privateKey')
                if key and key == stream.get('realitySettings', {}).get('privateKey'):
                    return True
            elif stream.get('network') == 'xhttp':
                old = previous.get('xhttpSettings', {})
                new = stream.get('xhttpSettings', {})
                if old.get('path') and old.get('path') == new.get('path'):
                    return True
        if kind == 'xhttp':
            config = stream.get('xhttpSettings', {})
            transport = (stream.get('network') == 'xhttp'
                         and (config.get('path', '').rstrip('/') == xhttp_path.rstrip('/')
                              or config.get('host') == domains[0]))
            return ((row['tag'] == TAGS[kind] or row['id'] == saved_id or transport)
                    and row['listen'].split(',')[0] == SOCKET)
        config = stream.get('realitySettings', {})
        transport = stream.get('security') == 'reality' and stream.get('network') == 'tcp'
        return ((row['tag'] == TAGS[kind] or row['id'] == saved_id or transport)
                and int(row['port']) == 8443 and bool(config)
                and (domains[1] in config.get('serverNames', [])
                     or config.get('target', config.get('dest')) == '127.0.0.1:9443'))
    except (KeyError, TypeError, ValueError):
        return False


def location_path(root):
    text = (root / 'etc/nginx/snippets/includes.conf').read_text(encoding='utf-8')
    paths = []
    for a, b, c, _ in block_spans(text):
        if SOCKET in text[b+1:c]:
            header = re.sub(r'(?m)#.*$', '', text[a:b]).strip()
            match = re.fullmatch(r'location\s+(?:\^~\s+)?(/[A-Za-z0-9_/-]+)', header)
            if match: paths.append(match[1].rstrip('/'))
    if len(paths) != 1: raise RuntimeError('Cannot identify managed XHTTP path')
    return paths[0]


def snapshot(db, row):
    return {'record': row, 'hosts': [r for r in rows(db, 'hosts') if r['inbound_id'] == row['id']],
            'memberships': [r for r in rows(db, 'client_inbounds') if r['inbound_id'] == row['id']],
            'traffic': [r for r in rows(db, 'client_traffics') if r.get('inbound_id') == row['id']],
            'clients': rows(db, 'clients')}


def archive_snapshot(root, kind, domains, path):
    """Recover keys/records only from checksum-verified Pro backups, newest first."""
    folder = root / 'var/backups/x-ui'
    for archive in sorted(folder.glob('lucx-ui-backup-*.tar.gz'), reverse=True):
        sums = Path(str(archive) + '.sha256')
        if not sums.is_file(): continue
        expected = sums.read_text().split()[0]
        digest = hashlib.sha256()
        with archive.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''): digest.update(chunk)
        if digest.hexdigest() != expected: continue
        with tarfile.open(archive, 'r:gz') as tar:
            found = [m for m in tar.getmembers() if m.name.lstrip('./') in
                     ('files/etc/x-ui/x-ui.db', 'etc/x-ui/x-ui.db') and m.isfile()]
            if len(found) != 1: continue
            with tempfile.TemporaryDirectory() as temp:
                dbfile = Path(temp) / 'db'
                dbfile.write_bytes(tar.extractfile(found[0]).read())
                with closing(sqlite3.connect(dbfile)) as db:
                    db.row_factory = sqlite3.Row
                    matches = [r for r in rows(db, 'inbounds') if proof(r, kind, domains, path)]
                    if len(matches) > 1: raise RuntimeError('Ambiguous managed inbound in backup')
                    if matches: return snapshot(db, matches[0])
    raise RuntimeError(f'Managed {kind} inbound missing; no verified recovery record. Restore a Pro backup first.')


def insert(db, table, record):
    columns = {r[1] for r in db.execute('PRAGMA table_info("' + table + '")')}
    record = {k: v for k, v in record.items() if k in columns}
    keys = list(record)
    db.execute('INSERT INTO "' + table + '" (' + ','.join('"' + k + '"' for k in keys) + ') VALUES (' + ','.join('?' for _ in keys) + ')', list(record.values()))


def restore(db, saved):
    row = copy.deepcopy(saved['record'])
    old_id = row['id']
    if db.execute('SELECT 1 FROM inbounds WHERE id=?', (old_id,)).fetchone():
        row['id'] = db.execute('SELECT COALESCE(MAX(id),0)+1 FROM inbounds').fetchone()[0]
    live = {r['id']: r for r in rows(db, 'clients')}
    valid = {r['id'] for r in saved.get('clients', []) if r['id'] in live
             and r.get('uuid') == live[r['id']].get('uuid') and r.get('email') == live[r['id']].get('email')}
    settings = json.loads(row['settings'])
    emails = {live[c]['email'] for c in valid}
    filtered = [c for c in settings.get('clients', []) if c.get('email') in emails]
    if filtered != settings.get('clients', []):
        settings['clients'] = filtered
        row['settings'] = json.dumps(settings, ensure_ascii=False)
    insert(db, 'inbounds', row)
    for table, entries in (('hosts', saved.get('hosts', [])), ('client_inbounds', saved.get('memberships', []))):
        for original in entries:
            if table == 'client_inbounds' and original['client_id'] not in valid: continue
            item = dict(original); item['inbound_id'] = row['id']; item.pop('id', None)
            insert(db, table, item)
    for original in saved.get('traffic', []):
        # Traffic is globally keyed by email in the panel. Never overwrite a
        # surviving record belonging to another inbound/client.
        if original.get('email') not in emails: continue
        if db.execute('SELECT 1 FROM client_traffics WHERE email=?', (original['email'],)).fetchone(): continue
        item = dict(original); item['inbound_id'] = row['id']
        if db.execute('SELECT 1 FROM client_traffics WHERE id=?', (item['id'],)).fetchone(): item.pop('id')
        insert(db, 'client_traffics', item)
    return row


def canonical(row, kind, xhttp_path=None, saved=None, domains=None, permitted_hosts=None):
    row = copy.deepcopy(row)
    stream = json.loads(row['stream_settings'])
    if kind == 'reality':
        row['listen'] = '127.0.0.1'
        row['port'] = 8443
        stream['network'] = 'tcp'
        stream['security'] = 'reality'
        config = stream.setdefault('realitySettings', {})
        previous = json.loads(saved['record']['stream_settings']).get('realitySettings', {}) if saved else {}
        for field in ('privateKey', 'shortIds'):
            if not config.get(field):
                if not previous.get(field):
                    raise RuntimeError('Managed REALITY ' + field + ' missing; no saved recovery value')
                config[field] = copy.deepcopy(previous[field])
        config['target'] = '127.0.0.1:9443'
        config['xver'] = 0  # Cover 9443 accepts TLS without a PROXY prefix.
        if 'dest' in config: config['dest'] = config['target']
        if domains and domains[1] not in config.get('serverNames', []):
            config.setdefault('serverNames', []).append(domains[1])
        tcp = stream.setdefault('tcpSettings', {})
        tcp['acceptProxyProtocol'] = True
        tcp.setdefault('header', {})['type'] = 'none'
        row['stream_settings'] = json.dumps(stream, ensure_ascii=False)
        return row
    row['listen'] = SOCKET + ',0666'
    row['port'] = 0
    stream['network'] = 'xhttp'
    stream['security'] = 'none'
    config = stream.setdefault('xhttpSettings', {})
    config['mode'] = 'stream-up'
    # Empty/absent host is a valid native choice. Keep host restrictions that
    # accept a managed nginx name; fix only an incompatible restriction (404).
    host = config.get('host')
    if domains and host and not any(fnmatch.fnmatchcase(name.lower(), host.lower())
                                   for name in (permitted_hosts or [domains[0]])):
        config['host'] = domains[0]
    if xhttp_path is not None and config.get('path', '').rstrip('/') != xhttp_path.rstrip('/'):
        config['path'] = xhttp_path
    sockopt = stream.setdefault('sockopt', {})
    for key, old in LEGACY_SOCKOPT.items():
        if sockopt.get(key) == old: sockopt.pop(key, None)
    # Xray expects header names here, not proxy IP addresses. The UDS is private.
    sockopt['trustedXForwardedFor'] = ['X-Forwarded-For']
    row['stream_settings'] = json.dumps(stream, ensure_ascii=False)
    return row


def protect_managed_identity(db, row, saved, kind):
    """Protect connection identities only, not native options/schema changes."""
    relevant = {r['client_id'] for r in saved.get('memberships', [])}
    previous = {r['id']: r for r in saved.get('clients', []) if r['id'] in relevant}
    current = {r['id']: r for r in rows(db, 'clients')}
    for identifier, client in previous.items():
        if identifier not in current or any(client.get(k) != current[identifier].get(k) for k in ('email', 'uuid')):
            raise RuntimeError('Managed connection client identity changed during panel update')
    if kind == 'reality':
        old = json.loads(saved['record']['stream_settings']).get('realitySettings', {})
        new = json.loads(row['stream_settings']).get('realitySettings', {})
        if old.get('privateKey') and old['privateKey'] != new.get('privateKey'):
            raise RuntimeError('Managed REALITY private key changed during panel update')
        if not set(old.get('shortIds', [])).issubset(new.get('shortIds', [])):
            raise RuntimeError('Managed REALITY shortIds lost during panel update')


def reconcile(root, remember=False, changes=None):
    if not (root / STATE / 'owned-by-lucx-ui-pro').is_file():
        raise RuntimeError('Installation is not owned by lucx-ui-pro')
    paths = layout(root)
    domains = (root / STATE / 'domains').read_text().splitlines()[:2]
    xhttp_path = location_path(root)
    permitted_hosts = [domains[0]]
    main_text = paths['xhttp'].read_text(encoding='utf-8')
    for _, b, c, _ in block_spans(main_text):
        body = main_text[b+1:c]
        if domains[0] in server_names(body) and re.search(r'\blisten\s+[^;]*\b7443\b', body):
            permitted_hosts.extend(server_names(body))
    manifest_path = root / STATE / 'architecture.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    if manifest and manifest.get('domains') != domains:
        raise RuntimeError('Managed domains differ from saved architecture identity')
    planned_files = {}
    listeners_changed = False
    for kind, port in (('xhttp', 7443), ('reality', 9443)):
        file = paths[kind]
        original = file.read_text(encoding='utf-8')
        planned_files[file] = patch_vhost(original, domains[0 if kind == 'xhttp' else 1], port)
        listen_lines = lambda text: re.findall(r'(?m)^\s*listen\s+[^;]*;', text)
        listeners_changed |= listen_lines(original) != listen_lines(planned_files[file])
    snippet = root / 'etc/nginx/snippets/includes.conf'
    planned_files[snippet] = patch_location(snippet.read_text(encoding='utf-8'), xhttp_path)
    with closing(sqlite3.connect(root / 'etc/x-ui/x-ui.db', timeout=30)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        all_rows = rows(db, 'inbounds')
        live_hosts = rows(db, 'hosts')
        new_manifest = {'format': 1, 'domains': domains, 'objects': {},
                        'nginx_files': {kind: path.relative_to(root).as_posix() for kind, path in paths.items()}}
        for kind in ('xhttp', 'reality'):
            saved = manifest.get('objects', {}).get(kind)
            def identify(saved):
                saved_id = saved['record']['id'] if saved else None
                host_ids = {h['id'] for h in saved.get('hosts', [])} if saved else set()
                host_link = any(h['id'] in host_ids and h['inbound_id'] == saved_id for h in live_hosts)
                return [r for r in all_rows if proof(r, kind, domains, xhttp_path, saved_id,
                                                     saved['record'] if saved else None, host_link)]
            candidates = identify(saved)
            if not candidates and not saved:
                # Legacy installs have no manifest. Verified backups retain the
                # stable IDs/host links even when the user changed the transport.
                saved = archive_snapshot(root, kind, domains, xhttp_path)
                candidates = identify(saved)
            # A reused ID is a user object, not an incompatibility: leave it alone
            # and recover the missing managed object using another free ID.
            if len(candidates) > 1: raise RuntimeError(f'Duplicate managed {kind} inbounds')
            if not candidates:
                # Reject partial identities rather than generate a duplicate next to a changed/user object.
                if kind == 'xhttp' and any(r.get('listen', '').split(',')[0] == SOCKET for r in all_rows):
                    raise RuntimeError('Required XHTTP Unix socket is occupied by another inbound')
                saved = saved or archive_snapshot(root, kind, domains, xhttp_path)
                if not proof(saved['record'], kind, domains, xhttp_path, saved['record']['id'],
                             saved['record'], bool(saved.get('hosts'))):
                    raise RuntimeError('Invalid saved managed identity')
                if remember:
                    new_manifest['objects'][kind] = saved
                    continue
                row = restore(db, saved)
                print(f'Restored managed {kind} inbound #{row["id"]}')
            else:
                row = candidates[0]
            patched = canonical(row, kind, xhttp_path, saved, domains, permitted_hosts)
            if not remember:
                if saved: protect_managed_identity(db, patched, saved, kind)
                for field in ('listen', 'port', 'stream_settings'):
                    if patched[field] != row[field]:
                        db.execute('UPDATE inbounds SET "' + field + '"=? WHERE id=?', (patched[field], row['id']))
                row = dict(db.execute('SELECT * FROM inbounds WHERE id=?', (row['id'],)).fetchone())
            # Preserve the last recoverable key/transport identity even if the
            # current form temporarily cleared required fields. DB stays untouched
            # during remember; reconciliation applies the owned-field patch later.
            new_manifest['objects'][kind] = snapshot(db, patched if remember else row)
        # File patches were fully parsed/validated before the first DB mutation.
        if not remember:
            for file, text in planned_files.items():
                if text != file.read_text(encoding='utf-8'): atomic(file, text)
        content = json.dumps(new_manifest, ensure_ascii=False, indent=2) + '\n'
        if not manifest_path.exists() or manifest_path.read_text() != content:
            atomic(manifest_path, content, True)
    print('Managed architecture: ' + ('identities saved.' if remember else 'XHTTP stream-up; Unix socket; internal listeners on loopback.'))
    if changes is not None:
        atomic(changes, json.dumps({'nginx_listeners_changed': listeners_changed}) + '\n', True)


def wait_listeners(ports, xhttp_enabled, timeout=20):
    """The panel process becomes active before its asynchronous Xray startup."""
    deadline = time.monotonic() + timeout
    while True:
        lines = subprocess.check_output(['ss', '-H', '-lnt'], text=True).splitlines()
        addresses = [line.split()[3] for line in lines]
        error = next((f'Managed loopback listener {port} is missing' for port in ports
                      if '127.0.0.1:' + str(port) not in addresses), None)
        # Other user listeners are outside our DB/vhost contract.
        if not error and xhttp_enabled and SOCKET not in subprocess.check_output(['ss', '-H', '-lx'], text=True).split():
            error = 'Managed XHTTP Unix socket is not listening'
        if not error and not {'0.0.0.0:443', '[::]:443'}.issubset(addresses):
            error = 'Public nginx SNI listeners are missing'
        if not error: return
        if time.monotonic() >= deadline: raise RuntimeError(error)
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('reconcile', 'remember', 'main-vhost', 'validate-live'))
    parser.add_argument('--root', type=Path, default=Path('/'))
    parser.add_argument('--changes', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == 'validate-live':
        if root != Path('/'): raise RuntimeError('Live validation requires the actual system root')
        # Check only our listeners; other inbound ports/routing are not a contract.
        manifest = json.loads((root / STATE / 'architecture.json').read_text())
        managed_id = manifest['objects']['reality']['record']['id']
        with closing(sqlite3.connect('/etc/x-ui/x-ui.db')) as db:
            enabled = db.execute('SELECT enable FROM inbounds WHERE id=?', (managed_id,)).fetchone()[0]
            xhttp_enabled = db.execute('SELECT enable FROM inbounds WHERE id=?',
                                      (manifest['objects']['xhttp']['record']['id'],)).fetchone()[0]
        wait_listeners((7443, 9443, *((8443,) if enabled else ())), xhttp_enabled)
        print('Managed listeners validated: public 443 IPv4/IPv6; internal loopback only.')
    elif args.action == 'main-vhost': print(layout(root)['xhttp'])
    else: reconcile(root, args.action == 'remember', args.changes)


if __name__ == '__main__':
    main()
