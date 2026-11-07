---
sidebar_position: 12
title: Anonymous Usage Report (Telemetry)
description: The opt-in anonymous usage report, exactly what it sends, and how to turn it on or off
---

# Anonymous Usage Report (Telemetry)

OpenTranscribe can send a small anonymous report, about once a day, saying that an install
exists, which version it runs and whether it is in use. It exists because the project sees
thousands of image downloads and cannot tell how many of them become real, running installs.

**It is off by default.** Nothing is sent until an administrator turns it on, and an upgrade
never turns it on. The report never says what an install is used for, who uses it or what it
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

- **Turn on or off:** the switch on that card. Turning it off deletes the install ID, so
  turning it on again later starts a new, unrelated ID.
- **`DO_NOT_TRACK=1`** in `.env` forces the report off, whatever the switch says
  ([Console Do Not Track](https://consoledonottrack.com/)). OpenTranscribe does not set this
  for you; it also turns off telemetry in libraries that honor it.
- **`OPENTRANSCRIBE_TELEMETRY=false`** in `.env` forces it off the same way. It cannot force
  the report on.
- **`OPENTRANSCRIBE_DEMO_MODE=true`** marks a public demo deployment; the report cannot be
  turned on there.
- If no receiver key is configured (`OPENTRANSCRIBE_TELEMETRY_PROJECT_KEY`), the report
  cannot be turned on.

While any of these applies, the card says which one, and turning the report on is refused.
Changes to these variables take effect after the backend and workers are restarted.

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

## Third-party libraries

Separately from this report, the shipped containers switch off the telemetry and update checks
of the libraries and services they include (Hugging Face Hub, pyannote, MinIO, Grafana, deno),
whether or not the report is on.

## The source

- `backend/app/services/telemetry.py`: the payload, the switches and the sender
- `backend/app/tasks/telemetry.py`: the hourly check
- `backend/app/api/endpoints/admin_telemetry.py`: the settings card's API
- `backend/tests/unit/test_telemetry_disabled.py`: proves nothing is sent by default
