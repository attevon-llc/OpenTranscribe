---
title: OpenTranscribe Terms of Use (self-hosted software)
key: opentranscribe-terms-of-use
audience: Operators and administrators of self-hosted OpenTranscribe deployments
version: 0.1
status: draft
effective:
reviewed:
owner: Attevon LLC
---

<!--
DRAFT. Engineering-authored, not reviewed by counsel, not legal advice.
Every [COUNSEL: n] marker refers to COUNSEL-QUESTIONS.md and must be resolved and removed
before this text is vendored into the app.
-->

# OpenTranscribe Terms of Use

These Terms of Use ("Terms") apply to the open-source OpenTranscribe software. OpenTranscribe is a
product of Attevon LLC ("Attevon," "we," "us"). The Operator Acknowledgment shown during first-run
setup summarizes these Terms; if the two differ, these Terms control. [COUNSEL: 1]

The OpenTranscribe hosted service is not yet available. When it launches, it will have its own
Terms of Service, Privacy Policy and Data Processing Agreement, which you accept when you create an
account; these Terms do not govern the hosted service.

## Definitions

- **"Software"** means the OpenTranscribe software published at
  `https://github.com/attevon-llc/OpenTranscribe`, in any version, and any copy of it.
- **"Operator"** means the person or organization that installs and runs a copy of the Software.
  Where these Terms say "you", they mean the Operator.
- **"Deployment"** means one installation of the Software run by an Operator.
- **"Authorized User"** means an individual whom the Operator allows to use its Deployment.
- **"Operator Content"** means the audio, video and other content uploaded to or imported into a
  Deployment, and everything generated from it, including transcripts, summaries, speaker labels,
  voiceprints and speaker profiles.
- **"Voiceprint"** means a mathematical representation of a speaker's voice characteristics (a
  speaker embedding) that the Software generates from audio. A **"recording voiceprint"** is
  created for each recording on which speaker detection runs, so speakers can be told apart and
  grouped. A **"speaker profile"** exists when a speaker is named; it keeps an averaged voiceprint
  so that speakers in future recordings can be matched to people already identified. These are the
  same meanings used in Section 4 of the OpenTranscribe Cloud Privacy Policy.
- **"Third-Party Provider"** means a speech recognition, speaker detection, AI or other service not
  operated by the Operator that the Operator connects to a Deployment.
- **"AI Output"** means transcripts, translations, speaker labels and suggestions, summaries, chat
  answers and any other output the Software or a Third-Party Provider generates.

## 1. Scope and acceptance

These Terms apply when you install, configure or operate the Software. The administrator who
completes first-run setup accepts them for the Operator. By accepting, that administrator confirms
that they have authority to do so. [COUNSEL: 6]

These Terms cover the relationship between you and Attevon only. They do not create any agreement
between Attevon and your Authorized Users, or between Attevon and the people whose voices or
information appear in Operator Content.

## 2. License

The Software is licensed under the GNU Affero General Public License v3.0 ("AGPL-3.0"). The AGPL-3.0
alone governs your rights to use, copy, modify and distribute the Software's code. Nothing in these
Terms restricts, conditions or adds to those rights. If anything in these Terms conflicts with the
AGPL-3.0, the AGPL-3.0 controls. [COUNSEL: 2]

These Terms describe responsibilities that come from running a deployment that processes personal
data, and the limits of what Attevon offers. They are not a condition of the license.

The OpenTranscribe name and logos are not licensed by the AGPL-3.0. [COUNSEL: 1]

## 3. Your responsibilities as Operator

**You control your Deployment.** You decide where the Software runs, who can use it, what it
processes, which features and Third-Party Providers are turned on, and how long data is kept. You
are the controller of, and responsible for, all personal data your Deployment processes. As
distributed, the Software sends no data to Attevon, and Attevon does not receive, host or have
access to Operator Content. See the OpenTranscribe Self-Hosted Privacy and Data Notice.

**Recording and consent.** You are responsible for having a legal basis and every consent the law
requires before you record, upload, import or transcribe a person. This includes laws that require
the consent of one or all parties to a call or conversation before it is recorded. [COUNSEL: 10]

**Voiceprints and speaker identification.** When speaker detection runs on a recording, the Software
creates recording voiceprints. When a speaker is named, the Software creates or updates a speaker
profile and uses it to match speakers in later recordings. Voiceprints are biometric data or
biometric identifiers under laws such as the EU and UK GDPR (Article 9), the Illinois Biometric
Information Privacy Act, the Texas Capture or Use of Biometric Identifier Act, Washington's
biometric identifier law and the California Consumer Privacy Act. You are responsible for:

- deciding whether to use speaker detection and speaker identification at all;
- giving any notice, obtaining any written consent or release, and publishing any retention and
  destruction schedule that the law requires before a voiceprint is created;
- deleting voiceprints and speaker profiles when the law, your schedule or the person concerned
  requires it.

Consent to be recorded is not consent to voiceprint creation. The Software does not include a
biometric consent-capture step. [COUNSEL: 7]

**Notice to the people you record.** You are responsible for telling the people whose voices or
information you process what you collect, why, who can see it, which Third-Party Providers receive
it, and how long you keep it.

**Authorized Users.** You will make your Authorized Users aware of these Terms and of your own rules
for using the Deployment. You are responsible for their use of it. The Software includes an optional
login banner that each user must acknowledge before using the Deployment; you may use it for this
purpose. [COUNSEL: 6]

**Security and configuration.** You are responsible for securing your Deployment, including the
servers it runs on, network access, accounts, credentials, backups and updates.

**Legal requests.** You are responsible for responding to requests from individuals, regulators and
courts about data in your Deployment. Attevon has no access to that data and cannot answer them for
you.

## 4. Acceptable use

The law, not these Terms, sets most limits on what you may do with recordings. In particular, you
must not use the Software to:

- record, transcribe or identify a person without the notice or consent the law requires;
- create or keep a voiceprint without the consent or release the law requires;
- process content you do not have the right to process, or that infringes another person's
  copyright, privacy or other rights;
- carry out unlawful surveillance, stalking, harassment or discrimination;
- violate any other applicable law.

Attevon does not authorize any of these uses. This section describes uses the law prohibits. It does
not restrict your rights under the AGPL-3.0. [COUNSEL: 2]

## 5. Third-party content and downloads

You are responsible for having the rights to every file you upload or import.

The Software can import media from a web address using the yt-dlp library, including from YouTube
and many other sites. Before you import from a site, you must check that its terms of service allow
it and that copyright law permits your use. Many sites, including YouTube, restrict downloading in
their terms. Attevon does not authorize, encourage or endorse downloading content you have no right
to download, and is not responsible for content you download. [COUNSEL: 8]

The Software can also import from storage locations and mailboxes you configure (watch sources). The
same responsibilities apply.

## 6. Third-Party Providers

The Software processes recordings locally by default. If you connect a Third-Party Provider, the
Software sends that provider the data it needs:

- **Cloud speech recognition or speaker detection** (for example Deepgram, AssemblyAI, OpenAI,
  Google, Microsoft Azure, Amazon Web Services, Speechmatics, Gladia or pyannoteAI) receives the
  **audio** of the recordings it processes.
- **AI providers** for summaries, chat and other AI features (for example OpenAI, Anthropic,
  OpenRouter, Amazon Bedrock, or an endpoint you specify) receive **transcript text** and prompts.
  When content redaction is turned on, the Software can mask detected personal information in text
  it sends to an AI provider outside your own network. Detection can miss information, so masking
  does not replace your own review of what you send.

Each Third-Party Provider processes that data under its own terms and privacy policy and under your
agreement with it. Attevon is not a party to that relationship and is not responsible for any
Third-Party Provider. You are responsible for any agreement, data processing terms or transfer
safeguards the law requires before you send data to one.

## 7. AI Output

AI Output is generated automatically and can be inaccurate, incomplete or misattributed. Speaker
labels can name the wrong person. Summaries and chat answers can state things the recording does not
support. You are responsible for reviewing AI Output before you rely on it or share it. AI Output is
not legal, medical, financial or other professional advice. Speaker names suggested by an AI model
are shown with a confidence score for you to confirm; the Software does not apply them on its own.

## 8. Disclaimer of warranties

THE SOFTWARE IS PROVIDED "AS IS" AND "AS AVAILABLE", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING ANY WARRANTY OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, TITLE OR
NON-INFRINGEMENT, AND ANY WARRANTY THAT THE SOFTWARE OR ANY AI OUTPUT IS ACCURATE, COMPLETE, SECURE
OR ERROR-FREE. THIS IS IN ADDITION TO SECTION 15 OF THE AGPL-3.0. [COUNSEL: 4] [COUNSEL: 9]

## 9. Limitation of liability

TO THE FULLEST EXTENT PERMITTED BY APPLICABLE LAW, IN NO EVENT WILL ATTEVON, OR ANY CONTRIBUTOR TO
THE SOFTWARE, BE LIABLE TO YOU OR ANY OTHER PERSON FOR ANY DAMAGES ARISING OUT OF OR RELATING TO THE
SOFTWARE, ITS USE OR INABILITY TO USE IT, OR ANY AI OUTPUT, INCLUDING ANY GENERAL, SPECIAL,
INCIDENTAL OR CONSEQUENTIAL DAMAGES AND ANY LOSS OR CORRUPTION OF DATA, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGES. THIS IS IN ADDITION TO SECTION 16 OF THE AGPL-3.0.
[COUNSEL: 4: whether to add a monetary cap and which liabilities the law does not allow to be
excluded] [COUNSEL: 9]

## 10. Indemnity

[COUNSEL: 5: decide whether to include an Operator indemnity at all for free software. If kept,
proposed scope:] You will defend and indemnify Attevon against third-party claims arising from
(a) Operator Content, including any claim that you recorded a person, created a voiceprint or
imported content without the required consent or rights, (b) your use of Third-Party Providers, or
(c) your breach of these Terms or applicable law.

## 11. Governing law and disputes

[COUNSEL: 3: confirm that the standard sentence below should apply to self-hosted operators,
including operators outside the United States.] These Terms are governed by the laws of the
Commonwealth of Virginia, without regard to its conflict-of-law rules. The state and federal courts
located in Prince William County, Virginia, have exclusive jurisdiction over any dispute arising out
of or relating to these Terms, and you consent to their personal jurisdiction. This section does not
apply to any dispute about the AGPL-3.0 itself.

## 12. Changes and versions

Each version of these Terms has a version number. The Software records which version an
administrator accepted, when, and a fingerprint of the exact text. A new version applies to a
Deployment once an administrator of that Deployment accepts it; the Software asks again when the
version it ships changes. Earlier versions stay published in the Software's source history.
[PRODUCT REQUIRED BEFORE PUBLISHING: the first-run wizard records completion only
(`first_run_wizard.completed_at`); acceptance of a document key, version and SHA-256, and
re-prompting on a new version, are not built yet.] [COUNSEL: 12]

## 13. Contact

**Attevon LLC**, a Virginia limited liability company
[STREET ADDRESS], Richmond, Virginia [ZIP]
Privacy, legal and security requests: [security@attevon.com](mailto:security@attevon.com)
General inquiries: [info@attevon.com](mailto:info@attevon.com)
