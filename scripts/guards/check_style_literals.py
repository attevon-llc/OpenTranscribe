#!/usr/bin/env python3
"""Ratchet guard: stylesheet literals that belong in design tokens (frontend styling layer).

Counts hard-coded colours, ``var()`` fallbacks, shadows, radii, spacing, font sizes and weights,
z-indexes, transition durations and physical (left/right) direction properties in
``frontend/src`` stylesheets, per ``<category>::<path>``, and records them in
``scripts/guards/baselines/style_literals.txt``. The baseline can only shrink: new literals are
blocked and existing ones are frozen per file until a migration lane removes them (see
``docs/design/frontend_styling_layer_plan.md`` section 4.3).

Scope: tracked ``frontend/src/**/*.svelte`` (``<style>`` blocks, ``style="..."`` attributes and
``style:prop="..."`` directives) and ``frontend/src/**/*.css``. Excluded: ``*.test.*``,
``frontend/src/styles/theme.css`` (the token source) and the custom-property declarations inside
``:root`` / ``[data-theme='dark']`` blocks of the other ``frontend/src/styles/*.css`` files
(component token sources such as ``--field-*``). Comments are stripped first. Unbalanced braces,
quotes or parentheses, and unreadable files, exit 2: an unparseable file must never read as clean.

Categories (the baseline value is the number counted):

* ``colour``: hex, ``rgb/rgba/hsl/hsla(`` with a numeric first channel, and CSS named colours
  (colour-bearing properties and custom properties only). Not ``transparent``, ``currentColor``,
  ``inherit``, ``rgba(var(--x-rgb), a)``. One per literal.
* ``fallback``: ``var(--x, <anything>)``. One per var().
* ``shadow``: a ``box-shadow``/``text-shadow`` declaration that is not ``none``/``inherit`` or
  only ``var(--x)`` references. One per declaration.
* ``radius``: each non-zero length in ``border-radius`` / ``border-*-*-radius``.
* ``spacing``: each non-zero length other than ``1px``, ``-1px``, ``%``, ``em`` in
  ``padding*``/``margin*``/``gap``/``row-gap``/``column-gap``.
* ``font-size``: a ``font-size`` declaration with a length. ``font-weight``: numeric or
  ``bold``/``bolder``/``lighter``. ``z-index``: an integer other than ``0``/``-1``.
* ``duration``: each non-zero time in ``transition``/``transition-duration``/``transition-delay``
  (loop animations are not counted).
* ``physical``: ``margin/padding/border-left|right*``, ``left``/``right`` other than ``50%``,
  ``text-align``/``float`` ``left|right``.

Length categories skip a declaration that wraps a ``var()`` in ``calc/min/max/clamp``; a literal
inside ``var(--x, <fallback>)`` is counted once, by ``fallback``, not again by its property.

Usage::

    check_style_literals.py [--root DIR] [--baseline FILE]
    check_style_literals.py --tighten
    check_style_literals.py --write-baseline --reason TEXT [--force]
    check_style_literals.py --report          # per-category totals, no baseline needed
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline as bl

_HERE = Path(__file__).resolve().parent
_PREFIX = 'frontend/src/'
_THEME_CSS = 'frontend/src/styles/theme.css'
_TOKEN_SOURCE_DIR = 'frontend/src/styles/'
_TOKEN_BLOCK_SELECTORS = frozenset({':root', "[data-theme='dark']", '[data-theme="dark"]'})


CATEGORIES = (
    'colour',
    'fallback',
    'shadow',
    'radius',
    'spacing',
    'font-size',
    'font-weight',
    'z-index',
    'duration',
    'physical',
)

_NAMED_COLOURS = frozenset(
    {
        'aliceblue',
        'antiquewhite',
        'aqua',
        'aquamarine',
        'azure',
        'beige',
        'bisque',
        'black',
        'blanchedalmond',
        'blue',
        'blueviolet',
        'brown',
        'burlywood',
        'cadetblue',
        'chartreuse',
        'chocolate',
        'coral',
        'cornflowerblue',
        'cornsilk',
        'crimson',
        'cyan',
        'darkblue',
        'darkcyan',
        'darkgoldenrod',
        'darkgray',
        'darkgreen',
        'darkgrey',
        'darkkhaki',
        'darkmagenta',
        'darkolivegreen',
        'darkorange',
        'darkorchid',
        'darkred',
        'darksalmon',
        'darkseagreen',
        'darkslateblue',
        'darkslategray',
        'darkslategrey',
        'darkturquoise',
        'darkviolet',
        'deeppink',
        'deepskyblue',
        'dimgray',
        'dimgrey',
        'dodgerblue',
        'firebrick',
        'floralwhite',
        'forestgreen',
        'fuchsia',
        'gainsboro',
        'ghostwhite',
        'gold',
        'goldenrod',
        'gray',
        'green',
        'greenyellow',
        'grey',
        'honeydew',
        'hotpink',
        'indianred',
        'indigo',
        'ivory',
        'khaki',
        'lavender',
        'lavenderblush',
        'lawngreen',
        'lemonchiffon',
        'lightblue',
        'lightcoral',
        'lightcyan',
        'lightgoldenrodyellow',
        'lightgray',
        'lightgreen',
        'lightgrey',
        'lightpink',
        'lightsalmon',
        'lightseagreen',
        'lightskyblue',
        'lightslategray',
        'lightslategrey',
        'lightsteelblue',
        'lightyellow',
        'lime',
        'limegreen',
        'linen',
        'magenta',
        'maroon',
        'mediumaquamarine',
        'mediumblue',
        'mediumorchid',
        'mediumpurple',
        'mediumseagreen',
        'mediumslateblue',
        'mediumspringgreen',
        'mediumturquoise',
        'mediumvioletred',
        'midnightblue',
        'mintcream',
        'mistyrose',
        'moccasin',
        'navajowhite',
        'navy',
        'oldlace',
        'olive',
        'olivedrab',
        'orange',
        'orangered',
        'orchid',
        'palegoldenrod',
        'palegreen',
        'paleturquoise',
        'palevioletred',
        'papayawhip',
        'peachpuff',
        'peru',
        'pink',
        'plum',
        'powderblue',
        'purple',
        'rebeccapurple',
        'red',
        'rosybrown',
        'royalblue',
        'saddlebrown',
        'salmon',
        'sandybrown',
        'seagreen',
        'seashell',
        'sienna',
        'silver',
        'skyblue',
        'slateblue',
        'slategray',
        'slategrey',
        'snow',
        'springgreen',
        'steelblue',
        'tan',
        'teal',
        'thistle',
        'tomato',
        'turquoise',
        'violet',
        'wheat',
        'white',
        'whitesmoke',
        'yellow',
        'yellowgreen',
    }
)
# Named colours are only searched where a colour can appear: elsewhere a word like ``tan`` or
# ``orange`` can be a font name, an animation name or a grid area.
_COLOUR_PROP = re.compile(
    r'^(?:--[\w-]+|color|fill|stroke|caret-color|accent-color|scrollbar-color|'
    r'background(?:-color|-image)?|border(?:-[a-z]+)*-color|border(?:-[a-z]+)?|'
    r'outline(?:-color)?|box-shadow|text-shadow|text-decoration(?:-color)?|column-rule(?:-color)?|'
    r'filter|text-emphasis-color|-webkit-text-fill-color|-webkit-text-stroke(?:-color)?)$'
)
_HEX = re.compile(r'#[0-9a-fA-F]{3,8}\b')
_COLOUR_FN = re.compile(r'\b(?:rgba?|hsla?)\(\s*[-+]?[\d.]')
_WORD = re.compile(r'(?<![\w-])([a-z]+)(?![\w-])')
_LENGTH = re.compile(
    r'(?<![\w.#-])([-+]?(?:\d+\.?\d*|\.\d+))'
    r'(px|rem|em|vh|vw|vmin|vmax|svh|dvh|lvh|ch|ex|pt|cm|mm|in|%)?(?![\w.%-])'
)
_TIME = re.compile(r'(?<![\w.#-])((?:\d+\.?\d*|\.\d+))(ms|s)\b')
_VAR_NAME_ONLY = re.compile(r'var\(\s*--[\w-]+\s*\)')
_PHYSICAL_PROP = re.compile(
    r'^(?:margin|padding)-(?:left|right)$|^border-(?:left|right)(?:-[a-z]+)*$'
)
_SPACING_PROP = re.compile(r'^(?:padding|margin)(?:-[a-z]+)*$|^(?:gap|row-gap|column-gap)$')
_RADIUS_PROP = re.compile(r'^border(?:-[a-z]+-[a-z]+)?-radius$')
_MATH_FN = re.compile(r'\b(?:calc|min|max|clamp)\(')


class Decl:
    """One ``prop: value`` declaration with the selector it sat under."""

    __slots__ = ('prop', 'selector', 'value')

    def __init__(self, prop: str, value: str, selector: str) -> None:
        self.prop, self.value, self.selector = prop, value, selector


# --- text preparation ---------------------------------------------------------------------------

_COMMENT = re.compile(r'/\*.*?\*/', re.DOTALL)
_HTML_COMMENT = re.compile(r'<!--.*?-->', re.DOTALL)


def _blank_strings_and_urls(css: str, rel: str) -> str:
    """Replace string literals and ``url(...)`` bodies so their contents are never scanned.

    A data-URI SVG holds ``;``, ``#`` and parentheses that would corrupt declaration splitting,
    and a ``content: "#"`` is not a colour. Raises ``ScanError`` on an unterminated string/url.
    """
    out: list[str] = []
    i, n = 0, len(css)
    while i < n:
        ch = css[i]
        if ch in '"\'':
            j = css.find(ch, i + 1)
            while j != -1 and css[j - 1] == '\\':
                j = css.find(ch, j + 1)
            if j == -1:
                msg = f'unterminated string in {rel}'
                raise bl.ScanError(msg)
            out.append(ch + ch)
            i = j + 1
        elif css.startswith('url(', i) and (i == 0 or not css[i - 1].isalnum()):
            depth, j, quote = 1, i + 4, ''
            while j < n and depth:
                c = css[j]
                if quote:
                    quote = '' if c == quote else quote
                elif c in '"\'':
                    quote = c
                elif c == '(':
                    depth += 1
                elif c == ')':
                    depth -= 1
                j += 1
            if depth:
                msg = f'unterminated url( in {rel}'
                raise bl.ScanError(msg)
            out.append('url()')
            i = j
        else:
            out.append(ch)
            i += 1
    return ''.join(out)


def parse_declarations(css: str, rel: str) -> list[Decl]:
    """Declarations of a stylesheet fragment, with their selector. Brace-aware, not regex-on-text.

    ``@media``/``@supports``/``:global(...)`` nest naturally; a ``;`` or ``{`` inside parentheses
    or quotes never splits. Raises ``ScanError`` on unbalanced braces or parentheses.
    """
    text = _blank_strings_and_urls(_COMMENT.sub('', css), rel)
    decls: list[Decl] = []
    stack: list[str] = []
    depth_paren = 0
    start = 0

    def flush(end: int) -> None:
        chunk = text[start:end].strip()
        if not chunk or chunk.startswith('@'):
            return
        prop, sep, value = chunk.partition(':')
        prop = prop.strip().lower()
        if sep and re.fullmatch(r'-{0,2}[a-z][\w-]*', prop):
            decls.append(Decl(prop, value.strip(), stack[-1] if stack else ''))

    for i, ch in enumerate(text):
        if ch == '(':
            depth_paren += 1
        elif ch == ')':
            depth_paren -= 1
            if depth_paren < 0:
                msg = f'unbalanced ) in {rel}'
                raise bl.ScanError(msg)
        elif depth_paren == 0 and ch == '{':
            stack.append(' '.join(text[start:i].split()))
            start = i + 1
        elif depth_paren == 0 and ch == ';':
            flush(i)
            start = i + 1
        elif depth_paren == 0 and ch == '}':
            flush(i)
            if not stack:
                msg = f'unbalanced }} in {rel}'
                raise bl.ScanError(msg)
            stack.pop()
            start = i + 1
    if stack or depth_paren:
        msg = f'unbalanced braces or parentheses in {rel}'
        raise bl.ScanError(msg)
    flush(len(text))
    return decls


_STYLE_BLOCK = re.compile(r'<style\b[^>]*>(.*?)</style>', re.DOTALL)
_SCRIPT_BLOCK = re.compile(r'<script\b[^>]*>.*?</script>', re.DOTALL)
_STYLE_ATTR = re.compile(r"""\bstyle\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_STYLE_DIRECTIVE = re.compile(r"""\bstyle:([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def svelte_declarations(source: str, rel: str) -> list[Decl]:
    """Declarations from a Svelte file: ``<style>`` blocks, ``style=""`` and ``style:prop=""``."""
    decls: list[Decl] = []
    for block in _STYLE_BLOCK.findall(source):
        decls.extend(parse_declarations(block, rel))
    markup = _HTML_COMMENT.sub('', _STYLE_BLOCK.sub('', _SCRIPT_BLOCK.sub('', source)))
    for match in _STYLE_ATTR.finditer(markup):
        value = match.group(1) if match.group(1) is not None else match.group(2)
        if '{' not in value:  # a template expression is not a literal
            decls.extend(parse_declarations(value, rel))
    for match in _STYLE_DIRECTIVE.finditer(markup):
        value = match.group(2) if match.group(2) is not None else match.group(3)
        if '{' not in value:
            decls.append(Decl(match.group(1).lower(), value.strip(), ''))
    return decls


# --- counting -----------------------------------------------------------------------------------


def _strip_vars(value: str) -> str:
    """Remove every ``var(...)`` (balanced), leaving the literals outside it."""
    out: list[str] = []
    i, n = 0, len(value)
    while i < n:
        if value.startswith('var(', i):
            depth, j = 1, i + 4
            while j < n and depth:
                depth += (value[j] == '(') - (value[j] == ')')
                j += 1
            out.append(' ')
            i = j
        else:
            out.append(value[i])
            i += 1
    return ''.join(out)


def _fallback_count(value: str) -> int:
    count = 0
    for match in re.finditer(r'var\(', value):
        depth, j = 1, match.end()
        top_comma = False
        while j < len(value) and depth:
            c = value[j]
            depth += (c == '(') - (c == ')')
            if c == ',' and depth == 1:
                top_comma = True
            j += 1
        count += 1 if top_comma else 0
    return count


def _nonzero(num: str) -> bool:
    try:
        return float(num) != 0.0
    except ValueError:
        return True


def _colour_count(prop: str, value: str, low: str, is_custom: bool) -> int:
    if not (_COLOUR_PROP.match(prop) or is_custom or prop == 'background'):
        return 0
    count = len(_HEX.findall(value)) + len(_COLOUR_FN.findall(value))
    # Property names inside var(--white) must not read as the colour white.
    names_blanked = re.sub(r'var\(\s*--[\w-]+', 'var(', low)
    return count + sum(1 for w in _WORD.findall(names_blanked) if w in _NAMED_COLOURS)


def _spacing_count(literal: str) -> int:
    count = 0
    for m in _LENGTH.finditer(literal):
        unit = m.group(2) or ''
        if not _nonzero(m.group(1)) or unit in ('%', 'em') or m.group(0) in ('1px', '-1px'):
            continue
        count += 1
    return count


def _is_physical(prop: str, low: str) -> bool:
    return bool(
        _PHYSICAL_PROP.match(prop)
        or (prop in ('left', 'right') and low.replace(' ', '') != '50%')
        or (prop in ('text-align', 'float') and low in ('left', 'right'))
    )


def _is_z_index_literal(literal: str) -> bool:
    return bool(re.fullmatch(r'[-+]?\d+', literal.strip())) and literal.strip() not in ('0', '-1')


def _has_font_weight_literal(literal: str) -> bool:
    return bool(re.search(r'(?<![\w.-])(?:[1-9]00|bold|bolder|lighter)(?![\w.-])', literal))


def _sized_category(prop: str, value: str, low: str, literal: str) -> dict[str, int]:
    """The one length/shadow/weight/z-index/duration category a property can fall in."""
    wraps_var_in_math = bool(_MATH_FN.search(value)) and 'var(' in value
    if prop in ('box-shadow', 'text-shadow'):
        plain = low not in ('none', 'inherit', 'initial', 'unset') and not _is_only_vars(value)
        return {'shadow': 1} if plain else {}
    if _RADIUS_PROP.match(prop) and not wraps_var_in_math:
        return {'radius': sum(1 for m in _LENGTH.finditer(literal) if _nonzero(m.group(1)))}
    if _SPACING_PROP.match(prop) and not wraps_var_in_math:
        return {'spacing': _spacing_count(literal)}
    if prop == 'font-size' and not wraps_var_in_math:
        return {'font-size': int(any(m.group(2) for m in _LENGTH.finditer(literal)))}
    if prop == 'font-weight':
        return {'font-weight': int(_has_font_weight_literal(literal))}
    if prop == 'z-index':
        return {'z-index': int(_is_z_index_literal(literal))}
    if prop in ('transition', 'transition-duration', 'transition-delay'):
        return {'duration': sum(1 for m in _TIME.finditer(literal) if _nonzero(m.group(1)))}
    return {}


def count_decl(decl: Decl, rel: str) -> dict[str, int]:
    """Category counts for one declaration."""
    prop, raw = decl.prop, re.sub(r'\s*!important\s*$', '', decl.value, flags=re.IGNORECASE)
    value = raw.strip()
    low = value.lower()
    is_custom = prop.startswith('--')
    if is_custom and rel.startswith(_TOKEN_SOURCE_DIR) and decl.selector in _TOKEN_BLOCK_SELECTORS:
        return {}  # a component token source: its literals ARE the definition

    counts: dict[str, int] = {
        # fallback + colour see the whole value (a literal inside a fallback is a colour too)
        'fallback': _fallback_count(value),
        'colour': _colour_count(prop, value, low, is_custom),
    }
    if not is_custom:  # lengths in a custom property are token definitions elsewhere
        counts.update(_sized_category(prop, value, low, _strip_vars(value)))
        counts['physical'] = int(_is_physical(prop, low))
    return {cat: n for cat, n in counts.items() if n}


def _is_only_vars(value: str) -> bool:
    """True for ``var(--a)`` / ``var(--a), var(--b)``: references with no literal and no fallback."""
    return bool(value) and _VAR_NAME_ONLY.sub('', value).replace(',', '').strip() == ''


# --- tree walk ----------------------------------------------------------------------------------


def list_files(root: Path) -> list[str]:
    """Tracked repo-relative paths (``git ls-files``); a failing git is a ``ScanError``."""
    try:
        proc = subprocess.run(
            ['git', '-C', str(root), 'ls-files', '-z'], check=True, capture_output=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        msg = f'git ls-files failed in {root}: {exc}'
        raise bl.ScanError(msg) from exc
    return [p for p in proc.stdout.decode('utf-8').split('\0') if p]


def _in_scope(rel: str) -> bool:
    return (
        rel.startswith(_PREFIX)
        and rel.endswith(('.svelte', '.css'))
        and '.test.' not in rel
        and rel != _THEME_CSS
    )


def count_source(rel: str, source: str) -> dict[str, int]:
    """Category totals for one file's text."""
    decls = (
        svelte_declarations(source, rel)
        if rel.endswith('.svelte')
        else parse_declarations(source, rel)
    )
    totals: dict[str, int] = {}
    for decl in decls:
        for cat, n in count_decl(decl, rel).items():
            totals[cat] = totals.get(cat, 0) + n
    return totals


def scan(root: Path, files: list[str] | None = None) -> dict[str, int]:
    """Map ``category::path`` -> count for every in-scope tracked file."""
    if files is None:
        files = list_files(root)
    findings: dict[str, int] = {}
    for rel in sorted(files):
        if not _in_scope(rel):
            continue
        try:
            source = (root / rel).read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as exc:
            msg = f'cannot read {rel}: {exc}'
            raise bl.ScanError(msg) from exc
        for cat, n in count_source(rel, source).items():
            findings[f'{cat}::{rel}'] = n
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--root', type=Path, default=None, help='repository root')
    parser.add_argument('--baseline', type=Path, default=None, help='baseline file')
    parser.add_argument('--tighten', action='store_true', help='lower values / drop stale entries')
    parser.add_argument('--report', action='store_true', help='print per-category totals and exit')
    bl.add_baseline_args(parser)
    args = parser.parse_args(argv)
    try:
        root = args.root or bl.find_root(_HERE, ('backend/app',))
        baseline_path = args.baseline or (
            root / 'scripts' / 'guards' / 'baselines' / 'style_literals.txt'
        )
        findings = scan(root)
        if args.report:
            for cat in CATEGORIES:
                total = sum(v for k, v in findings.items() if k.startswith(f'{cat}::'))
                files = sum(1 for k in findings if k.startswith(f'{cat}::'))
                print(f'{cat:12} {total:6d} in {files:4d} files')
            return 0
        if args.write_baseline:
            written: int = bl.run_write_baseline(args, baseline_path, findings)
            return written
        baseline = bl.load(baseline_path)
    except (bl.ScanError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    return bl.finish_ratchet(
        args,
        baseline_path,
        findings,
        baseline,
        unit='style literals that belong in design tokens',
        heading='stylesheet literals must use design tokens (frontend/src/styles/CLAUDE.md):',
        ok_message=(
            f'style literals: {sum(findings.values())} in {len(findings)} entries,'
            ' within the baseline'
        ),
    )


if __name__ == '__main__':
    sys.exit(main())
