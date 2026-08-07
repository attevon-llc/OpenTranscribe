# Draft replies — Politiezone MIDOW (#353, #354, #355 + email)

**Not posted yet — for review before publishing.** These go to a real user; the wording is
yours to approve. Nothing here discloses a working exploit path.

> **Corrected 2026-08-07 after Max's follow-up.** He reports the `/flower/` and `/docs/` 404s
> were **their own reverse proxy** — the virtual directory pointed at the wrong port. So those
> were never our bug on his deployment, and the earlier drafts claiming "real bug, now fixed"
> were wrong. The `/flower/` gap we found is genuine and independent (the frontend nginx image
> serves no `/flower/` location, so the button 404s on any prod/PKI stack without the optional
> nginx overlay) — but it is **not** what he hit, and we should not imply otherwise. He also
> declined a hotfix and has worked around registration by blocking `/api/auth/register` at the
> proxy, so nothing is blocking him.

---

## #355 — Flower container always reports as unhealthy

> Thanks for the detailed report — the container inspection you included made this immediate.
>
> You're exactly right about the cause. Flower reuses the backend image, and in v0.4.1 the
> flower service didn't define its own `healthcheck`, so Docker inherited the one baked into
> that image — `curl -f http://localhost:8080/health`, which is the API's port, not Flower's.
> The service was healthy the whole time; only the probe was wrong.
>
> This is already fixed on `master` (commit `ee5f1320`): the flower service now defines
> `curl -fs http://127.0.0.1:5555/${FLOWER_URL_PREFIX:-flower}/healthcheck`. Flower's
> `/healthcheck` endpoint is exempt from `--basic_auth`, which is why it needs no credentials.
>
> Investigating this turned up a second, less visible problem in the same service definition,
> which is also fixed: the persistent database was mounted at `/app` — the directory the
> application code lives in — so the named volume shadowed the image's code. Docker seeds an
> empty named volume from the image once and then keeps it, which means Flower was pinned to
> whatever code existed when the volume was first created and never picked up a rebuilt image.
> It now lives under `/app/temp`. If you want to clear the old one after upgrading:
> `docker volume rm <project>_flower_data` — it only holds task history.
>
> Both ship in v0.5.0.

---

## #354 — Disable self-registration via environment variable

> This is a good catch, and the situation was worse than "not implemented yet" — thank you for
> pushing on it.
>
> `ALLOW_OPEN_REGISTRATION` already existed on `master` and did gate the endpoint, so that part
> of your suggestion was in place. But the admin UI has shown an **"Allow self-registration"
> toggle since v0.4.0 that was wired to nothing**: the endpoint read the environment variable
> while the UI wrote a database key, and the two were never connected (the environment variable
> was missing from the env→DB mapping table). So an administrator could turn the switch off,
> get a success message, and still have open registration — which is what you were seeing.
>
> In v0.5.0:
>
> - `allow_registration` is DB-backed and authoritative, with `ALLOW_OPEN_REGISTRATION` as the
>   environment fallback. Precedence is DB > `.env` > default, the same as every other auth
>   setting.
> - The "Register" link is hidden and `/register` redirects when it's off, instead of letting
>   someone fill in the form and then hit a 403.
> - There's a companion `local_enabled` switch that stops accounts holding a *local password*
>   from signing in at all — which is what you actually want when LDAP owns identity.
>
> Two behaviours worth knowing before you set it:
>
> - The username/password form stays visible with `local_enabled=false`, because LDAP
>   authenticates through that same form.
> - Your active super_admin can still sign in with its password. That exemption is deliberate:
>   authentication configuration is super_admin-gated, so without it a misconfigured IdP would
>   lock you out of the screen you need to fix it.
>
> For your deployment the combination is `ldap_enabled=true`, `local_enabled=false`,
> `allow_registration=false`.

---

## #353 — Generic OIDC / Authentik does not work

> This report was accurate in every particular, including the code references — thank you, it
> saved us a lot of time.
>
> You're right that the implementation was Keycloak-specific despite the documentation. Every
> endpoint was built by string-concatenating `server_url + /realms/<realm>/...`, and the one
> place that *did* fetch `.well-known/openid-configuration` was the admin "Test connection"
> button, which parsed the metadata for display and then discarded it. The docs page claiming
> "Full OIDC discovery support" was simply wrong, and we've corrected it.
>
> There was a second problem you'd have hit immediately after the URLs: roles were read from
> `realm_access.roles`, which is a Keycloak-only claim. Authentik puts group membership in
> `groups`, so even with working URLs your admin mapping would have silently failed.
>
> v0.5.0 adds real OIDC discovery:
>
> - `keycloak_discovery_url` (also accepted as `OIDC_DISCOVERY_URL`) — when set, the
>   authorization, token, userinfo, JWKS and end-session endpoints and the issuer all come from
>   the provider's metadata document. The realm-based construction remains the fallback, so
>   existing Keycloak deployments are unaffected.
> - `keycloak_roles_claim` — set this to `groups` for Authentik.
> - The ID token is now validated in preference to the access token. Keycloak happens to issue
>   JWT access tokens; the OIDC spec only guarantees that of the ID token, which is the other
>   reason non-Keycloak providers didn't work.
>
> **One question so the documentation example is right for you:** which claim does your
> Authentik provider actually put the group membership in — `groups`, or something you've
> mapped yourself? We'd like the worked example in the docs to match a real Authentik setup
> rather than our assumption.

---

## Email reply (Dewispelaere Max)

> Hello Max,
>
> Following up on all four points, plus something we found while investigating them.
>
> **1. LDAP configuration page not appearing.** The page exists and is at Settings →
> Authentication → LDAP, but it requires the **super_admin** role — a plain `admin` sees no
> "Authentication" entry at all, which is almost certainly what you're hitting. That was true in
> v0.4.1 as well; it isn't a regression, it's a documentation failure on our side, and we've
> fixed the docs.
>
> Two related improvements in v0.5.0: an admin now sees the entry disabled with an explanation
> instead of it silently not existing, and there's finally a way to **create additional
> super_admins from the UI** (Settings → Users → Role). Previously the role dropdown offered
> only "User" and "Administrator", so the tier couldn't be granted without calling the API
> directly. On startup we also now repair any account left in an older inconsistent
> admin/superuser state, which may be how yours ended up as it is.
>
> **2. Queue dashboard and docs 404s.** Thanks for closing the loop — and no need to excuse it,
> a wrong port in a virtual directory is exactly the kind of thing that presents as an
> application bug. Nothing to apologise for.
>
> Your report did surface a separate, genuine gap on our side that you would have hit eventually:
> the `/flower/` path was only served by our optional nginx overlay, so on a prod or PKI
> deployment without that overlay the button 404s regardless of the proxy in front. The frontend
> image now serves it too. While fixing that we also made Flower admin-only and put it behind a
> session check — it exposes task names and arguments, and it was previously reachable by any
> logged-in user.
>
> On the docs page: a related nginx location-precedence bug was fixed on master in `133efc77`,
> which caused the same "wrong baseUrl" symptom on direct container access. It may or may not
> have contributed to what you saw — your proxy port explains it on its own.
>
> **4. Self-registration with LDAP enabled.** Covered in #354 above — the admin toggle for this
> existed but was not connected to anything, which is worse than it simply being absent. Fixed,
> along with a switch that disables local password login outright. Your proxy-level block on
> `/api/auth/register` is a sound workaround in the meantime; once you're on v0.5.0 you can drop
> it and use the setting, which also hides the "Create Account" link rather than letting someone
> fill in the form first.
>
> **One thing to raise directly.** Your reports prompted a full audit of the authentication
> code, and it found a number of issues more serious than the ones you reported — including a
> way to bypass multi-factor authentication. None of them are specific to your deployment and we
> have no reason to think anyone has exploited them, but if you have MFA enabled we'd recommend
> upgrading promptly once v0.5.0 is out. The full list is in the release notes under Security.
>
> If any of this is blocking you before the release, tell us which and we'll look at a v0.4.2
> patch with just those fixes.
>
> Thank you again — this was unusually good feedback, and it has made the product materially
> safer for everyone.
