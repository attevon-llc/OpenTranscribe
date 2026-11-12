---
sidebar_position: 12
title: Anonymous Usage Report (Telemetry)
description: The opt-in anonymous usage report, exactly what it sends, and how to turn it on or off
---

# Anonymous Usage Report (Telemetry)

OpenTranscribe can send a small anonymous report, about once a day, saying that an install
exists, which version it runs and whether it is in use. It exists because the project sees
thousands of image downloads and cannot tell how many of them become real, running installs.

**It is off by default, and turning it on takes two deliberate steps.** The images ship with
`DO_NOT_TRACK=1`, which keeps the report off. An operator who wants to allow it sets
`DO_NOT_TRACK=0` in `.env` and restarts; only then can a super admin turn it on in Settings.
An upgrade never turns it on. The report never says what an install is used for, who uses it or what it
holds.

## Exactly what is sent

Four fields, and nothing else:

| Field | Example | What it means |
|---|---|---|
| `schema_version` | `1` | The version of this field list. It changes, together with this page, whenever a field is added or removed. |
| `install_id` | `3f2b8c1e-...` | A random ID created when the report is turned on. It is not derived from the computer, the network or any account, and it is deleted when the report is turned off. |
| `app_version` | `v0.7.0` | The OpenTranscribe release this server runs. A custom build string is reported as `other`. |
| `files_last_7d_bucket` | `1-10` | How many media files were added in the last seven days, as one of `0`, `1-10`, `11-100` or `100+`. Never an exact number. |

A real request body looks like this:

```json
{
  "api_key": "<the project's public write-only key>",
  "event": "opentranscribe_heartbeat",
  "distinct_id": "3f2b8c1e-7d4a-4f0e-9b61-2c5a8e9d1f07",
  "properties": {
    "schema_version": 1,
    "install_id": "3f2b8c1e-7d4a-4f0e-9b61-2c5a8e9d1f07",
    "app_version": "v0.7.0",
    "files_last_7d_bucket": "1-10",
    "$ip": null,
    "$geoip_disable": true,
    "$process_person_profile": false
  }
}
```

`api_key`, `event` and `distinct_id` are what the receiver needs to file the report (the
`distinct_id` is the same random install ID). The three `$` properties are instructions to the
receiver, not data: do not keep an IP address, do not look up a location, and do not build a
profile.

A test fails the build if the fields the server sends and the table above ever differ.

## Exactly what is never sent

IP addresses (the receiver is told to discard them), host names, domain names, URLs, user
names, email addresses or email domains, file names, transcript or audio content, speaker
names, LLM endpoints, models, prompts or API keys, exact counts or durations, error text, stack
traces, hardware details, and any value from your `.env` file.

## See exactly what would be sent

**Settings → Privacy & Redaction → Redaction Policy → Anonymous usage report** (super admin)
shows the request body above, built by the same code that sends it, whether or not the report
is on. It also shows when the last report was sent and anything that is forcing the report off.

## Turning it on and off

To turn it on:

1. In `.env`, set `DO_NOT_TRACK=0` and restart OpenTranscribe.
2. As a super admin, turn on the switch on the card above.

To turn it off, use the same switch (or set `DO_NOT_TRACK=1` again). Turning it off deletes the
install ID, so turning it on again later starts a new, unrelated ID.

What forces it off, whatever the switch says:

- **`DO_NOT_TRACK`**, the cross-tool convention
  ([Console Do Not Track](https://consoledonottrack.com/)). The images and compose files set
  `DO_NOT_TRACK=1` by default, because it also keeps any third-party library that honours it
  quiet. Only `0`, `false`, `no`, `off` or an empty value allow the report; any other value,
  including a typo, keeps it off. Setting `DO_NOT_TRACK=0` does not turn on any library's
  telemetry: each library that reads it is also switched off on its own (for example
  `HF_HUB_DISABLE_TELEMETRY=1`), and a test checks that for every installed library.
- **`OPENTRANSCRIBE_TELEMETRY=false`** in `.env` forces it off the same way. It cannot force
  the report on.
- **`OPENTRANSCRIBE_DEMO_MODE=true`** marks a public demo deployment; the report cannot be
  turned on there.
- **Offline, PKI and FIPS deployments** (`HF_HUB_OFFLINE`, `PKI_ENABLED`, `FIPS_MODE`): the
  offline and PKI compose overlays force it off, and the server refuses to send whenever any of
  these three is set.
- If no receiver key is configured (`OPENTRANSCRIBE_TELEMETRY_PROJECT_KEY`), the report
  cannot be turned on.

While any of these applies, the card says which one, and turning the report on is refused.
Changes to these variables take effect after the backend and workers are restarted.

## First-login prompt

When nothing forces the report off (so `DO_NOT_TRACK=0` is set), the first super admin to sign in
sees a one-time prompt offering to turn it on, with a link to the exact request body. "No thanks"
is remembered in that browser; the Settings card remains the place to change the choice.

## Debug switch

`OPENTRANSCRIBE_TELEMETRY_DEBUG=true` makes the daily check build the report and write it to the
worker log instead of sending it. Nothing leaves the machine, and the "last sent" time stays empty.

## How often

About once a day. The server checks every hour whether a report is due; each install adds its
own fixed offset of up to an hour either way so installs do not all report at the same minute.
A report that cannot be delivered waits for the next day: it is never retried sooner, the
request gives up after 5 seconds, and OpenTranscribe works the same with the receiver
unreachable or blocked.

## Where it goes

To PostHog Cloud in the EU region (`https://eu.i.posthog.com`), into a project used only for
this report, with client IP capture discarded. You can block that host at your firewall
without affecting anything else.

## Configuring the receiver

The report needs a receiver, and **no receiver ships in the image**. Both variables are
deployment configuration in `.env` (the admin UI cannot change them), read by the backend and
the workers; restart them after changing either.

| Variable | Default | Meaning |
|---|---|---|
| `OPENTRANSCRIBE_TELEMETRY_PROJECT_KEY` | unset | The receiver's public, write-only project key (a PostHog project API key, `phc_...`). Unset or blank means the report cannot be turned on. |
| `OPENTRANSCRIBE_TELEMETRY_ENDPOINT` | `https://eu.i.posthog.com/i/v0/e/` | The capture URL. Change it for another PostHog region or a self-hosted PostHog. |

With the key unset, which is the shipped state, the install fails closed: the settings card
says no receiver is configured on this server, turning the report on is refused, and the hourly task sends
nothing even if the admin switch was somehow stored as on. The default endpoint is not a
reason to send: without a key, no request is built. A test proves this with both variables
unset (`test_the_shipped_state_no_key_and_no_endpoint_sends_nothing`).

For the project that receives the reports (maintainers and anyone running their own receiver):

1. Create a PostHog project used only for this report, in the region matching the endpoint.
2. In the project settings, turn on "Discard client IP data", so the address is dropped on
   arrival even though the request already asks for that.
3. Put the project API key in `OPENTRANSCRIBE_TELEMETRY_PROJECT_KEY`. The key can only write
   events, so it is not a secret in the access-control sense, but keep it out of the git history
   of forks; set it in `.env` or the release environment.
4. Turn the report on in a test install (`DO_NOT_TRACK=0`, then Settings > Privacy & Redaction)
   and check that one `opentranscribe_heartbeat` event arrives with the four properties above.

## Third-party libraries

Separately from this report, the shipped containers switch off the telemetry and update checks
of the libraries and services they include (Hugging Face Hub, pyannote, MinIO, Grafana, deno),
whether or not the report is on.

## The source

- [`TELEMETRY.md`](https://github.com/attevon-llc/OpenTranscribe/blob/master/TELEMETRY.md): the short root summary
- `backend/app/services/telemetry.py`: the payload, the switches and the sender
- `backend/app/tasks/telemetry.py`: the hourly check
- `backend/app/api/endpoints/admin_telemetry.py`: the settings card's API
- `backend/tests/unit/test_telemetry_disabled.py`: proves nothing is sent by default
