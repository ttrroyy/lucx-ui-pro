#!/usr/bin/env python3
"""Reconcile only the XHTTP/REALITY fields and nginx blocks owned by Pro.

The root-only manifest retains ownership and connection identities. Deleted
objects are not recreated. Unknown fields survive both DB and nginx patches.
"""
import argparse
import base64
from contextlib import closing
import copy
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
# Explicit fields read by the 286/287 panel/core. Do not infer ownership from
# unfamiliar keys or replace complete objects: future native fields survive.
SOCKOPT_FIELDS = set(LEGACY_SOCKOPT) | {
    'penetrate', 'addressPortStrategy', 'happyEyeballs', 'customSockopt',
    'trustedXForwardedFor',
}
XHTTP_FIELDS = {
    'path', 'host', 'mode', 'headers', 'xPaddingBytes', 'xPaddingObfsMode',
    'xPaddingKey', 'xPaddingHeader', 'xPaddingPlacement', 'xPaddingMethod',
    'sessionIDPlacement', 'sessionIDKey', 'sessionIDTable', 'sessionIDLength',
    'sessionPlacement', 'sessionKey', 'seqPlacement', 'seqKey',
    'uplinkDataPlacement', 'uplinkDataKey', 'uplinkHTTPMethod',
    'scMaxEachPostBytes', 'scMinPostsIntervalMs', 'scMaxBufferedPosts',
    'scStreamUpServerSecs', 'serverMaxHeaderBytes', 'noSSEHeader',
    'uplinkChunkSize', 'noGRPCHeader', 'enableXmux',
}


def object_value(value):
    if isinstance(value, str):
        try: value = json.loads(value)
        except (ValueError, TypeError): value = {}
    return value if isinstance(value, dict) else {}


def reset_fields(value, fields, defaults=None):
    value = copy.deepcopy(object_value(value))
    for key in fields: value.pop(key, None)
    value.update(copy.deepcopy(defaults or {}))
    return value


def reset_xhttp(value, path, host=None):
    defaults = {'path': path, 'mode': 'stream-up'}
    if host: defaults['host'] = host
    value = reset_fields(value, XHTTP_FIELDS, defaults)
    if 'xmux' in value:
        rest = reset_fields(value['xmux'], {
            'maxConcurrency', 'maxConnections', 'cMaxReuseTimes',
            'hMaxRequestTimes', 'hMaxReusableSecs', 'hKeepAlivePeriod'})
        if rest: value['xmux'] = rest
        else: value.pop('xmux')
    if 'extra' in value:
        rest = reset_xhttp(value.pop('extra'), path)
        rest.pop('path', None); rest.pop('mode', None)
        if rest: value['extra'] = rest
    return value


def reset_sockopt(value):
    original = object_value(value)
    result = reset_fields(original, SOCKOPT_FIELDS)
    future = reset_fields(original.get('happyEyeballs'),
        {'tryDelayMs', 'prioritizeIPv6', 'interleave', 'maxConcurrentTry'})
    if future: result['happyEyeballs'] = future
    return result


def reality_public_key(private_key):
    """Derive the share-link key with the installed native core; never rotate keys."""
    try:
        if len(base64.urlsafe_b64decode(private_key + '=' * (-len(private_key) % 4))) != 32: return None
        binary = next(iter(Path('/usr/local/x-ui/bin').glob('xray-linux-*')), None)
        if not binary: return None
        result = subprocess.run([str(binary), 'x25519', '-i', private_key], capture_output=True, text=True, timeout=5)
        if result.returncode: return None
        match = re.search(r'(?im)^(?:PublicKey|Public key|Password(?:\s*\(PublicKey\))?):\s*(\S+)', result.stdout)
        return match[1] if match else None
    except (ValueError, TypeError, OSError, subprocess.SubprocessError):
        return None  # A new native CLI format must not falsely block update.


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
        if (row['id'] == saved_id and saved_record and 'created_at' in row
                and 'created_at' in saved_record and row['created_at'] == saved_record['created_at']):
            return True  # Panel IDs are AUTOINCREMENT; creation identity survives all option edits.
        if row['id'] == saved_id and host_link:
            return True  # Stable host IDs survive names/transport option edits.
        if row['protocol'] != ('hysteria' if kind == 'hy2' else 'vless'): return False
        stream = object_value(row['stream_settings'])
        if kind == 'hy2':
            if row['id'] == saved_id and saved_record:
                old = object_value(saved_record['stream_settings'])
                password = salamander_password(old)
                if password and password == salamander_password(stream): return True
            tls = object_value(stream.get('tlsSettings'))
            certs = tls.get('certificates', [])
            if not isinstance(certs, list): certs = []
            return (not row.get('created_at')  # Pro SQL inserts, unlike native user API creation.
                    and row['protocol'] == 'hysteria' and stream.get('network') == 'hysteria'
                    and stream.get('security') == 'tls' and salamander_password(stream)
                    and row['tag'] == 'inbound-' + str(row['port'])
                    and any(isinstance(c, dict) and c.get('certificateFile') ==
                            '/root/cert/' + domains[0] + '/fullchain.pem' for c in certs))
        if row['id'] == saved_id and row['tag'] == TAGS[kind]:
            return True  # Persisted ownership survives native transport option changes.
        if row['id'] == saved_id and saved_record:
            previous = object_value(saved_record['stream_settings'])
            if kind == 'reality':
                key = object_value(previous.get('realitySettings')).get('privateKey')
                if key and key == object_value(stream.get('realitySettings')).get('privateKey'):
                    return True
            elif stream.get('network') == 'xhttp':
                old = object_value(previous.get('xhttpSettings'))
                new = object_value(stream.get('xhttpSettings'))
                if old.get('path') and old.get('path') == new.get('path'):
                    return True
        if kind == 'xhttp':
            config = object_value(stream.get('xhttpSettings'))
            transport = (stream.get('network') == 'xhttp'
                         and (config.get('path', '').rstrip('/') == xhttp_path.rstrip('/')
                              or config.get('host') == domains[0]))
            return ((row['tag'] == TAGS[kind] or row['id'] == saved_id or transport)
                    and (row.get('listen') or '').split(',')[0] == SOCKET)
        config = object_value(stream.get('realitySettings'))
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


def archive_snapshot(root, kind, domains, path, optional=False):
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
                    if len(matches) > 1:
                        if optional: continue
                        raise RuntimeError('Ambiguous managed inbound in backup')
                    if matches: return snapshot(db, matches[0])
    if optional: return None
    raise RuntimeError(f'Managed {kind} inbound missing; no verified recovery record. Restore a Pro backup first.')


def insert(db, table, record):
    columns = {r[1] for r in db.execute('PRAGMA table_info("' + table + '")')}
    record = {k: v for k, v in record.items() if k in columns}
    keys = list(record)
    db.execute('INSERT INTO "' + table + '" (' + ','.join('"' + k + '"' for k in keys) + ') VALUES (' + ','.join('?' for _ in keys) + ')', list(record.values()))


def salamander_password(stream):
    masks = object_value(stream.get('finalmask')).get('udp', [])
    if not isinstance(masks, list): return None
    return next((object_value(m.get('settings')).get('password') for m in masks
                 if isinstance(m, dict) and m.get('type') == 'salamander'), None)


def canonical(row, kind, xhttp_path=None, saved=None, domains=None, permitted_hosts=None):
    row = copy.deepcopy(row)
    row['protocol'] = 'hysteria' if kind == 'hy2' else 'vless'
    stream = object_value(row['stream_settings'])
    stream.pop('method', None)  # This native alias overrides network in Xray.
    stream.pop('externalProxy', None)  # Managed hosts describe the public entry.
    sockopt = reset_sockopt(stream.get('sockopt'))
    if sockopt: stream['sockopt'] = sockopt
    else: stream.pop('sockopt', None)
    settings = object_value(row['settings'])
    previous_stream = object_value(saved['record']['stream_settings']) if saved else {}
    if kind == 'hy2':
        row['listen'] = ''
        row['port'] = saved.get('architecture_port', saved['record']['port']) if saved else row['port']
        settings['version'] = 2
        stream['network'] = 'hysteria'; stream['security'] = 'tls'
        stream['hysteriaSettings'] = reset_fields(stream.get('hysteriaSettings'),
            {'version', 'udpIdleTimeout', 'auth', 'masquerade'}, {'version': 2, 'udpIdleTimeout': 60})
        tls = object_value(stream.get('tlsSettings'))
        tls.update(serverName=domains[0], minVersion='1.2', maxVersion='1.3',
                   cipherSuites='', rejectUnknownSni=False, disableSystemRoot=False,
                   enableSessionResumption=False, alpn=['h3'])
        old_certs = tls.get('certificates', [])
        cert = copy.deepcopy(old_certs[0]) if old_certs and isinstance(old_certs[0], dict) else {}
        cert.update(useFile=True, certificateFile='/root/cert/'+domains[0]+'/fullchain.pem',
                    keyFile='/root/cert/'+domains[0]+'/privkey.pem', certificate=[], key=[],
                    ocspStapling=0, oneTimeLoading=False, usage='encipherment', buildChain=False)
        tls['certificates'] = [cert]
        client_tls = object_value(tls.get('settings'))
        client_tls.update(fingerprint='', echConfigList='', pinnedPeerCertSha256=[], verifyPeerCertByName='')
        tls['settings'] = client_tls
        stream['tlsSettings'] = tls
        password = salamander_password(stream) or salamander_password(previous_stream)
        if not password: raise RuntimeError('Managed HY2 obfuscation secret missing; no saved recovery value')
        finalmask = object_value(stream.get('finalmask'))
        masks = finalmask.get('udp', [])
        mask = next((copy.deepcopy(m) for m in masks if isinstance(m, dict) and m.get('type') == 'salamander'), {})
        mask['type'] = 'salamander'
        mask_settings = object_value(mask.get('settings')); mask_settings['password'] = password
        mask['settings'] = mask_settings
        finalmask.update(tcp=[], udp=[mask]); stream['finalmask'] = finalmask
    else:
        settings.update(decryption='none', encryption='none', fallbacks=[])
        stream.pop('finalmask', None)
    if kind == 'reality':
        row['listen'] = '127.0.0.1'
        row['port'] = 8443
        stream['network'] = 'tcp'
        stream['security'] = 'reality'
        config = object_value(stream.get('realitySettings'))
        previous = object_value(previous_stream.get('realitySettings'))
        for field in ('privateKey', 'shortIds'):
            if not config.get(field):
                if not previous.get(field):
                    raise RuntimeError('Managed REALITY ' + field + ' missing; no saved recovery value')
                config[field] = copy.deepcopy(previous[field])
        config['target'] = '127.0.0.1:9443'
        config['xver'] = 0  # Cover 9443 accepts TLS without a PROXY prefix.
        config.update(show=False, minClientVer='', maxClientVer='', maxTimediff=0)
        for alias in ('minClient', 'maxClient'): config.pop(alias, None)
        if 'dest' in config: config['dest'] = config['target']
        if domains: config['serverNames'] = [domains[1]]
        client = object_value(config.get('settings'))
        public_key = reality_public_key(config['privateKey'])
        if public_key: client['publicKey'] = public_key
        if not client.get('publicKey') and object_value(previous.get('settings')).get('publicKey'):
            client['publicKey'] = previous['settings']['publicKey']
        client.update(fingerprint='firefox', serverName='', spiderX='/')
        config['settings'] = client; stream['realitySettings'] = config
        tcp = object_value(stream.get('tcpSettings'))
        tcp['acceptProxyProtocol'] = True
        tcp['header'] = reset_fields(tcp.get('header'), {'type', 'request', 'response'}, {'type': 'none'})
        stream['tcpSettings'] = tcp
    elif kind == 'xhttp':
        row['listen'] = SOCKET + ',0666'; row['port'] = 0
        stream['network'] = 'xhttp'; stream['security'] = 'none'
        stream['xhttpSettings'] = reset_xhttp(stream.get('xhttpSettings'), xhttp_path, domains[0])
        # Managed XHTTP uses a Unix socket; Sockopt is explicitly disabled.
        stream.pop('sockopt', None)
    row['settings'] = json.dumps(settings, ensure_ascii=False)
    row['stream_settings'] = json.dumps(stream, ensure_ascii=False)
    return row


def protect_managed_identity(db, row, saved, kind):
    """Protect connection identities only, not native options/schema changes."""
    if not saved.get('protect_for_update'):
        return  # Long-lived ownership snapshots are not a ban on native user edits.
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


def patch_hosts(db, row, kind, saved, domains):
    # Only the original host IDs, not extra hosts a user added to this inbound.
    # Older manifests stored all hosts: the installer creates exactly one,
    # whose lowest ID precedes additional user hosts.
    entries = sorted((saved or {}).get('hosts', []), key=lambda h: h['id'])
    identifiers = (saved or {}).get('managed_host_ids', [entries[0]['id']] if entries else [])
    defaults = {'address': domains[0], 'port': row['port'] if kind == 'hy2' else 443,
                'security': 'tls' if kind == 'xhttp' else 'same',
                'fingerprint': 'firefox' if kind == 'xhttp' else '',
                'alpn': '["h2","http/1.1"]' if kind == 'xhttp' else '[]',
                'sni': domains[0] if kind == 'xhttp' else '',
                'host_header': domains[0] if kind == 'xhttp' else '', 'path': '', 'cipher_suites': '',
                'override_sni_from_address': 0, 'keep_sni_blank': 0,
                'pinned_peer_cert_sha256': '[]', 'verify_peer_cert_by_name': '',
                'allow_insecure': 0, 'ech_config_list': '', 'mux_params': '',
                'sockopt_params': '', 'final_mask': '', 'vless_route': ''}
    columns = {r[1] for r in db.execute('PRAGMA table_info(hosts)')}
    defaults = {k:v for k,v in defaults.items() if k in columns}
    for identifier in identifiers:
        host = db.execute('SELECT * FROM hosts WHERE id=? AND inbound_id=?', (identifier, row['id'])).fetchone()
        if not host: continue  # User deletion is intentional, never recreate.
        changed = {k:v for k,v in defaults.items() if host[k] != v}
        if changed:
            db.execute('UPDATE hosts SET '+','.join('"'+k+'"=?' for k in changed)+' WHERE id=?',
                       [*changed.values(), identifier])
    return identifiers


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
        for kind in ('xhttp', 'reality', 'hy2'):
            saved = manifest.get('objects', {}).get(kind)
            selection_file = root / STATE / 'hy2-ownership.json'
            selection = json.loads(selection_file.read_text()) if kind == 'hy2' and selection_file.is_file() else {}
            if kind == 'hy2' and selection.get('selected') is False:
                continue
            def identify(saved):
                saved_id = saved['record']['id'] if saved else None
                entries = sorted(saved.get('hosts', []), key=lambda h:h['id']) if saved else []
                host_ids = set(saved.get('managed_host_ids', [entries[0]['id']] if entries else [])) if saved else set()
                host_link = any(h['id'] in host_ids and h['inbound_id'] == saved_id for h in live_hosts)
                return [r for r in all_rows if proof(r, kind, domains, xhttp_path, saved_id,
                                                     saved['record'] if saved else None, host_link)]
            candidates = identify(saved)
            if kind == 'hy2' and selection.get('selected') and not saved:
                row = next((r for r in all_rows if r['id'] == selection['id'] and r['protocol'] == 'hysteria'), None)
                if row:
                    saved = snapshot(db, row)
                    saved.update(architecture_port=selection['port'], managed_host_ids=[selection['host_id']])
                    candidates = identify(saved)
            if not candidates and not saved:
                # Legacy installs have no manifest. Verified backups retain the
                # stable IDs/host links even when the user changed the transport.
                saved = archive_snapshot(root, kind, domains, xhttp_path, optional=True)
                candidates = identify(saved)
            # Persisted identities take precedence over coincidental signatures.
            if saved:
                exact = [r for r in candidates if r['id'] == saved['record']['id']]
                candidates = exact
            if len(candidates) > 1:
                print(f'Skipped ambiguous {kind} ownership; user objects left unchanged')
                continue
            if not candidates:
                # Update normalizes surviving objects only. Keep proof for a
                # subsequent run, but never resurrect intentionally deleted rows.
                if saved:
                    new_manifest['objects'][kind] = copy.deepcopy(saved)
                    new_manifest['objects'][kind]['present'] = False
                print(f'Skipped absent/unidentified managed {kind}; nothing created')
                continue
            row = candidates[0]
            if not saved:
                saved = snapshot(db, row)
                saved['managed_host_ids'] = [h['id'] for h in sorted(saved['hosts'], key=lambda h:h['id'])[:1]]
            patched = canonical(row, kind, xhttp_path, saved, domains, permitted_hosts)
            if not remember:
                if saved: protect_managed_identity(db, patched, saved, kind)
                for field in ('protocol', 'listen', 'port', 'settings', 'stream_settings'):
                    if patched[field] != row[field]:
                        db.execute('UPDATE inbounds SET "' + field + '"=? WHERE id=?', (patched[field], row['id']))
                row = dict(db.execute('SELECT * FROM inbounds WHERE id=?', (row['id'],)).fetchone())
                host_ids = patch_hosts(db, row, kind, saved, domains)
            else:
                entries = sorted(saved.get('hosts', []), key=lambda h:h['id'])
                host_ids = saved.get('managed_host_ids', [entries[0]['id']] if entries else [])
            # Preserve the last recoverable key/transport identity even if the
            # current form temporarily cleared required fields. DB stays untouched
            # during remember; reconciliation applies the owned-field patch later.
            new_manifest['objects'][kind] = snapshot(db, patched if remember else row)
            new_manifest['objects'][kind].update(managed_host_ids=host_ids, present=True)
            if remember: new_manifest['objects'][kind]['protect_for_update'] = True
            if kind == 'hy2': new_manifest['objects'][kind]['architecture_port'] = patched['port']
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


def wait_hy2_listener(port, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        addresses = [line.split()[3] for line in subprocess.check_output(['ss','-H','-lnu'], text=True).splitlines()]
        if any(address.rsplit(':',1)[-1] == str(port) for address in addresses): return
        if time.monotonic() >= deadline: raise RuntimeError('Managed HY2 UDP listener is missing')
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
        with closing(sqlite3.connect('/etc/x-ui/x-ui.db')) as db:
            def enabled(kind):
                obj = manifest['objects'].get(kind, {})
                if not obj.get('present', True) or not obj.get('record'): return False
                row = db.execute('SELECT enable FROM inbounds WHERE id=?', (obj['record']['id'],)).fetchone()
                return bool(row and row[0])
            reality_enabled, xhttp_enabled = enabled('reality'), enabled('xhttp')
            hy2_port = manifest['objects']['hy2']['record']['port'] if enabled('hy2') else None
        wait_listeners((7443, 9443, *((8443,) if reality_enabled else ())), xhttp_enabled)
        if hy2_port is not None: wait_hy2_listener(hy2_port)
        print('Managed listeners validated: public 443 IPv4/IPv6; internal loopback only.')
    elif args.action == 'main-vhost': print(layout(root)['xhttp'])
    else: reconcile(root, args.action == 'remember', args.changes)


if __name__ == '__main__':
    main()
