# Running it on your own server

For keeping the route mapper on all the time — a home server you can reach from
the road, rather than a laptop you start by hand.

Written for a barebone Debian box with nothing but SSH on it. Debian 12
(bookworm) ships Python 3.11, which is the minimum this needs.

---

## Read this bit first

**The app has no authentication. None.** Anyone who can reach it can upload
files and, more to the point, use it to hammer Overpass, Open-Meteo and OSRM
under your IP address — which is exactly how you get blocked by the free
services the whole thing depends on.

So the service binds to `127.0.0.1` only, and the job of letting your tablet in
belongs to something in front of it. The recommended answer is Tailscale, which
solves the reachability and the authentication together and never puts the app
on the public internet at all.

**HTTPS is not optional either.** Service workers only register in a secure
context, so over plain `http://192.168.1.20:8000` the offline *map tile* cache
silently does nothing — the button appears to work and no tiles are stored. The
saved ride uses IndexedDB and works either way, but you want both.

---

## Install

Clone or copy the repository onto the box, then:

```bash
sudo apt install python3 python3-venv git
git clone https://github.com/gmarkop/motorcycle-route-mapper.git
cd motorcycle-route-mapper
sudo deploy/install.sh
```

The script installs from the checkout it lives in, so a private repository needs
no credentials on the server — you have already done the cloning. It is
idempotent: run it again to update.

What it does:

| Step | Result |
| --- | --- |
| Creates a system user | `motoroute`, no login shell, no home directory |
| Copies the app | `/opt/moto-route`, owned by that user, `.git` and `.venv` excluded |
| Builds a virtualenv | `/opt/moto-route/.venv` with the pinned dependencies |
| Installs the unit | `/etc/systemd/system/moto-route.service` |
| Writes settings | `/etc/moto-route.env`, everything commented out |
| Starts it | `systemctl enable --now moto-route` |

Override the defaults with environment variables if you need to:

```bash
sudo APP_USER=moto APP_DIR=/srv/moto PORT=8080 deploy/install.sh
```

### By hand instead

If you would rather not run a script as root:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin motoroute
sudo mkdir -p /opt/moto-route
sudo cp -r . /opt/moto-route && sudo rm -rf /opt/moto-route/.git
sudo python3 -m venv /opt/moto-route/.venv
sudo /opt/moto-route/.venv/bin/pip install -r /opt/moto-route/requirements.txt
sudo chown -R motoroute:motoroute /opt/moto-route

sudo cp deploy/moto-route.service /etc/systemd/system/
sudo cp deploy/moto-route.env.example /etc/moto-route.env
sudo systemctl daemon-reload
sudo systemctl enable --now moto-route
```

Check it came up:

```bash
systemctl status moto-route
curl -s http://127.0.0.1:8000/api/health
```

---

## Letting your tablet in

### Tailscale (recommended)

No port forwarding, no dynamic DNS, no open ports, and a real certificate:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale serve --bg 8000
```

You get `https://<hostname>.<your-tailnet>.ts.net`. Install the Tailscale app on
the iPad, sign in, and the app is reachable from anywhere in Europe.

Two things must be enabled in the [Tailscale admin
console](https://login.tailscale.com/admin/dns) first: **MagicDNS** and
**HTTPS certificates**. Without them `tailscale serve` has no certificate to
offer and you are back to plain HTTP.

Why this rather than a port forward:

- The app is never exposed to the internet, so its lack of authentication stops
  mattering. Only devices on your tailnet can reach it.
- The certificate is real, so the service worker registers and offline tiles work.
- Nothing on your router changes, and there is no dynamic DNS to keep alive.

### Caddy and a port forward (the alternative)

If you are not on Tailscale and have a domain pointed at your home address:

```bash
sudo apt install caddy
```

`/etc/caddy/Caddyfile`:

```
moto.example.org {
    # Basic auth is the bare minimum, because the app has none of its own.
    # Generate the hash with: caddy hash-password
    basic_auth {
        rider $2a$14$...replace.with.your.hash...
    }
    reverse_proxy 127.0.0.1:8000
}
```

```bash
sudo systemctl reload caddy
```

Caddy gets a Let's Encrypt certificate automatically. You will also need to
forward ports 80 and 443 on the router and keep the DNS record current.

This is more exposed than the Tailscale route: your server is answering the open
internet, and basic auth is all that stands between a scanner and your Overpass
quota. Prefer Tailscale unless you have a reason not to.

### Serve it at a hostname root

The frontend uses absolute `/api` and `/static` paths, so the app must live at
the root of a hostname — `https://moto.example.org/`, not
`https://example.org/moto/`. Both recipes above do that correctly.

---

## On the iPad

**Add it to the Home Screen.** Safari evicts script-writable storage — the tile
cache and your saved rides — after roughly a week without visiting a site. Web
apps launched from the Home Screen are exempt. This matters if you cache a ride
on Sunday and set off on Friday.

Open the site in Safari → Share → *Add to Home Screen*.

Then, before you leave: load your route, wait for the layers, and press **Cache
tiles for this route**. On the road the ride, the elevation profile and the
cached tiles keep working with no signal, and a banner tells you which data is
live and which is from the last refresh.

---

## Settings

Edit `/etc/moto-route.env` (see `deploy/moto-route.env.example` for the full
list), then:

```bash
sudo systemctl restart moto-route
```

The ones most worth setting:

| Variable | Why |
| --- | --- |
| `MOTO_TANK_RANGE_KM` | Your bike's real usable range — the fuel planning depends on it |
| `MOTO_INCIDENT_FEEDS` | A GeoJSON incident feed from your road authority |
| `MOTO_OSRM_URL` | Point at your own OSRM if you use alternates regularly |
| `MOTO_OFFLINE` | `1` disables every outbound request |

Full reference in the README.

---

## Day to day

```bash
journalctl -u moto-route -f          # logs
sudo systemctl restart moto-route    # restart
sudo systemctl stop moto-route       # stop
```

**Updating:**

```bash
cd ~/motorcycle-route-mapper
git pull
sudo deploy/install.sh               # re-run; it updates in place
```

**Uninstalling:**

```bash
sudo systemctl disable --now moto-route
sudo rm /etc/systemd/system/moto-route.service /etc/moto-route.env
sudo systemctl daemon-reload
sudo rm -rf /opt/moto-route /var/lib/moto-route
sudo userdel motoroute
```

---

## When it will not start

`journalctl -u moto-route -n 50` first. The usual causes:

**`python3 -m venv` fails** — `sudo apt install python3-venv`. Debian splits it
out of the base Python package.

**`Python 3.11 or newer is required`** — you are on Debian 11 or older. Either
upgrade the distribution or install a newer Python and point `install.sh` at it.

**Port already in use** — something else has 8000. Reinstall with
`sudo PORT=8081 deploy/install.sh`, and update the `tailscale serve` port to match.

**Weather and closures are empty, everything else works** — the box cannot reach
the outside world, or `MOTO_OFFLINE` is set. Check with
`curl -s 'https://api.open-meteo.com/v1/forecast?latitude=48&longitude=11&hourly=temperature_2m' | head -c 100`.

**Tile caching does nothing** — you are on plain HTTP. Service workers need
https or localhost; see the HTTPS note at the top.

**Permission denied writing the cache** — the unit relies on systemd's
`StateDirectory=` to create `/var/lib/moto-route`. If you wrote your own unit,
create that directory and `chown` it to the service user.

---

## About the unit file

`deploy/moto-route.service` runs the app as an unprivileged user with most of
the system taken away from it: `ProtectSystem=strict`, no capabilities, a
syscall filter, and network access restricted to TCP. It parses files from the
internet and talks to third-party services, so it gets no more than it needs.

The one writable path is `/var/lib/moto-route`, which systemd creates and owns
via `StateDirectory=`; that is where the forecast and closure caches live.

If you edit the unit, check it before reloading:

```bash
systemd-analyze verify /etc/systemd/system/moto-route.service
systemd-analyze security moto-route            # after a daemon-reload
```

---

## What has actually been tested

Honesty about this, since a deployment guide that quietly overstates itself
wastes your evening:

**Verified**, by running it: the installer end to end on Debian-family Linux
(user creation, file copy, venv build, dependency install, unit installation);
the app running as the unprivileged `motoroute` user under a clean
systemd-style environment, serving uploads, curviness and GPX export; the
service refusing connections on anything but loopback; and
`systemd-analyze verify` passing on the unit.

**Not verified**, because the build environment has no systemd as PID 1 and no
outbound network: `systemctl enable --now` itself, the Tailscale and Caddy
recipes, and the live third-party APIs. The commands are standard and the unit
is validated, but you are the first to run those particular steps.
