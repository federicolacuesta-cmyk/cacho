"""Historial nativo de Antigravity, ligado a la terminal por su presencia abierta.

No usa «la última conversación del proyecto»: puede haber varias a la vez.
El CLI mantiene abierto presence/<conversation-id>.lock en SU proceso.
"""
import os
from pathlib import Path
import re
import subprocess

BASE = Path('~/.gemini/antigravity-cli').expanduser()
UUID = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'


def buscar(sid):
    if not re.fullmatch(UUID, sid or ''):
        return None
    p = BASE / 'brain' / sid / '.system_generated/logs/transcript.jsonl'
    return str(p) if p.is_file() else None


def del_terminal(tty):
    """También sirve detrás de tmux: tty es el panel, no el cliente attach."""
    if not tty or not tty.startswith('/dev/'):
        raise ValueError('Falta la terminal de Antigravity')
    rows = subprocess.run(['/bin/ps', '-axo', 'pid=,tty=,comm='],
                          capture_output=True, text=True, check=True, timeout=5).stdout
    candidatos = []
    for row in rows.splitlines():
        p = row.split(None, 2)
        if len(p) == 3 and '/dev/' + p[1] == tty and os.path.basename(p[2]) == 'agy':
            candidatos.append(p[0])
    if not candidatos:
        return None
    r = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', ','.join(candidatos), '-Fn'],
                       capture_output=True, text=True, timeout=5)
    if r.returncode not in (0, 1):
        raise RuntimeError('No pude leer la presencia abierta de Antigravity')
    presencia = (BASE / 'presence').resolve()
    ids = set()
    for row in r.stdout.splitlines():
        if not row.startswith('n'):
            continue
        p = Path(row[1:])
        if p.parent.resolve() == presencia and p.suffix == '.lock' and re.fullmatch(UUID, p.stem):
            ids.add(p.stem)
    if len(ids) > 1:
        raise RuntimeError('Hay varias conversaciones de Antigravity en la terminal; no cierro ninguna')
    return next(iter(ids), None)
