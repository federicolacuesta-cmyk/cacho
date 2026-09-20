"""Vincula una terminal con el rollout que SU proceso Codex tiene abierto.

RAÍZ: un UUID de Claude no identifica una charla de Codex. Ni el cwd ni la hora
alcanzan cuando hay varias pestañas. La evidencia es el descriptor del proceso.
"""
import glob
import json
import os
import re
import subprocess
import time

SESIONES = os.path.expanduser('~/.codex/sessions')
_cache = {}


def buscar(sid):
    if not re.fullmatch(r'[0-9a-fA-F-]{8,64}', sid or ''):
        return None
    hits = glob.glob(os.path.join(SESIONES, '*', '*', '*', 'rollout-*-' + sid + '.jsonl'))
    return hits[0] if len(hits) == 1 else None


def metadata(path):
    with open(path, encoding='utf-8') as fh:
        d = json.loads(fh.readline())
    return d.get('payload', {}) if d.get('type') == 'session_meta' else {}


def primer_pedido(path, lineas=60):
    """El primer mensaje REAL del usuario en el rollout: lo que se le pidió a Gpto.

    12-set-2026: todas las pestañas de Gpto se llamaban «Gpto (GPT-6 Astra, ChatGPT)»
    porque el título de una pestaña sale del transcript de Claude, y Codex no deja uno.
    El rollout sí guarda el pedido: es un `response_item`/`message` con role `user`, después
    de los `developer` y del AGENTS.md que Codex inyecta como si fuera del usuario.
    """
    with open(path, encoding='utf-8') as fh:
        for _ in range(lineas):
            line = fh.readline()
            if not line:
                break
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p = d.get('payload') or {}
            if d.get('type') != 'response_item' or p.get('type') != 'message' or p.get('role') != 'user':
                continue
            txt = ' '.join(b.get('text', '') for b in p.get('content') or []
                           if isinstance(b, dict) and b.get('type') == 'input_text').strip()
            if not txt or txt.startswith('#') or txt.startswith('<'):
                continue   # AGENTS.md, instrucciones entre tags: no es el pedido
            return txt
    return ''


def del_proceso(pid, cwd):
    """Devuelve el thread propio; nunca el rollout más reciente del directorio."""
    now = time.monotonic()
    cached = _cache.get(pid)
    if cached and now - cached[0] < 5:
        return cached[1]
    rows = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,comm='],
                          capture_output=True, text=True, check=True, timeout=5).stdout
    procs = []
    for row in rows.splitlines():
        parts = row.strip().split(None, 2)
        if len(parts) == 3:
            procs.append((int(parts[0]), int(parts[1]), parts[2]))
    family = {pid}
    while True:
        grown = family | {p for p, parent, _ in procs if parent in family}
        if grown == family:
            break
        family = grown
    candidates = [p for p, _, cmd in procs
                  if p in family and os.path.basename(cmd) == 'codex']
    found = set()
    if candidates:
        r = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', ','.join(map(str, candidates)), '-Fn'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode not in (0, 1):
            raise RuntimeError('No pude identificar el historial abierto de Codex')
        base = os.path.realpath(SESIONES) + os.sep
        for row in r.stdout.splitlines():
            if not row.startswith('n'):
                continue
            path = os.path.realpath(row[1:])
            if not path.startswith(base) or not path.endswith('.jsonl'):
                continue
            m = metadata(path)
            if m.get('source') == 'cli' and os.path.realpath(m.get('cwd', '/')) == os.path.realpath(cwd):
                found.add(m['id'])
    if len(found) > 1:
        raise RuntimeError('Más de un historial de Codex en la misma terminal; no los mezclo')
    sid = next(iter(found), None)
    _cache[pid] = (now, sid)
    return sid
