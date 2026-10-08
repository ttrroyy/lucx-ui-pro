#!/usr/bin/env python3
"""Regenerate the self-contained installer/restore helper after editing pro-compat.py."""
from pathlib import Path

root = Path(__file__).resolve().parents[2]
helper = (root / 'assets/compat/pro-compat.py').read_text(encoding='utf-8').rstrip()
awg = (root / 'assets/compat/awg-bbr.py').read_text(encoding='utf-8').rstrip()
architecture = (root / 'assets/compat/architecture.py').read_text(encoding='utf-8').rstrip()
logs = (root / 'assets/compat/log-policy.py').read_text(encoding='utf-8').rstrip()
for relative in ('lucx-ui-latest.sh', 'assets/backup/lucx-ui-backup.sh'):
    path = root / relative
    text = path.read_text(encoding='utf-8')
    marker = "<<'PY_PRO_COMPAT'\n"
    if text.count(marker) != 1:
        raise SystemExit(f'Expected exactly one helper in {relative}')
    before, tail = text.split(marker, 1)
    _, after = tail.split('\nPY_PRO_COMPAT\n', 1)
    text = before + marker + helper + '\nPY_PRO_COMPAT\n' + after
    marker = "<<'PY_LUCX_AWG_BBR'\n"
    if text.count(marker) != 1:
        raise SystemExit(f'Expected exactly one AWG helper in {relative}')
    before, tail = text.split(marker, 1)
    _, after = tail.split('\nPY_LUCX_AWG_BBR\n', 1)
    text = before + marker + awg + '\nPY_LUCX_AWG_BBR\n' + after
    marker = "<<'PY_LUCX_ARCHITECTURE'\n"
    if text.count(marker) != 1:
        raise SystemExit(f'Expected exactly one architecture helper in {relative}')
    before, tail = text.split(marker, 1)
    _, after = tail.split('\nPY_LUCX_ARCHITECTURE\n', 1)
    text = before + marker + architecture + '\nPY_LUCX_ARCHITECTURE\n' + after
    marker = "<<'PY_LUCX_LOG_POLICY'\n"
    if text.count(marker) != 1:
        raise SystemExit(f'Expected exactly one log policy helper in {relative}')
    before, tail = text.split(marker, 1)
    _, after = tail.split('\nPY_LUCX_LOG_POLICY\n', 1)
    path.write_text(before + marker + logs + '\nPY_LUCX_LOG_POLICY\n' + after,
                    encoding='utf-8', newline='\n')
    print(relative)
