#!/usr/bin/env bash
#
# Install (or update) the motorcycle route mapper as a systemd service.
#
#   sudo deploy/install.sh
#
# Installs from the checkout this script lives in, so it needs no credentials
# for a private repository — clone or copy the folder to the box first, then
# run this. Re-running it updates an existing install in place.
#
# The service listens on 127.0.0.1 only. Nothing in this app authenticates its
# callers, so exposing it directly would let anyone who finds it use your IP to
# hammer the free Overpass/Open-Meteo/OSRM services. Put Tailscale or a reverse
# proxy in front — see DEPLOY.md.

set -euo pipefail

APP_USER="${APP_USER:-motoroute}"
APP_DIR="${APP_DIR:-/opt/moto-route}"
PORT="${PORT:-8000}"
UNIT_NAME="moto-route.service"

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this with sudo"
[ -f "$SOURCE_DIR/requirements.txt" ] || die "no requirements.txt in $SOURCE_DIR — run this from the repo checkout"
command -v python3 >/dev/null || die "python3 is not installed (apt install python3 python3-venv)"

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "python 3.11 or newer is required (found $(python3 -V))"

say "Creating the service user if it does not exist"
if id "$APP_USER" >/dev/null 2>&1; then
  echo "  $APP_USER already exists"
else
  # A system account with no login shell and no home: it only runs the service.
  useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"
  echo "  created $APP_USER"
fi

say "Copying the application to $APP_DIR"
mkdir -p "$APP_DIR"
# Trailing slash on the source copies its contents. The venv is excluded so a
# re-run never clobbers the installed one with a developer's local copy.
if command -v rsync >/dev/null; then
  rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
    --exclude '.pytest_cache' \
    "$SOURCE_DIR/" "$APP_DIR/"
else
  # tar keeps this working on a box without rsync installed.
  tar -C "$SOURCE_DIR" \
      --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
      --exclude='.pytest_cache' -cf - . \
    | tar -C "$APP_DIR" -xf -
fi

say "Building the virtual environment"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/.venv" \
    || die "could not create a venv (apt install python3-venv)"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
echo "  $("$APP_DIR/.venv/bin/python" -V) with dependencies installed"

say "Setting ownership"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
# The app never writes inside its own directory; the cache lives in the
# StateDirectory systemd creates. Read-only is enough.
chmod -R go-w "$APP_DIR"

say "Installing the systemd unit"
install -m 0644 "$APP_DIR/deploy/moto-route.service" "/etc/systemd/system/$UNIT_NAME"
if [ "$PORT" != "8000" ] || [ "$APP_DIR" != "/opt/moto-route" ] || [ "$APP_USER" != "motoroute" ]; then
  sed -i \
    -e "s#--port 8000#--port $PORT#" \
    -e "s#/opt/moto-route#$APP_DIR#g" \
    -e "s#^User=motoroute#User=$APP_USER#" \
    -e "s#^Group=motoroute#Group=$APP_USER#" \
    "/etc/systemd/system/$UNIT_NAME"
  echo "  adjusted for user=$APP_USER dir=$APP_DIR port=$PORT"
fi

if [ ! -f /etc/moto-route.env ]; then
  install -m 0640 "$APP_DIR/deploy/moto-route.env.example" /etc/moto-route.env
  chown root:"$APP_USER" /etc/moto-route.env
  echo "  wrote /etc/moto-route.env (all settings commented out)"
else
  echo "  kept your existing /etc/moto-route.env"
fi

say "Starting the service"
was_running=$(systemctl is-active "$UNIT_NAME" 2>/dev/null || true)
before=$(systemctl show -p MainPID --value "$UNIT_NAME" 2>/dev/null || echo 0)

systemctl daemon-reload
systemctl enable "$UNIT_NAME" >/dev/null 2>&1 || true
# restart, not `enable --now`. `--now` only *starts* a stopped unit, so
# re-running this installer against a live service copied the new code and left
# the old process serving the old one -- while printing "Running." in green.
# Days of measurements were taken against code that had been replaced on disk.
systemctl restart "$UNIT_NAME"
sleep 2

after=$(systemctl show -p MainPID --value "$UNIT_NAME" 2>/dev/null || echo 0)

if systemctl is-active --quiet "$UNIT_NAME"; then
  if [ "$was_running" = "active" ] && [ "$before" = "$after" ]; then
    # Should not happen, but a silent no-op here is exactly the failure this
    # change exists to remove, so it is worth saying out loud.
    echo "  WARNING: the service kept PID $after — it may still be running the"
    echo "           code that was there before. Try: sudo systemctl restart $UNIT_NAME"
  else
    echo "  restarted: PID $before -> $after"
  fi
  printf '\n\033[32mRunning.\033[0m http://127.0.0.1:%s\n\n' "$PORT"
  echo "It is bound to loopback on purpose — this app has no authentication."
  echo "To reach it from your tablet, put Tailscale in front:"
  echo
  echo "    sudo tailscale serve --bg $PORT"
  echo
  echo "See DEPLOY.md for that and the reverse-proxy alternative."
  echo "Logs:    journalctl -u $UNIT_NAME -f"
  echo "Restart: sudo systemctl restart $UNIT_NAME"
else
  echo
  echo "The service did not come up. What it said:"
  journalctl -u "$UNIT_NAME" -n 30 --no-pager || true
  exit 1
fi
