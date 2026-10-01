---
sidebar_position: 6
---

# Password policy levels

OpenTranscribe has four password levels, chosen in **Settings -> Authentication -> Local**
(a four-card picker that previews the exact rules before you save) or with
`PASSWORD_POLICY_PROFILE`:

| Level | For | Rules |
| --- | --- | --- |
| `basic` | Local development, small teams | 8 characters, no composition rules, no expiry, history or minimum age |
| `standard` | Most installs (recommended) | NIST SP 800-63B-4: long passphrases, no composition rules, no forced rotation, breached-password screening |
| `hardened` | Government, DoD, air-gapped | DoD STIG / FedRAMP style: 12 characters, complexity, 60-day rotation, history of 24 |
| `custom` | Anything else | Exactly the individual `PASSWORD_*` values you set |

`nist` and `stig` still work everywhere as aliases of `standard` and `hardened`.

**Trade-off.** Lighter levels are easier on users but accept weaker passwords. `standard` follows
current NIST guidance: length plus a breached-password check protects better than complexity rules
and scheduled changes, which push people toward predictable patterns. `hardened` keeps complexity,
expiry and history for compliance regimes that still require them. `basic` is opt-in only and is
not recommended for anything exposed to the internet.

## Which level do I get?

* **Fresh installs** (`.env` created from `.env.example` by `setup-opentranscribe.sh`) get
  `PASSWORD_POLICY_PROFILE=standard`.
* **Existing installs** have no such line in their `.env`, so the code default `hardened` applies and
  nothing changes on upgrade. To move to `standard`, pick it in the admin UI or add
  `PASSWORD_POLICY_PROFILE=standard` to `.env` and restart. Existing passwords keep working; existing
  expiry dates stop being enforced; new passwords are checked against the new rules.
* Some DoD/agency customers mandate the STIG values. Those deployments should set
  `PASSWORD_POLICY_PROFILE=hardened` explicitly so the choice is recorded.

Precedence is unchanged: admin UI (`auth_config` table) > `.env` > coded default.

## Settings

| Setting | `basic` | `standard` | `hardened` | `custom` |
| --- | --- | --- | --- | --- |
| Minimum length | 8 | 15; 8 if the account is MFA-protected | `PASSWORD_MIN_LENGTH` (12) | `PASSWORD_MIN_LENGTH` |
| Maximum length (`PASSWORD_MAX_LENGTH`, 0 = default) | 128, a configured value is raised to at least 64 | same as `basic` | none | as configured |
| Composition (`PASSWORD_REQUIRE_*`) | never | never | upper, lower, digit, special | as configured |
| Unicode / spaces | all accepted, NFKC-normalised | same | accepted | accepted |
| Expiry (`PASSWORD_MAX_AGE_DAYS`) | never | never (change on evidence of compromise: admin reset, `must_change_password`) | 60 days | as configured |
| History (`PASSWORD_HISTORY_COUNT`), minimum age (`password_min_age_hours`) | off | off | 24 / 24 h | as configured |
| Blocklist + context words | on if a list is installed | on | off | off |
| Account lockout | unchanged: `ACCOUNT_LOCKOUT_*` | unchanged | unchanged | unchanged |

`PASSWORD_BLOCKLIST_ENABLED=true|false` overrides the level default in any level.
`PASSWORD_POLICY_ENABLED=false` still disables the whole policy.

An unrecognised `PASSWORD_POLICY_PROFILE` value is logged and treated as `hardened`.

### In the admin UI

Under `basic` and `standard` the individual fields are hidden, because the level fixes those rules.
`hardened` shows its values read-only; choose `custom` to edit them. In `custom` the server enforces
bounds on save (minimum length 8-128, history 0-100, expiry 0-3650 days, minimum age 0-8760 hours,
and a maximum length that is not below the minimum); an operator can still set a lower minimum with
`PASSWORD_MIN_LENGTH`. The page also controls the breached-password check (level default / on / off),
shows whether the list is installed, and has the optional online lookup switch and
**Require MFA for administrators**. Every change is written to the audit log.

### "MFA-protected" (the 8-character floor)

Under `standard` the minimum is 8 instead of 15 when the account has TOTP enrolled, or MFA is
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

1. is on the **offline blocklist** (when installed), or
2. contains a **context word** of 4+ characters: the product name, any word in
   `PASSWORD_CONTEXT_WORDS` (comma-separated, e.g. your organisation), the account's email local
   part, or a part of the account's full name.

Existing weak-pattern warnings (`password123`, sequences, repeats) are unchanged.

### Offline list (installed on request)

OpenTranscribe does **not** ship a password list. Install one with a single command:

```bash
./opentranscribe.sh download-models password-blocklist
```

This builds the 100,000 most-breached passwords from Have I Been Pwned "Pwned Passwords" as
**SHA-1 hashes only** (uppercase hex, one per line, most common first) in
`<MODEL_CACHE_DIR>/password-blocklist/pwned-passwords-top100k-sha1.txt`, plus a small
`.meta.json` recording the retrieval date. It walks the official k-anonymity range API
(1,048,576 small requests; expect a few hours and roughly 35 GB of transfer, resumable if interrupted), then keeps the top
100,000 hashes by breach count. Re-run it to refresh. Nothing is sent but 5-character hash
prefixes, and no plaintext password is requested or stored.

*Data source and terms.* Pwned Passwords is published by Have I Been Pwned (Troy Hunt). Its
documentation states "no licensing or attribution requirement"
([API v3](https://haveibeenpwned.com/API/V3), [Passwords](https://haveibeenpwned.com/Passwords),
checked 2026-10-01); we credit it anyway as a courtesy.

Matching: the password is NFKC-normalised and its SHA-1 compared. Because the hashes are
case-exact, the lowercased and case-folded forms are checked too, so `PASSWORD` is caught by the
entry for `password`.

**Air-gapped installs.** `scripts/build-offline-package.sh` builds the list into the package (set
`INCLUDE_PASSWORD_BLOCKLIST=false` to skip it) and `install-offline-package.sh` installs it under
`models/password-blocklist/`. Or build it on a connected machine and copy that directory.

**If no list is installed** the check is skipped, one startup `WARNING` names the command above,
and the admin Settings page shows "breached-password list not installed". The context-word check
still applies. Nothing is fetched at runtime unless the optional online lookup below is on.

**Your own list.** Set `PASSWORD_BLOCKLIST_PATH` to a file of SHA-1 hashes (40 hex characters per
line, optionally `HASH:count`) or a legacy plaintext file (one password per line, matched
case-insensitively); the format is detected per line. It **replaces** the default list; edits are
picked up by file modification time without a restart. An unreadable path logs an ERROR and falls
back to the default location. The whole list is held in memory (100k hashes is roughly 10 MB).

To regenerate with different options:
`docker compose run --rm backend python -m app.scripts.build_password_blocklist --help`.

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
| Min 15 characters single-factor; 8 with MFA | `standard` level, `min_length_for()` |
| Accept at least 64 characters; no truncation | `PASSWORD_MAX_LENGTH` floor 64 (default 128); no hash truncation |
| Accept all printing ASCII, space, Unicode; normalise | no character-class filter; NFKC |
| SHALL NOT impose composition rules | `PASSWORD_REQUIRE_*` ignored under `standard` |
| SHALL NOT require periodic change; change on compromise | `PASSWORD_MAX_AGE_DAYS` ignored under `standard`; admin reset / `must_change_password` |
| SHALL check against a blocklist (breached, dictionary, context words) | offline list, context words, optional range lookup |
| Rate-limit failed attempts | `ACCOUNT_LOCKOUT_*` |
| Multi-factor for privileged accounts | `MFA_REQUIRED_FOR_ADMINS` |

### DISA STIG / FedRAMP IA-5 (`hardened` level)

| Control | Setting |
| --- | --- |
| Minimum length 12-15 | `PASSWORD_MIN_LENGTH` (12; raise to 15 for the current DoD value) |
| Upper / lower / digit / special | `PASSWORD_REQUIRE_UPPERCASE/LOWERCASE/DIGIT/SPECIAL` |
| Maximum lifetime 60 days | `PASSWORD_MAX_AGE_DAYS` |
| Minimum lifetime 24 hours | `password_min_age_hours` |
| Reuse prevention (24) | `PASSWORD_HISTORY_COUNT` |
| Lockout 3-5 attempts | `ACCOUNT_LOCKOUT_THRESHOLD` / `_DURATION_MINUTES` |
