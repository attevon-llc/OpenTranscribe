---
sidebar_position: 3
---

# Install for a small business (HTTPS on your office network)

This page gets OpenTranscribe running on one server so everyone in the office can open it from
their own computer or phone, sign in, upload recordings, see live progress and record from the
microphone. It uses a self-signed certificate, so no public domain name is needed.

:::info What has been checked
The certificate, the nginx redirect and the installer prompts on this page are covered by automated
tests (`backend/tests/unit/test_lan_*.py`). Trusting the certificate on a phone or tablet
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

If you are installing now, answer **y** to "Do you want to set up HTTPS with self-signed
certificates?" and accept the suggested hostname (`opentranscribe.local`) or enter your own.

If OpenTranscribe is already installed:

```bash
cd opentranscribe
./opentranscribe.sh setup-ssl
```

Both do the same thing:

- generate `nginx/ssl/server.crt` and `nginx/ssl/server.key`. The certificate names the hostname,
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

The start output should include `Adding NGINX reverse proxy (HTTPS enabled)`. If it prints a warning
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

## Step 4: trust the certificate on each device

A self-signed certificate shows a browser warning until the device is told to trust it. Copy
`nginx/ssl/server.crt` from the server to the device and import it:

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
2. Select `server.crt` and tick **Trust this certificate for identifying websites**.

### Linux (Firefox)

1. Open `about:preferences#privacy` and scroll to **Certificates**, then **View Certificates**.
2. In the **Authorities** tab choose **Import**, select `server.crt` and tick **Trust this CA to identify websites**.

### iOS

1. AirDrop or email `server.crt` to the device and open it; a "Profile Downloaded" message appears.
2. In **Settings**, **General**, **VPN & Device Management**, install the profile.
3. In **Settings**, **General**, **About**, **Certificate Trust Settings**, enable full trust for the certificate.

### Android

1. Copy `server.crt` to the device.
2. In **Settings**, **Security**, **Encryption & credentials**, choose **Install a certificate**, then **CA certificate**.

The imported certificate is trusted for every site on that device, so only import a certificate you
generated yourself, and keep `nginx/ssl/server.key` private.

## Step 5: use it

Open `https://opentranscribe.local` (or `https://<server-ip>`), sign in, and try an upload and a
recording. Bookmark the HTTPS address and use that one: the plain app port (5173) stays published
on the server next to 443 and does not keep a sign-in from other devices.

## Ports

The defaults are 80 (redirect) and 443. If something else on the server uses them, set the
alternatives in `.env` and restart:

```bash
NGINX_HTTP_PORT=8080
NGINX_HTTPS_PORT=8443
```

You then browse to `https://<hostname>:8443`, and visitors who arrive on the HTTP port are sent to
that HTTPS port, not to 443.

## If the server's address changes

The certificate lists the server's addresses at the time it was generated. After an address
change, run `./opentranscribe.sh setup-ssl` again, answer **y** to overwriting the certificate,
restart, and import the new `server.crt` on each device. Browsers remember that the site is
HTTPS-only for a year, so a device that still holds the old certificate shows an error without a
"proceed anyway" option until the new certificate is imported.

## Troubleshooting

**Sign-in succeeds and then you are signed out, or you see "session cookie rejected".** You are on
a plain-HTTP address. Use the `https://` address from Step 5. The sign-in page then shows "Your password was correct, but your browser rejected the session cookie."

**The record tab says it needs HTTPS.** Same cause: open the page over `https://`, with the
certificate trusted.

**The browser says the connection is not private.** The certificate is not trusted on that device
(Step 4), or you opened an address the certificate does not cover (for example a new IP). Check what it
covers with `openssl x509 -in nginx/ssl/server.crt -noout -ext subjectAltName`.

**Visiting `http://` goes to a page that will not load.** In versions before 0.6.0 the redirect always
pointed to port 443, even when `NGINX_HTTPS_PORT` was different. Update, or open the `https://` address with your port directly.

**Nothing answers on 443.** Check the container: `docker compose ps nginx` and
`docker compose logs nginx`. The start output tells you whether the nginx overlay was loaded.

See [NGINX & HTTPS Setup](../configuration/nginx-setup.md) for Let's Encrypt certificates on a
public domain, every NGINX variable, and customising the proxy.
