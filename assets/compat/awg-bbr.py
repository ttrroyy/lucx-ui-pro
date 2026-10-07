#!/usr/bin/env python3
"""Keep AWG's performance file BBR-neutral; leave module installation upstream."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import urllib.request


def strip_bbr(text):
    return re.sub(r'(?m)^[ \t]*net\.(?:core\.default_qdisc|ipv4\.tcp_congestion_control)[ \t]*=[^\n]*\n?', '', text)


def sync_panel_state(root=Path('/'), apply=False):
    """Persist the panel's choice, including its drop-in deletion on disable."""
    config = root/'etc/sysctl.d/99-bbr-x-ui.conf'
    state = root/'etc/x-ui/.lucx-bbr-state.json'
    if config.exists():
        text = config.read_text()
        qdisc = re.search(r'(?m)^\s*net\.core\.default_qdisc\s*=\s*(\w+)\s*$', text)
        cc = re.search(r'(?m)^\s*net\.ipv4\.tcp_congestion_control\s*=\s*(\w+)\s*$', text)
        previous = re.match(r'#(\w+):(\w+)\s*\n', text)
        if not (qdisc and cc and previous):
            raise RuntimeError('Unknown panel BBR config; refusing to replace it')
        restore = list(previous.groups())
        if restore[1] == 'bbr':
            restore = ['fq_codel', 'cubic']
            text = '#' + ':'.join(restore) + '\n' + text.split('\n', 1)[1]
            config.write_text(text)
        data = {'selected': [qdisc.group(1), cc.group(1)], 'restore': restore}
    elif state.exists() or (root/'etc/x-ui/.lucx-bbr-restore').exists():
        # The web panel deletes the enabled drop-in after applying its saved
        # pre-BBR values. Keep that disabled choice in the same panel file.
        if state.exists():
            selected = json.loads(state.read_text())['restore']
        else:
            selected = (root/'etc/x-ui/.lucx-bbr-restore').read_text().strip().split(':')
        if len(selected) != 2 or any(not re.fullmatch(r'\w+', item) for item in selected):
            raise RuntimeError('Invalid saved BBR state')
        if selected[1] == 'bbr':
            selected = ['fq_codel', 'cubic']
        data = {'selected': selected, 'restore': selected}
        config.write_text('#' + ':'.join(selected) + '\nnet.core.default_qdisc = ' + selected[0] +
                          '\nnet.ipv4.tcp_congestion_control = ' + selected[1] + '\n')
    else:
        return
    state.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, sort_keys=True) + '\n'
    if not state.exists() or state.read_text() != content:
        state.write_text(content)
    if apply:
        current = subprocess.check_output(['sysctl', '-n', 'net.core.default_qdisc',
                                           'net.ipv4.tcp_congestion_control'], text=True).splitlines()
        if current == data['selected']:
            return
        modules = {'fq': 'sch_fq', 'fq_codel': 'sch_fq_codel', 'cake': 'sch_cake',
                   'bbr': 'tcp_bbr', 'cubic': 'tcp_cubic'}
        for value in data['selected']:
            if value in modules:
                subprocess.run(['modprobe', modules[value]], check=False, capture_output=True)
        subprocess.run(['sysctl', '-p', str(config)], check=True, capture_output=True)
        qdisc_helper = root/'usr/local/sbin/lucx-apply-qdisc'
        if qdisc_helper.is_file():
            subprocess.run([str(qdisc_helper)], check=True, capture_output=True)


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
    parser.add_argument('command', choices=('prepare', 'sanitize', 'clean-cli', 'cleanup-legacy', 'panel-state'))
    parser.add_argument('--apply-state', action='store_true')
    parser.add_argument('path', type=Path, nargs='?', default=Path('/'))
    args = parser.parse_args()
    if args.command == 'panel-state':
        sync_panel_state(apply=args.apply_state)
        return
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
