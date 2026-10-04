#!/usr/bin/env bash
# lucx-ui-backup.sh — backup and restore LucX-UI panel + nginx + certs
# Usage:
#   lucx-ui-backup.sh backup           — create timestamped backup
#   lucx-ui-backup.sh restore <file>   — restore from backup archive
#   lucx-ui-backup.sh list             — list available backups
set -Eeuo pipefail
umask 077

BACKUP_STORE="/var/backups/x-ui"
PACKAGES="ca-certificates curl wget jq bash sudo nginx-full certbot python3-certbot-nginx sqlite3 ufw netcat-openbsd mtr python3 libcap2-bin cron iproute2 ipset iptables rsyslog whois fail2ban nftables"

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
    lucx-awg-readiness.service
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
install_awg_compat_support() {
    install -d -m 0755 /usr/local/lib/lucx-ui-pro
    cat > /usr/local/lib/lucx-ui-pro/awg-compat.py <<'PY_LUCX_AWG_COMPAT'
#!/usr/bin/env python3
"""Patch the bundled LucX installer; retain upstream repositories, pins and DKMS."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

REV = 'upstream-udp-280-1'
MARKER = Path('/etc/x-ui/.lucx-awg-compat-version')
REPORT = Path('/var/lib/lucx-ui-pro/awg-readiness.json')
SELF = '/usr/local/lib/lucx-ui-pro/awg-compat.py'

# Exact upstream function from LucX 279/280. Used only to retire old Pro
# wrappers in restored/previously patched installers; fresh stock retains it.
# Copyright (c) 2025 LucX-UI Project; PolyForm Noncommercial 1.0.0.
UPSTREAM_UDP_FUNCTION = r'''apply_udp_tunnel_abi_compat() {
    local f="${1:-compat/compat.h}"
    if grep -qF 'wg_setup_udp_tunnel_sock' "$f" 2>/dev/null; then
        echo -e "${GREEN}udp_tunnel ABI wrappers already in tree — skip.${NC}"
        return 0
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        echo -e "${YELLOW}python3 нет — патч udp_tunnel ABI пропущен (ядра с backport-ABI не соберутся).${NC}"
        return 0
    fi
    echo -e "${YELLOW}Патч udp_tunnel ABI (детект сигнатуры вместо версии)...${NC}"
    python3 - "$f" <<'PY'
import sys
path = sys.argv[1]
text = open(path, encoding="utf-8", errors="surrogateescape").read()
needle = """\
#if LINUX_VERSION_CODE < KERNEL_VERSION(7, 1, 5)
#include <net/udp_tunnel.h>
#define setup_udp_tunnel_sock(net, sk, sock_cfg) setup_udp_tunnel_sock(net, sk->sk_socket, sock_cfg)
#define udp_tunnel_sock_release(sk) udp_tunnel_sock_release(sk->sk_socket)
#endif
"""
dispatch = """\
/*
 * Linux 7.1.5 changed udp_tunnel_sock_release()/setup_udp_tunnel_sock() from
 * struct socket * to struct sock *, but distros backport the new ABI below
 * that version (Ubuntu generic 7.0.0-38, Debian 13 7.1.7+deb13), so
 * LINUX_VERSION_CODE cannot detect it. Probe the real signature at compile
 * time instead; call sites in socket.c already pass struct sock *.
 * From amneziawg-linux-kernel-module PR #218, adapted. LucX-UI patch.
 */
#if LINUX_VERSION_CODE < KERNEL_VERSION(7, 1, 5)
#include <net/udp_tunnel.h>

static inline void wg_udp_tunnel_sock_release(struct sock *sk)
{
	if (__builtin_types_compatible_p(typeof(&udp_tunnel_sock_release), void (*)(struct sock *)))
		((void (*)(struct sock *))udp_tunnel_sock_release)(sk);
	else
		((void (*)(struct socket *))udp_tunnel_sock_release)(sk->sk_socket);
}

static inline void wg_setup_udp_tunnel_sock(struct net *net, struct sock *sk,
					    struct udp_tunnel_sock_cfg *cfg)
{
	if (__builtin_types_compatible_p(typeof(&setup_udp_tunnel_sock),
					 void (*)(struct net *, struct sock *, struct udp_tunnel_sock_cfg *)))
		((void (*)(struct net *, struct sock *, struct udp_tunnel_sock_cfg *))setup_udp_tunnel_sock)(net, sk, cfg);
	else
		((void (*)(struct net *, struct socket *, struct udp_tunnel_sock_cfg *))setup_udp_tunnel_sock)(net, sk->sk_socket, cfg);
}

/* Macros come AFTER the wrapper bodies: inside a wrapper the raw symbol
 * must still resolve to the real kernel function, not to itself. */
#define setup_udp_tunnel_sock(net, sk, sock_cfg) wg_setup_udp_tunnel_sock(net, sk, sock_cfg)
#define udp_tunnel_sock_release(sk) wg_udp_tunnel_sock_release(sk)
#endif
"""
if needle not in text:
    sys.stderr.write("udp_tunnel ABI: version-gated block not found in compat.h\n")
    sys.exit(1)
open(path, "w", encoding="utf-8", errors="surrogateescape").write(text.replace(needle, dispatch, 1))
PY
}'''

def patch_source(directory):
    """Retain upstream ABI patches; stamp DKMS builds with the actual version."""
    src = Path(directory)
    dkms = src / 'dkms.conf'
    data = dkms.read_text()
    make_line = 'MAKE[0]="make KERNELRELEASE=${kernelver} WIREGUARD_VERSION=${PACKAGE_VERSION}"\n'
    if make_line in data:
        return
    if re.search(r'(?m)^MAKE\[', data):
        raise RuntimeError('Unknown DKMS MAKE override; sources not changed')
    # No edits to compat.h/Kbuild/socket.c: upstream owns all kernel ABI fixes.
    dkms.write_text(data + '\n' + make_line)


def run(*args, timeout=30):
    try:
        return subprocess.run(args, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 127, '', str(exc))

def kernels():
    # Check all installed kernel images, including kernels lacking headers.
    targets = {p.name[8:] for p in Path('/boot').glob('vmlinuz-*') if p.is_file()}
    targets.add(os.uname().release)
    for p in Path('/lib/modules').glob('*'):
        if (p / 'build').exists():
            targets.add(p.name)
    return sorted(targets)

def archived_without_headers(kernel, current, next_kernel=None):
    # Retained old boot images may have no installable headers. They must not
    # invalidate the current module or force endless rebuilds. Explicit boot
    # targets, current kernels and kernels with headers remain mandatory.
    if kernel in (current, next_kernel) or (Path('/lib/modules') / kernel / 'build').exists():
        return False
    if not re.match(r'^\d', kernel) or not re.match(r'^\d', current):
        return False
    return run('dpkg', '--compare-versions', kernel, 'lt', current).returncode == 0


def needs_rebuild():
    if not MARKER.is_file() or MARKER.read_text().strip() != REV:
        return True
    current = os.uname().release
    return any('-lucxpro280' not in run('modinfo', '-k', k, '-F', 'version', 'amneziawg').stdout
               for k in kernels() if not archived_without_headers(k, current))

def patch_installer(file):
    path = Path(file)
    text = path.read_text(encoding='utf-8')
    marker = '# LUCX AWG installer upstream-udp-280-1'
    if marker in text:
        if UPSTREAM_UDP_FUNCTION not in text or '    python3 ' + SELF + ' patch-source "$PWD" || exit 1\n' not in text:
            raise RuntimeError('Incomplete Pro DKMS identity patch')
        return
    legacy = bool(re.search(r'# LUCX UDP installer udp-api-[12]', text))
    start = text.find('apply_udp_tunnel_abi_compat() {')
    if start < 0:
        raise RuntimeError('Unsupported LucX installer; no changes made')
    if legacy:
        end = text.find('\n}\n', start)
        if end < 0 or ' patch-source "$PWD"' not in text[start:end]:
            raise RuntimeError('Unknown legacy Pro UDP wrapper; no changes made')
        # Old Pro replaced the upstream function; restore the exact 279/280 fix.
        text = text[:start] + UPSTREAM_UDP_FUNCTION + text[end + len('\n}'):]
        text = re.sub(r'# LUCX UDP installer udp-api-[12]\nif \[\[.*?\nfi\n(?=# Skip DKMS/kernel when the installed module SHA)',
                      '', text, flags=re.S)
        text = re.sub(r'(?m)^    MOD_VER="\$\{MOD_VER\}-lucxudp[12]"\n', '', text)
    else:
        end = text.find('\nPY\n}\n', start)
        block = text[start:end] if end >= 0 else ''
        if '__builtin_types_compatible_p' not in block or 'wg_setup_udp_tunnel_sock' not in block:
            raise RuntimeError('Installer lacks the upstream UDP signature fix; no changes made')
    gate = '# Skip DKMS/kernel when the installed module SHA'
    pattern = r'    apply_udp_tunnel_abi_compat (?:socket\.c|compat/compat\.h) \|\| (?:' + re.escape(chr(92)) + r'\n[^\n]*|exit 1)\n'
    if text.count(gate) != 1 or len(list(re.finditer(pattern, text))) != 1:
        raise RuntimeError('Unsupported LucX installer layout; no changes made')
    text = re.sub(pattern,
                  '    MOD_VER="${MOD_VER}-lucxpro280"\n'
                  f'    python3 {SELF} patch-source "$PWD" || exit 1\n'
                  '    apply_udp_tunnel_abi_compat compat/compat.h || exit 1\n', text, count=1)
    text = text.replace(gate, f'''{marker}
if [[ "$DO_UNINSTALL" -ne 1 ]]; then
    if python3 {SELF} needs-rebuild; then FORCE_REBUILD=1; fi
    trap 'lucx_rc=$?; lucx_ready_rc=0; python3 {SELF} ready --installed --installer-exit "$lucx_rc" || lucx_ready_rc=$?; if [[ "$lucx_rc" -eq 0 ]]; then lucx_rc=$lucx_ready_rc; fi; exit "$lucx_rc"' EXIT
fi
{gate}''', 1)
    uninstall = 'if [[ $DO_UNINSTALL -eq 1 ]]; then\n'
    if text.count(uninstall) != 1:
        raise RuntimeError('Unsupported uninstall branch; no changes made')
    if not legacy:
        text = text.replace(uninstall, uninstall +
                            f"    trap 'lucx_rc=$?; if [[ \"$lucx_rc\" -eq 0 ]]; then python3 {SELF} cleanup; fi; exit \"$lucx_rc\"' EXIT\n", 1)
    path.write_text(text, encoding='utf-8')


def ready(installed=False, next_kernel=None, installer_exit=None):
    current = os.uname().release
    targets = set(kernels())
    if next_kernel:
        if not re.fullmatch(r'[0-9][A-Za-z0-9._+-]{0,127}', next_kernel):
            raise ValueError('Invalid next kernel release')
        targets.add(next_kernel)
    results = {k: '-lucxpro280' in run('modinfo', '-k', k, '-F', 'version', 'amneziawg').stdout for k in sorted(targets)}
    archived = [k for k, ok in results.items() if not ok and archived_without_headers(k, current, next_kernel)]
    required = {k: ok for k, ok in results.items() if k not in archived}
    tools = all(shutil.which(t) for t in ('awg', 'awg-quick', 'ip'))
    loaded = False
    interface = False
    failures = []
    present = any(results.values()) or Path('/etc/x-ui/.awg-module-version').exists() or REPORT.is_file()
    if not present and not installed:
        print('AWG: not installed; readiness check skipped.')
        return 0
    for k, ok in results.items():
        print(f'AWG patched module [{k}]: {"INSTALLED" if ok else "MISSING"}')
    for k in archived:
        print(f'AWG older kernel [{k}]: no headers/module; excluded from current readiness. Use --next-kernel if selected for boot.')
    if run('modinfo', '-k', current, 'amneziawg').returncode == 0:
        load = run('modprobe', 'amneziawg')
        loaded = load.returncode == 0
        if not loaded:
            failures.append('modprobe: ' + load.stderr.strip())
    if loaded and tools:
        ns = f'lucx-awg-check-{os.getpid()}'
        created = False
        try:
            create = run('ip', 'netns', 'add', ns)
            created = create.returncode == 0
            if not created:
                failures.append('network namespace: ' + create.stderr.strip())
            else:
                with tempfile.TemporaryDirectory(prefix='lucx-awg-check-', dir='/run') as tmp:
                    conf = Path(tmp) / 'lucxawgtest.conf'
                    key = run('awg', 'genkey')
                    if key.returncode:
                        failures.append('awg genkey failed')
                    else:
                        conf.write_text('[Interface]\nPrivateKey = ' + key.stdout.strip() + '\n')
                        conf.chmod(0o600)
                        up = run('ip', 'netns', 'exec', ns, 'awg-quick', 'up', str(conf))
                        if up.returncode == 0:
                            interface = run('ip', 'netns', 'exec', ns, 'awg', 'show', 'lucxawgtest').returncode == 0
                        if not interface:
                            failures.append('awg-quick temporary interface failed: ' + up.stderr.strip())
                        run('ip', 'netns', 'exec', ns, 'awg-quick', 'down', str(conf))
        finally:
            if created:
                run('ip', 'netns', 'delete', ns)
    local_ready = tools and loaded and interface and all(required.values())
    # A loaded old module is not proof that the replacement is active.
    reboot_flag = Path('/etc/x-ui/.awg-reboot-needed')
    reboot_pending = reboot_flag.is_file()
    disk_version = run('modinfo', '-F', 'version', 'amneziawg').stdout.strip()
    active_file = Path('/sys/module/amneziawg/version')
    active_version = active_file.read_text().strip() if active_file.is_file() else ''
    replacement_active = bool(active_version and active_version == disk_version)
    if reboot_pending and replacement_active and '-lucxpro280' in disk_version and all(required.values()):
        reboot_flag.unlink()
        reboot_pending = False
    local_ready = local_ready and replacement_active and not reboot_pending and installer_exit in (None, 0)
    if installed and installer_exit in (None, 0) and all(required.values()) and '-lucxpro280' in disk_version:
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(REV + '\n')
    dns = run('getent', 'ahostsv4', 'example.org', timeout=8).returncode == 0
    interfaces = run('awg', 'show', 'interfaces').stdout.split() if tools else []
    now = int(time.time())
    handshakes = []
    rx = tx = 0
    if tools:
        for line in run('awg', 'show', 'all', 'latest-handshakes').stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[2].isdigit() and int(parts[2]) > 0:
                handshakes.append(int(parts[2]))
        for line in run('awg', 'show', 'all', 'transfer').stdout.splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
                rx += int(parts[2])
                tx += int(parts[3])
    report = dict(revision=REV, checked_at=int(time.time()), boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                  current_kernel=current, modules=results, required_modules=required, archived_kernels_without_modules=archived, tools_available=bool(tools),
                  module_loaded=loaded, temporary_interface=interface,
                  installed_module_version=disk_version, loaded_module_version=active_version,
                  installer_exit=installer_exit,
                  reboot_pending=reboot_pending, local_ready=bool(local_ready),
                  next_boot_kernel=next_kernel or 'not_verified; all installed kernel images checked',
                  host_dns=dns, interfaces=interfaces, client_dns='NOT_TESTED',
                  observed_recent_handshake=any(0 <= now - h <= 180 for h in handshakes),
                  observed_received_bytes=rx, observed_sent_bytes=tx,
                  client_handshake_and_traffic='NOT_TESTED; server counters recorded separately', errors=failures)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=REPORT.parent, prefix='.awg-readiness-')
    with os.fdopen(fd, 'w') as out:
        json.dump(report, out, indent=2)
        out.write('\n')
    os.replace(tmp, REPORT)
    print(f'AWG current module: {"LOADED" if loaded else "NOT LOADED"}; tools: {"OK" if tools else "MISSING"}')
    if installer_exit is not None:
        print(f'AWG original installer exit code: {installer_exit}')
    print(f'AWG isolated awg-quick interface: {"OK" if interface else "FAILED/NOT TESTED"}')
    print(f'AWG loaded replacement: {"YES" if replacement_active else "NO"}; reboot flag: {reboot_pending}')
    print(f'AWG host DNS: {"OK" if dns else "FAILED"}; client DNS and traffic: NOT TESTED')
    print(f'AWG server observations: recent peer handshake={report["observed_recent_handshake"]}; received={rx} bytes; sent={tx} bytes')
    print('AWG next boot kernel: ' + (next_kernel + ' (specified by operator)' if next_kernel else
          'NOT VERIFIED; module availability checked for all installed images.'))
    for failure in failures:
        print(failure)
    print('AWG local readiness: ' + ('READY (client traffic still requires testing)' if local_ready else 'INCOMPLETE'))
    print('AWG report:', REPORT)
    return 0 if local_ready else 1

def reboot_required():
    """Distinguish a successful fresh kernel upgrade from a broken build."""
    if not REPORT.is_file() or not Path('/etc/x-ui/.awg-reboot-needed').is_file():
        return False
    report = json.loads(REPORT.read_text())
    current = os.uname().release
    modules = report.get('required_modules', {})
    return (report.get('current_kernel') == current
            and report.get('installer_exit') in (None, 0)
            and not report.get('errors') and report.get('tools_available')
            and not modules.get(current) and not Path('/lib/modules', current, 'build').exists()
            and any(k != current and ok for k, ok in modules.items())
            and all(ok for k, ok in modules.items() if k != current))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['patch-source', 'patch-installer', 'needs-rebuild', 'ready', 'reboot-required', 'cleanup'])
    parser.add_argument('path', nargs='?')
    parser.add_argument('--installed', action='store_true')
    parser.add_argument('--next-kernel')
    parser.add_argument('--installer-exit', type=int)
    args = parser.parse_args()
    if args.action == 'patch-source':
        patch_source(args.path)
    elif args.action == 'patch-installer':
        patch_installer(args.path)
    elif args.action == 'needs-rebuild':
        return 0 if needs_rebuild() else 1
    elif args.action == 'reboot-required':
        return 0 if reboot_required() else 1
    elif args.action == 'cleanup':
        MARKER.unlink(missing_ok=True)
        REPORT.unlink(missing_ok=True)
    else:
        return ready(args.installed, args.next_kernel, args.installer_exit)
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f'LucX AWG compat: {exc}')
PY_LUCX_AWG_COMPAT
    chmod 0755 /usr/local/lib/lucx-ui-pro/awg-compat.py
    cat > /etc/systemd/system/lucx-awg-readiness.service <<'AWG_READINESS_UNIT'
[Unit]
Description=Check LucX AmneziaWG readiness after boot
After=network-online.target x-ui.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/lucx-ui-pro/awg-compat.py ready
TimeoutStartSec=180

[Install]
WantedBy=multi-user.target
AWG_READINESS_UNIT
    systemctl daemon-reload
    systemctl enable lucx-awg-readiness.service >/dev/null
    # Older backup archives contain the previous BBR-only guard.
    if [[ -f /usr/local/sbin/lucx-awg-sysctl-guard ]] &&
       ! grep -q 'awg-compat.py patch-installer' /usr/local/sbin/lucx-awg-sysctl-guard; then
        cat >> /usr/local/sbin/lucx-awg-sysctl-guard <<'AWG_COMPAT_GUARD'
if [[ -f /usr/local/x-ui/bin/install-awg-module.sh ]]; then
    python3 /usr/local/lib/lucx-ui-pro/awg-compat.py patch-installer /usr/local/x-ui/bin/install-awg-module.sh || exit 1
fi
AWG_COMPAT_GUARD
    fi
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
    python3 /usr/local/lib/lucx-ui-pro/awg-compat.py patch-installer "$script" || return 1
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

patch_panel_awg_command() {
    local script
    for script in /usr/bin/x-ui /usr/local/x-ui/x-ui.sh; do
        [[ -f "$script" ]] || continue
        python3 - "$script" <<'PY_AWG_CMD_RESTORE'
import sys
path=sys.argv[1]
text=open(path,encoding="utf-8",errors="surrogateescape").read()
start=text.find("install_awg_module() {")
if start < 0:
    raise SystemExit(0)
end=text.find("\nuninstall_awg_module()", start)
if end < 0:
    raise SystemExit(0)
block=text[start:end]
block=block.replace("    /usr/local/sbin/lucx-awg-sysctl-guard >/dev/null 2>&1 || true\n", "    /usr/local/sbin/lucx-awg-sysctl-guard || return 1\n", 1)
if 'lucx-awg-sysctl-guard' not in block and 'bash "$script" "$@"' in block:
    block=block.replace(
        '    bash "$script" "$@"\n',
        '    /usr/local/sbin/lucx-awg-sysctl-guard || return 1\n'
        '    bash "$script" "$@"\n'
        '    local rc=$?\n'
        '    /usr/local/sbin/lucx-awg-sysctl-guard >/dev/null 2>&1 || true\n'
        '    return $rc\n',1)
text=text[:start]+block+text[end:]
open(path,'w',encoding='utf-8',errors='surrogateescape').write(text)
PY_AWG_CMD_RESTORE
        chmod +x "$script" 2>/dev/null || true
    done
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
    bash "$script" --no-kernel-upgrade || install_rc=$?
    if (( install_rc != 0 )); then
        red "AmneziaWG restore failed: installer exited with code ${install_rc}"
        return 1
    fi
    if ! modinfo amneziawg >/dev/null 2>&1; then
        red "AmneziaWG restore failed: module is unavailable for the running kernel $(uname -r)"
        return 1
    fi
    if ! modprobe amneziawg; then
        red "AmneziaWG restore failed: module could not be loaded"
        return 1
    fi
    for tool in awg awg-quick; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            red "AmneziaWG restore failed: required tool is missing: $tool"
            return 1
        fi
    done
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
allowed = [p.lstrip('/') for p in ['/etc/nginx', '/etc/x-ui', '/etc/default/x-ui', '/etc/fail2ban', '/etc/cron.d', '/usr/local/x-ui', '/usr/bin/x-ui', '/usr/local/lib/3x-ui-pro', '/usr/local/lib/lucx-ui-pro', '/usr/local/sbin/lucx-apply-qdisc', '/usr/local/sbin/lucx-awg-sysctl-guard', '/etc/systemd/system', '/etc/letsencrypt', '/var/lib/letsencrypt', '/var/log/letsencrypt', '/etc/sysctl.conf', '/root/cert', '/var/www/html', '/var/www/diagnostics', '/var/www/subpage', '/var/www/tproxy', '/root/.lucx-tg-web-proxy-info', '/var/lib/lucx-ui-preinstall', '/etc/sysctl.d/99-lucx-ui-forwarding.conf', '/etc/sysctl.d/99-bbr-x-ui.conf', '/etc/sysctl.d/99-awg-performance.conf', '/etc/sysctl.d/99-zz-lucx-ui-tuning.conf', '/etc/modules-load.d/lucx-ui-network.conf', '/etc/modules-load.d/amneziawg.conf', '/etc/modules-load.d/tcp-bbr.conf', '/etc/default/ufw', '/etc/ufw/user.rules', '/etc/ufw/user6.rules', '/etc/ufw/before.rules', '/etc/ufw/before6.rules', '/etc/ipset.conf', '/etc/iptables/ipsets', '/etc/rsyslog.d/10-iptables-scanners.conf', '/etc/logrotate.d/iptables-scanners', '/usr/local/bin/rkn-guard', '/usr/local/bin/rkn', '/usr/local/bin/antiscan-aggregate-logs.sh', '/opt/rkn-guard-manager.sh', '/opt/rkn-guard-manual.list', '/opt/AdGuardHome', '/root/.lucx-adguard-info']]
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
import json
from pathlib import Path
import re
import sqlite3

REVISION = '2026.10.04-280.2'
PROTOCOLS = "('qwdtt','csqtt','tproxy','olcrtc')"


def client_sync_sql(client_columns=(), link_columns=('client_id', 'inbound_id')):
    # Rebuild from normalized records, not an email snapshot of a deleted row.
    rebuild = """UPDATE inbounds SET settings = json_set(
      CASE WHEN json_valid(settings) THEN settings ELSE '{}' END, '$.clients',
      json(COALESCE((SELECT json_group_array(json_object(
        'email', c.email, 'enable', json(CASE WHEN c.enable THEN 'true' ELSE 'false' END)))
        FROM clients c JOIN client_inbounds ci ON ci.client_id=c.id
        WHERE ci.inbound_id=inbounds.id AND c.email != ''), '[]')))
      WHERE protocol IN %s""" % PROTOCOLS
    sql = 'BEGIN IMMEDIATE;\n'
    for name in ('ins', 'del', 'link_update', 'client_update', 'client_delete', 'inbound_delete', 'rename_merge'):
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
        ('client_update', 'AFTER UPDATE OF email,enable ON clients',
         'id IN (SELECT inbound_id FROM client_inbounds WHERE client_id=NEW.id)'),
    ):
        sql += (f'CREATE TRIGGER lucx_shareonly_clients_{name} {event} BEGIN\n'
                + rebuild + ' AND ' + predicate + ';\nEND;\n')
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
        for row_id, raw in db.execute("SELECT id,settings FROM inbounds WHERE protocol='csqtt'").fetchall():
            data = json.loads(raw)
            data['routeThroughXray'] = False
            db.execute('UPDATE inbounds SET settings=? WHERE id=?',
                       (json.dumps(data, ensure_ascii=False), row_id))
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
            change = ' → false (штатный direct)' if protocol == 'csqtt' else ' (сохраняется)'
            print(f'{protocol} #{row_id}: routeThroughXray={route}{change}')
        triggers = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'lucx_shareonly_%'")]
        print('Синхронизация клиентов:', ', '.join(triggers) or 'отсутствует')
        dangling = db.execute('SELECT COUNT(*) FROM client_inbounds WHERE client_id NOT IN '
                              '(SELECT id FROM clients) OR inbound_id NOT IN (SELECT id FROM inbounds)').fetchone()[0]
        print('Осиротевшие связи:', dangling)
    for name, relative in (
        ('AWG guard', 'usr/local/sbin/lucx-awg-sysctl-guard'),
        ('Clash renderer', 'etc/systemd/system/lucx-clash-sub.service'),
        ('AdGuard', 'opt/AdGuardHome'), ('RKN guard', 'usr/local/bin/rkn-guard'),
        ('BBR панели', 'etc/sysctl.d/99-bbr-x-ui.conf'),
        ('Pro ip_forward override', 'etc/sysctl.d/99-lucx-ui-forwarding.conf'),
        ('Сайт заглушка', 'var/lib/lucx-ui-preinstall/cover-generator.json'),
    ):
        print(f'{name}: {"есть" if (root / relative).exists() else "нет"}')
    print('Правки: связи клиентов, CSQTT direct, native Clash provider, TLS панели, AWG guard, таймеры RKN.')
    print('UFW allow routed сохраняется; правила CSQTT обслуживает панель.')
    print('Аккаунты, порты, DNS и содержимое сайта сохраняются.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('report', 'clients', 'apply', 'firewall'))
    parser.add_argument('--root', type=Path, default=Path('/'))
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == 'firewall':
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
    install_awg_compat_support
    patch_panel_awg_command
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
    if [[ -s "${staging}/root-crontab" ]]; then
        crontab - < "${staging}/root-crontab"
        green "    Root crontab restored"
    fi

    if [[ -d "${staging}/cron.d" ]]; then
        cp -a "${staging}/cron.d/." /etc/cron.d/
        green "    /etc/cron.d restored"
    fi

    # Older archives contain a second RUNET scheduler. Keep the restored .dat
    # files and Xray geodata configuration; retire only the legacy cron job.
    if command -v crontab &>/dev/null; then
        local geo_cron
        geo_cron=$(mktemp "${staging}/geodata-cron.XXXXXX")
        crontab -l > "$geo_cron" 2>/dev/null || true
        if grep -Eq '^[[:space:]]*[^#].*/usr/local/x-ui/update-geodata\.sh([[:space:];]|$)' "$geo_cron"; then
            awk '/^[[:space:]]*#/ || $0 !~ /\/usr\/local\/x-ui\/update-geodata\.sh([[:space:];]|$)/' "$geo_cron" > "${geo_cron}.new"
            crontab "${geo_cron}.new" || die "Failed to remove legacy RUNET cron"
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

    echo
    if [[ "$awg_installed_from_backup" == "1" ]]; then
        python3 /usr/local/lib/lucx-ui-pro/awg-compat.py ready || awg_restore_failed=1
    fi
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
