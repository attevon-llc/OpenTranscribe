#!/bin/bash
# Regression test for scripts/mutation-verify-apply.py (issue #459).
#
# `run-mutation-tests.sh --verify <mutant-id>` applies a single mutmut diff to LIVE source
# via exact-text context matching, so a survivor can be checked against the CURRENT test
# suite instead of trusted on mutmut's own (sometimes stale) verdict. It worked for every
# top-level-function mutant triaged on this branch (lockout, dependencies, security) but
# failed 100% of the time for a class-method mutant -- e.g.
# `app.auth.session.xǁInMemoryStoreǁdelete__mutmut_2` -- reporting
# "UNVERIFIABLE (context block matched 0 times)" even though the mutation is real and the
# context text is genuinely present in the file.
#
# Root cause: `mutmut show` prints a method's diff DEDENTED to the method body's own frame,
# as if `def method(...)` started at column 0 -- i.e. with the class's indentation stripped.
# The old inline patch-application logic read that diff and matched it against the actual,
# un-dedented file text, so the context lines never matched anything for a method whose real
# column offset is > 0. A top-level function's diff has no such offset (it already starts at
# column 0), which is why the bug was invisible until the first class-method `--verify` call
# this session.
#
# The fix (scripts/mutation-verify-apply.py) locates the mutated def via `ast` -- handling
# both a bare function name and mutmut's `ǁ`-joined `ClassNameǁmethod_name` class-method
# encoding -- and re-indents the diff text by that def's real `col_offset` before matching,
# which is a no-op (indent "") for a top-level function and therefore does not change
# already-working behaviour.
#
# This test drives the extracted script directly with synthetic mutmut-shaped input, so it
# needs neither mutmut, Redis, nor the auth test suite -- it is a regression test for the
# patch-application logic, not for any of that machinery.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
APPLY_PY="$REPO_ROOT/scripts/mutation-verify-apply.py"
PYTHON="${PYTHON:-python3}"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

pass=0
fail=0

ok() { echo "  ok   - $1"; pass=$((pass + 1)); }
bad() { echo "  FAIL - $1"; fail=$((fail + 1)); }

FIXTURE="$TMP_ROOT/fixture.py"
cat >"$FIXTURE" <<'PYEOF'
def top_level_func(x):
    with open("/dev/null"):
        if x:
            return 1
        return 0


class Foo:
    """A class whose method body sits two indent levels deep."""

    def method(self, key):
        with self._lock:
            if key:
                return 1
            return 0
PYEOF

echo "mutation-verify-apply selftest"

# --- Case: class-method mutant -- THE bug. mutmut prints this diff DEDENTED to the
# method's own frame (as if `def method` started at column 0); the real file has it at
# column 4 (inside `class Foo:`). Both functions in the fixture contain "return 1" so a
# wrong-scope match would silently mutate top_level_func instead and still print APPLIED --
# this case checks not just the verdict but WHERE the edit landed.
target="$TMP_ROOT/case1.py"
cp "$FIXTURE" "$target"
out=$(MUT_TARGET="$target" MUT_FUNC="ǁFooǁmethod" "$PYTHON" "$APPLY_PY" <<'DIFFEOF'
--- app/fixture.py
+++ app/fixture.py
@@ -3,5 +3,5 @@
     with self._lock:
         if key:
-            return 1
+            return 2
         return 0
DIFFEOF
)
if [[ "$out" == "APPLIED" ]]; then
    ok "class method: verdict is APPLIED (was UNVERIFIABLE before the fix)"
else
    bad "class method: expected APPLIED, got '$out'"
fi
method_body=$(awk '/def method/,0' "$target")
toplevel_body=$(awk '/def top_level_func/,/^class Foo/' "$target")
if grep -q "return 2" <<<"$method_body" && ! grep -q "return 2" <<<"$toplevel_body"; then
    ok "class method: mutation landed inside Foo.method only"
else
    bad "class method: mutation landed in the wrong place (or not at all)"
fi

# --- Case: top-level function mutant -- must keep working. col_offset is 0 here, so the
# fix's re-indent step is a no-op and this must behave exactly as before the fix.
target="$TMP_ROOT/case2.py"
cp "$FIXTURE" "$target"
out=$(MUT_TARGET="$target" MUT_FUNC="top_level_func" "$PYTHON" "$APPLY_PY" <<'DIFFEOF'
--- app/fixture.py
+++ app/fixture.py
@@ -1,5 +1,5 @@
     with open("/dev/null"):
         if x:
-            return 1
+            return 2
         return 0
DIFFEOF
)
if [[ "$out" == "APPLIED" ]]; then
    ok "top-level function: verdict is APPLIED"
else
    bad "top-level function: expected APPLIED, got '$out'"
fi
toplevel_body=$(awk '/def top_level_func/,/^class Foo/' "$target")
method_body=$(awk '/def method/,0' "$target")
if grep -q "return 2" <<<"$toplevel_body" && ! grep -q "return 2" <<<"$method_body"; then
    ok "top-level function: mutation landed inside top_level_func only"
else
    bad "top-level function: mutation landed in the wrong place (or not at all)"
fi

# --- Case: identical old/new (mutmut sometimes shows a no-op diff) -> NODIFF, not APPLIED,
# and the file must be left untouched.
target="$TMP_ROOT/case3.py"
cp "$FIXTURE" "$target"
out=$(MUT_TARGET="$target" MUT_FUNC="top_level_func" "$PYTHON" "$APPLY_PY" <<'DIFFEOF'
--- app/fixture.py
+++ app/fixture.py
@@ -1,3 +1,3 @@
     with open("/dev/null"):
         if x:
             return 1
DIFFEOF
)
if [[ "$out" == "NODIFF" ]] && cmp -s "$FIXTURE" "$target"; then
    ok "identical old/new diff: NODIFF, file untouched"
else
    bad "identical old/new diff: expected NODIFF + untouched file, got '$out'"
fi

# --- Case: context text absent everywhere -> AMBIGUOUS 0, not a silent false APPLIED.
# This is the exact failure shape the bug produced for every class-method mutant before the
# fix: a real, present mutation reported as if its context could not be found at all.
target="$TMP_ROOT/case4.py"
cp "$FIXTURE" "$target"
out=$(MUT_TARGET="$target" MUT_FUNC="top_level_func" "$PYTHON" "$APPLY_PY" <<'DIFFEOF'
--- app/fixture.py
+++ app/fixture.py
@@ -1,3 +1,3 @@
    this_context_does_not_exist_in_the_file()
-    return 1
+    return 2
DIFFEOF
)
if [[ "$out" == "AMBIGUOUS 0" ]] && cmp -s "$FIXTURE" "$target"; then
    ok "context not found anywhere: AMBIGUOUS 0, file untouched"
else
    bad "context not found anywhere: expected 'AMBIGUOUS 0' + untouched file, got '$out'"
fi

echo
echo "$pass passed, $fail failed"
if [[ "$fail" -gt 0 ]]; then
    exit 1
fi
