# openspot-bm-proxy

A small web proxy that brings the BrandMeister parts of an old **SharkRF openSPOT** web UI back to life.

The openSPOT status page and its "BrandMeister manager" call BrandMeister's retired v1 API from your
*browser*, so on old firmware the BrandMeister rows (static TGs, dynamic TGs, reflector) stay empty and
the add/remove buttons fail. This proxy serves the openSPOT UI unchanged, except that it redirects those
calls to itself and answers them from **BrandMeister API v2**. It also adds a WPSD-style
[BrandMeister manager page](#wpsd-style-brandmeister-manager) with a menu entry in the openSPOT UI.

Tested with an openSPOT v1.1, firmware 0142. Python 3, standard library only.

Also on GitHub: <https://github.com/oh7nfc/openspot-bm-proxy>

## What works

| openSPOT UI function | Result |
|---|---|
| Status page: static and dynamic TGs | works (read from the v2 profile, cached for 30 s); the page reloads them every 60 s while it is open |
| Auto-static TG | BrandMeister's auto-static TG (the profile's `autoStatic`) is added to the static TG list on the Status and Connectors pages as `2447 (auto)`, without a remove link |
| BM Manager menu entry | a "BM Manager" item before Logout opens the manager page; it is shown only when the BrandMeister rows are (see "How it works") |
| Add / remove static TG | works |
| Drop dynamic TGs, drop current call | works |
| Check key | checks the local key file (it exists, is a JWT, is not expired); BrandMeister is not called |
| "Last heard" link | opens BrandMeister's new Last Heard page filtered to your hotspot (`#/lh?ContextID=<DMR ID>`); the old per-master page is gone |
| "Get key" button | opens the proxy's own help page (`/help/api-key`, in English or Finnish by browser language, `?lang=fi` / `?lang=en` to force one) that explains the key name and links to BrandMeister's new API Keys page; the old key page returns 403 |
| Change reflector | not available: API v2 has no such call, the page shows its own error |
| Reflector row on the Status page | always shows "-" (v2 profile has no reflector field) |

## WPSD-style BrandMeister manager

`http://<proxy>:8081/bm` is a manager page laid out like WPSD's BrandMeister Manager, in the openSPOT colors,
with the openSPOT logo, menu (Status, Connectors, Modem, Settings, BM Manager, Logout), device name and
activity dots in its header.

**You must be logged in to the openSPOT through the proxy.** The page takes the hotspot's DMR ID (the Homebrew
connector's repeater ID) and the API key name from the openSPOT's own settings, so there is nothing to type.
Without a login it shows only a "Log in to the openSPOT first" notice. If the openSPOT has no key name saved,
reading works but changes ask you to save one on the Connectors page.

- **Header line:** DMR ID (links to `brandmeister.network/#/device/<id>`), callsign, the master the hotspot is
  connected to, and "Simplex" for DMO hotspots.
- **Static talkgroups:** TG number, slot chip, BrandMeister name, an enable/disable switch and Delete.
  Disabling unlinks the TG from BrandMeister but keeps it in the list so it can be linked again; that list is
  kept in the browser (per DMR ID), not on the proxy or at BrandMeister.
- **Add & Link:** several talkgroups at once (one per line, spaces and commas also work) on TS1 or TS2.
  Enable all, Disable all and Delete all work on the whole list one call at a time.
- **Auto-static TG:** shown as a row with an AUTO chip and no controls (the API cannot remove it).
- **Dynamic talkgroups:** with the time left before they time out, plus Drop dynamic TGs and Drop current call
  for each timeslot.
- **Simplex (DMO) hotspots** have only one slot, which BrandMeister stores as slot 0 and the page shows as TS2;
  TS1 is greyed out and changes are sent for slot 0.

The page reloads the lists every 60 s. Reading a profile needs no key; changes use the key as described below.

## How it works

- Browse to `http://<proxy>:8081/` instead of the device. Everything is passed through to the openSPOT.
  A few strings in `main.min.js_gz` and `main.html_gz` are rewritten: the two BrandMeister API URLs (to
  `/bmapi/` on the proxy), the Last heard link, the periodic reload of the BrandMeister rows, the auto-static
  display, the Get key button and the BM Manager menu entry. A warning is logged if one is no longer found
  (for example after a firmware update).
- The BrandMeister rows, the manager and the BM Manager menu entry only appear on the openSPOT when the
  **active connector is Homebrew/MMDVM** and the **selected server is a BrandMeister one**. The menu entry
  follows the page's own rule.
- The firmware only stores short API keys, but BrandMeister v2 keys are long JWTs. In the openSPOT's
  **API key** field enter a short **key name** (letters, digits, `-`, `_`, up to 32 characters). The proxy
  reads the real token from `<keys-dir>/<name>` and sends it to BrandMeister as a Bearer token. The token
  never reaches the browser or the openSPOT. Reading a profile needs no key.

## Install from the .deb package (Debian/Ubuntu with systemd)

Download `openspot-bm-proxy_<version>_all.deb` from the release page, then:

```sh
sudo dpkg -i openspot-bm-proxy_1.2.0_all.deb
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

## Pages and endpoints

| Path | What |
|---|---|
| `/` | redirects to the openSPOT UI (`/main.html_gz`), passed through from the device |
| `/bm` | the BrandMeister manager page |
| `/help/api-key` | help page for the key name (English or Finnish) |
| `/bmapi/` | the BrandMeister v1-style calls the openSPOT page makes, answered from API v2 |
| `/bmapi/manager/profile?q=<DMR ID>` | profile for the manager page: static and dynamic TGs with slots, names and timeouts, the auto-static TG and basic device info |
| `/bmapi/manager/name?tg=<n>` | name of a talkgroup (BrandMeister's directory, cached for 24 h) |

## Options

```
proxy.py --device <openSPOT address> [--bind 0.0.0.0] [--port 8081] [--keys-dir /etc/openspot-bm-proxy/keys]
```

## Security notes

- The proxy has no login of its own. Anyone who can reach it can use the openSPOT UI through it (the
  openSPOT's own login still applies), and anyone who knows a **key name** can change the TGs of that key's
  BrandMeister account through it. Use a name that is hard to guess and keep the proxy on a trusted LAN.
- The manager page reads the DMR ID and key name from the openSPOT with your openSPOT login (the same calls
  the openSPOT's own pages make). It uses only those two values and does not show or store anything else
  from that response.
- Key names are restricted to `[A-Za-z0-9_-]{1,32}`, so they cannot point outside the keys directory.
- Tokens are never logged. Error bodies from BrandMeister are.

## Notes on the BrandMeister v2 API

- The live API wants the field `group` for adding a static TG (its published spec says `talkgroup`); the
  proxy sends both.
- Static TGs come back as strings (`"2400"`); the proxy converts them to numbers as v1 did.
- The profile also has an `autoStatic` object (`{"talkgroup": 2447, ...}`) for the one TG BrandMeister linked
  on its own; it is not in `staticSubscriptions`.
- A simplex (DMO) hotspot's TGs are stored with slot `"0"`, while other hotspots use 1 and 2.
- The profile endpoint is sometimes down (HTTP 500/503 or timeouts) while other endpoints work. The proxy then
  answers 502 and the manager shows "Could not load"; try again after a few minutes.

## Development

Work on feature branches and open a pull request; direct pushes to `master` are blocked by a hook in
`.githooks/` (enable it in a clone with `git config core.hooksPath .githooks`).

To try it locally: `python proxy.py --device <openSPOT address> --port 8099`, then open
`http://localhost:8099/` and log in, and `http://localhost:8099/bm` for the manager.

## Releases

Development happens in a private Gitea repository. The GitHub repository gets one snapshot commit per
release, tagged `vX.Y.Z`; the Gitea history is not published there.
