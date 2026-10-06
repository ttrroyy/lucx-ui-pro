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
