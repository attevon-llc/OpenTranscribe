"""Arms: the two rule strings under test, loaded at run time and never recorded as text.

An arm is a JSON file the lead writes OUTSIDE the repo (``{"name", "scope_rules",
"merge_rules"}``), or the shorthand ``@module``, which reads the two constants from the imported
module. Both strings are concatenated straight after a sentence, so each must start with a space.

The no-leak rule (plan section 0): the harness never contains, prints or writes rule text.
:class:`Arm` keeps the text out of ``repr`` and :meth:`Arm.as_record` returns only a name, a
SHA-256 and a length per string; ``test_no_arm_text_in_artifacts`` pins that.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from summary_bench.config import SABOTAGE_ARM_FIXTURE, HarnessError

MODULE_SPEC = '@module'
_MODULE = 'app.services.llm_summarization'


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class Arm:
    """A named pair of rule strings. The text is readable by the injector only."""

    name: str
    scope_rules: str = field(repr=False)
    merge_rules: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise HarnessError('an arm needs a non-empty name')
        for label, text in (('scope_rules', self.scope_rules), ('merge_rules', self.merge_rules)):
            if not isinstance(text, str) or not text.strip():
                raise HarnessError(f'arm {self.name!r}: {label} must be a non-empty string')
            if not text.startswith(' '):
                raise HarnessError(
                    f'arm {self.name!r}: {label} must start with a space, because the module '
                    'concatenates it directly after a sentence'
                )

    def renamed(self, name: str) -> Arm:
        """The same rules under another label (the A/A control)."""
        return Arm(name, self.scope_rules, self.merge_rules)

    def as_record(self) -> dict[str, Any]:
        """What a results file may hold about this arm: hashes and lengths, never text."""
        return {
            'name': self.name,
            'scope_sha256': _sha(self.scope_rules),
            'scope_len': len(self.scope_rules),
            'merge_sha256': _sha(self.merge_rules),
            'merge_len': len(self.merge_rules),
        }


def _from_module(name: str) -> Arm:
    import importlib

    module = importlib.import_module(_MODULE)
    try:
        return Arm(name, module.SECTION_SCOPE_RULES, module.SECTION_MERGE_RULES)
    except AttributeError as exc:
        raise HarnessError(f'{_MODULE} no longer defines both rule constants') from exc


def _from_file(name: str, path: Path) -> Arm:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise HarnessError(f'cannot read arm file {path}: {type(exc).__name__}') from exc
    if not isinstance(payload, dict) or {'scope_rules', 'merge_rules'} - payload.keys():
        raise HarnessError(f'arm file {path} needs "scope_rules" and "merge_rules"')
    return Arm(name, payload['scope_rules'], payload['merge_rules'])


def load_arm(spec: str) -> Arm:
    """Parse ``NAME=@module`` or ``NAME=PATH.json``. Raises :class:`HarnessError` (exit 2)."""
    name, sep, source = spec.partition('=')
    if not sep or not name or not source:
        raise HarnessError(f'--arm expects NAME=@module or NAME=PATH.json, got {spec!r}')
    if source == MODULE_SPEC:
        return _from_module(name)
    return _from_file(name, Path(source).expanduser())


def sabotage_arm() -> Arm:
    """The negative-control arm: fresh text written for this plan, so it ships in the repo."""
    return _from_file('sabotage', SABOTAGE_ARM_FIXTURE)
