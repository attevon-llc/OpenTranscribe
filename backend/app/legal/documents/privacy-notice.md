---
title: OpenTranscribe Self-Hosted Privacy and Data Notice
key: opentranscribe-privacy-notice
audience: Operators and administrators of self-hosted OpenTranscribe deployments, and the people whose data a deployment processes
version: 0.1
status: draft
effective:
reviewed:
owner: Attevon LLC
verified_against: attevon-llc/OpenTranscribe master @ 107cd925 (2026-10-20)
---

<!--
DRAFT. Engineering-authored, not reviewed by counsel, not legal advice.
This is NOT the hosted-service privacy policy (opentranscribe-cloud/privacy-policy.md).
Every factual statement about the software must be re-verified against the code before each
release; SOURCE-MAP.md lists what was checked.
-->

# OpenTranscribe Self-Hosted Privacy and Data Notice

This Privacy and Data Notice ("this policy") describes what the open-source OpenTranscribe software
does with data when an organization or person runs it on their own systems. OpenTranscribe is a
product of Attevon LLC ("Attevon," "we," "us"). "Software", "Operator", "Deployment", "Authorized
User", "Operator Content", "Voiceprint", "recording voiceprint", "speaker profile", "Third-Party
Provider" and "AI Output" have the meanings given in the OpenTranscribe Terms of Use.

The OpenTranscribe hosted service is not yet available. When it launches, it will have its own
Terms of Service, Privacy Policy and Data Processing Agreement, which you accept when you create an
account; this policy does not cover the hosted service.

## 1. Who is responsible for your data

The Operator of a Deployment decides what the Software processes and why. The Operator is the
controller of all personal data in its Deployment and is responsible for complying with privacy
law. Attevon writes and publishes the Software. Attevon does not operate your Deployment and has no
access to it. [COUNSEL: 11]

If your voice or information is in a recording processed by a Deployment, contact the organization
that runs it. Attevon cannot see, change or delete that data.

## 2. What Attevon receives

**From a running Deployment: nothing.** The Software contains no code that contacts an Attevon
server. It has no analytics, usage reporting, update check or crash reporting that sends data to
Attevon.

**During installation and updates:** the install and update scripts download the Software from
GitHub and Docker Hub, and models from Hugging Face and other model hosts. Those services, not
Attevon, operate those servers and see the requesting IP address as with any download.

## 3. Data the Software processes

All of the following is stored in the Deployment's own database, search index and file storage,
on systems the Operator controls.

| Category | What it includes |
| --- | --- |
| Recordings | Audio and video files uploaded, recorded in the browser, imported from a web address, or imported from storage locations or mailboxes the Operator configures |
| Transcripts and derived content | Transcript text with timestamps, translations, speaker labels, summaries, topics, tags, comments, notes, chat conversations and other AI Output |
| Voice and speaker data | Recording voiceprints, speaker profiles (with names and optional pictures), speaker groupings, and a predicted gender for each speaker unless turned off (see Section 4) |
| Account data | Name, email address, password hash (for local accounts), role, sign-in method and identifiers from a directory or single sign-on provider, multi-factor authentication settings, sign-in times, and acknowledgment of the login banner if one is used |
| Session and security data | Session records including IP address, and an audit log of sign-ins and account and administrative changes with IP address and browser information |
| Settings | User and system settings, including any provider API keys, which the Software stores encrypted |

## 4. Voiceprints and other biometric data

**What the Software creates.** When speaker detection runs on a recording, the Software creates a
recording voiceprint for each speaker in it. When someone names a speaker, the Software creates or
updates a speaker profile, keeps an averaged voiceprint for it, and uses it to suggest or match the
same person in later recordings. The Software also predicts a speaker's gender from their voice;
this is on by default and can be turned off by a user or for the whole Deployment.
Voiceprints are biometric data or biometric identifiers under many privacy laws, including the EU
and UK GDPR, the Illinois Biometric Information Privacy Act, the Texas Capture or Use of Biometric
Identifier Act, Washington's biometric identifier law and the California Consumer Privacy Act.
[COUNSEL: 7]

**Where it stays.** Voiceprints are created and stored inside the Deployment. Voiceprints are
always created by the Deployment itself, never taken from a Third-Party Provider.

**How it is deleted.**

- Deleting a recording deletes that recording's voiceprints and recalculates any speaker profile
  they contributed to. It does not delete the speaker profile.
- Deleting a speaker profile deletes its stored voiceprint.
- Deleting a user account deletes that account's recordings, voiceprints and speaker profiles.
  The Software refuses to delete an account while one of its recordings is on legal hold.

**The Operator's duties.** The Software does not include a biometric consent-capture step or a
published retention schedule. The Operator decides whether to use speaker detection and speaker
identification, and is responsible for any notice, written consent or release, and retention and
destruction schedule the law requires. Speaker detection can be skipped when a recording is
uploaded or reprocessed.

## 5. Data sent outside the Deployment

**Third-party telemetry is off by default.** (Detail: `data-flows-annex.md`.) Some libraries the Software uses can report usage to
their publishers. The Software turns these off by default: pyannote.audio usage metrics, Hugging
Face library telemetry, and the update check of the Deno runtime used by yt-dlp. An Operator can
turn them back on by changing the setting.

**Downloads.** The Software downloads models from Hugging Face and the OpenSearch artifact server
when it needs them, unless it is installed in offline mode with models already in place.

**Only if the Operator or a user configures it:**

| Feature | Recipient | What is sent |
| --- | --- | --- |
| Cloud speech recognition or speaker detection | The chosen provider (for example Deepgram, AssemblyAI, OpenAI, Google, Microsoft Azure, Amazon Web Services, Speechmatics, Gladia, pyannoteAI) | Audio of the recordings it processes |
| AI summaries, chat and other AI features | The chosen provider (for example OpenAI, Anthropic, OpenRouter, Amazon Bedrock, or an endpoint the Operator specifies) | Transcript text and prompts. When content redaction is on, detected personal information can be masked in text sent to a provider outside the Operator's network |
| Import from a web address | The site in the address | The request for the media, and for a thumbnail image |
| Watch sources | The storage service or mailbox the Operator connects | Requests to read files or messages |
| Email notifications | The Operator's mail server | Notification email content |
| Single sign-on and directory sign-in | The Operator's identity provider | Sign-in requests and account details |
| Breached-password check (off by default) | Have I Been Pwned | The first five characters of a hash of the password, never the password |
| External object storage | The Operator's S3-compatible storage provider | All stored media |

The full inventory of what leaves the network, with the code location of each item, is in the
OpenTranscribe Data-Flows Annex (`opentranscribe-data-flows-annex`).

Each Third-Party Provider processes data under its own terms and the Operator's agreement with it.
Attevon is not a party to that relationship.

## 6. Retention and deletion controls in the Software

The Software keeps data until someone deletes it, unless the Operator turns on automatic deletion.
It includes these controls:

- **Delete a recording**, which removes the media, transcript, search entries, summaries and the
  recording's voiceprints.
- **Delete a speaker profile**, which removes its stored voiceprint.
- **Delete a user account**, by an administrator.
- **Erase a user's data** on request, by an administrator. The Software keeps a record that the
  erasure was requested and carried out. That record does not contain the erased personal data.
- **Automatic file retention**, off by default, which deletes recordings older than a set number of
  days (90 days when first turned on, adjustable).
- **Chat retention**, which keeps chat conversations indefinitely by default and can be set to
  delete them after a number of days.
- **Legal hold**, which prevents deletion of a held recording.
- **Content redaction**, which masks detected personal information, profanity or toxic language
  when transcripts are displayed or exported. The original transcript is kept unchanged.
- **Login banner**, off by default, which every user must acknowledge before using the Deployment.

Backups the Operator makes, and any copies a Third-Party Provider keeps, are outside these controls.

## 7. Rights of the people whose data is processed

People whose data is in a Deployment may have rights to access, correct, delete or export it, or to
object to its processing. Those requests go to the Operator, who is responsible for answering them
within the time required by applicable law. The controls in Section 6 help the Operator do so.
Attevon cannot act on data in a Deployment.

## 8. Changes to this policy

Each version of this policy has a version number. A new version describes the Software as of a
release; check the version that ships with your Deployment.

## 9. Contact

Questions about this policy or the Software: contact Attevon below. Questions about data in a
particular Deployment: contact the organization that runs it.

**Attevon LLC**, a Virginia limited liability company
[STREET ADDRESS], Richmond, Virginia [ZIP]
Privacy, legal and security requests: [security@attevon.com](mailto:security@attevon.com)
General inquiries: [info@attevon.com](mailto:info@attevon.com)
