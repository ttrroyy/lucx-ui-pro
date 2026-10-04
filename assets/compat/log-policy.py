#!/usr/bin/env python3
"""Keep nginx logs enabled; bound each log family and the default journal."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

BUDGET = 100 * 1024 * 1024
AGE = 3 * 24 * 60 * 60
HEADER = '# Managed by lucx-ui-pro log policy\n'
ROTATE = HEADER + '''/var/log/nginx/*.log {
    hourly
    maxsize 25M
    rotate 72
    maxage 3
    missingok
    notifempty
    compress
    delaycompress
    nodateext
    create 0640 www-data adm
    sharedscripts
    postrotate
        if systemctl is-active --quiet nginx.service; then
            systemctl kill --kill-who=main --signal=USR1 nginx.service
        fi
    endscript
}
'''
JOURNAL = HEADER + '''[Journal]
SystemMaxUse=100M
RuntimeMaxUse=100M
SystemMaxFileSize=25M
RuntimeMaxFileSize=25M
MaxRetentionSec=3day
MaxFileSec=1day
'''
SERVICE = '''[Unit]
Description=Limit nginx log archives and system journal
After=nginx.service systemd-journald.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/lucx-ui-pro/log-policy.py maintain
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=180
'''
TIMER = '''[Unit]
Description=Check lucx-ui-pro log retention every minute

[Timer]
OnActiveSec=1min
OnUnitActiveSec=1min
AccuracySec=10s
Unit=lucx-log-policy.service

[Install]
WantedBy=timers.target
'''
CONFIGS = ('etc/logrotate.d/nginx', 'etc/systemd/journald.conf.d/99-lucx-ui-pro-logs.conf')


def command(*args, allowed=(0,)):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=150)
    if result.returncode not in allowed:
        raise RuntimeError(args[0] + ': ' + result.stderr.strip())
    return result


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.lucx-tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.chmod(0o644)
    os.replace(temporary, path)


def install(root):
    if root == Path('/') and not shutil.which('logrotate'):
        command('apt-get', 'install', '-y', '--no-install-recommends', 'logrotate')
    state = root / 'var/lib/lucx-ui-preinstall/log-policy-baseline'
    marker = state / 'state.json'
    if not marker.is_file():
        state.mkdir(parents=True, exist_ok=True); state.chmod(0o700)
        saved = {}
        for index, relative in enumerate(CONFIGS):
            path = root / relative
            present = path.is_file()
            saved[relative] = present
            if present:
                shutil.copy2(path, state / str(index))
        marker.write_text(json.dumps(saved), encoding='utf-8'); marker.chmod(0o600)
    for relative, text in zip(CONFIGS, (ROTATE, JOURNAL)):
        write(root / relative, text)
    units = root / 'etc/systemd/system'
    write(units / 'lucx-log-policy.service', SERVICE)
    write(units / 'lucx-log-policy.timer', TIMER)
    if root == Path('/'):
        command('logrotate', '--debug', '/etc/logrotate.d/nginx')
        command('systemctl', 'daemon-reload')
        command('systemctl', 'restart', 'systemd-journald.service')
        # Close old active journal files once so the initial vacuum can remove
        # the backlog. Normal maintenance need not rotate journald every minute.
        command('journalctl', '--rotate')
        maintain(root)
        command('systemctl', 'enable', '--now', 'lucx-log-policy.timer')
        command('systemctl', 'restart', 'lucx-log-policy.timer')
    print('Log policy: nginx ~100 MiB per log including archives; journal 100 MiB; retention up to 3 days.')


def prune(directory, budget=BUDGET, age=AGE, now=None):
    """Delete only nginx numbered archives; never truncate live log files."""
    now = time.time() if now is None else now
    groups = {}
    for path in directory.iterdir() if directory.is_dir() else ():
        if path.is_symlink() or not path.is_file():
            continue
        match = re.fullmatch(r'(.+\.log)(?:\.([0-9]+)(?:\.gz)?)?', path.name)
        if not match:
            continue
        active = match[2] is None
        if not active and path.stat().st_mtime < now - age:
            path.unlink(); continue
        groups.setdefault(match[1], []).append((path, active))
    for entries in groups.values():
        total = sum(path.stat().st_size for path, _ in entries)
        archives = sorted((path for path, active in entries if not active), key=lambda path: path.stat().st_mtime)
        for path in archives:
            if total <= budget:
                break
            total -= path.stat().st_size; path.unlink()


def maintain(root):
    if root == Path('/'):
        import fcntl
        lock = root / 'run/lock/lucx-log-policy.lock'
        with lock.open('w') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Reuse the system state/lock; a concurrent system logrotate run
            # returns 3 and will perform the same rotation itself.
            result = command('logrotate', '/etc/logrotate.d/nginx', allowed=(0, 3))
            if result.returncode == 0:
                prune(root / 'var/log/nginx')
            command('journalctl', '--vacuum-size=100M', '--vacuum-time=3d')
    else:
        prune(root / 'var/log/nginx')


def remove(root):
    state = root / 'var/lib/lucx-ui-preinstall/log-policy-baseline'
    marker = state / 'state.json'
    if not marker.is_file():
        return
    if root == Path('/'):
        command('systemctl', 'disable', '--now', 'lucx-log-policy.timer', allowed=(0, 1))
        command('systemctl', 'stop', 'lucx-log-policy.service', allowed=(0, 1, 5))
    saved = json.loads(marker.read_text(encoding='utf-8'))
    for index, relative in enumerate(CONFIGS):
        path = root / relative
        if saved[relative]:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(state / str(index), path)
        else:
            path.unlink(missing_ok=True)
    for suffix in ('service', 'timer'):
        (root / f'etc/systemd/system/lucx-log-policy.{suffix}').unlink(missing_ok=True)
    if root == Path('/'):
        command('systemctl', 'daemon-reload')
        command('systemctl', 'restart', 'systemd-journald.service')
    print('Previous nginx rotation and journald configuration restored.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('install', 'maintain', 'remove'))
    parser.add_argument('--root', type=Path, default=Path('/'))
    args = parser.parse_args()
    globals()[args.action](args.root.resolve())


if __name__ == '__main__':
    main()
