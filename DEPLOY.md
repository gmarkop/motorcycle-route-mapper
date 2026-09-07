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
sudo tailscale set --ssh          # optional; see "Monitoring it from the iPad"
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

### Monitoring it from the iPad

Two tiers, depending on how much you want to install.

**Tier 1 — Safari, nothing to set up.** Bookmark:

```
https://<host>.<tailnet>.ts.net/api/health
```

JSON with `"status": "ok"` confirms three things in one tap: the box is up,
Tailscale is up, and the app is up. That is the check to make from a petrol
station before you bother with anything else.

**Tier 2 — a real shell.** Install an SSH client: *Termius* (the free tier is
enough, and its soft-keyboard toolbar has the keys a terminal needs) or *Blink
Shell* (paid, better if you live in a terminal). Connect to the box's MagicDNS
name, not an IP.

Enable Tailscale SSH on the box first:

```bash
sudo tailscale set --ssh
```

This is worth doing for a tablet specifically: there is **no keypair to
generate, store or protect on the iPad**, because authentication is your
tailnet identity. A device that rides in a tank bag is a poor place to keep a
private key.

One caveat. The default tailnet policy permits SSH to your own devices in
`check` mode, which periodically makes you re-authenticate in a browser —
mid-connection, on the iPad, in Safari. If that irritates you on the road,
either raise `checkPeriod` (up to 168 hours) or change that rule's `"action"`
from `"check"` to `"accept"` in the admin console under Access Controls.

Then the commands worth knowing:

```bash
systemctl status moto-route                              # state, uptime, recent log
journalctl -u moto-route -f                              # follow live
journalctl -u moto-route -n 50                           # last 50 lines
journalctl -u moto-route --since "1 hour ago" -p warning # just the complaints
```

The last one is the useful one on tour. The Overpass deadline logs at warning
level whenever it gives up on part of a route:

```
Overpass budget of 120s spent; skipping 1 chunk(s)
Overpass chunk 1/3 failed: Overpass did not answer within 105s.
```

So when a panel comes back saying some sections could not be checked, that
filter tells you which of the two it was — the budget ran out, or the server
refused the query.

Typing any of that on a soft keyboard is miserable. Put this in `~/.bashrc` on
the box and it becomes four keystrokes:

```bash
alias mstat='systemctl status moto-route --no-pager -n 20'
alias mlog='journalctl -u moto-route -f'
alias mwarn='journalctl -u moto-route --since today -p warning --no-pager'
```

---

## Does it come back by itself?

Yes, and there is nothing for you to enable — but confirm it deliberately
rather than discovering the answer in a hotel car park.

Two independent services have to survive a reboot.

**The app.** `install.sh` runs `systemctl enable --now moto-route`. Those two
words do different jobs, and the distinction is the whole answer: `--now`
starts it this instant, while `enable` symlinks the unit into
`multi-user.target.wants/`, which is what makes systemd start it at every boot.
The symlink is created because the unit ends with `[Install] WantedBy=
multi-user.target`. Crashes are covered separately by `Restart=on-failure`,
rate-limited to 5 restarts per 300 s so a crash loop cannot hammer the free
APIs.

**Tailscale.** `tailscaled` is enabled by its own installer, and a
`tailscale serve --bg` mapping is written into the node's serve config, so it
returns with the daemon after a reboot. The `--bg` matters: without it, `serve`
runs in the foreground and dies with the shell you started it from.

Four commands confirm the lot:

```bash
systemctl is-enabled moto-route    # enabled  -> starts at boot
systemctl is-active  moto-route    # active   -> running now
systemctl is-enabled tailscaled    # enabled
tailscale serve status             # your https URL, not "No serve config"
```

If `serve status` prints **No serve config**, the mapping was never made
persistent — run `sudo tailscale serve --bg 8000` again.

The honest test is `sudo reboot`, then those same four commands.

### What none of this survives

Tailscale reaches *your* box. It is not a copy in the cloud. If the home
connection drops or the power goes, the app is unreachable until both come
back, wherever in Europe you happen to be.

That is what the offline cache is for, and why the step above is worth the two
minutes: load the route and press **Cache tiles for this route** before you
leave, and the ride, the elevation profile and the map keep working with no
signal at all. A power cut is the one failure systemd cannot do anything about
— if the box sits somewhere unattended, a small UPS is the missing piece.

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

More log filters, and how to reach all of this from the tablet, are under
[Monitoring it from the iPad](#monitoring-it-from-the-ipad).

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

## Once it is running

The box has a connection, which the machine this was built on did not. Two
things are worth doing from there:

```bash
cd /opt/moto-route
.venv/bin/python tools/check_services.py
.venv/bin/python tools/check_services.py --route ~/my-tour.gpx
.venv/bin/python tools/verify_autobahn.py
```

Run these **as yourself, without sudo**. They need no privileges — they only
read a route file and make outbound requests — and running them as the
`motoroute` service user fails on anything in your home directory, because that
account is deliberately shut out of `/home`.

The first checks every external service the app needs, sending its real queries
— including the heavy closure query, which the public Overpass servers refuse
more readily than the lighter ones. Pass `--route` with one of your own long
tours as well: query cost scales with route length, and a 77 km route is not
evidence about a 500 km one. The last checks the German incident provider
against the live Autobahn API — the
one part of the app that has never seen real data. Exit 0 means it works; any
failure prints the field names it actually found, so the fix is obvious.

If you use the incidents layer outside Germany, set `MOTO_INCIDENT_FEEDS` in
`/etc/moto-route.env` to a GeoJSON feed from your own road authority.

---

## Running your own Overpass

Every slow layer in this app is an Overpass query. The public servers answer a
389 km closure query in 20-40 seconds when they are not refusing it outright,
and no amount of work in this codebase changes that — the measurements in
`CLAUDE.md` end with the same conclusion each time. A local instance is the
only thing that makes those layers fast.

### Read this before you start: it is a RAM problem, not a disk problem

A conventional Overpass covering a dozen European countries wants **8-16 GB of
RAM**. The project's own Docker image documents 4 GB plus 2 GB of swap as the
*minimum to complete an installation*, and people report the import being
killed at 3 GB on an extract as small as Great Britain. Overpass gets its speed
from having the database in page cache; starve it and it is slower than the
public servers, not faster.

So on a 2 GB machine, importing thirteen countries the ordinary way will not
work. Two honest ways forward:

1. **Put more RAM in the box.** It is an old machine if it has 2 GB, and 8 GB of
   DDR3 costs less than a tank of fuel. Then ignore the rest of this section's
   cleverness and import the country extracts directly.
2. **Import only what this app asks for**, which is what
   `deploy/overpass/build-extract.sh` does, and what the rest of this section
   describes.

### Why a filtered extract works

This app does not query the map. It queries nine tags:

    fuel, cafe, viewpoint                      (pois.py)
    highway=construction, construction,
    access=no, motor_vehicle=no, seasonal,
    snowplowing, barrier                       (hazards.py)

Everything else in a country extract — every building, address, field boundary
and power line — is imported, indexed, and never read once. Filtering it out
first turns roughly 16 GB of country extracts into something small enough that
the finished database fits in page cache on a modest machine.

**The trade, stated plainly:** the resulting database answers this app's
questions and no others. Add a layer that needs a new tag and the extract must
be rebuilt with that tag added to `KEEP` in the script. That is written at the
top of the script too, so nobody discovers it by getting empty results.

### Build the extract

```bash
sudo apt install osmium-tool
sudo mkdir -p /var/lib/overpass-build && sudo chown "$USER" /var/lib/overpass-build
deploy/overpass/build-extract.sh /var/lib/overpass-build
```

It downloads each country, filters it, and merges the results. Downloads resume
if interrupted (`curl -C -`) and countries already filtered are skipped, so it
is safe to re-run. Budget ~16 GB of downloads and a few hours on a home
connection; the filtering is I/O-bound and modest on RAM.

It finishes by printing the settings to paste into `/etc/moto-route.env`,
including the coverage box computed from the data itself.

To change which countries are covered, edit `COUNTRIES` at the top of the
script — they are Geofabrik paths. (Note that Geofabrik still files North
Macedonia under `europe/macedonia`.)

### Run Overpass

Docker is much the easiest route on a barebone Debian box:

```bash
sudo apt install docker.io
sudo docker run -d --restart unless-stopped \
  -e OVERPASS_MODE=init \
  -e OVERPASS_META=no \
  -e OVERPASS_PLANET_URL=file:///data/touring-europe.osm.pbf \
  -e OVERPASS_DIFF_URL='' \
  -e OVERPASS_RULES_LOAD=5 \
  -v /var/lib/overpass-db:/db \
  -v /var/lib/overpass-build:/data:ro \
  -p 127.0.0.1:12345:80 \
  --name overpass wiktorn/overpass-api
```

`OVERPASS_META=no` skips version and changeset metadata, which this app never
reads and which costs both disk and import time. Binding to `127.0.0.1` keeps
the instance off the network, like the app itself.

Watch the import with `sudo docker logs -f overpass`. When it is serving:

```bash
curl -s -X POST http://127.0.0.1:12345/api/interpreter \
  --data-urlencode 'data=[out:json];node(around:2000,37.98,23.73)["amenity"="fuel"];out center 5;'
```

### Point the app at it

```
MOTO_OVERPASS_URL=http://127.0.0.1:12345/api/interpreter
MOTO_OVERPASS_COVERAGE=<the box the script printed>
MOTO_OVERPASS_CONCURRENCY=4
```

Keep `MOTO_OVERPASS_FALLBACK_URLS` at its default. Your instance is the
primary; the public servers stay as the fallback.

`MOTO_OVERPASS_COVERAGE` is the important one, and it is worth understanding
rather than pasting. An instance built from country extracts does not know what
it is missing: ask it about a road in Spain and it returns HTTP 200 with an
empty result, which is indistinguishable from a road with no closures on it.
The rider is told there is nothing to worry about, by a database that has never
heard of the road. With the coverage box set, a route that leaves the box is
sent to the public servers instead — the decision is made once per layer, so a
route crossing the edge never has half its sections answered by a database that
only holds the other half.

Raising `MOTO_OVERPASS_CONCURRENCY` is only worth doing once the server is your
own. On the public servers 2 is the documented per-IP allowance; on your own
machine the limit is the machine.

### What has actually been tested here

`build-extract.sh` was run end to end against a local stand-in for Geofabrik,
and the tag filter was verified on a hand-built sample: a `highway=construction`
way survives with all of its geometry nodes, an `access=no` road survives, a
building and a bench do not. The coverage box the script prints was fed back
into the app and confirmed to route an inside route to the local server and an
outside one to the public servers.

**Not tested:** the real download, the real import, and the Docker recipe —
this build environment has neither the disk nor the network for a 16 GB
extract. The osmium commands are verified; the Docker invocation follows the
image's documented variables but you are the first to run it.

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
outbound network: `systemctl enable --now` itself and therefore the reboot
behaviour, the Tailscale and Caddy recipes including Tailscale SSH, and the
live third-party APIs. The commands are standard and the unit
is validated, but you are the first to run those particular steps.
