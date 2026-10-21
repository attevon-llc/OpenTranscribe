---
sidebar_label: Email delivery
sidebar_position: 6
title: Authentication email delivery
---

# Authentication email delivery

Password reset links, invitations and email-verification links are sent by email. If
OpenTranscribe has no way to send mail, none of them arrive, and nothing else in the product can
stand in for them. This page explains what stops working, why a plain "just send it" setup is
not enough, and how to set up a sender that actually delivers.

## What stops working without a mail transport

OpenTranscribe sends authentication mail through one of two transports:

1. the **email configuration designated** for authentication mail, or
2. the `SMTP_*` environment settings, when no configuration is designated (or the designated one
   is missing or disabled).

With neither, the credential-bearing messages are **refused rather than faked**:

| Message | Without a transport |
|---|---|
| Invitation | The invite request fails (HTTP 503). The invitation is saved as **pending** but was never mailed; the admin sees an error. |
| Password reset | The user sees the usual "if the account exists, we sent a link" message. The link is **not** sent. |
| Email verification | The same: the user is asked to verify, and no link arrives. If **require email verification** is on, new local accounts cannot sign in until mail works. |
| Security notices (for example an email-change alert) | Skipped; the notice is logged and nothing is sent. |

The password-reset and verification pages answer identically whether or not the account exists
or mail is configured. That is deliberate: a different answer would let anyone probe which
addresses have accounts. It also means **the signal goes to administrators, not to the person
who requested the link**, which is why OpenTranscribe shows a warning to admins when this state
is detected.

## Where you see the warning

- **Settings → Authentication → Auth Email** shows a notice stating the cause: nothing is
  designated and no `SMTP_*` settings exist, or the designated configuration was deleted or
  disabled.
- The **Auth Email** tab carries a marker while mail cannot be delivered, so you do not have to
  open the tab to find out.
- The **Invite user** form shows the same notice to a super admin before you send.
- The first-run wizard's final step lists whether authentication email is configured.

:::note
The notice means a sending method is **configured**, not that mail is **proven to arrive**. A
mistyped host, a wrong password or a blocked port still reads as configured. Use the **Test**
button on the saved email configuration, then send yourself an invitation.
:::

## Why a relay or your own mail provider is needed

It is tempting to point the application at the internet and let it deliver mail directly. That
rarely works well for authentication mail:

- **Outbound port 25 is commonly blocked.** Many cloud platforms and residential or office
  networks block or throttle outbound SMTP on port 25 to stop spam. Check your host's current
  policy; do not assume it is open.
- **Receiving servers judge the sender.** A message from an unknown address that is not covered
  by the sending domain's DNS records, or that comes from an address with no sending history, is
  often rejected or filed as spam. A password-reset mail that lands in spam is, to the user,
  the same as one that was never sent.
- **Reputation is earned by the sender.** A mail provider or relay your organisation already
  uses has an established reputation and handles retries and bounces.

So send through a **mail relay or the organisation's own mail platform** (Google Workspace,
Microsoft 365, or an SMTP relay service), using an address on a domain you control.

### Publish SPF, DKIM and DMARC for the sending domain

These DNS records tell receiving servers that your sender is allowed to use your domain:

- **SPF** lists the servers permitted to send for the domain.
- **DKIM** signs messages so the receiver can check they were not altered and came from the
  domain.
- **DMARC** tells receivers what to do when SPF or DKIM checks fail, and where to report.

The **From address** of the email configuration must be on the domain you published these for.
Your mail provider's documentation gives the exact records; follow their current instructions
rather than copying values from an old guide.

## Set it up

Authentication mail needs two things: an email configuration, and the choice to use it.

1. **Create the configuration** at **Settings → Watch Sources → Email configurations**. The
   provider rows live here for historical reasons; they are also used by watch-source
   notifications.
2. **Designate it** at **Settings → Authentication → Auth Email** (super admin). Nothing is
   designated automatically: mailing password resets out of an unrelated mailbox would be a
   credential leak and a deliverability problem. Saving a designation that names a missing or
   disabled configuration is refused.
3. **Check the result.** The Auth Email tab should say authentication email is sent through your
   configuration, and the warning marker should be gone.

Clearing the designation falls back to the `SMTP_*` environment settings.

### Generic SMTP relay (SMTP provider)

Use the **SMTP** provider with your relay's host and a submission port. Port 587 with STARTTLS is
the usual choice; port 465 connects over TLS from the start and is also supported. Enter the
username and password your relay issues, and set the From address to a mailbox on your
domain.

### Google Workspace

Use the **SMTP** provider against Google's SMTP service. Which method applies (an app password
for a mailbox, or Google's SMTP relay service with an allowed sending address) depends on your
Workspace administrator's settings. Confirm the current requirements in Google's Workspace
documentation before you rely on either.

### Microsoft 365

Many tenants disable SMTP basic authentication, so a username and password may not work. The
**Microsoft 365** provider avoids that by sending through the Microsoft Graph `sendMail` API
with an app registration:

1. Register an application in your tenant and create a client secret.
2. Grant it the application permission to send mail (`Mail.Send`) and have an administrator
   consent to it.
3. Enter the tenant ID, client ID and client secret in the email configuration, and set the From
   address to the mailbox the messages are sent as.

This uses an **application** (client-credentials) token, not a per-user sign-in. An
application with `Mail.Send` can, by default, send as any mailbox in the tenant. Restrict it to
the intended sending mailbox with an Exchange application access policy so a leaked secret
cannot be used to send as other people.

:::caution[Verify before you rely on it]
Delivery through Microsoft Graph has not yet been verified against a real Microsoft 365 tenant
in OpenTranscribe's own testing, and the developer guide still marks watch-source email delivery
as experimental. Use the **Test** button, send a real invitation to yourself, and confirm it
arrives before depending on it for password resets.
:::

### Exchange (on-premises)

The **Exchange** provider sends through the Exchange server over SMTP using the server,
domain and credentials you enter. The same checks apply: a From address on your domain, and
a real test message.

## Also required: `FRONTEND_URL`

Even with a working transport, OpenTranscribe **refuses to send** a credential link that would
point at `http://localhost:5173`, the development default of `FRONTEND_URL`. A link to the
recipient's own machine is useless, and a single-use token wasted on it also uses up their
reset allowance. Set `FRONTEND_URL` in `.env` to the public address people use to reach
OpenTranscribe. This is a second, separate reason mail can fail, and the in-app notice does not
cover it.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Invite fails with a message about email | No transport is configured, or sending is failing. The invitation is saved as pending; fix mail, then send it again. |
| Notice says "no email configuration is designated" | Nothing is selected under Auth Email and `SMTP_HOST` is empty. |
| Notice says the designated configuration "no longer exists" or "is disabled" | It was deleted or disabled after being designated. Designate another, or re-enable it. |
| Everything looks configured but nothing arrives | Check spam folders, your relay's logs, and the SPF/DKIM/DMARC records. Check outbound port policy. |
| Reset mail arrives but the link goes to `localhost` | `FRONTEND_URL` is unset. (OpenTranscribe normally refuses to send this; see above.) |
