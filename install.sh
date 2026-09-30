#!/usr/bin/env bash
# Installs or updates Planespotter as a systemd service.
# Run as root inside the machine/LXC that runs tar1090:
#   curl -fsSL https://raw.githubusercontent.com/nguyenware/trmnl-planespotter/main/install.sh | bash
set -euo pipefail

REPO="${REPO:-https://github.com/nguyenware/trmnl-planespotter.git}"
BRANCH="${BRANCH:-main}"
DIR="${DIR:-/opt/trmnl-planespotter}"
SVC_USER=planespotter

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root (inside the LXC: pct enter <id>, or sudo bash install.sh)." >&2
  exit 1
fi

echo "==> Installing packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv git curl ca-certificates >/dev/null

echo "==> Fetching code into $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin "$BRANCH"
  git -C "$DIR" checkout -q "$BRANCH"
  git -C "$DIR" reset -q --hard "origin/$BRANCH"
else
  git clone -q -b "$BRANCH" "$REPO" "$DIR"
fi

echo "==> Setting up Python environment"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

echo "==> Creating service user '$SVC_USER'"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"

if [ ! -f "$DIR/.env" ]; then
  cp "$DIR/.env.example" "$DIR/.env"
  echo "==> Created $DIR/.env from the example"
fi
chown root:"$SVC_USER" "$DIR/.env"
chmod 640 "$DIR/.env"

echo "==> Installing systemd unit"
cp "$DIR/planespotter.service" /etc/systemd/system/planespotter.service
systemctl daemon-reload
systemctl enable -q planespotter

echo "==> Looking for tar1090 data"
if runuser -u "$SVC_USER" -- "$DIR/.venv/bin/python" "$DIR/planespotter.py" detect; then
  echo "    OK"
else
  echo "    Set TAR1090_URL or AIRCRAFT_JSON in $DIR/.env, then re-run this check:"
  echo "    runuser -u $SVC_USER -- $DIR/.venv/bin/python $DIR/planespotter.py detect"
fi

if grep -q '^TRMNL_WEBHOOK_URL=https://trmnl.com/api/custom_plugins/your-plugin-uuid' "$DIR/.env"; then
  echo
  echo "Next: put your webhook URL in $DIR/.env (nano $DIR/.env), then:"
  echo "  systemctl restart planespotter && journalctl -u planespotter -f"
else
  systemctl restart planespotter
  echo
  echo "Running. Follow the log with: journalctl -u planespotter -f"
fi
