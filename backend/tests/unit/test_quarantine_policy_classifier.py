"""The bucket-policy verdict is conservative: ``enforced`` only when PROVEN.

A Deny that an extra condition, a ``Not*`` element, a narrower principal or a partial resource
could exempt the app's URLs from is ``unknown`` (reported as ``enforcement_unverified``),
never ``enforced``. The cheap, safe broadenings (Principal AWS list form, ``s3:Get*``,
a resource list whose wildcard covers every media key) are recognised. A prefix wildcard
is NOT: media keys are not all under one prefix (legacy ``media/...`` keys exist).
"""

from __future__ import annotations

import json

import pytest

from app.core.constants import STORAGE_QUARANTINE_TAG_KEY
from app.services.storage_presign_identity import classify_quarantine_policy

BUCKET = "media"
TAG = f"s3:ExistingObjectTag/{STORAGE_QUARANTINE_TAG_KEY}"


def _policy(**overrides) -> str:
    stmt = {
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:GetObject",
        "Resource": f"arn:aws:s3:::{BUCKET}/*",
        "Condition": {"StringEquals": {TAG: "true"}},
    }
    for key, value in overrides.items():
        if value is None:
            stmt.pop(key, None)
        else:
            stmt[key] = value
    return json.dumps({"Version": "2012-10-17", "Statement": [stmt]})


def test_the_documented_policy_is_enforced():
    assert classify_quarantine_policy(_policy(), BUCKET) == "enforced"


@pytest.mark.parametrize(
    "overrides",
    [
        {"Principal": {"AWS": ["*"]}},
        {"Principal": {"AWS": "*"}},
        {"Action": "s3:Get*"},
        {"Action": ["s3:*"]},
        {"Resource": [f"arn:aws:s3:::{BUCKET}/*", f"arn:aws:s3:::{BUCKET}"]},
        {"Condition": {"StringEquals": {TAG.lower(): ["true"]}}},
    ],
    ids=["aws-list", "aws-star", "get-wildcard", "s3-star", "resource-list", "key-case"],
)
def test_safe_broadenings_are_recognised(overrides):
    assert classify_quarantine_policy(_policy(**overrides), BUCKET) == "enforced"


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "Condition": {
                "StringEquals": {TAG: "true"},
                "ArnNotEquals": {"aws:PrincipalArn": "arn:aws:iam::1:role/app"},
            }
        },
        {"Condition": {"StringEquals": {TAG: "true", "aws:SourceVpce": "vpce-1"}}},
        {
            "Condition": {
                "StringEquals": {TAG: "true"},
                "StringNotLike": {"aws:userid": "AROA*"},
            }
        },
        {"Condition": {"StringEqualsIgnoreCase": {TAG: "true"}}},
        {"Principal": None, "NotPrincipal": {"AWS": "arn:aws:iam::1:role/app"}},
        {"Resource": None, "NotResource": f"arn:aws:s3:::{BUCKET}/public/*"},
        {"Action": None, "NotAction": "s3:PutObject"},
        {"Principal": {"AWS": "arn:aws:iam::1:root"}},
        {"Resource": f"arn:aws:s3:::{BUCKET}/user_1/file_1/original.mp4"},
        {"Resource": f"arn:aws:s3:::{BUCKET}/*.mp4"},
        {"Resource": f"arn:aws:s3:::{BUCKET}/user_*"},
        {"Action": "s3:PutObject"},
        {"Condition": {"StringEquals": {TAG: "TRUE"}}},
    ],
    ids=[
        "principal-arn-exemption",
        "vpce-key",
        "userid-exemption",
        "other-operator",
        "not-principal",
        "not-resource",
        "not-action",
        "one-principal",
        "probe-key-only",
        "one-extension",
        "one-prefix",
        "wrong-action",
        "case-mismatched-value",
    ],
)
def test_anything_narrower_is_unknown_not_enforced(overrides):
    assert classify_quarantine_policy(_policy(**overrides), BUCKET) == "unknown"


def test_a_policy_without_the_tag_is_missing():
    other = _policy(Condition={"StringEquals": {"s3:ExistingObjectTag/other": "true"}})
    assert classify_quarantine_policy(other, BUCKET) == "missing"
    assert classify_quarantine_policy(None, BUCKET) == "missing"


def test_an_allow_on_the_tag_is_not_a_deny():
    assert classify_quarantine_policy(_policy(Effect="Allow"), BUCKET) == "missing"


def test_one_proven_statement_wins_over_a_narrower_sibling():
    proven = json.loads(_policy())["Statement"][0]
    narrow = dict(proven, Principal={"AWS": "arn:aws:iam::1:root"})
    doc = json.dumps({"Statement": [narrow, proven]})
    assert classify_quarantine_policy(doc, BUCKET) == "enforced"


def test_an_unparseable_document_is_unknown():
    assert classify_quarantine_policy("{not json", BUCKET) == "unknown"
