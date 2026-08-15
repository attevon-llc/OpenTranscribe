"""Apply one mutmut diff (read from stdin, `mutmut show <id>` format) to live source.

Extracted from `run-mutation-tests.sh`'s `_verify_survivor_locked` so it can be exercised
directly by a regression test (`scripts/tests/test-verify-apply-mutant.sh`) without booting
mutmut or the auth test suite. Behaviour is unchanged from the inline version this replaced;
see that test file's header for the bug this fixes (issue #459).

Env vars (both required):
    MUT_TARGET -- absolute path of the file to mutate in place.
    MUT_FUNC   -- the mutant's function name, mutmut's own encoding: a bare name for a
                  top-level function (`_get_store`), or `ǁ`-joined (U+01C1) for a class
                  method (`ǁInMemoryStoreǁdelete` -> class InMemoryStore, method delete).

Stdout (exactly one line, matched by the caller):
    APPLIED             -- the file was patched.
    NODIFF              -- `mutmut show` produced no usable diff (nothing to apply).
    AMBIGUOUS <n>        -- the context block matched `n` times (0 or >1), not applied.
"""

import ast
import os
import pathlib
import sys


def main() -> None:
    show = sys.stdin.read().splitlines()
    old: list[str] = []
    new: list[str] = []
    for line in show:
        if line.startswith(('---', '+++', '@@', '#')):
            continue
        if line.startswith('-'):
            old.append(line[1:])
        elif line.startswith('+'):
            new.append(line[1:])
        else:
            old.append(line[1:] if line.startswith(' ') else line)
            new.append(line[1:] if line.startswith(' ') else line)

    if not old or old == new:
        print('NODIFF')
        return

    old_block, new_block = '\n'.join(old), '\n'.join(new)
    target = pathlib.Path(os.environ['MUT_TARGET'])
    source = target.read_text()

    # Narrow to the mutated function/method when its name is known, so a context block
    # shared by two near-duplicate functions is no longer ambiguous. `want` is `ǁ`-joined
    # for a class method and bare for a top-level function -- mutmut's own naming scheme.
    lo, hi = 0, len(source)
    indent = ''
    want = os.environ.get('MUT_FUNC', '')
    if want:
        lines = source.splitlines(keepends=True)
        offsets: list[int] = []
        run = 0
        for line in lines:
            offsets.append(run)
            run += len(line)
        offsets.append(run)
        parts = [p for p in want.split('ǁ') if p]
        target_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None
        tree = ast.parse(source)
        if len(parts) == 2:
            class_name, method_name = parts
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == class_name:
                    for child in node.body:
                        if (
                            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and child.name == method_name
                        ):
                            target_node = child
                            break
                    break
        elif len(parts) == 1:
            (fname,) = parts
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fname:
                    target_node = node
                    break
        if target_node is not None:
            lo = offsets[target_node.lineno - 1]
            # end_lineno is None only for a node built without location info (e.g. via
            # ast.parse's own fallback for a syntax error); a node reached via ast.walk
            # over ast.parse's normal output always has one.
            end_lineno = (
                target_node.end_lineno if target_node.end_lineno is not None else len(lines)
            )
            hi = offsets[min(end_lineno, len(lines))]
            # mutmut prints the diff dedented to the OWN frame of the function/method
            # body -- a class method is reported as if `def name(...)` started at column
            # 0, stripping exactly the indentation of the class. Re-indent the diff text
            # by the real column offset of the def (0 for a top-level function, so this
            # is a no-op there) before matching it against the actual, un-dedented file.
            indent = ' ' * target_node.col_offset

    if indent:
        old_block = '\n'.join(indent + line if line else line for line in old_block.split('\n'))
        new_block = '\n'.join(indent + line if line else line for line in new_block.split('\n'))

    region = source[lo:hi]
    count = region.count(old_block)
    if count != 1:
        # Fall back to the whole file (same indentation, still deterministic when the
        # AST located the node above): mutmut names nested/decorated helpers in ways
        # ast may not match, and a whole-file unique match is still unambiguous.
        count = source.count(old_block)
        if count != 1:
            print(f'AMBIGUOUS {count}')
            return
        target.write_text(source.replace(old_block, new_block))
        print('APPLIED')
        return

    target.write_text(source[:lo] + region.replace(old_block, new_block) + source[hi:])
    print('APPLIED')


if __name__ == '__main__':
    main()
