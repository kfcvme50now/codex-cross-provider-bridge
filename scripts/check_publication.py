#!/usr/bin/env python3
"""Reject private runtime artifacts and machine-local references without printing values."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRECTORIES = {'state', 'backups', 'archive', '.codex', '.claude', '.cc-switch', 'security-results', '__pycache__'}
PRIVATE_NAMES = {'auth.json', 'settings.json', '.env'}
PRIVATE_HOME = re.compile(r'(?i)(?:[A-Z]:[\\/]+Users[\\/]+[A-Za-z0-9_.-]+|/(?:Users|home)/[A-Za-z0-9_.-]+|[A-Z]:[\\/]+home[\\/]+[A-Za-z0-9_.-]+)')


def check_files(names, root=ROOT):
    findings=[]
    for name in names:
        path=Path(name)
        if (set(path.parts) & PRIVATE_DIRECTORIES or path.name in PRIVATE_NAMES
                or path.name.startswith('.env.') or re.search(r'\.(?:sqlite\d*|db|log|bak)(?:[-.]|$)',path.name)):
            findings.append((name,'private runtime artifact'))
            continue
        file=root/path
        if not file.is_file():continue
        try:content=file.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            findings.append((name,'unexpected binary/non-UTF-8 file'))
            continue
        if PRIVATE_HOME.search(content):findings.append((name,'private user directory'))
        if re.search(r'\]\((?:\.\./){3,}',content):findings.append((name,'documentation link outside repository'))
    return findings


def main():
    data=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT)
    names=[x for x in data.decode('utf-8').split('\0') if x]
    findings=check_files(names)
    for name,reason in findings:print(f'{name}: {reason}')
    print(f'Publication check: {len(names)} files; {len(findings)} findings.')
    return 1 if findings else 0


if __name__=='__main__':sys.exit(main())
