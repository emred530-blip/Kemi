#!/usr/bin/env bash
# Turn a fresh Debian/Ubuntu server into a public Kemi deployment:
#
#   sudo ./deploy/install-server.sh --domain kemi.example.com
#
# What it does, all of it reversible:
#   1. creates the unprivileged 'kemi' system user and /var/lib/kemi
#   2. installs Kemi into its own virtualenv at /opt/kemi/venv
#   3. writes /etc/kemi/kemi.env (with a generated admin key) if absent
#   4. installs and enables the three systemd units
#   5. writes the nginx site for --domain and, with --tls, obtains a cert
#
# Re-running it is safe: it upgrades the code and units, and never overwrites
# an existing /etc/kemi/kemi.env or an identity that already has a balance.
#
# Flags:
#   --domain <name>   the public hostname (required for nginx/TLS)
#   --tls             also run certbot for that domain
#   --email <addr>    contact address for Let's Encrypt
#   --no-nginx        install the services only, no reverse proxy
#   --peer host:port  join an existing network (repeatable)
set -euo pipefail

DOMAIN=""
EMAIL=""
WANT_TLS=0
WANT_NGINX=1
PEERS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --domain)   DOMAIN="$2"; shift 2 ;;
    --email)    EMAIL="$2"; shift 2 ;;
    --peer)     PEERS+=("$2"); shift 2 ;;
    --tls)      WANT_TLS=1; shift ;;
    --no-nginx) WANT_NGINX=0; shift ;;
    -h|--help)  sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run this with sudo" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# --- 1. user and state ------------------------------------------------------
say "user and state directories"
if ! id -u kemi >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/kemi --shell /usr/sbin/nologin kemi
  echo "created system user 'kemi'"
fi
install -d -o kemi -g kemi -m 0750 /var/lib/kemi \
  /var/lib/kemi/node /var/lib/kemi/portal /var/lib/kemi/gateway
install -d -m 0755 /etc/kemi /opt/kemi /opt/kemi/bin

# --- 2. the code ------------------------------------------------------------
say "installing Kemi into /opt/kemi/venv"
if ! command -v python3 >/dev/null; then
  echo "python3 is required" >&2; exit 1
fi
python3 -m venv /opt/kemi/venv || {
  echo "creating the virtualenv failed — on Debian/Ubuntu this usually means:" >&2
  echo "  sudo apt-get install -y python3-venv" >&2; exit 1; }
/opt/kemi/venv/bin/pip install --quiet --upgrade pip
if [ -f "$REPO/pyproject.toml" ]; then
  /opt/kemi/venv/bin/pip install --quiet "$REPO[crypto]"
  echo "installed from the checkout at $REPO"
else
  /opt/kemi/venv/bin/pip install --quiet "kemi[crypto]"
  echo "installed from the package index"
fi
install -m 0755 "$HERE/kemi-launch.sh" /opt/kemi/bin/kemi-launch
/opt/kemi/venv/bin/kemi --version || true

# --- 3. settings ------------------------------------------------------------
say "settings at /etc/kemi/kemi.env"
if [ -f /etc/kemi/kemi.env ]; then
  echo "keeping the existing file (delete it to regenerate)"
else
  ADMIN_KEY="$(/opt/kemi/venv/bin/python -c \
    'import secrets; print(secrets.token_urlsafe(24))')"
  peer_list="$(IFS=,; echo "${PEERS[*]-}")"
  sed -e "s|^KEMI_ADVERTISE=.*|KEMI_ADVERTISE=${DOMAIN}|" \
      -e "s|^KEMI_ADMIN_KEY=.*|KEMI_ADMIN_KEY=${ADMIN_KEY}|" \
      -e "s|^KEMI_PEERS=.*|KEMI_PEERS=${peer_list}|" \
      "$HERE/kemi.env.example" >/etc/kemi/kemi.env
  echo "generated a fresh admin key"
fi
chown root:kemi /etc/kemi/kemi.env
chmod 0640 /etc/kemi/kemi.env

# --- 4. services ------------------------------------------------------------
say "systemd units"
install -m 0644 "$HERE"/systemd/kemi-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kemi-node kemi-portal kemi-gateway
sleep 3
for unit in kemi-node kemi-portal kemi-gateway; do
  state="$(systemctl is-active "$unit" || true)"
  printf '  %-14s %s\n' "$unit" "$state"
  [ "$state" = "active" ] || echo "    journalctl -u $unit -n 40   # to see why"
done

# --- 5. reverse proxy -------------------------------------------------------
if [ "$WANT_NGINX" -eq 1 ] && [ -n "$DOMAIN" ]; then
  say "nginx site for $DOMAIN"
  if ! command -v nginx >/dev/null; then
    echo "nginx is not installed — skipping (apt-get install nginx, then re-run)"
  else
    sed "s|kemi.example.com|$DOMAIN|g" "$HERE/nginx/kemi.conf" \
      >/etc/nginx/sites-available/kemi
    ln -sf /etc/nginx/sites-available/kemi /etc/nginx/sites-enabled/kemi
    if nginx -t; then
      systemctl reload nginx
      echo "proxying $DOMAIN to the portal"
    else
      echo "nginx rejected the config; the site was written but not enabled" >&2
    fi
    if [ "$WANT_TLS" -eq 1 ]; then
      if command -v certbot >/dev/null; then
        certbot_args=(--nginx -d "$DOMAIN" --non-interactive --agree-tos)
        if [ -n "$EMAIL" ]; then
          certbot_args+=(--email "$EMAIL")
        else
          certbot_args+=(--register-unsafely-without-email)
        fi
        certbot "${certbot_args[@]}"
      else
        echo "certbot is not installed — apt-get install certbot python3-certbot-nginx" >&2
      fi
    fi
  fi
elif [ "$WANT_NGINX" -eq 1 ]; then
  echo
  echo "no --domain given, so no reverse proxy was configured."
  echo "The portal is listening on 127.0.0.1:8090 only."
fi

# --- done -------------------------------------------------------------------
KEY="$(grep '^KEMI_ADMIN_KEY=' /etc/kemi/kemi.env | cut -d= -f2-)"
PORT="$(grep '^KEMI_PORT=' /etc/kemi/kemi.env | cut -d= -f2-)"
say "done"
cat <<EOF
  Portal            https://${DOMAIN:-<your-domain>}/
  Operator console  https://${DOMAIN:-<your-domain>}/admin
  Admin key         ${KEY}
  OpenAI endpoint   https://${DOMAIN:-<your-domain>}/v1

  Open the peer-to-peer port for BOTH protocols, or no one can reach this node:
    sudo ufw allow ${PORT:-7700}/tcp && sudo ufw allow ${PORT:-7700}/udp

  Logs:     journalctl -u kemi-portal -f
  Settings: /etc/kemi/kemi.env   (restart the services after editing)
EOF
