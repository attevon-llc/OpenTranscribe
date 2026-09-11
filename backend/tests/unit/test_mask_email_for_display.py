"""``mask_email_for_display`` (issue #904) — the disambiguation property that is
the whole reason it exists as a function separate from ``mask_identifier``.
"""

from __future__ import annotations

from app.auth.utils import mask_email_for_display
from app.auth.utils import mask_identifier


def test_two_different_local_parts_render_differently():
    """The property this function exists for: two accounts sharing a domain and
    a first name must not render identically in the picker."""
    jane = mask_email_for_display("jane@acme.com")
    john = mask_email_for_display("john@acme.com")
    assert jane != john
    assert jane == "ja***@acme.com"
    assert john == "jo***@acme.com"


def test_mask_identifier_collides_on_the_same_pair_as_the_written_control():
    """The control this test guards against silently regressing: `mask_identifier`
    (log-safety masking, one char of local part) genuinely collides on the exact
    pair `mask_email_for_display` disambiguates. If this ever stops colliding,
    the docstring's justification for keeping the two functions separate needs
    re-checking, not just this test."""
    assert mask_identifier("jane@acme.com") == mask_identifier("john@acme.com")
    assert mask_identifier("jane@acme.com") == "j***@acme.com"


def test_a_one_character_local_part_masks_to_no_visible_characters():
    assert mask_email_for_display("j@acme.com") == "***@acme.com"


def test_a_value_with_no_at_sign_falls_back_to_the_username_rule():
    assert mask_email_for_display("justauser") == "ju***"
    assert mask_email_for_display("j") == "j***"
    assert mask_email_for_display("") == "***"


def test_domain_is_preserved_verbatim():
    assert mask_email_for_display("alice@example.org") == "al***@example.org"
