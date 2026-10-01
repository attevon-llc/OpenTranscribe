---
sidebar_position: 6
---

# Password policy profiles

OpenTranscribe ships three password rule sets, selected with `PASSWORD_POLICY_PROFILE`
(or Settings -> Authentication -> Password Policy):

| Profile | Intent |
| --- | --- |
| `nist` | NIST SP 800-63B-4: long passphrases, no composition rules, no forced rotation, breached-password screening |
| `stig` | DoD STIG / FedRAMP style: 12 characters, complexity, 60-day rotation, history of 24 |
| `custom` | Exactly the individual `PASSWORD_*` values you set |

## Which profile do I get?

* **Fresh installs** (`.env` created from `.env.example` by `setup-opentranscribe.sh`) get
  `PASSWORD_POLICY_PROFILE=nist`.
* **Existing installs** have no such line in their `.env`, so the code default `stig` applies and
  nothing changes on upgrade. To move to NIST, add `PASSWORD_POLICY_PROFILE=nist` to `.env` and
  restart (or set it in the admin UI). Existing passwords keep working; existing expiry dates
  stop being enforced; new passwords are checked against the new rules.
* Some DoD/agency customers mandate the STIG values. Those deployments should set
  `PASSWORD_POLICY_PROFILE=stig` explicitly so the choice is recorded.

Precedence is unchanged: admin UI (`auth_config` table) > `.env` > coded default.

## Settings

| Setting | `nist` | `stig` | `custom` |
| --- | --- | --- | --- |
| Minimum length | 15; 8 if the account is MFA-protected | `PASSWORD_MIN_LENGTH` (12) | `PASSWORD_MIN_LENGTH` |
| Maximum length (`PASSWORD_MAX_LENGTH`, 0 = default) | 128, a configured value is raised to at least 64 | none | as configured |
| Composition (`PASSWORD_REQUIRE_*`) | never | upper, lower, digit, special | as configured |
| Unicode / spaces | all accepted, NFKC-normalised | accepted | accepted |
| Expiry (`PASSWORD_MAX_AGE_DAYS`) | never (change on evidence of compromise: admin reset, `must_change_password`) | 60 days | as configured |
| History (`PASSWORD_HISTORY_COUNT`), minimum age | off | 24 / 24 h | as configured |
| Blocklist + context words | on | off | off |
| Account lockout | unchanged: `ACCOUNT_LOCKOUT_*` | unchanged | unchanged |

`PASSWORD_BLOCKLIST_ENABLED=true|false` overrides the profile default in any profile.
`PASSWORD_POLICY_ENABLED=false` still disables the whole policy.

An unrecognised `PASSWORD_POLICY_PROFILE` value is logged and treated as `stig`.

### "MFA-protected" (the 8-character floor)

Under `nist` the minimum is 8 instead of 15 when the account has TOTP enrolled, or MFA is
mandatory for it (`MFA_REQUIRED`, or `MFA_REQUIRED_FOR_ADMINS` for an admin). This is applied on
password change and reset and on admin-set passwords. New registrations and invitation
acceptance use 15, because the account has no second factor yet.

### Unicode and normalisation

Passwords are NFKC-normalised before length counting, blocklist comparison **and hashing**, so
the same visible passphrase typed with composed vs decomposed accents (or full-width forms)
is one password. Hashes created before this change were computed over the raw string; login
tries the normalised form first and the raw form second, so no existing user is locked out.
Nothing is truncated: the hash schemes in use (PBKDF2-SHA256, bcrypt-SHA256) have no 72-byte limit.

## Breached / common password screening

When enabled, a new password is rejected if, after NFKC + case folding, it:

1. is an entry of the **offline blocklist**, or
2. contains a **context word** of 4+ characters: the product name, any word in
   `PASSWORD_CONTEXT_WORDS` (comma-separated, e.g. your organisation), the account's email local
   part, or a part of the account's full name.

Existing weak-pattern warnings (`password123`, sequences, repeats) are unchanged.

### Offline list

`backend/app/auth/data/common_passwords.txt`: the UK NCSC top-100,000 most-used passwords
(99,835 entries), obtained from SecLists (MIT). Source and licence are recorded in
`backend/app/auth/data/README.md`. It works on air-gapped installs and nothing leaves the host.

To use a larger list, set `PASSWORD_BLOCKLIST_PATH` to a UTF-8 file with one password per line
(for example a downloaded HIBP/rockyou-derived list, `cat`-ed together with the bundled one). It
**replaces** the bundled list; edits are picked up by file modification time without a restart.
An unreadable path logs an ERROR and falls back to the bundled list rather than disabling the check.
The whole list is held in memory (about 100k entries is roughly 10 MB); keep that in mind for
multi-million-entry files.

### Optional online lookup (default OFF)

`PASSWORD_HIBP_ENABLED=true` adds a Have I Been Pwned-style range query
(`PASSWORD_HIBP_URL`, default `https://api.pwnedpasswords.com/range`). Only the first five hex
characters of the password's SHA-1 are sent (k-anonymity) with the `Add-Padding` header; the password
and the full hash never leave the host. Leave it **off for air-gapped installs**: it is the only
outbound call in the policy.

**Fail-open for availability.** If the service times out (`PASSWORD_HIBP_TIMEOUT_SECONDS`, default 3),
is unreachable, returns a non-200 or garbage, the password is **accepted** and a `WARNING`
("Breached-password range lookup ... failing open") is logged. A third-party outage must not
stop users registering or recovering accounts. The offline list is therefore the baseline control,
and the online lookup an additional one. The lookup only runs once every other check has passed.

## MFA for administrators

`MFA_REQUIRED_FOR_ADMINS` (default `false`): when `true` and `MFA_ENABLED=true`, any user with role
`admin` or `super_admin` must enrol TOTP at next login and verify it on every login, even if `MFA_REQUIRED=false`.
**Recommended `true`** for every deployment that enables MFA; it defaults to `false` only so an
upgrade cannot lock an admin out before they have enrolled. It has no effect while `MFA_ENABLED=false`.
Users authenticated by an external IdP (OIDC/PKI/SAML) are subject to the same IdP-owns-the-second-factor
exemptions as `MFA_REQUIRED`.

WebAuthn / passkeys (phishing-resistant, AAL2/AAL3) are a planned follow-up and are not part of this change.

## Mapping

### NIST SP 800-63B-4 (section 3.1.1)

| Requirement | Where |
| --- | --- |
| Min 15 characters single-factor; 8 with MFA | `nist` profile, `min_length_for()` |
| Accept at least 64 characters; no truncation | `PASSWORD_MAX_LENGTH` floor 64 (default 128); no hash truncation |
| Accept all printing ASCII, space, Unicode; normalise | no character-class filter; NFKC |
| SHALL NOT impose composition rules | `PASSWORD_REQUIRE_*` ignored under `nist` |
| SHALL NOT require periodic change; change on compromise | `PASSWORD_MAX_AGE_DAYS` ignored under `nist`; admin reset / `must_change_password` |
| SHALL check against a blocklist (breached, dictionary, context words) | offline list, context words, optional range lookup |
| Rate-limit failed attempts | `ACCOUNT_LOCKOUT_*` |
| Multi-factor for privileged accounts | `MFA_REQUIRED_FOR_ADMINS` |

### DISA STIG / FedRAMP IA-5 (`stig` profile)

| Control | Setting |
| --- | --- |
| Minimum length 12-15 | `PASSWORD_MIN_LENGTH` (12; raise to 15 for the current DoD value) |
| Upper / lower / digit / special | `PASSWORD_REQUIRE_UPPERCASE/LOWERCASE/DIGIT/SPECIAL` |
| Maximum lifetime 60 days | `PASSWORD_MAX_AGE_DAYS` |
| Minimum lifetime 24 hours | `password_min_age_hours` |
| Reuse prevention (24) | `PASSWORD_HISTORY_COUNT` |
| Lockout 3-5 attempts | `ACCOUNT_LOCKOUT_THRESHOLD` / `_DURATION_MINUTES` |
