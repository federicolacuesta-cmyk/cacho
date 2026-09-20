"""Cierre explícito del ENCARGO, separado del fin de turno. Sin inferir por silencio.

El agente termina su respuesta con «✓ Encargo completo.» sólo cuando no queda
trabajo ni decisiones pendientes. Leemos exclusivamente mensajes del asistente
y esperamos la confirmación de fin del motor. No ejecuta ni borra nada.
"""
import json
from pathlib import Path
import re
import sys

MARCA = "✓ Encargo completo."
# El bloque «❓ queda por decidir (vos): …» del RC. Si dice algo que no sea «nada»,
# el encargo NO está completo aunque el agente lo firme: la decisión es del usuario.
_DECIDIR = re.compile(r"❓[^\n]*queda[^\n]*por decidir[^\n]*\(vos\)\W*(?P<que>[^\n]*)", re.I)
_NADA = re.compile(r"^\W*nada\b", re.I)


def decision_abierta(texto):
    """True si el texto deja una decisión a el usuario («❓ queda por decidir (vos): X», X ≠ nada)."""
    lineas = texto.splitlines()
    for i, linea in enumerate(lineas):
        m = _DECIDIR.search(linea)
        if not m:
            continue
        que = m.group("que").strip()
        if not que:  # la decisión viene en la línea siguiente no vacía
            que = next((l.strip() for l in lineas[i + 1:] if l.strip()), "")
        if que and not _NADA.match(que):
            return True
    return False


def marcado(texto):
    """La firma vale sólo si es la última línea Y no queda una decisión abierta para el usuario.
    RAÍZ (17-set-2026): Jaime firmó «✓ Encargo completo.» debajo de «❓ Queda por decidir
    (vos): si atendés a Sulenka o no» y la pestaña se cerró con la pregunta adentro."""
    if not isinstance(texto, str) or texto.strip().splitlines()[-1:] != [MARCA]:
        return False
    return not decision_abierta(texto)


class Lector:
    """Incremental, sólo estado; nunca guarda el texto de una conversación."""

    def __init__(self):
        self.cache = {}

    def leer(self, ruta):
        p = Path(ruta)
        st = p.stat()
        k = str(p)
        s = self.cache.get(k)
        if s is None or s['inode'] != st.st_ino or st.st_size < s['pos']:
            s = dict(inode=st.st_ino, pos=0, marca=False, completo=False,
                     turno='', bloqueado=False, agy_index=-1, corrupto=False)
            self.cache[k] = s
        with p.open('rb') as f:
            f.seek(s['pos'])
            while True:
                inicio = f.tell()
                linea = f.readline()
                if not linea or not linea.endswith(b'\n'):
                    s['pos'] = inicio
                    break
                try:
                    d = json.loads(linea)
                except ValueError as e:
                    # Corrupción: ese transcript NO se cierra nunca más (`corrupto` es
                    # permanente: los eventos siguientes no lo levantan), se avisa UNA vez y
                    # se sigue leyendo. Antes `pos` quedaba clavado en la línea rota y `leer()`
                    # reventaba en cada tick: el server lo imprimía cada 2 s y el autocierre de
                    # esa pestaña moría mudo (el Policía, 20-set-2026).
                    if not s['corrupto']:
                        print("⚠️ autocierre: línea corrupta en %s (%s): esa charla no se "
                              "cierra sola nunca más" % (p.name[-18:-6], e), file=sys.stderr)
                    s['corrupto'] = True
                    s['pos'] = f.tell()
                    continue
                self._evento(s, d)
                s['pos'] = f.tell()
        return dict(s, firma=(st.st_ino, st.st_size, st.st_mtime_ns),
                    mtime=st.st_mtime,
                    cerrar=bool(s['marca'] and s['completo'] and not s['bloqueado']
                                and not s['corrupto'] and s['pos'] == st.st_size))

    @staticmethod
    def _evento(s, d):
        t = d.get('type')
        if 'step_index' in d and 'source' in d:  # transcript nativo de Antigravity
            indice = d['step_index']
            if not isinstance(indice, int) or indice < s['agy_index']:
                return  # una actualización vieja no puede cerrar un pedido posterior
            s['agy_index'] = indice
            # DONE de una herramienta no es entrega. Sólo una respuesta del modelo
            # con texto, sin llamadas pendientes, puede declarar el encargo completo.
            final = (t == 'PLANNER_RESPONSE' and d.get('source') == 'MODEL'
                     and d.get('status') == 'DONE' and not d.get('tool_calls')
                     and not d.get('subagent_info'))
            s.update(marca=final and marcado(d.get('content')),
                     completo=final, bloqueado=False)
            return
        if d.get('isMeta') or d.get('isSidechain'):
            return
        if t == 'event_msg':  # Codex: el mensaje final aún no es task_complete
            v = d.get('payload') or {}
            tipo = v.get('type')
            if tipo in ('task_started', 'user_message', 'turn_aborted'):
                s.update(marca=False, completo=False, bloqueado=False)
                if tipo == 'task_started':
                    s['turno'] = v.get('turn_id') or ''
            elif tipo == 'task_complete' and s['turno'] and v.get('turn_id') == s['turno']:
                s['completo'] = True
            return
        if t == 'response_item':
            v = d.get('payload') or {}
            if v.get('type') == 'message':
                if v.get('role') == 'user':
                    s.update(marca=False, completo=False)
                elif v.get('role') == 'assistant':
                    texto = '\n'.join(b.get('text', '') for b in v.get('content', [])
                                      if b.get('type') == 'output_text')
                    s.update(marca=marcado(texto) and v.get('phase') in ('final', 'final_answer'), completo=False)
            elif v.get('type') in ('function_call', 'custom_tool_call'):
                s.update(marca=False, completo=False)
            return
        if t == 'queue-operation' and d.get('operation') == 'enqueue':
            s.update(marca=False, completo=False, bloqueado=True)
        elif t == 'system' and d.get('subtype') == 'turn_duration':
            if d.get('pendingBackgroundAgentCount'):
                s.update(marca=False, completo=False, bloqueado=True)
        elif t == 'user':
            s.update(marca=False, completo=False, bloqueado=False)
        elif t == 'assistant':
            m = d.get('message') or {}
            content = m.get('content') or []
            texto = content if isinstance(content, str) else '\n'.join(
                b.get('text', '') for b in content if b.get('type') == 'text')
            s.update(marca=marcado(texto), completo=m.get('stop_reason') == 'end_turn')


def misma_firma(ruta, firma):
    st = Path(ruta).stat()
    return (st.st_ino, st.st_size, st.st_mtime_ns) == firma
