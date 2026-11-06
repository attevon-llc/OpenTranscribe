# scripts — test overlays: auth IdPs, mock LLM, mock ASR

Moved intact from the root `CLAUDE.md`. Fixtures and the scenario-model table: `backend/tests/fixtures/CLAUDE.md`.

### Auth method overlays

Configure auth via Admin UI (Settings → Authentication); DB config takes precedence over `.env`. Pass test-container flags to spin up local IdPs:

```bash
./opentr.sh start dev --with-ldap-test       # LDAP at localhost:3890, UI :17170 (admin/admin_password)
./opentr.sh start dev --with-keycloak-test   # a Keycloak IdP to test OIDC against, localhost:8180 (admin/admin)
                                             # first start builds a pre-augmented image once; ~25 s to healthy after — see below
./opentr.sh start dev --with-authentik-test  # an Authentik IdP to test OIDC against, localhost:9022 (bootstrap: admin@example.com/admin_password)
./opentr.sh start prod --build --with-pki    # PKI/mTLS at https://localhost:5182 (prod-only — Vite can't do mTLS)
```

### Mock LLM (chat / AI features without a model)

```bash
./opentr.sh start dev --with-mock-llm        # OpenAI-compatible mock at http://mock-llm:5199/v1
```

Runs `scripts/mock-llm-server.py` on the app network so chat, summarization and
topic extraction work with **no GPU, API key, or internet**. Only token generation
is canned — retrieval, redaction masking, citations, SSE and usage recording all
take their real paths. Scenario models drive the app's real error handling:
`mock-gpt` (normal), `mock-echo` (returns the prompt it was given — assert what the
app actually *sent*), `mock-empty`, `mock-error`, `mock-slow`, `mock-reasoning`
(streams a "thinking" phase before the answer — exercises the collapsible reasoning display.
⚠️ It separates reasoning **only when the request activated thinking** via
`chat_template_kwargs={"enable_thinking": true}`; unasked, it reproduces the real server's
#439 behaviour and leaks the thoughts into the answer, so the app's handling of that is
testable). Three JSON-shaped scenarios — `mock-summary-json`, `mock-topics-json`,
`mock-speakers-json` — return constant, schema-valid replies for the summary, topic and
speaker-ID tasks (wiring only; they measure no quality). Never start it as a
bare host process: it binds 5199 and then blocks the container. Fixtures and the
full table: `backend/tests/CLAUDE.md`.
`--with-mock-asr` is the sibling overlay — a mocked cloud ASR (Gladia stand-in) provider at
`http://mock-asr:5198`, so `--lite`-mode ASR can be exercised with no vendor account either;
same fixture/table location.
⚠️ **Keycloak used to take ~10 minutes to start; it is now pre-built (`docker/keycloak/Dockerfile`).**
The stock image's `start-dev` re-ran Quarkus augmentation on **every** start (the output lands in
`/opt/keycloak/lib/quarkus`, which is not on the volume, so nothing was reused), and its
`JarResultBuildStep` walks the built tree calling `File.setReadable` per entry. Measured 2026-09-06
on this host: **~600 s** before it listened, then Quarkus started in ~16 s. That marked a perfectly
healthy Keycloak unhealthy at the old 210 s budget, failed `up --wait`, and aborted
`run-dev-tests.sh --full` **before a single test ran**. A container sitting at 0.2% CPU there was
doing I/O-bound work, not deadlocked; check the thread dump before concluding otherwise.
The real fix is **done**: `kc.sh build` runs once in a small custom image and the container starts
`--optimized` (~19 s to "Listening on", ~25 s to healthy), so `docker-compose.keycloak.yml`'s
`start_period` is `120s`, not the old `900s` — do not raise it without re-measuring. The image is
**pinned** (`26.7.3` in both the Dockerfile `FROM` and the compose tag, whose suffix is a hash of
the build context and is policed by `backend/tests/unit/test_keycloak_image_tag_tracks_dockerfile.py`),
not `:latest` as it once was. `smallstep/step-ca` is still `:latest` and wants the same treatment.

Combine flags as needed. PKI client certs: `scripts/pki/test-certs/clients/*.p12`.
Details: `backend/app/auth/CLAUDE.md`, `docs/PKI_SETUP.md`, `docs/LDAP_AUTH.md`, `docs/OIDC_SETUP.md`.
