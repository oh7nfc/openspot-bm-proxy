# openspot-bm-proxy

A small web proxy that brings the BrandMeister parts of an old **SharkRF openSPOT** web UI back to life.

The openSPOT status page and its "BrandMeister manager" call BrandMeister's retired v1 API from your
*browser*, so on old firmware the BrandMeister rows (static TGs, dynamic TGs, reflector) stay empty and
the add/remove buttons fail. This proxy serves the openSPOT UI unchanged, except that it redirects those
calls to itself and answers them from **BrandMeister API v2**.

Tested with an openSPOT v1.1, firmware 0142. Python 3, standard library only.

## What works

| openSPOT UI function | Result |
|---|---|
| Status page: static and dynamic TGs | works (read from the v2 profile, cached for 30 s); the page reloads them every 60 s while it is open |
| Add / remove static TG | works |
| Drop dynamic TGs, drop current call | works |
| Check key | checks the local key file (it exists, is a JWT, is not expired); BrandMeister is not called |
| "Last heard" link | opens BrandMeister's new Last Heard page filtered to your hotspot (`#/lh?ContextID=<DMR ID>`); the old per-master page is gone |
| "Get key" button | opens the proxy's own help page (`/help/api-key`, in English or Finnish by browser language, `?lang=fi` / `?lang=en` to force one) that explains the key name and links to BrandMeister's new API Keys page; the old key page returns 403 |
| Change reflector | not available: API v2 has no such call, the page shows its own error |
| Reflector row on the Status page | always shows "-" (v2 profile has no reflector field) |

## WPSD-style BrandMeister manager

`http://<proxy>:8081/bm` is a standalone manager page in the style of WPSD's BrandMeister Manager, with no
openSPOT needed. Enter your hotspot's DMR ID and your key name (both are remembered in the browser; the
token never is). It shows:

- static talkgroups with their BrandMeister names, slot and a Remove button, plus an Add static TG form
  (the name of the TG is looked up as you type)
- dynamic talkgroups with their timeouts
- Drop dynamic TGs and Drop current call buttons for each timeslot

The page reloads the lists every 60 s. Writes need the key; viewing does not.

## How it works

- Browse to `http://<proxy>:8081/` instead of the device. Everything is passed through to the openSPOT,
  and two URLs in `main.min.js_gz` are rewritten to `/bmapi/` on the proxy.
- The BrandMeister rows and manager only appear on the openSPOT when the **active connector is
  Homebrew/MMDVM** and the **selected server is a BrandMeister one**.
- The firmware only stores short API keys, but BrandMeister v2 keys are long JWTs. In the openSPOT's
  **API key** field enter a short **key name** (letters, digits, `-`, `_`, up to 32 characters). The proxy
  reads the real token from `<keys-dir>/<name>` and sends it to BrandMeister as a Bearer token. The token
  never reaches the browser or the openSPOT. Reading a profile needs no key.

## Install from the .deb package (Debian/Ubuntu with systemd)

Download `openspot-bm-proxy_<version>_all.deb` from the release page, then:

```sh
sudo dpkg -i openspot-bm-proxy_1.1.0_all.deb
sudoedit /etc/default/openspot-bm-proxy      # set OPENSPOT_DEVICE (and PORT if needed)
sudo systemctl restart openspot-bm-proxy
```

The package creates the `openspotproxy` user and the keys directory; create the key file as described
below. Upgrades keep your `/etc/default/openspot-bm-proxy`. Build one yourself with
`python3 packaging/build_deb.py <version> [out-dir]` (no dpkg needed).

## Install by hand (Debian/Ubuntu with systemd)

```sh
sudo useradd --system --no-create-home --shell /usr/sbin/nologin openspotproxy
sudo install -d -m 755 /opt/openspot-bm-proxy /etc/openspot-bm-proxy
sudo install -d -m 700 -o openspotproxy -g openspotproxy /etc/openspot-bm-proxy/keys
sudo install -m 755 proxy.py /opt/openspot-bm-proxy/proxy.py
# set --device (your openSPOT's address) in the unit first
sudo install -m 644 deploy/openspot-bm-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now openspot-bm-proxy
```

Create a BrandMeister API key under your profile on brandmeister.network (API section), then put it in a
file named after your chosen key name. Write it yourself on the server; do not paste it into chat or commit it:

```sh
sudo -u openspotproxy tee /etc/openspot-bm-proxy/keys/mykey >/dev/null   # paste token, Enter, Ctrl-D
sudo chmod 600 /etc/openspot-bm-proxy/keys/mykey
```

Then open the proxy in a browser, go to the **Connectors** page (below "Quick call"), enter `mykey` in the
BrandMeister manager's API key field, save, and use "Check key".

## Options

```
proxy.py --device <openSPOT address> [--bind 0.0.0.0] [--port 8081] [--keys-dir /etc/openspot-bm-proxy/keys]
```

## Security notes

- The proxy has no login of its own. Anyone who can reach it can use the openSPOT UI through it (the
  openSPOT's own login still applies), and anyone who knows a **key name** can change the TGs of that key's
  BrandMeister account through it. Use a name that is hard to guess and keep the proxy on a trusted LAN.
- Key names are restricted to `[A-Za-z0-9_-]{1,32}`, so they cannot point outside the keys directory.
- Tokens are never logged. Error bodies from BrandMeister are.

## Notes on the BrandMeister v2 API

- The live API wants the field `group` for adding a static TG (its published spec says `talkgroup`); the
  proxy sends both.
- Static TGs come back as strings (`"2400"`); the proxy converts them to numbers as v1 did.

## Development

Work on feature branches and open a pull request; direct pushes to `master` are blocked by a hook in
`.githooks/` (enable it in a clone with `git config core.hooksPath .githooks`).
