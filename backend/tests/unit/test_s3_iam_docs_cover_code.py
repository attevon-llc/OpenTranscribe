"""The documented S3 IAM policy must cover every S3 action the backend code calls (issue #1359).

AST-scans ``backend/app`` for object-store client calls (minio-py and boto3 method names),
maps each to the IAM action it needs, and fails when the "Complete IAM policy reference"
section of ``object-storage.md`` -- its table and its example policy -- omits one. A new
``client.<something>`` call that this test cannot classify also fails, so the mapping cannot
silently fall behind the code.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
APP = REPO_ROOT / "backend" / "app"
DOC = REPO_ROOT / "docs-site" / "docs" / "operations" / "object-storage.md"
SECTION = "### Complete IAM policy reference"

pytestmark = pytest.mark.skipif(not DOC.exists(), reason="docs-site not present in this checkout")

# client method name -> IAM actions its request is authorised by
_PUT = {"s3:PutObject", "s3:AbortMultipartUpload"}  # uploads can be multipart and abort on error
METHOD_ACTIONS: dict[str, set[str]] = {
    # reads
    "get_object": {"s3:GetObject"},
    "fget_object": {"s3:GetObject"},
    "stat_object": {"s3:GetObject"},
    "head_object": {"s3:GetObject"},
    "presigned_get_object": {"s3:GetObject"},
    "download_file": {"s3:GetObject"},
    # writes
    "put_object": _PUT,
    "fput_object": _PUT,
    "upload_file": _PUT,
    "upload_fileobj": _PUT,
    "presigned_put_object": {"s3:PutObject"},
    "_create_multipart_upload": {"s3:PutObject"},
    "_complete_multipart_upload": {"s3:PutObject"},
    "_abort_multipart_upload": {"s3:AbortMultipartUpload"},
    # deletes
    "remove_object": {"s3:DeleteObject"},
    "remove_objects": {"s3:DeleteObject"},
    "delete_object": {"s3:DeleteObject"},
    "delete_objects": {"s3:DeleteObject"},
    # listing and bucket probes (HEAD bucket is authorised by s3:ListBucket)
    "list_objects": {"s3:ListBucket"},
    "list_objects_v2": {"s3:ListBucket"},
    "bucket_exists": {"s3:ListBucket"},
    "head_bucket": {"s3:ListBucket"},
    "make_bucket": {"s3:CreateBucket"},
    "create_bucket": {"s3:CreateBucket"},
    # bucket configuration
    "get_bucket_policy": {"s3:GetBucketPolicy"},
    "delete_bucket_policy": {"s3:DeleteBucketPolicy"},
    "get_bucket_versioning": {"s3:GetBucketVersioning"},
    "get_bucket_lifecycle": {"s3:GetLifecycleConfiguration"},
    "set_bucket_lifecycle": {"s3:PutLifecycleConfiguration"},
    "put_bucket_cors": {"s3:PutBucketCORS"},
    # tags and legal hold
    "get_object_tags": {"s3:GetObjectTagging"},
    "set_object_tags": {"s3:PutObjectTagging"},
    "enable_object_legal_hold": {"s3:PutObjectLegalHold"},
    "disable_object_legal_hold": {"s3:PutObjectLegalHold"},
}
PAGINATOR_ACTIONS = {"list_objects_v2": {"s3:ListBucket"}}

# The application's own wrappers share names with client methods; a call to one is not a
# client call (its body is scanned in its own right).
APP_WRAPPERS = {
    "set_object_legal_hold",
    "_get_object_size",
    "get_presigned_url",
    "presigned_download_url",
    "upload_file",
    "download_file",
}
S3ISH = re.compile(
    r"^_?(get|put|remove|delete|stat|list|copy|compose|set|enable|disable|fget|fput|make|head"
    r"|abort|complete|create|select|restore)_(object|objects|bucket|buckets|multipart_upload"
    r"|object_\w+|bucket_\w+|objects_\w+)$|^presigned_\w+_object$"
)


def _scan() -> tuple[dict[str, set[str]], set[str]]:
    """Return ({action: {file:line, ...}}, {unclassified call names})."""
    needed: dict[str, set[str]] = {}
    unknown: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(APP)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            where = f"{rel}:{node.lineno}"
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "DeleteObject":
                if len(node.args) >= 2 or any(k.arg == "version_id" for k in node.keywords):
                    needed.setdefault("s3:DeleteObjectVersion", set()).add(where)
                continue
            if not isinstance(fn, ast.Attribute):
                continue
            name = fn.attr
            if name == "get_paginator" and node.args and isinstance(node.args[0], ast.Constant):
                for a in PAGINATOR_ACTIONS.get(str(node.args[0].value), set()):
                    needed.setdefault(a, set()).add(where)
                continue
            if name in METHOD_ACTIONS:
                if name in APP_WRAPPERS and not _is_client_receiver(fn.value):
                    continue
                for a in METHOD_ACTIONS[name]:
                    needed.setdefault(a, set()).add(where)
                if name == "list_objects" and any(
                    k.arg == "include_version" for k in node.keywords
                ):
                    needed.setdefault("s3:ListBucketVersions", set()).add(where)
            elif S3ISH.match(name) and name not in APP_WRAPPERS:
                unknown.add(f"{name} ({where})")
    return needed, unknown


def _is_client_receiver(value: ast.expr) -> bool:
    """True for ``client.x`` / ``self.client.x`` / ``s3.x`` style receivers."""
    text = ast.unparse(value)
    return bool(re.search(r"(^|\.)(_?client|s3|_s3|s3_client|minio_client)(\(\))?$", text))


def _documented() -> tuple[set[str], set[str]]:
    """(actions named in the section's table, actions in its example policy)."""
    text = DOC.read_text(encoding="utf-8")
    assert SECTION in text, f"{DOC.name} must contain a '{SECTION}' section"
    body = text.split(SECTION, 1)[1]
    body = re.split(r"\n#{1,3} (?!#)", body, maxsplit=1)[0]
    table: set[str] = set()
    for row in re.finditer(r"^\|([^|\n]*)\|", body, re.M):  # first cell of each table row
        table |= set(re.findall(r"`(s3:[A-Za-z]+)`", row.group(1)))
    policy: set[str] = set()
    blocks = re.findall(r"```json\n(.*?)```", body, re.S)
    assert blocks, "the section must carry a complete example policy in a ```json block"
    complete = json.loads(blocks[0])
    for stmt in complete["Statement"]:
        acts = stmt["Action"]
        policy |= {acts} if isinstance(acts, str) else set(acts)
    return table, policy


def test_scan_finds_the_known_core_actions():
    """Guard the guard: a scanner matching nothing would pass everything."""
    needed, _ = _scan()
    for action in (
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:ListBucketVersions",
        "s3:DeleteObjectVersion",
        "s3:PutObjectTagging",
        "s3:PutLifecycleConfiguration",
    ):
        assert action in needed, f"scanner no longer sees {action}"


def test_every_s3_client_call_is_classified():
    _, unknown = _scan()
    assert not unknown, (
        "S3-looking client calls with no IAM mapping in METHOD_ACTIONS (map them, then "
        f"document the action): {sorted(unknown)}"
    )


def test_documented_table_covers_every_action_the_code_calls():
    needed, _ = _scan()
    table, _ = _documented()
    missing = {a: sorted(w)[:3] for a, w in needed.items() if a not in table}
    assert not missing, f"actions called by the code but missing from the doc table: {missing}"


def test_example_policy_grants_every_action_the_code_calls():
    needed, _ = _scan()
    _, policy = _documented()
    missing = sorted(a for a in needed if a not in policy)
    assert not missing, f"actions called by the code but absent from the example policy: {missing}"


def test_doc_table_names_no_action_the_code_never_calls():
    """Keeps the table from rotting into a wish list (and over-broad policies)."""
    needed, _ = _scan()
    table, _ = _documented()
    extra = sorted(a for a in table if a not in needed)
    assert not extra, f"documented but never called by the code: {extra}"
