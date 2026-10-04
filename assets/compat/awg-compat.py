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
