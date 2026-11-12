# Telemetry

OpenTranscribe can send one small anonymous report about once a day, so the project can tell how
many of its image downloads become real, running installs. **It is off by default.** Turning it on
takes two deliberate steps, and it is forced off in offline, PKI, FIPS and demo deployments.

Full detail, the receiver setup and the settings card are in
[docs-site/docs/operations/telemetry.md](docs-site/docs/operations/telemetry.md).

## What is collected

Four fields, and nothing else:

| Field | Meaning |
|---|---|
| `schema_version` | The version of this field list. |
| `install_id` | A random UUID created when the report is turned on and deleted when it is turned off. Not derived from the host, the network or any account. |
| `app_version` | The OpenTranscribe release (`other` for a custom build). |
| `files_last_7d_bucket` | Files added in the last seven days as `0`, `1-10`, `11-100` or `100+`. Never an exact count. |

The receiver is told to keep no IP address, derive no location and build no profile.

## What is never collected

IP addresses, host or domain names, URLs, user names, email addresses, file names, transcript or
audio content, speaker names, LLM endpoints, models, prompts or keys, exact counts or durations,
error text, stack traces, hardware details, and anything from your `.env`.

## How to turn it off

It is already off. The images ship `DO_NOT_TRACK=1`; the report can only be enabled by setting
`DO_NOT_TRACK=0` in `.env` **and** a super admin turning it on (a first-login prompt offers this
once). To switch it off for good:

- leave `DO_NOT_TRACK=1`, or set `OPENTRANSCRIBE_TELEMETRY=false` in `.env`; or
- turn the switch off in Settings, Privacy & Redaction, which also deletes the install ID.

Forced off whatever the switch says: `DO_NOT_TRACK` set, `OPENTRANSCRIBE_TELEMETRY=false`, demo
mode, offline deployments (`HF_HUB_OFFLINE`), PKI (`PKI_ENABLED`), FIPS (`FIPS_MODE`) and no
configured receiver key.

## See what would be sent

Settings, Privacy & Redaction shows the exact request body. To log it from the server instead of
sending it, set `OPENTRANSCRIBE_TELEMETRY_DEBUG=true`: the report is built and written to the
backend worker log, and nothing leaves the machine.

## Third-party libraries

Separately, the shipped containers switch off the telemetry and update checks of Hugging Face Hub,
pyannote, MinIO, Grafana and deno.
