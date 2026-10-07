#!/usr/bin/env bash

# Keep package hooks noninteractive during automated restores.
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
export NEEDRESTART_SUSPEND=1
# lucx-ui-backup.sh — backup and restore LucX-UI panel + nginx + certs
# Usage:
#   lucx-ui-backup.sh backup           — create timestamped backup
#   lucx-ui-backup.sh restore <file>   — restore from backup archive
#   lucx-ui-backup.sh list             — list available backups
set -Eeuo pipefail
umask 077

BACKUP_STORE="/var/backups/x-ui"
PACKAGES="ca-certificates curl wget jq bash sudo nginx-full certbot python3-certbot-nginx sqlite3 ufw netcat-openbsd mtr python3 libcap2-bin cron iproute2 ipset iptables rsyslog whois fail2ban nftables logrotate"

# Self-contained log retention helper; also used when restoring older backups.
run_log_policy() {
    install -d -m 0755 /usr/local/lib/lucx-ui-pro
    cat > /usr/local/lib/lucx-ui-pro/log-policy.py <<'PY_LUCX_LOG_POLICY'
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
PY_LUCX_LOG_POLICY
    chmod 0755 /usr/local/lib/lucx-ui-pro/log-policy.py
    python3 /usr/local/lib/lucx-ui-pro/log-policy.py "$@"
}

# ── paths to back up ──────────────────────────────────────────────────────────
BACKUP_PATHS=(
    /etc/nginx
    /etc/x-ui
    /etc/default/x-ui
    /etc/fail2ban
    /etc/cron.d
    /usr/local/x-ui
    /usr/bin/x-ui
    /usr/local/lib/3x-ui-pro
    /usr/local/lib/lucx-ui-pro
    /etc/systemd/system/lucx-clash-sub.service
    /usr/local/sbin/lucx-apply-qdisc
    /usr/local/sbin/lucx-awg-sysctl-guard
    /etc/systemd/system/lucx-qdisc-sync.service
    /etc/systemd/system/lucx-qdisc-sync.path
    /etc/letsencrypt
    /var/lib/letsencrypt
    /var/log/letsencrypt
    /etc/sysctl.conf
    /root/cert
    /var/www/html
    /var/www/diagnostics
    /var/www/subpage
    /var/www/tproxy
    /root/.lucx-tg-web-proxy-info
    /var/lib/lucx-ui-preinstall # Includes panel auto-domain mode; TG asks separately on every install.
    /etc/sysctl.d/99-lucx-ui-forwarding.conf
    /etc/sysctl.d/99-bbr-x-ui.conf
    /etc/sysctl.d/99-awg-performance.conf
    /etc/sysctl.d/99-zz-lucx-ui-tuning.conf
    /etc/modules-load.d/lucx-ui-network.conf
    /etc/modules-load.d/amneziawg.conf
    /etc/modules-load.d/tcp-bbr.conf
    /etc/default/ufw
    /etc/ufw/user.rules
    /etc/ufw/user6.rules
    /etc/ufw/before.rules
    /etc/ufw/before6.rules
    /etc/ipset.conf
    /etc/iptables/ipsets
    /etc/rsyslog.d/10-iptables-scanners.conf
    /etc/logrotate.d/iptables-scanners
    /etc/logrotate.d/nginx
    /etc/systemd/journald.conf.d/99-lucx-ui-pro-logs.conf
    /usr/local/bin/rkn-guard
    /usr/local/bin/rkn
    /usr/local/bin/antiscan-aggregate-logs.sh
    /opt/rkn-guard-manager.sh
    /opt/rkn-guard-manual.list
    /opt/AdGuardHome
    /root/.lucx-adguard-info
)
SYSTEMD_UNITS=(
    x-ui.service mtr-backend.service lucx-clash-sub.service AdGuardHome.service fail2ban.service
    lucx-apply-qdisc.service
    lucx-qdisc-sync.service lucx-qdisc-sync.path
    lucx-awg-sysctl-guard.service lucx-awg-sysctl-guard.path
    lucx-log-policy.service lucx-log-policy.timer
    antiscan-ipset-restore.service antiscan-move-rules.service
    antiscan-aggregate.service antiscan-aggregate.timer
    rkn-guard-list-update.service rkn-guard-list-update.timer
    rkn-guard-self-update.service rkn-guard-self-update.timer
)

# ── colours ───────────────────────────────────────────────────────────────────
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
blue()  { printf '\033[34m%s\033[0m\n' "$*"; }
die()   { red "ERROR: $*" >&2; exit 1; }

require_root() { [[ $EUID -eq 0 ]] || die "Run as root (sudo $0 $*)"; }

# free space in KB on the filesystem containing $1
avail_kb() { df -Pk "$1" | awk 'NR==2 {print $4}'; }

# staging must NOT live in /tmp: on Ubuntu 24.10+ /tmp is a size-limited tmpfs
# and a full uncompressed copy of the panel + web roots does not fit there
make_staging() {
    local prefix="$1"
    install -d -m 0700 "${BACKUP_STORE}"
    mktemp -d "${BACKUP_STORE}/.${prefix}-XXXXXX"
}

# Persist the module needed by the currently selected default qdisc. The
# panel owns the qdisc value; backup/restore only makes sure the kernel module
# required to honor that value is available again after reboot.
persist_current_qdisc_module() {
    local qdisc module file=/etc/modules-load.d/lucx-ui-network.conf
    qdisc=$(sysctl -n net.core.default_qdisc 2>/dev/null || true)
    case "$qdisc" in
        fq)       module=sch_fq ;;
        fq_codel) module=sch_fq_codel ;;
        cake)     module=sch_cake ;;
        *)        module="" ;;
    esac
    [[ -n "$module" ]] || return 0
    modprobe "$module" >/dev/null 2>&1 || return 0
    mkdir -p /etc/modules-load.d
    touch "$file"
    if ! grep -Fxq "$module" "$file" 2>/dev/null; then
        printf '%s\n' "$module" >> "$file"
    fi
    chmod 0644 "$file"
}

# ── backup ────────────────────────────────────────────────────────────────────
cmd_backup() {
    require_root

    local ts name staging dest
    ts=$(date +%Y%m%d-%H%M%S)
    name="lucx-ui-backup-${ts}"

    # estimate size and verify free space before touching anything:
    # staging holds an uncompressed copy, the archive lands next to it
    local est_kb need_kb have_kb
    est_kb=$(du -skc "${BACKUP_PATHS[@]}" 2>/dev/null | awk 'END {print $1}' || true)
    need_kb=$(( est_kb * 2 + 102400 ))   # copy + archive + 100 MB margin
    install -d -m 0700 "${BACKUP_STORE}"
    have_kb=$(avail_kb "${BACKUP_STORE}")
    (( have_kb >= need_kb )) || die "Not enough free space in ${BACKUP_STORE}: need ~$(( need_kb / 1024 )) MB, have $(( have_kb / 1024 )) MB"

    staging=$(make_staging staging)
    BACKUP_WAS_ACTIVE=0
    systemctl is-active --quiet x-ui && BACKUP_WAS_ACTIVE=1 || true
    backup_cleanup() {
        if (( BACKUP_WAS_ACTIVE )); then systemctl start x-ui 2>/dev/null || true; fi
        rm -rf -- "$BACKUP_STAGING"
        [[ ! -f "${BACKUP_DEST}.partial" ]] || rm -f -- "${BACKUP_DEST}.partial"
    }
    trap backup_cleanup EXIT

    dest="${BACKUP_STORE}/${name}.tar.gz"
    BACKUP_STAGING="$staging"
    BACKUP_DEST="$dest"

    blue "==> Stopping x-ui for consistent DB snapshot..."
    if (( BACKUP_WAS_ACTIVE )); then systemctl stop x-ui || die "Failed to stop x-ui for consistent backup"; fi

    : > "${staging}/services-state"
    local svc enabled active
    for svc in x-ui nginx lucx-clash-sub AdGuardHome mtr-backend lucx-apply-qdisc lucx-qdisc-sync.path \
               lucx-awg-sysctl-guard.path antiscan-ipset-restore.service \
               antiscan-move-rules.service antiscan-aggregate.timer \
               rkn-guard-list-update.timer rkn-guard-self-update.timer; do
        enabled=0; active=0
        systemctl is-enabled --quiet "$svc" && enabled=1 || true
        systemctl is-active --quiet "$svc" && active=1 || true
        [[ "$svc" == x-ui ]] && active="$BACKUP_WAS_ACTIVE"
        printf '%s %s %s\n' "$svc" "$enabled" "$active" >> "${staging}/services-state"
    done
    ufw status 2>/dev/null | grep -q '^Status: active' && \
        printf 'active\n' > "${staging}/ufw-state" || \
        printf 'inactive\n' > "${staging}/ufw-state"

    # ── collect filesystem paths ───────────────────────────────────────────
    blue "==> Collecting files..."
    local files_root="${staging}/files"
    for path in "${BACKUP_PATHS[@]}"; do
        [[ -e "${path}" ]] || continue
        local dst="${files_root}${path}"
        mkdir -p "$(dirname "${dst}")"
        cp -a "${path}" "${dst}"
    done

    # systemd units
    mkdir -p "${files_root}/etc/systemd/system"
    for unit in "${SYSTEMD_UNITS[@]}"; do
        [[ -f "/etc/systemd/system/${unit}" ]] && \
            cp "/etc/systemd/system/${unit}" "${files_root}/etc/systemd/system/"
    done

    # ── crontab ───────────────────────────────────────────────────────────
    crontab -l 2>/dev/null > "${staging}/root-crontab" || true

    if [[ -d /etc/cron.d ]]; then
        cp -a /etc/cron.d "${staging}/cron.d"
    fi

    # ── metadata ──────────────────────────────────────────────────────────
    local xui_ver awg_installed
    xui_ver=$(/usr/local/x-ui/x-ui -v 2>/dev/null | grep -oP 'v?\d+\.\d+\.\d+(?:-lucx\.\d+)?' | head -1 || echo "unknown")
    if modinfo amneziawg >/dev/null 2>&1 || [[ -f /etc/modules-load.d/amneziawg.conf ]] || [[ -f /etc/x-ui/.awg-module-version ]]; then
        awg_installed=1
    else
        awg_installed=0
    fi
    cat > "${staging}/meta.json" <<JSON
{
  "created":      "${ts}",
  "hostname":     "$(hostname -f 2>/dev/null || hostname)",
  "x-ui":         "${xui_ver}",
  "kernel":       "$(uname -r)",
  "awg_installed": ${awg_installed},
  "packages":     "${PACKAGES}"
}
JSON

    blue "==> Restarting x-ui..."
    if (( BACKUP_WAS_ACTIVE )); then systemctl start x-ui || die "Failed to restart x-ui"; fi

    # ── compress ──────────────────────────────────────────────────────────
    blue "==> Compressing..."
    tar -czf "${dest}.partial" -C "${staging}" .
    mv -f "${dest}.partial" "$dest"
    chmod 0600 "$dest"
    (cd "$BACKUP_STORE" && sha256sum "$(basename "$dest")" > "$(basename "$dest").sha256")

    local size
    size=$(du -sh "${dest}" | cut -f1)
    green "==> Backup saved: ${dest} (${size})"
}

# Keep the official AWG installer BBR-neutral, matching lucx-ui-pro latest.
install_awg_bbr_support() {
    systemctl stop lucx-awg-readiness.service lucx-awg-sysctl-guard.path lucx-awg-sysctl-guard.service 2>/dev/null || true
    systemctl disable lucx-awg-readiness.service 2>/dev/null || true
    mkdir -p /usr/local/lib/lucx-ui-pro
    cat > /usr/local/lib/lucx-ui-pro/awg-bbr.py <<'PY_LUCX_AWG_BBR'
#!/usr/bin/env python3
"""Keep AWG's performance file BBR-neutral; leave module installation upstream."""
import argparse
from pathlib import Path
import re
import subprocess
import urllib.request


def strip_bbr(text):
    return re.sub(r'(?m)^[ \t]*net\.(?:core\.default_qdisc[ \t]*=[ \t]*fq|ipv4\.tcp_congestion_control[ \t]*=[ \t]*bbr)[ \t]*\r?\n?', '', text)


def prepare_installer(path, version=None):
    text = path.read_text(encoding='utf-8')
    # Old archives may contain our module wrapper. Restore the corresponding
    # author's installer, rather than carrying a local kernel patch forward.
    if any(marker in text for marker in ('awg-compat.py', '# LUCX UDP installer', '# LUCX AWG installer')):
        if version is None:
            version = subprocess.check_output(['/usr/local/x-ui/x-ui', '-v'], text=True).strip()
        tag = 'v' + version.lstrip('v')
        if not re.fullmatch(r'v\d+\.\d+\.\d+-lucx\.\d+', tag):
            raise RuntimeError('Cannot identify upstream AWG installer version')
        url = f'https://raw.githubusercontent.com/AlexeyLCP/lucx-ui/{tag}/bin/install-awg-module.sh'
        with urllib.request.urlopen(url, timeout=60) as response:
            original = response.read().decode('utf-8')
        subprocess.run(['bash', '-n'], input=original, text=True, check=True)
        text = original
    updated = strip_bbr(text)
    if updated != path.read_text(encoding='utf-8'):
        path.write_text(updated, encoding='utf-8')


def clean_cli(path):
    text = path.read_text(encoding='utf-8')
    start = text.find('install_awg_module() {')
    end = text.find('\nuninstall_awg_module()', start)
    if start < 0 or end < 0:
        return
    block = text[start:end]
    if 'lucx-awg-sysctl-guard' not in block:
        return
    block = re.sub(r'(?m)^    /usr/local/sbin/lucx-awg-sysctl-guard[^\n]*\n', '', block)
    block = block.replace('    local rc=$?\n    return $rc\n', '')
    path.write_text(text[:start] + block + text[end:], encoding='utf-8')


def cleanup_legacy_modules():
    """One-time retirement of Pro DKMS identities, never native module versions."""
    changed = False
    result = subprocess.run(['dkms', 'status', 'amneziawg'], capture_output=True, text=True)
    versions = set(re.findall(r'^amneziawg[/,]\s*([^,\s]+)', result.stdout, re.M))
    for version in sorted(versions):
        if re.fullmatch(r'[A-Za-z0-9_.-]+-lucx(?:pro280|udp[12])', version):
            # A partially completed transition can register the native build
            # while the Pro module remains installed. Retire that same-pin
            # registration as well, so upstream installs its clean build.
            base = re.sub(r'-lucx(?:pro280|udp[12])$', '', version)
            if base in versions:
                subprocess.run(['dkms', 'remove', '-m', 'amneziawg', '-v', base, '--all'], check=True)
            subprocess.run(['dkms', 'remove', '-m', 'amneziawg', '-v', version, '--all'], check=True)
            changed = True
    # DKMS can restore an older "original_module" during uninstall. Remove
    # only remnants whose embedded version still identifies a Pro build.
    for file in Path('/lib/modules').glob('*/updates/dkms/amneziawg.ko*'):
        version = subprocess.run(['modinfo', '-F', 'version', str(file)], capture_output=True, text=True).stdout.strip()
        if re.fullmatch(r'[A-Za-z0-9_.-]+-lucx(?:pro280|udp[12])', version):
            kernel = file.parents[2].name
            file.unlink()
            subprocess.run(['depmod', '-a', kernel], check=True)
            changed = True
    if changed:
        Path('/etc/x-ui/.awg-module-version').unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('prepare', 'sanitize', 'clean-cli', 'cleanup-legacy'))
    parser.add_argument('path', type=Path, nargs='?', default=Path('/'))
    args = parser.parse_args()
    if args.command == 'cleanup-legacy':
        if subprocess.run(['sh', '-c', 'command -v dkms'], capture_output=True).returncode == 0:
            cleanup_legacy_modules()
        return
    if not args.path.is_file():
        return
    if args.command == 'prepare':
        prepare_installer(args.path)
    elif args.command == 'clean-cli':
        clean_cli(args.path)
    else:
        text = args.path.read_text(encoding='utf-8')
        updated = strip_bbr(text)
        if updated != text:
            args.path.write_text(updated, encoding='utf-8')


if __name__ == '__main__':
    main()
PY_LUCX_AWG_BBR
    chmod 0755 /usr/local/lib/lucx-ui-pro/awg-bbr.py
    for cli in /usr/bin/x-ui /usr/local/x-ui/x-ui.sh; do
        python3 /usr/local/lib/lucx-ui-pro/awg-bbr.py clean-cli "$cli" || return 1
    done
    python3 /usr/local/lib/lucx-ui-pro/awg-bbr.py prepare /usr/local/x-ui/bin/install-awg-module.sh || return 1
    python3 /usr/local/lib/lucx-ui-pro/awg-bbr.py cleanup-legacy || return 1
    rm -f /usr/local/lib/lucx-ui-pro/awg-compat.py /etc/x-ui/.lucx-awg-compat-version \
          /var/lib/lucx-ui-pro/awg-readiness.json /etc/systemd/system/lucx-awg-readiness.service \
          /etc/systemd/system/multi-user.target.wants/lucx-awg-readiness.service
    systemctl daemon-reload || return 1
}

install_awg_sysctl_guard() {
    install_awg_bbr_support || return 1
    mkdir -p /usr/local/sbin /etc/systemd/system
    cat > /usr/local/sbin/lucx-awg-sysctl-guard <<'AWG_BBR_GUARD'
#!/bin/bash
set -e
python3 /usr/local/lib/lucx-ui-pro/awg-bbr.py prepare /usr/local/x-ui/bin/install-awg-module.sh
python3 /usr/local/lib/lucx-ui-pro/awg-bbr.py sanitize /etc/sysctl.d/99-awg-performance.conf
AWG_BBR_GUARD
    chmod 0755 /usr/local/sbin/lucx-awg-sysctl-guard
    cat > /etc/systemd/system/lucx-awg-sysctl-guard.service <<'AWG_BBR_SERVICE'
[Unit]
Description=Keep AWG performance settings BBR-neutral
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/lucx-awg-sysctl-guard
AWG_BBR_SERVICE
    cat > /etc/systemd/system/lucx-awg-sysctl-guard.path <<'AWG_BBR_PATH'
[Unit]
Description=Watch AWG performance settings for BBR assignments
[Path]
PathChanged=/usr/local/x-ui/bin/install-awg-module.sh
PathChanged=/etc/sysctl.d/99-awg-performance.conf
Unit=lucx-awg-sysctl-guard.service
[Install]
WantedBy=multi-user.target
AWG_BBR_PATH
    systemctl daemon-reload || return 1
    systemctl enable --now lucx-awg-sysctl-guard.path || return 1
    /usr/local/sbin/lucx-awg-sysctl-guard || return 1
}

patch_awg_installer() {
    local script=/usr/local/x-ui/bin/install-awg-module.sh
    [[ -f "$script" ]] || return 0
    python3 - "$script" <<'PY_AWG_RESTORE'
import re, sys
path=sys.argv[1]
text=open(path,encoding="utf-8",errors="surrogateescape").read()
text=re.sub(r'(?m)^\s*net\.core\.default_qdisc\s*=\s*fq\s*$\n?', '', text)
text=re.sub(r'(?m)^\s*net\.ipv4\.tcp_congestion_control\s*=\s*bbr\s*$\n?', '', text)
open(path,'w',encoding='utf-8',errors='surrogateescape').write(text)
PY_AWG_RESTORE
    chmod +x "$script" 2>/dev/null || true
}

setup_fail2ban() {
    if [[ -n "${XUI_ENABLE_FAIL2BAN+x}" && "${XUI_ENABLE_FAIL2BAN}" != "true" ]]; then
        blue "==> XUI_ENABLE_FAIL2BAN=${XUI_ENABLE_FAIL2BAN}; skipping Fail2ban restore/setup."
        return 0
    fi

    if [[ ! -x /usr/bin/x-ui ]]; then
        blue "==> x-ui CLI not found; skipping Fail2ban restore/setup."
        return 0
    fi

    if ! grep -q '"setup-fail2ban")' /usr/bin/x-ui; then
        blue "==> This x-ui.sh predates 'x-ui setup-fail2ban'; skipping Fail2ban restore/setup."
        return 0
    fi

    blue "==> Restoring/configuring Fail2ban for the IP Limit feature..."
    if /usr/bin/x-ui setup-fail2ban; then
        green "    Fail2ban setup complete"
    else
        blue "    Fail2ban setup did not finish; continuing restore"
    fi
    return 0
}

sanitize_awg_sysctl_file() {
    local f=/etc/sysctl.d/99-awg-performance.conf
    [[ -f "$f" ]] || return 0
    sed -i -E \
        -e '/^[[:space:]]*net\.core\.default_qdisc[[:space:]]*=[[:space:]]*fq[[:space:]]*$/d' \
        -e '/^[[:space:]]*net\.ipv4\.tcp_congestion_control[[:space:]]*=[[:space:]]*bbr[[:space:]]*$/d' \
        "$f" || true
    if [[ -f /etc/sysctl.d/99-bbr-x-ui.conf ]]; then
        sysctl -p /etc/sysctl.d/99-bbr-x-ui.conf >/dev/null 2>&1 || true
    fi
}


restore_awg_module() {
    local script=/usr/local/x-ui/bin/install-awg-module.sh
    local want_awg="${1:-0}"
    if [[ "$want_awg" != "1" ]]; then
        # A backup can be restored onto a host that currently has AWG even
        # though the backup itself did not. Make the restored state truthful.
        if [[ -x "$script" ]]; then
            blue "==> Backup has no AmneziaWG; removing any existing AWG module/tools..."
            bash "$script" --uninstall >/dev/null 2>&1 || true
        else
            rmmod amneziawg >/dev/null 2>&1 || true
            if command -v dkms >/dev/null 2>&1; then
                while read -r ver; do
                    [[ -n "$ver" ]] || continue
                    dkms remove -m amneziawg -v "$ver" --all >/dev/null 2>&1 || true
                done < <(dkms status amneziawg 2>/dev/null | grep -oP 'amneziawg[,/] ?\K[^,]+' | sort -u || true)
            fi
            rm -rf /usr/src/amneziawg-* /var/lib/dkms/amneziawg
            rm -f /usr/bin/awg /usr/bin/awg-quick /usr/local/bin/awg /usr/local/bin/awg-quick \
                  /usr/sbin/awg /usr/sbin/awg-quick /usr/local/sbin/awg /usr/local/sbin/awg-quick \
                  /etc/modules-load.d/amneziawg.conf /etc/sysctl.d/99-awg-performance.conf \
                  /etc/x-ui/.awg-module-version /etc/x-ui/.awg-reboot-needed
            update-initramfs -u -k all >/dev/null 2>&1 || update-initramfs -u >/dev/null 2>&1 || true
        fi
        rm -f /etc/x-ui/.lucx-awg-compat-version /var/lib/lucx-ui-pro/awg-readiness.json
        return 0
    fi
    if [[ ! -x "$script" ]]; then
        red "AmneziaWG restore failed: bundled installer is missing or not executable: $script"
        return 1
    fi
    patch_awg_installer || {
        red "AmneziaWG restore failed: could not prepare the bundled installer"
        return 1
    }
    # A restored marker without the actual module would make the upstream
    # installer incorrectly skip DKMS. Remove the marker in that case.
    if ! modinfo amneziawg >/dev/null 2>&1; then
        rm -f /etc/x-ui/.awg-module-version
    fi
    echo "==> Restoring/building AmneziaWG module..."
    local install_rc=0 tool
    # Keep installer diagnostics visible; a failed build must not look successful.
    bash "$script" || install_rc=$?
    if (( install_rc != 0 )); then
        red "AmneziaWG restore failed: installer exited with code ${install_rc}"
        return 1
    fi
    patch_awg_installer
    for script in /usr/bin/x-ui /usr/local/x-ui/x-ui.sh; do
        [[ -f "$script" ]] || continue
        python3 - "$script" <<'PY_BBR_RESTORE_PATCH'
import sys
path=sys.argv[1]
text=open(path,encoding="utf-8",errors="surrogateescape").read()
old="""enable_bbr() {
    if [[ $(sysctl -n net.ipv4.tcp_congestion_control) == "bbr" ]] && [[ $(sysctl -n net.core.default_qdisc) =~ ^(fq|cake)$ ]]; then"""
new="""enable_bbr() {
    modprobe tcp_bbr >/dev/null 2>&1 || true
    modprobe sch_fq >/dev/null 2>&1 || true
    if [[ $(sysctl -n net.ipv4.tcp_congestion_control) == "bbr" ]] && [[ $(sysctl -n net.core.default_qdisc) =~ ^(fq|cake)$ ]]; then"""
if old in text and 'modprobe tcp_bbr >/dev/null 2>&1 || true' not in text:
    text=text.replace(old,new,1)
needle="""        {
            echo "#$(sysctl -n net.core.default_qdisc):$(sysctl -n net.ipv4.tcp_congestion_control)"
            echo "net.core.default_qdisc = fq"""
repl="""        mkdir -p /etc/x-ui
        printf '%s:%s\\n' "$(sysctl -n net.core.default_qdisc)" "$(sysctl -n net.ipv4.tcp_congestion_control)" > /etc/x-ui/.lucx-bbr-restore
        {
            echo "#$(sysctl -n net.core.default_qdisc):$(sysctl -n net.ipv4.tcp_congestion_control)"
            echo "net.core.default_qdisc = fq"""
if needle in text and '/etc/x-ui/.lucx-bbr-restore' not in text:
    text=text.replace(needle,repl,1)
needle2="""        sysctl -w net.ipv4.tcp_congestion_control=\"${old_settings#*:}\"
        rm /etc/sysctl.d/99-bbr-x-ui.conf"""
repl2="""        sysctl -w net.ipv4.tcp_congestion_control=\"${old_settings#*:}\"
        mkdir -p /etc/x-ui
        printf '%s\\n' \"$old_settings\" > /etc/x-ui/.lucx-bbr-restore
        rm /etc/sysctl.d/99-bbr-x-ui.conf"""
if needle2 in text and "printf '%s\\n' \"$old_settings\" > /etc/x-ui/.lucx-bbr-restore" not in text:
    text=text.replace(needle2,repl2,1)
open(path,'w',encoding='utf-8',errors='surrogateescape').write(text)
PY_BBR_RESTORE_PATCH
        chmod +x "$script" 2>/dev/null || true
    done
    if [[ -f /etc/sysctl.d/99-awg-performance.conf ]] && grep -Eq '^[[:space:]]*net\.core\.default_qdisc[[:space:]]*=[[:space:]]*fq[[:space:]]*$|^[[:space:]]*net\.ipv4\.tcp_congestion_control[[:space:]]*=[[:space:]]*bbr[[:space:]]*$' /etc/sysctl.d/99-awg-performance.conf; then
        sed -i -E \
            -e '/^[[:space:]]*net\.core\.default_qdisc[[:space:]]*=[[:space:]]*fq[[:space:]]*$/d' \
            -e '/^[[:space:]]*net\.ipv4\.tcp_congestion_control[[:space:]]*=[[:space:]]*bbr[[:space:]]*$/d' \
            /etc/sysctl.d/99-awg-performance.conf || true
    fi
}

# Validate the complete gzip/tar stream and its member names before extraction.
# The sidecar checksum detects corruption; it is not an authenticity signature.
verify_backup_archive() {
    local file="$1" checksum="${1}.sha256"
    [[ -s "$checksum" ]] || die "Missing archive checksum: $checksum"
    (cd "$(dirname "$file")" && sha256sum -c --status "$(basename "$checksum")") || die "Backup checksum mismatch"
    python3 - "$file" <<'PY_VERIFY_ARCHIVE' || die "Invalid or unsafe backup archive"
import json, pathlib, sys, tarfile
archive = pathlib.Path(sys.argv[1])
allowed = [p.lstrip('/') for p in ['/etc/nginx', '/etc/x-ui', '/etc/default/x-ui', '/etc/fail2ban', '/etc/cron.d', '/usr/local/x-ui', '/usr/bin/x-ui', '/usr/local/lib/3x-ui-pro', '/usr/local/lib/lucx-ui-pro', '/usr/local/sbin/lucx-apply-qdisc', '/usr/local/sbin/lucx-awg-sysctl-guard', '/etc/systemd/system', '/etc/letsencrypt', '/var/lib/letsencrypt', '/var/log/letsencrypt', '/etc/sysctl.conf', '/root/cert', '/var/www/html', '/var/www/diagnostics', '/var/www/subpage', '/var/www/tproxy', '/root/.lucx-tg-web-proxy-info', '/var/lib/lucx-ui-preinstall', '/etc/sysctl.d/99-lucx-ui-forwarding.conf', '/etc/sysctl.d/99-bbr-x-ui.conf', '/etc/sysctl.d/99-awg-performance.conf', '/etc/sysctl.d/99-zz-lucx-ui-tuning.conf', '/etc/modules-load.d/lucx-ui-network.conf', '/etc/modules-load.d/amneziawg.conf', '/etc/modules-load.d/tcp-bbr.conf', '/etc/default/ufw', '/etc/ufw/user.rules', '/etc/ufw/user6.rules', '/etc/ufw/before.rules', '/etc/ufw/before6.rules', '/etc/ipset.conf', '/etc/iptables/ipsets', '/etc/rsyslog.d/10-iptables-scanners.conf', '/etc/logrotate.d/iptables-scanners', '/etc/logrotate.d/nginx', '/etc/systemd/journald.conf.d/99-lucx-ui-pro-logs.conf', '/usr/local/bin/rkn-guard', '/usr/local/bin/rkn', '/usr/local/bin/antiscan-aggregate-logs.sh', '/opt/rkn-guard-manager.sh', '/opt/rkn-guard-manual.list', '/opt/AdGuardHome', '/root/.lucx-adguard-info']]
seen, links = set(), set()
try:
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()  # reads the complete gzip stream and CRC
        assert len(members) <= 200000
        assert any(m.name.lstrip('./') == 'meta.json' for m in members)
        assert any(m.name.lstrip('./') == 'files/etc/x-ui/x-ui.db' for m in members)
        for m in members:
            name = m.name
            parts = pathlib.PurePosixPath(name).parts
            assert not name.startswith('/') and '..' not in parts and '\\' not in name
            norm = name.lstrip('./').rstrip('/')
            assert norm not in seen or norm == ''
            seen.add(norm)
            assert m.isfile() or m.isdir() or m.issym()
            assert m.size <= 16 * 1024**3
            allowed_file = norm.startswith('files/') and any(
                norm[6:] == p or norm[6:].startswith(p + '/') for p in allowed)
            allowed_parent = m.isdir() and norm.startswith('files/') and any(
                p.startswith(norm[6:] + '/') for p in allowed)
            assert norm in ('', 'files', 'meta.json', 'root-crontab', 'cron.d', 'services-state', 'ufw-state') or \
                norm.startswith('cron.d/') or allowed_file or allowed_parent
            if m.issym():
                links.add(norm)
        assert all(not any(n.startswith(link + '/') for link in links) for n in seen)
        meta = tar.extractfile(next(m for m in members if m.name.lstrip('./') == 'meta.json'))
        assert isinstance(json.load(meta), dict)
        print(sum(m.size for m in members))
except (OSError, tarfile.TarError, ValueError, AssertionError, StopIteration) as exc:
    sys.exit(f'Archive validation failed: {exc}')
PY_VERIFY_ARCHIVE
}

# ── restore ───────────────────────────────────────────────────────────────────
run_pro_compat() {
    python3 - "$@" <<'PY_PRO_COMPAT'
#!/usr/bin/env python3
"""Local, idempotent Pro migrations. Never resets panel accounts or ports."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

REVISION = '2026.10.07-286.5'
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
                    rows = [row for row in rows if row['protocol'] in ('qwdtt','csqtt','tproxy','olcrtc','openflux')]
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
        text, count = re.subn(
            r'(?m)^(    url: https://[^/\s]+)/[^/\s]+/\$\{SUB_ID\}\?provider=1$',
            r'\1/__lucx_provider/${SUB_ID}', text)
        if not count and '/__lucx_provider/${SUB_ID}' not in text:
            raise RuntimeError('Unknown Clash provider URL; refusing to guess')
        if not snippet.is_file():
            raise RuntimeError('Clash nginx snippet is missing')
        route = provider_route(settings)
        original = snippet.read_text(encoding='utf-8')
        cleaned = re.sub(r'    # LUCX PRO native provider BEGIN\n.*?    # LUCX PRO native provider END\n',
                         '', original, flags=re.S)
        snippet.write_text(route + cleaned, encoding='utf-8')
        template.write_text(text, encoding='utf-8')
    # Match only the saved panel port; Xray inbound HTTP proxies remain HTTP.
    panel_port = int(settings.get('webPort', 54321))
    scheme = 'https' if settings.get('webCertFile') and settings.get('webKeyFile') else 'http'
    for path in (root / 'etc/nginx/sites-available').glob('*.conf'):
        text = path.read_text(encoding='utf-8')
        repaired = re.sub(r'proxy_pass https?://127\.0\.0\.1:' + str(panel_port) + r'(?=[/;])',
                          f'proxy_pass {scheme}://127.0.0.1:{panel_port}', text)
        if repaired != text:
            path.write_text(repaired, encoding='utf-8')


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
PY_PRO_COMPAT
}

repair_clash_template() {
    [[ -f /var/www/subpage/clash.yaml.tpl ]] || return 0
    python3 <<'PY_CLASH_RESTORE'
from pathlib import Path
import re

path = Path('/var/www/subpage/clash.yaml.tpl')
text = path.read_text(encoding='utf-8')
original = text
expression = '''        - '(select(.type == "vless" and .["reality-opts"] != null) | .["client-fingerprint"]) = "chrome"'\n'''
anchor = '''        - '(select(.type == "vless" and .["reality-opts"] != null) | .["reality-opts"]["support-x25519mlkem768"]) = true'\n'''
if expression not in text:
    if text.count(anchor) != 1:
        raise SystemExit('Unexpected Clash template; no changes made.')
    text = text.replace(anchor, expression + anchor)
text = text.replace('global-client-fingerprint: chrome\n', '')
if '    path: ./proxy_providers/base64.yml\n' in text:
    match = re.search(r'(?m)^    url: https://([^/\s]+)/([^/\s]+)/\$\{SUB_ID\}(?:\?provider=1)?$', text)
    if not match:
        raise SystemExit('Unexpected provider URL; no changes made.')
    domain, sub_path = match.groups()
    text = text.replace('    path: ./proxy_providers/base64.yml\n',
                        f'    path: ./proxy_providers/{domain}_{sub_path}_${{SUB_ID}}.yaml\n')
provider_header = 'proxy-providers:\n  sub:\n    type: http\n'
if provider_header + '    proxy: DIRECT\n' not in text:
    text = text.replace(provider_header, provider_header + '    proxy: DIRECT\n')
if text != original:
    path.write_text(text, encoding='utf-8')
    print('Restored Clash template updated for current Mihomo.')
PY_CLASH_RESTORE
}

repair_panel_clash_route() {
    [[ -f /var/www/subpage/clash.yaml.tpl && -f /usr/local/lib/lucx-ui-pro/clash-sub-server.py ]] || return 0
    python3 <<'PY_PANEL_CLASH_ROUTE'
from pathlib import Path
from contextlib import closing
from urllib.parse import urlsplit
import re
import sqlite3

with closing(sqlite3.connect('file:/etc/x-ui/x-ui.db?mode=ro', uri=True)) as db:
    settings = dict(db.execute('SELECT key, value FROM settings ORDER BY id'))
if settings.get('subClashEnable', 'false') != 'true':
    raise SystemExit(0)
uri = settings.get('subClashURI', '')
prefix = urlsplit(uri).path if uri else settings.get('subClashPath', '/clash/')
prefix = '/' + prefix.strip('/') + '/'
if not re.fullmatch(r'/[A-Za-z0-9_/-]+/', prefix) or '//' in prefix:
    raise SystemExit('Unsupported panel Clash path; route was not changed.')
if prefix == settings.get('subPath') or prefix == settings.get('subJsonPath'):
    raise SystemExit('Panel Clash path conflicts with another subscription path.')
path = Path('/etc/nginx/snippets/includes.conf')
text = path.read_text(encoding='utf-8')
if '/__lucx_clash' not in text:
    raise SystemExit('Clash renderer route is missing; no changes made.')
route = f'''    # Dedicated Clash link displayed by the panel uses the same YAML renderer.
    location ~ ^{prefix}(?<panel_clash_sub_id>[^/]+)/?$ {{
        if ($hack = 1) {{ return 404; }}
        rewrite ^ /__lucx_clash?sub_id=$panel_clash_sub_id last;
    }}
'''
if route not in text:
    # Remove a previously generated alias if the saved panel path has changed.
    text = re.sub(r'    # Dedicated Clash link displayed by the panel uses the same YAML renderer\.\n'
                  r'    location ~ [^\n]+\n'
                  r'        if \(\$hack = 1\) \{ return 404; \}\n'
                  r'        rewrite \^ /__lucx_clash\?sub_id=\$panel_clash_sub_id last;\n'
                  r'    \}\n', '', text)
    path.write_text(route + text, encoding='utf-8')
    print('Restored panel Clash link routed to the shared YAML renderer.')
PY_PANEL_CLASH_ROUTE
}

save_renewal_state() {
    local dir="$1" unit
    mkdir -p "$dir" || return 1
    if command -v crontab >/dev/null 2>&1 && crontab -l > "$dir/root-crontab" 2>/dev/null; then
        touch "$dir/crontab-present" || return 1
    else
        : > "$dir/root-crontab" || return 1
    fi
    for unit in certbot.timer cron; do
        systemctl is-enabled "$unit" > "$dir/$unit-enabled" 2>/dev/null || true
        systemctl is-active "$unit" > "$dir/$unit-active" 2>/dev/null || true
    done
}

restore_renewal_state() {
    local dir="$1" unit enabled active failed=0
    [[ -d "$dir" ]] || return 0
    if command -v crontab >/dev/null 2>&1; then
        if [[ -f "$dir/crontab-present" ]]; then
            crontab "$dir/root-crontab" || failed=1
        else
            if crontab -l >/dev/null 2>&1; then
                crontab -r || failed=1
            fi
        fi
    fi
    for unit in certbot.timer cron; do
        enabled=$(cat "$dir/$unit-enabled" 2>/dev/null)
        active=$(cat "$dir/$unit-active" 2>/dev/null)
        case "$enabled" in
            masked*) systemctl mask --now "$unit" >/dev/null 2>&1 || failed=1 ;;
            *)
                systemctl unmask "$unit" >/dev/null 2>&1 || failed=1
                case "$enabled" in
                    enabled*) systemctl enable "$unit" >/dev/null 2>&1 || failed=1 ;;
                    disabled) systemctl disable "$unit" >/dev/null 2>&1 || failed=1 ;;
                esac
                if [[ "$active" == active ]]; then
                    systemctl start "$unit" >/dev/null 2>&1 || failed=1
                elif [[ "$active" != unknown && "$enabled" != not-found && -n "$enabled" ]]; then
                    systemctl stop "$unit" >/dev/null 2>&1 || failed=1
                fi ;;
        esac
    done
    return "$failed"
}

cmd_restore() {
    require_root

    local backup_file="${1:-}"
    [[ -n "${backup_file}" ]] || die "Usage: $0 restore <backup.tar.gz>"
    [[ -f "${backup_file}" ]]  || die "File not found: ${backup_file}"

    local unpacked_bytes
    unpacked_bytes=$(verify_backup_archive "$backup_file")

    # verify free space for the extracted copy before unpacking
    local unpacked_kb have_kb
    unpacked_kb=$(( (unpacked_bytes + 1023) / 1024 ))
    mkdir -p "${BACKUP_STORE}"
    have_kb=$(avail_kb "${BACKUP_STORE}")
    (( have_kb >= unpacked_kb * 2 + 102400 )) || \
        die "Not enough free space in ${BACKUP_STORE}: need ~$(( (unpacked_kb * 2 + 102400) / 1024 )) MB, have $(( have_kb / 1024 )) MB"

    local staging awg_installed_from_backup=0 awg_restore_failed=0
    staging=$(make_staging restore)
    RESTORE_STAGING="$staging"
    trap 'rm -rf -- "$RESTORE_STAGING"' EXIT

    blue "==> Extracting backup: ${backup_file}"
    tar -xzf "${backup_file}" -C "${staging}"

    if [[ -f "${staging}/meta.json" ]]; then
        blue "==> Backup metadata:"
        cat "${staging}/meta.json"
        awg_installed_from_backup=$(python3 - "${staging}/meta.json" <<'PY_META_AWG'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        print(1 if json.load(f).get('awg_installed') else 0)
except Exception:
    print(0)
PY_META_AWG
)
    fi
    # Backward compatibility: old backups have no awg_installed field. Infer
    # installation from the persisted AWG module marker/config when present.
    if [[ "$awg_installed_from_backup" != "1" ]]; then
        [[ -e "${staging}/files/etc/modules-load.d/amneziawg.conf" || -e "${staging}/files/etc/x-ui/.awg-module-version" ]] && awg_installed_from_backup=1
    fi
    echo

    # ── install packages ──────────────────────────────────────────────────
    blue "==> Installing packages..."
    apt-get update -qq
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get install -y ${PACKAGES}

    # ── stop running services ─────────────────────────────────────────────
    blue "==> Stopping services..."
    for svc in lucx-awg-readiness nginx x-ui lucx-clash-sub mtr-backend AdGuardHome lucx-apply-qdisc lucx-qdisc-sync lucx-qdisc-sync.path lucx-awg-sysctl-guard lucx-awg-sysctl-guard.path; do
        systemctl stop "${svc}" 2>/dev/null || true
    done

    # A restore replaces the generated cover, including its old page directory.
    # Clean only owned files; old backups without a generator record still work.
    if [[ -f /var/lib/lucx-ui-preinstall/cover-generator.json ]]; then
        [[ -f /usr/local/lib/lucx-ui-pro/cover-generator/generator.py ]] || {
            red "Cover cleanup helper missing; restore stopped."
            return 1
        }
        python3 /usr/local/lib/lucx-ui-pro/cover-generator/generator.py cleanup || return 1
    fi

    # ── restore files ─────────────────────────────────────────────────────
    blue "==> Restoring files..."
    if [[ -d "${staging}/files" ]]; then
        # Save certificates from the archive separately. A restore on the
        # same VPS must not replace certificates renewed since the backup.
        mkdir -p "${staging}/saved-certs/etc" "${staging}/saved-certs/var/lib" \
                 "${staging}/saved-certs/var/log" "${staging}/saved-certs/root"
        local cert_path
        for cert_path in etc/letsencrypt var/lib/letsencrypt var/log/letsencrypt root/cert; do
            if [[ -e "${staging}/files/${cert_path}" ]]; then
                mv "${staging}/files/${cert_path}" "${staging}/saved-certs/${cert_path}"
            fi
        done
        # The staging tree is created under umask 077. Copying files/. as a
        # whole would also apply its 0700 parent-directory modes to /etc,
        # /var, /usr and /var/www. Restore only the paths actually backed up.
        local path src parent unit
        for path in "${BACKUP_PATHS[@]}"; do
            src="${staging}/files${path}"
            [[ -e "$src" || -L "$src" ]] || continue
            parent=$(dirname "$path")
            [[ -d "$parent" ]] || install -d -m 0755 "$parent"
            if [[ -d "$src" && ! -L "$src" ]]; then
                [[ -d "$path" ]] || install -d -m 0755 "$path"
                cp -a "$src/." "$path/"
                chown --reference="$src" "$path"
                chmod --reference="$src" "$path"
            else
                cp -a --remove-destination "$src" "$path"
            fi
        done
        # Unit files are collected separately from BACKUP_PATHS.
        [[ -d /etc/systemd/system ]] || install -d -m 0755 /etc/systemd/system
        for unit in "${SYSTEMD_UNITS[@]}"; do
            src="${staging}/files/etc/systemd/system/${unit}"
            if [[ -f "$src" ]]; then
                cp -a --remove-destination "$src" "/etc/systemd/system/${unit}"
            fi
        done
        # Fill missing certificate entries without changing existing renewed
        # certificates or the modes of their parent directories.
        local cert_dest cert_mode cert_owner existed
        for cert_path in etc/letsencrypt var/lib/letsencrypt var/log/letsencrypt root/cert; do
            src="${staging}/saved-certs/${cert_path}"
            [[ -d "$src" ]] || continue
            cert_dest="/${cert_path}"
            parent=$(dirname "$cert_dest")
            [[ -d "$parent" ]] || install -d -m 0755 "$parent"
            existed=0
            if [[ -d "$cert_dest" ]]; then
                existed=1
                cert_mode=$(stat -c '%a' "$cert_dest")
                cert_owner=$(stat -c '%u:%g' "$cert_dest")
            else
                install -d -m 0700 "$cert_dest"
            fi
            cp -an "$src/." "$cert_dest/"
            if (( existed )); then
                chown "$cert_owner" "$cert_dest"
                chmod "$cert_mode" "$cert_dest"
            else
                chown --reference="$src" "$cert_dest"
                chmod --reference="$src" "$cert_dest"
            fi
        done
    fi

    # ── permissions ───────────────────────────────────────────────────────
    chown -R www-data:www-data /var/www/html        2>/dev/null || true
    chown -R www-data:www-data /var/www/diagnostics 2>/dev/null || true
    chown -R www-data:www-data /var/www/subpage     2>/dev/null || true
    [[ -f /usr/local/x-ui/x-ui ]] && chmod +x /usr/local/x-ui/x-ui
    [[ -f /usr/bin/x-ui ]]        && chmod +x /usr/bin/x-ui
    [[ -f /usr/local/bin/rkn-guard ]] && chmod +x /usr/local/bin/rkn-guard
    [[ -f /usr/local/bin/rkn ]]       && chmod +x /usr/local/bin/rkn
    [[ -f /usr/local/bin/antiscan-aggregate-logs.sh ]] && chmod +x /usr/local/bin/antiscan-aggregate-logs.sh
    [[ -f /opt/rkn-guard-manager.sh ]] && chmod +x /opt/rkn-guard-manager.sh
    [[ -f /usr/local/sbin/lucx-apply-qdisc ]] && chmod +x /usr/local/sbin/lucx-apply-qdisc
    find /usr/local/lib/3x-ui-pro -name "*.py" -exec chmod +x {} \; 2>/dev/null || true
    find /usr/local/lib/lucx-ui-pro -name "*.sh" -exec chmod +x {} \; 2>/dev/null || true

    # Restore the archived site exactly; never regenerate or download a cover.
    if [[ -f /var/lib/lucx-ui-preinstall/cover-generator.json ]]; then
        [[ -f /usr/local/lib/lucx-ui-pro/cover-generator/generator.py ]] &&
            python3 /usr/local/lib/lucx-ui-pro/cover-generator/generator.py check || {
                red "Restored cover files failed integrity verification."
                return 1
            }
    fi

    # Reconcile Fail2ban through the panel CLI. This keeps the restored jail/filter/action
    # format aligned with the x-ui version instead of treating copied files as authoritative.
    setup_fail2ban || true
    install_awg_sysctl_guard || return 1
    sanitize_awg_sysctl_file
    repair_clash_template
    repair_panel_clash_route
    run_pro_compat apply

    # ── panel cert symlinks (/root/cert/<domain> → letsencrypt) ──────────
    # Backups made before /root/cert was in BACKUP_PATHS lack the symlinks
    # the panel's webCertFile points to — without them x-ui serves plain
    # HTTP and every nginx proxy_pass https:// (panel) breaks.
    # Recreate them from the restored DB ("-e" follows symlinks, so a
    # dangling link reads as missing).
    local db=/etc/x-ui/x-ui.db web_cert cert_domain
    if [[ -f "${db}" ]] && command -v sqlite3 &>/dev/null; then
        web_cert=$(sqlite3 "${db}" "SELECT value FROM settings WHERE key='webCertFile';" 2>/dev/null || true)
        if [[ "${web_cert}" =~ ^/root/cert/([^/]+)/ ]]; then
            cert_domain="${BASH_REMATCH[1]}"
            if [[ ! -e "${web_cert}" && -d "/etc/letsencrypt/live/${cert_domain}" ]]; then
                blue "==> Recreating panel cert symlinks in /root/cert/${cert_domain}..."
                mkdir -p "/root/cert/${cert_domain}"
                chmod 755 /root/cert/* 2>/dev/null || true
                [[ -e "/root/cert/${cert_domain}/fullchain.pem" || -L "/root/cert/${cert_domain}/fullchain.pem" ]] || \
                    ln -s "/etc/letsencrypt/live/${cert_domain}/fullchain.pem" "/root/cert/${cert_domain}/fullchain.pem"
                [[ -e "/root/cert/${cert_domain}/privkey.pem" || -L "/root/cert/${cert_domain}/privkey.pem" ]] || \
                    ln -s "/etc/letsencrypt/live/${cert_domain}/privkey.pem" "/root/cert/${cert_domain}/privkey.pem"
            fi
        fi
    fi

    # Recreate Telegram WEB-proxy certificate links from its inbound settings.
    if [[ -f "${db}" ]] && command -v sqlite3 &>/dev/null; then
        while IFS= read -r cert_domain; do
            [[ -n "${cert_domain}" && -d "/etc/letsencrypt/live/${cert_domain}" ]] || continue
            mkdir -p "/root/cert/${cert_domain}"
            [[ -e "/root/cert/${cert_domain}/fullchain.pem" || -L "/root/cert/${cert_domain}/fullchain.pem" ]] || \
                ln -s "/etc/letsencrypt/live/${cert_domain}/fullchain.pem" "/root/cert/${cert_domain}/fullchain.pem"
            [[ -e "/root/cert/${cert_domain}/privkey.pem" || -L "/root/cert/${cert_domain}/privkey.pem" ]] || \
                ln -s "/etc/letsencrypt/live/${cert_domain}/privkey.pem" "/root/cert/${cert_domain}/privkey.pem"
        done < <(python3 - "${db}" <<'PY_TG_RESTORE'
import json, sqlite3, sys
try:
    con = sqlite3.connect(sys.argv[1], timeout=10)
    rows = con.execute("SELECT id, settings FROM inbounds WHERE protocol='tproxy' OR tag='inbound-tproxy'").fetchall()
    for inbound_id, raw in rows:
        value = json.loads(raw or "{}")
        value["siteSource"] = "dir"
        value["siteDir"] = "/var/www/html"
        con.execute("UPDATE inbounds SET settings=? WHERE id=?", (json.dumps(value, ensure_ascii=False), inbound_id))
        host = value.get("hostname", "")
        if host:
            print(host)
    con.commit()
    con.close()
except Exception:
    pass
PY_TG_RESTORE
)
    fi

    # ── recreate mtr-backend system user if missing ───────────────────────
    id mtr-backend &>/dev/null || \
        useradd --system --no-create-home --shell /usr/sbin/nologin mtr-backend

    # grant mtr net_raw capability
    if command -v setcap &>/dev/null && command -v mtr &>/dev/null; then
        setcap cap_net_raw+ep "$(command -v mtr)" 2>/dev/null || true
    fi

    # ── AmneziaWG / AWG-specific sysctl ──────────────────────────────────
    if ! restore_awg_module "${awg_installed_from_backup}"; then
        awg_restore_failed=1
        red "AmneziaWG was not restored. Continuing to restore the other services."
    fi

    # ── network tuning / modules ─────────────────────────────────────────
    # The panel owns persistent BBR/FQ sysctl state in 99-bbr-x-ui.conf.
    # Pro owns kernel-module loading/persistence. Load restored modules before
    # applying the panel sysctl file so BBR/fq can be activated after reboot.
    if [[ -f /etc/modules-load.d/lucx-ui-network.conf ]]; then
        while IFS= read -r module; do
            [[ -n "$module" && "$module" != \#* ]] || continue
            modprobe "$module" >/dev/null 2>&1 || true
        done < /etc/modules-load.d/lucx-ui-network.conf
    fi
    # Compatibility with backups created by older Pro releases. Migrate the
    # old module filename into the current combined module file when possible.
    if [[ -f /etc/modules-load.d/tcp-bbr.conf ]]; then
        modprobe tcp_bbr >/dev/null 2>&1 || true
        if [[ ! -f /etc/modules-load.d/lucx-ui-network.conf ]]; then
            if modprobe sch_fq >/dev/null 2>&1; then
                printf '%s\n' tcp_bbr sch_fq > /etc/modules-load.d/lucx-ui-network.conf
            else
                printf '%s\n' tcp_bbr > /etc/modules-load.d/lucx-ui-network.conf
            fi
            chmod 0644 /etc/modules-load.d/lucx-ui-network.conf
        fi
    fi

    restore_unit_state() {
        local unit="$1" saved_en saved_active
        if [[ -f "${staging}/services-state" ]]; then
            if ! read -r saved_en saved_active < <(awk -v u="$unit" -v base="${unit%.service}" \
                '$1==u || $1==base {print $2, $3; exit}' "${staging}/services-state"); then
                # Older archives may omit a service they still contain.
                systemctl enable "$unit" 2>/dev/null || true
                systemctl start "$unit" 2>/dev/null || true
                return 0
            fi
            [[ "$saved_en" == 1 ]] && systemctl enable "$unit" 2>/dev/null || systemctl disable "$unit" 2>/dev/null || true
            [[ "$saved_active" == 1 ]] && systemctl start "$unit" 2>/dev/null || systemctl stop "$unit" 2>/dev/null || true
        else
            systemctl enable "$unit" 2>/dev/null || true
            systemctl start "$unit" 2>/dev/null || true
        fi
    }

    # A oneshot ipset service may have been inactive at backup time although
    # its sets existed in RAM. UFW still needs those sets after a fresh restore.
    local ipset_archive
    for ipset_archive in /etc/ipset.conf /etc/iptables/ipsets; do
        [[ -s "$ipset_archive" && -f "$ipset_archive" ]] || continue
        ipset restore -exist -f "$ipset_archive" || die "Failed to restore firewall ipsets"
    done

    # ── systemd ───────────────────────────────────────────────────────────
    blue "==> Enabling and starting services..."
    systemctl daemon-reload

    for svc in x-ui mtr-backend AdGuardHome lucx-apply-qdisc; do
        restore_unit_state "$svc"
    done
    if [[ -f /etc/systemd/system/lucx-clash-sub.service &&
          -f /usr/local/lib/lucx-ui-pro/clash-sub-server.py &&
          -f /var/www/subpage/clash.yaml.tpl ]]; then
        restore_unit_state lucx-clash-sub.service
    fi
    if [[ -f /etc/systemd/system/lucx-qdisc-sync.path ]]; then
        restore_unit_state lucx-qdisc-sync.path
    fi
    if [[ -f /etc/systemd/system/lucx-awg-sysctl-guard.path ]]; then
        restore_unit_state lucx-awg-sysctl-guard.path
    fi

    # Restore rkn-guard runtime units and LucX automatic-update timers when present.
    if [[ -x /usr/local/bin/rkn-guard ]]; then
        for unit in antiscan-ipset-restore.service antiscan-move-rules.service antiscan-aggregate.timer; do
            [[ -f "/etc/systemd/system/${unit}" ]] || continue
            restore_unit_state "$unit"
        done
        for timer in rkn-guard-list-update.timer rkn-guard-self-update.timer; do
            [[ -f "/etc/systemd/system/${timer}" ]] || continue
            restore_unit_state "$timer"
            if systemctl is-active --quiet "$timer"; then systemctl restart "$timer" || die "Failed to reschedule $timer"; fi
        done
    fi

    # nginx: test config before starting
    if nginx -t 2>/dev/null; then
        restore_unit_state nginx
        green "    nginx state restored"
    else
        red "    nginx config test failed — fix manually:"
        nginx -t
    fi

    # ── crontab ───────────────────────────────────────────────────────────
    blue "==> Restoring cron..."
    save_renewal_state "${staging}/renewal-before" || die "Failed to snapshot renewal scheduler"
    if [[ -f "${staging}/root-crontab" ]]; then
        if ! crontab "${staging}/root-crontab"; then
            restore_renewal_state "${staging}/renewal-before" || true
            die "Failed to restore root crontab; previous scheduler restored where possible"
        fi
        green "    Root crontab restored"
    fi

    if [[ -d "${staging}/cron.d" ]]; then
        cp -a "${staging}/cron.d/." /etc/cron.d/
        green "    /etc/cron.d restored"
    fi

    # Restored Pro cron owns standalone renewal and the nginx/panel hooks.
    if crontab -l 2>/dev/null | grep -F 'certbot renew' | grep -Fq -- '--pre-hook "systemctl stop nginx"'; then
        if ! { systemctl enable --now cron && systemctl mask --now certbot.timer; }; then
            restore_renewal_state "${staging}/renewal-before" || true
            die "Failed to configure renewal scheduler; previous state restored where possible"
        fi
    fi

    # Older archives contain a second RUNET scheduler. Keep the restored .dat
    # files and Xray geodata configuration; retire only the legacy cron job.
    if command -v crontab &>/dev/null; then
        local geo_cron
        geo_cron=$(mktemp "${staging}/geodata-cron.XXXXXX")
        crontab -l > "$geo_cron" 2>/dev/null || true
        if grep -Eq '^[[:space:]]*[^#].*/usr/local/x-ui/update-geodata\.sh([[:space:];]|$)' "$geo_cron"; then
            awk '/^[[:space:]]*#/ || $0 !~ /\/usr\/local\/x-ui\/update-geodata\.sh([[:space:];]|$)/' "$geo_cron" > "${geo_cron}.new"
            crontab "${geo_cron}.new" || { restore_renewal_state "${staging}/renewal-before" || true; die "Failed to remove legacy RUNET cron"; }
        fi
        rm -f "$geo_cron" "${geo_cron}.new"
    fi
    rm -f /usr/local/x-ui/update-geodata.sh

    # ── UFW ───────────────────────────────────────────────────────────────
    blue "==> Restoring UFW..."
    # Pro forwarding override was retired by run_pro_compat; panel owns it.
    if [[ -f /etc/sysctl.d/99-zz-lucx-ui-tuning.conf ]]; then
        # Backups from older Pro releases may contain BBR/FQ keys here. If the
        # panel-owned file is absent, migrate an old BBR+fq state into it before
        # stripping those keys from the legacy Pro file.
        if [[ ! -f /etc/sysctl.d/99-bbr-x-ui.conf ]]; then
            legacy_cc=$(awk -F= '$1 ~ /^[[:space:]]*net\.ipv4\.tcp_congestion_control[[:space:]]*$/ {gsub(/[[:space:]]/, "", $2); print $2; exit}' /etc/sysctl.d/99-zz-lucx-ui-tuning.conf || true)
            legacy_qdisc=$(awk -F= '$1 ~ /^[[:space:]]*net\.core\.default_qdisc[[:space:]]*$/ {gsub(/[[:space:]]/, "", $2); print $2; exit}' /etc/sysctl.d/99-zz-lucx-ui-tuning.conf || true)
            if [[ "$legacy_cc" == "bbr" && "$legacy_qdisc" == "fq" ]]; then
                {
                    echo "#fq_codel:cubic"
                    echo "net.core.default_qdisc = fq"
                    echo "net.ipv4.tcp_congestion_control = bbr"
                } > /etc/sysctl.d/99-bbr-x-ui.conf
                chmod 0644 /etc/sysctl.d/99-bbr-x-ui.conf
            fi
        fi
        # Keep only non-BBR tuning so the panel remains the sole owner of those keys.
        sed -i -E \
            -e '/^[[:space:]]*net\.core\.default_qdisc[[:space:]]*=/d' \
            -e '/^[[:space:]]*net\.ipv4\.tcp_congestion_control[[:space:]]*=/d' \
            /etc/sysctl.d/99-zz-lucx-ui-tuning.conf || true
        sysctl -p /etc/sysctl.d/99-zz-lucx-ui-tuning.conf >/dev/null 2>&1 || true
    fi
    if [[ -f /etc/sysctl.d/99-bbr-x-ui.conf ]]; then
        # Use the panel-owned file exactly as the panel does.
        sysctl -p /etc/sysctl.d/99-bbr-x-ui.conf >/dev/null 2>&1 || true
    fi
    modprobe sch_fq_codel >/dev/null 2>&1 || true
    persist_current_qdisc_module
    # The qdisc helper must see the restored panel-owned default_qdisc, not the
    # pre-restore runtime value. Re-run it after all sysctl files are applied.
    if [[ -f /etc/systemd/system/lucx-apply-qdisc.service ]]; then
        systemctl restart lucx-apply-qdisc 2>/dev/null || true
    fi
    # Restore saved UFW policy without changing its routed-traffic default.
    if [[ -f "${staging}/ufw-state" ]] && grep -qx inactive "${staging}/ufw-state"; then
        ufw --force disable || die "Failed to restore inactive UFW state"
    else
        ufw --force enable || die "Failed to enable restored UFW policy"
        # enable is a no-op when UFW is already active. Load the restored files
        # now, so runtime ports/routed policy match the backup before reporting success.
        ufw reload || die "Failed to apply restored UFW rules"
    fi
    green "    UFW state and runtime rules restored"
    if systemctl is-active --quiet x-ui; then
        systemctl restart x-ui || die "Panel failed to reapply tunnel firewall after UFW restore"
    fi

    local required_service saved_active
    for required_service in x-ui nginx lucx-clash-sub AdGuardHome; do
        saved_active=$(awk -v u="$required_service" '$1==u {print $3; exit}' "${staging}/services-state" 2>/dev/null || true)
        if [[ "$saved_active" == 1 ]] && ! systemctl is-active --quiet "$required_service"; then
            die "Restored service is inactive: ${required_service}"
        fi
    done
    if [[ -f /var/www/subpage/clash.yaml.tpl ]] &&
       ! runuser -u www-data -- test -r /var/www/subpage/clash.yaml.tpl; then
        die "Clash subscription template is unreadable by www-data"
    fi
    if [[ -f /etc/systemd/system/lucx-clash-sub.service &&
          -f /usr/local/lib/lucx-ui-pro/clash-sub-server.py &&
          -f /var/www/subpage/clash.yaml.tpl ]] &&
       ! systemctl is-active --quiet lucx-clash-sub; then
        die "Clash subscription service is inactive after restore"
    fi

    # Also migrate old backups that predate bounded log retention.
    run_log_policy install
    echo
    if (( awg_restore_failed )); then
        die "Restore incomplete: AmneziaWG could not be restored. Other components were restored; see the errors above."
    fi
    green "==> Restore complete."
    green "    Check status with:"
    green "      systemctl status x-ui nginx"
}

# ── list ──────────────────────────────────────────────────────────────────────
cmd_list() {
    if [[ ! -d "${BACKUP_STORE}" ]]; then
        echo "No backups found (${BACKUP_STORE} does not exist)"
        return
    fi

    local archives
    mapfile -t archives < <(ls -t "${BACKUP_STORE}"/*.tar.gz 2>/dev/null)

    if [[ ${#archives[@]} -eq 0 ]]; then
        echo "No backups in ${BACKUP_STORE}"
        return
    fi

    blue "Backups in ${BACKUP_STORE}:"
    for f in "${archives[@]}"; do
        printf "  %-55s  %s\n" "$(basename "${f}")" "$(du -sh "${f}" | cut -f1)"
    done
}

# ── entry point ───────────────────────────────────────────────────────────────
case "${1:-}" in
    backup)  cmd_backup ;;
    restore) cmd_restore "${2:-}" ;;
    list)    cmd_list ;;
    *)
        cat <<EOF
Usage: $(basename "$0") {backup|restore <file>|list}

  backup            create timestamped backup in ${BACKUP_STORE}/
  restore <file>    restore from backup archive (installs packages first)
  list              list available backups

What is backed up:
  /etc/nginx                      nginx config
  /etc/x-ui                       panel DB + config
  /usr/local/x-ui                 panel binary + xray core
  /usr/bin/x-ui                   x-ui management CLI
  /usr/local/lib/3x-ui-pro        optional helper scripts
  /usr/local/lib/lucx-ui-pro      cover generator and automatic-update helpers
  /usr/local/sbin/lucx-apply-qdisc  network qdisc helper
  /etc/letsencrypt                SSL certificates
  /root/cert                      panel cert symlinks
  /var/www/{html,diagnostics,subpage}  web content (shared cover in html)
  Telegram WEB-proxy domain, certificate and inbound (inside x-ui DB)
  /opt/AdGuardHome                self-hosted DoH (if installed)
  rkn-guard binary, manager, ipset/UFW state and update timers
  /var/lib/lucx-ui-preinstall     pre-install firewall snapshot and auto-domain selection
  legacy Pro forwarding file is archived and retired on restore (panel owns forwarding)
  relevant services and timers from /etc/systemd/system
  /etc/default/ufw + /etc/ufw/{user,before}*.rules  firewall policy/rules
  root crontab + /etc/cron.d/
EOF
        exit 1
        ;;
esac
