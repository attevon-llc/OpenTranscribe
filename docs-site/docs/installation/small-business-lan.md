---
sidebar_position: 3
---

# Install for a small business (HTTPS on your office network)

This page gets OpenTranscribe running on one server so everyone in the office can open it from
their own computer or phone, sign in, upload recordings, see live progress and record from the
microphone. It uses a certificate authority (CA) that the installer creates on the server, so no public domain
name is needed.

:::info What has been checked
The certificate, the nginx redirect and the installer prompts on this page are covered by automated
tests (`backend/tests/unit/test_lan_*.py`). Trusting the CA on a phone or tablet
(iOS and Android) and recording from a real phone microphone have **not** yet been confirmed on real
devices; the steps for those follow the vendors' documented flows. If one of them does not work for
you, please open an issue with the device, browser and version.
:::

## What you need

- A server with Docker, installed with the [installer](../getting-started/quick-start.md).
- The server and the other devices on the same network.
- Ports 80 and 443 free on the server (see [Ports](#ports) if they are not).

## Why HTTPS is needed here

With HTTPS turned on, OpenTranscribe is reached at one address on port 443. Port 80 only forwards
visitors to HTTPS. Three things depend on that:

- **Sign-in.** The session cookie is marked secure, so a browser only keeps it for an HTTPS address.
  Reaching the plain `http://<server>:5173` address from another device can look like a successful
  sign-in that immediately signs you out again.
- **Microphone recording.** Browsers hide the microphone from any page that is not served over HTTPS
  (or from the same computer). On a plain-HTTP address the record tab says it needs HTTPS.
- **Live updates and playback** work from any address that reaches the HTTPS site; nothing needs to
  be configured per address.

## Step 1: turn on HTTPS

If you are installing now, answer **y** to "Do you want to set up HTTPS with a certificate
made on this server?" and accept the suggested hostname (`opentranscribe.local`) or enter your own.

If OpenTranscribe is already installed:

```bash
cd opentranscribe
./opentranscribe.sh setup-ssl
```

Both do the same thing:

- create a local CA once (`nginx/ssl/ca.crt` and `nginx/ssl/ca.key`) and sign the server
  certificate with it (`nginx/ssl/server.crt` and `nginx/ssl/server.key`). The server certificate names the hostname,
  `localhost`, and every IPv4 address the server has, so it is valid for `https://<hostname>` and
  for `https://<server-ip>`;
- write `NGINX_SERVER_NAME=<hostname>` into `.env`.

The hostname may contain letters, digits, dots and hyphens only.

For an unattended install, set `NGINX_SERVER_NAME` in the environment. The installer writes it to
`.env`, but it does not create certificates unattended: run
`./scripts/generate-ssl-cert.sh <hostname> --auto-ip` yourself before starting.

## Step 2: start and check

```bash
./opentranscribe.sh restart    # or "start" on a first run
```

The start output should include `HTTPS enabled (NGINX reverse proxy with SSL)` (from a source checkout, `./opentr.sh` prints `Adding NGINX reverse proxy (HTTPS enabled)`). If it prints a warning
about missing certificates instead, the stack came up **without** HTTPS; fix the certificates and
start again.

## Step 3: let other devices find the server

Pick one:

- **By IP address.** Nothing to set up. Use `https://<server-ip>` (the installer prints the address it
  detected). If the address can change, give the server a fixed address in your router.
- **By name.** Add a DNS entry for the hostname in your router, or a line in each device's hosts file:

  ```
  192.168.1.100  opentranscribe.local
  ```

  Linux and macOS: `/etc/hosts`. Windows: `C:\Windows\System32\drivers\etc\hosts`. Names ending in
  `.local` resolve by themselves on macOS, iOS and most Linux systems but not reliably on Windows or
  Android, so use the IP address or a router DNS entry for those.

## Step 4: trust the CA on each device

A browser shows a warning until the device trusts the CA that signed the server certificate. Copy
`nginx/ssl/ca.crt` (the CA, **not** `server.crt`) from the server to each device and import it. You
do this once per device: when the server certificate is renewed or the server's address changes,
the CA stays the same and devices need nothing more. Re-running the installer reuses the existing CA.

### Windows

1. Double-click the `.crt` file.
2. Click **Install Certificate** and choose **Local Machine**.
3. Select **Place all certificates in the following store**, then **Trusted Root Certification Authorities**.
4. Finish the wizard and restart the browser.

### macOS

1. Double-click the file to open it in Keychain Access.
2. Double-click the certificate in the list and expand **Trust**.
3. Set **When using this certificate** to **Always Trust**, then close the window and confirm with your password.

### Linux (Chrome or Chromium)

1. Open `chrome://settings/certificates` and choose the **Authorities** tab, then **Import**.
2. Select `ca.crt` and tick **Trust this certificate for identifying websites**.

### Linux (Firefox)

1. Open `about:preferences#privacy` and scroll to **Certificates**, then **View Certificates**.
2. In the **Authorities** tab choose **Import**, select `ca.crt` and tick **Trust this CA to identify websites**.

### iOS

1. AirDrop or email `ca.crt` to the device and open it; a "Profile Downloaded" message appears.
2. In **Settings**, **General**, **VPN & Device Management**, install the profile.
3. In **Settings**, **General**, **About**, **Certificate Trust Settings**, enable full trust for "OpenTranscribe Local CA". The second step is easy to miss: without it the CA is installed but not trusted.

### Android

1. Copy `ca.crt` to the device.
2. In **Settings**, **Security**, **Encryption & credentials**, choose **Install a certificate**, then **CA certificate**.

A trusted CA can vouch for any website on that device, so only import a CA you generated yourself.
Keep `nginx/ssl/ca.key` on the server and never copy it to a device or share it: anyone holding it
can create certificates your devices will accept. The server certificate is valid for 825 days (the
longest Apple devices accept); renew it before then by running `./opentranscribe.sh setup-ssl`
again. The CA itself is valid for 10 years.

## Step 5: use it

Open `https://opentranscribe.local` (or `https://<server-ip>`), sign in, and try an upload and a
recording. Bookmark the HTTPS address and use that one. With HTTPS on, the plain app port (5173)
is only published on the server itself (`127.0.0.1`), so other devices cannot reach it by mistake.
Set `FRONTEND_BIND_HOST=0.0.0.0` in `.env` only if you really need plain HTTP on the network as
well: over plain HTTP, sign-in passwords cross the network unencrypted.

## Ports

The defaults are 80 (redirect) and 443. If something else on the server uses them, set the
alternatives in `.env` and restart:

```bash
NGINX_HTTP_PORT=8080
NGINX_HTTPS_PORT=8443
```

You then browse to `https://<hostname>:8443`, and visitors who arrive on the HTTP port are sent to
that HTTPS port, not to 443.

## Check the install from a phone or laptop

`scripts/test-lan-install.sh` checks the install from the **client's** side of the network. Run it
on a laptop on the same network as the server (not on the server: there every port answers). It
is read-only and never signs in. It needs `bash`, `curl` and `openssl`.

1. Copy `nginx/ssl/ca.crt` from the server to the laptop.
2. Run it with the address you will really use (name and IP are separate runs):

   ```bash
   scripts/test-lan-install.sh --host opentranscribe.local --ca ./ca.crt
   scripts/test-lan-install.sh --host 192.168.1.50 --ca ./ca.crt --expect-untrusted
   ```

   Add `--https-port 8443` if you changed `NGINX_HTTPS_PORT`.

It checks that HTTPS answers with the certificate chaining to `ca.crt`, that the certificate names
the address you used, that port 80 only redirects, that the plain app port (5173), the API port
(8080) and the docs port (5183) are **not** reachable from the network, and that cookies carry
`Secure`. `--expect-untrusted` adds a control: without `ca.crt` the certificate must be rejected,
which shows the CA is not already trusted by accident. Exit code 0 means every check passed.

The script cannot see a phone's trust store or microphone prompt. It prints a short checklist for
those: install `ca.crt`, open the address with no warning, sign in, reload and stay signed in, and
confirm the Record page asks for microphone access.

:::note
Validated on real devices: **pending owner run** (iOS, Android, macOS, Windows). The script's
verdict logic is unit-tested; the device flows above are not yet confirmed on hardware.
:::

## If the server's address changes

The server certificate lists the server's addresses at the time it was generated. After an address
change, run `./opentranscribe.sh setup-ssl` again, answer **y** to issuing a new server certificate,
and restart. Devices that already trust the CA need nothing more.

## Troubleshooting

**Sign-in succeeds and then you are signed out, or you see "session cookie rejected".** You are on
a plain-HTTP address. Use the `https://` address from Step 5. The sign-in page then shows "Your password was correct, but your browser rejected the session cookie."

**The record tab says it needs HTTPS.** Same cause: open the page over `https://`, with the
certificate trusted.

**The browser says the connection is not private.** The certificate is not trusted on that device
(Step 4: is `ca.crt` installed and trusted?), or you opened an address the certificate does not cover (for example a new IP). Check what it
covers with `openssl x509 -in nginx/ssl/server.crt -noout -ext subjectAltName`.

**Visiting `http://` goes to a page that will not load.** In versions before 0.6.0 the redirect always
pointed to port 443, even when `NGINX_HTTPS_PORT` was different. Update, or open the `https://` address with your port directly.

**Nothing answers on 443.** Check the container: `./opentranscribe.sh status` and
`./opentranscribe.sh logs nginx`. The start output tells you whether the nginx overlay was loaded.

See [NGINX & HTTPS Setup](../configuration/nginx-setup.md) for Let's Encrypt certificates on a
public domain, every NGINX variable, and customising the proxy.
