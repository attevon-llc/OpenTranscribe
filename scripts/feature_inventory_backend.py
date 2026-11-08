#!/usr/bin/env python3
"""Backend half of the feature-inventory tool (see feature_inventory.py).

Run with the backend venv python, cwd = <tree>/backend, as a subprocess:
``python feature_inventory_backend.py <out.json>``. Every section is extracted
independently; a failure is recorded as ``{"__error__": ...}`` rather than silently
dropped, because a missing section would read as "nothing changed".

Module locations are discovered, not hardcoded, so the same script works on trees
where the cleanup moved the Celery app, Base, Settings or constants.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib
import json
import os
import re
import sys
from pathlib import Path

BACKEND = Path.cwd()
APP = BACKEND / 'app'
SECRETISH = re.compile(r'secret|password|passwd|token|api_?key|private|credential|salt', re.I)
_STABLE = re.compile(r' at 0x[0-9a-fA-F]+')


def _jsonable(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple, set, frozenset)):
        items = [_jsonable(i) for i in v]
        return sorted(items, key=repr) if isinstance(v, (set, frozenset)) else items
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    return _STABLE.sub('', repr(v))


def _mask(name: str, value):
    """Secret-looking defaults are stored as a hash, never as the literal."""
    if SECRETISH.search(name) and isinstance(value, str) and value:
        return 'sha256:' + hashlib.sha256(value.encode()).hexdigest()[:16]
    return value


def _py_files(sub: Path):
    return sorted(p for p in sub.rglob('*.py') if '__pycache__' not in p.parts)


def _module_of(path: Path) -> str:
    rel = path.relative_to(BACKEND).with_suffix('')
    parts = list(rel.parts)
    if parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts)


def _find_module_defining(pattern: str) -> list[str]:
    return [
        _module_of(p) for p in _py_files(APP) if re.search(pattern, p.read_text('utf-8', 'replace'))
    ]


# Optional heavy third-party dependencies whose absence on a CPU-only host may legitimately
# stop a *discovered* module from importing. Anything else that fails to import is a hard error.
OPTIONAL_DEPS = frozenset(
    {'torch', 'torchaudio', 'whisperx', 'faster_whisper', 'pyannote', 'gliner'}
)
SKIPPED: dict[str, str] = {}


def _import(mod: str, *, preferred: bool = False):
    """Import ``mod``; a failure raises, except two explicit, recorded cases.

    * ``preferred`` and the module itself does not exist (it moved): returns None so the
      caller falls back to discovery.
    * ModuleNotFoundError naming a package in OPTIONAL_DEPS: recorded in SKIPPED, returns None.
    """
    try:
        return importlib.import_module(mod)
    except ModuleNotFoundError as e:
        if preferred and e.name == mod:
            return None
        if e.name and e.name.split('.')[0] in OPTIONAL_DEPS:
            SKIPPED[mod] = f'optional dependency missing: {e.name}'
            return None
        raise


def routes_and_openapi(app) -> tuple[dict, dict]:
    from fastapi.routing import APIRoute

    def dep_names(dependant, out):
        for d in dependant.dependencies:
            out.append(getattr(d.call, '__name__', None) or type(d.call).__name__)
            dep_names(d, out)
        return out

    routes = {}
    for r in app.routes:
        if isinstance(r, APIRoute):
            for m in sorted(r.methods or []):
                routes[f'{m} {r.path}'] = {
                    'name': r.name,
                    'dependencies': sorted(set(dep_names(r.dependant, []))),
                }
        else:
            routes[f'{type(r).__name__.upper()} {getattr(r, "path", "")}'] = {
                'name': getattr(r, 'name', None)
            }
    doc = app.openapi()
    doc['info']['version'] = '0.0.0-snapshot'
    ops = {}
    for path, item in doc.get('paths', {}).items():
        for method, op in item.items():
            ops[f'{method.upper()} {path}'] = op
    ops['info'] = doc.get('info')
    for name, sch in doc.get('components', {}).items():
        for k, v in (sch or {}).items():
            ops[f'components.{name}.{k}'] = v
    return routes, _jsonable(ops)


def find_celery_app():
    from celery import Celery

    for mod, preferred in [
        ('app.core.celery', True),
        *((m, False) for m in _find_module_defining(r'\bCelery\(')),
    ]:
        m = _import(mod, preferred=preferred)
        if m is None:
            continue
        for v in vars(m).values():
            # isinstance: the app may be a subclass (DemoAwareCelery, issue #628).
            if isinstance(v, Celery):
                return v
    raise RuntimeError('no Celery app found')


def celery_sections() -> dict:
    app = find_celery_app()
    app.loader.import_default_modules()
    router = app.amqp.router
    tasks = {}
    for name in sorted(app.tasks):
        if name.startswith('celery.'):
            continue
        try:
            q = router.route({}, name).get('queue')
            queue = getattr(q, 'name', q)
        except KeyError as e:  # kombu QueueNotFound: no queue for this task
            queue = f'<unroutable:{type(e).__name__}>'
        tasks[name] = {'queue': queue}
    beat = {
        k: _jsonable({**v, 'schedule': str(v.get('schedule'))})
        for k, v in (app.conf.beat_schedule or {}).items()
    }
    queues = {q.name: {'routing_key': q.routing_key} for q in (app.amqp.queues.values())}
    return {'celery.tasks': tasks, 'celery.beat': beat, 'celery.queues': queues}


def find_base():
    from sqlalchemy.orm import DeclarativeBase

    m = _import('app.db.base', preferred=True)
    if m is not None and hasattr(m, 'Base'):
        return m.Base
    for m in list(sys.modules.values()):
        for v in list(vars(m).values()) if m and m.__name__.startswith('app.') else []:
            if (
                isinstance(v, type)
                and issubclass(v, DeclarativeBase)
                and v is not DeclarativeBase
                and hasattr(v, 'metadata')
                and v.metadata.tables
            ):
                return v
    raise RuntimeError('no declarative Base found')


def schema_section() -> dict:
    from sqlalchemy.dialects import postgresql

    meta = find_base().metadata
    out = {}
    for tname, t in sorted(meta.tables.items()):
        for c in t.columns:
            out[f'{tname}.{c.name}'] = {
                'type': c.type.compile(dialect=postgresql.dialect()),
                'nullable': c.nullable,
                'pk': c.primary_key,
                'default': _STABLE.sub('', repr(getattr(c.default, 'arg', None)))
                if c.default is not None
                else None,
                'server_default': _STABLE.sub('', str(getattr(c.server_default, 'arg', None)))
                if c.server_default is not None
                else None,
                'fks': sorted(f.target_fullname for f in c.foreign_keys),
            }
        for ix in t.indexes:
            out[f'{tname}#index:{ix.name}'] = {
                'columns': [str(e) for e in ix.expressions],
                'unique': bool(ix.unique),
            }
        for con in t.constraints:
            cols = sorted(c.name for c in getattr(con, 'columns', []))
            out[f'{tname}#{type(con).__name__}:{con.name or ",".join(cols)}'] = {
                'columns': cols,
                'sql': _STABLE.sub('', str(getattr(con, 'sqltext', ''))),
            }
    return out


def alembic_head() -> dict:
    revs, downs = {}, set()
    for p in _py_files(BACKEND / 'alembic' / 'versions'):
        vals = {}
        for node in ast.parse(p.read_text('utf-8')).body:
            tgt = node.targets[0] if isinstance(node, ast.Assign) else getattr(node, 'target', None)
            if (
                isinstance(tgt, ast.Name)
                and tgt.id in ('revision', 'down_revision')
                and getattr(node, 'value', None) is not None
            ):
                with contextlib.suppress(ValueError):
                    vals[tgt.id] = ast.literal_eval(node.value)
        if 'revision' in vals:
            revs[vals['revision']] = p.name
            d = vals.get('down_revision')
            downs.update([d] if isinstance(d, str) else (d or []))
    return {'heads': sorted(set(revs) - downs), 'revision_count': len(revs)}


def pydantic_settings() -> dict:
    from pydantic_settings import BaseSettings

    out = {}
    for mod in _find_module_defining(r'\bBaseSettings\b'):
        m = _import(mod)
        if m is None:
            continue
        for cls in vars(m).values():
            if (
                isinstance(cls, type)
                and issubclass(cls, BaseSettings)
                and cls is not BaseSettings
                and cls.__module__ == m.__name__
            ):
                for fname, f in cls.model_fields.items():
                    d = (
                        None
                        if f.is_required()
                        else _mask(fname, _jsonable(f.get_default(call_default_factory=True)))
                    )
                    out[fname] = {
                        'class': cls.__name__,
                        'type': str(f.annotation),
                        'required': f.is_required(),
                        'default': d,
                    }
    return out


def constants_section() -> dict:
    """Module-level UPPER_CASE literal defaults under app/core, keyed by name (module-agnostic)."""
    out: dict = {}
    for p in _py_files(APP / 'core'):
        for node in ast.parse(p.read_text('utf-8')).body:
            tgt = (
                node.targets[0]
                if isinstance(node, ast.Assign) and len(node.targets) == 1
                else getattr(node, 'target', None)
            )
            val = getattr(node, 'value', None)
            if not (isinstance(tgt, ast.Name) and tgt.id.isupper() and val is not None):
                continue
            try:
                v = _jsonable(ast.literal_eval(val))
            except (ValueError, SyntaxError):
                continue
            out.setdefault(tgt.id, [])
            out[tgt.id].append(_mask(tgt.id, v))
    return {k: (v[0] if len(v) == 1 else {'__multiple__': v}) for k, v in sorted(out.items())}


def ws_events() -> dict:
    consts = constants_section()
    ntypes = {
        k: v for k, v in consts.items() if k.startswith('NOTIFICATION_TYPE_') and isinstance(v, str)
    }
    events: dict = dict.fromkeys(ntypes.values(), 'constant')
    ident = re.compile(r'^[a-z][a-z0-9_.:-]*$')
    for p in _py_files(APP):
        for node in ast.walk(ast.parse(p.read_text('utf-8', 'replace'))):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, 'attr', '')
            if not re.search(r'ws_event|ws_notif|send_notification', name):
                continue
            for a in [*node.args, *(k.value for k in node.keywords)]:
                if (
                    isinstance(a, ast.Constant)
                    and isinstance(a.value, str)
                    and ident.match(a.value)
                ):
                    events.setdefault(a.value, 'literal')
                elif isinstance(a, (ast.Name, ast.Attribute)):
                    n = a.id if isinstance(a, ast.Name) else a.attr
                    if n in ntypes:
                        events.setdefault(ntypes[n], 'constant')
    return dict(sorted(events.items()))


def load_app():
    for mod, preferred in [
        ('app.main', True),
        *((m, False) for m in _find_module_defining(r'\bFastAPI\(')),
    ]:
        m = _import(mod, preferred=preferred)
        if m is not None and type(getattr(m, 'app', None)).__name__ == 'FastAPI':
            return m.app
    raise RuntimeError('FastAPI app not importable')


def main(out_path: str) -> int:
    """Extract every section; any failure raises (traceback, non-zero exit) rather than omitting it."""
    result: dict = {}
    result['db.alembic_head'] = alembic_head()
    result['settings.constants'] = constants_section()
    result['ws.events'] = ws_events()
    result['settings.pydantic'] = pydantic_settings()
    result['api.routes'], result['api.openapi'] = routes_and_openapi(load_app())
    result['db.schema'] = schema_section()
    result.update(celery_sections())
    result['skipped'] = dict(sorted(SKIPPED.items()))
    Path(out_path).write_text(
        json.dumps(result, sort_keys=True, indent=1, default=repr), encoding='utf-8'
    )
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(BACKEND))
    os.chdir(BACKEND)
    sys.exit(main(sys.argv[1]))
