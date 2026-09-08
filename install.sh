#!/usr/bin/env bash
# Install the collector as a systemd service. Run with sudo on the target host.
#
#   sudo ./install.sh
#
# Expects the working copy in the current directory and an environment file
# at /etc/homearchive.env (see .env.example). Creates a dedicated
# service user without a login shell.
set -euo pipefail

DEST=/opt/homearchive
USER_NAME=collector
ENV_FILE=/etc/homearchive.env

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE — see .env.example"; exit 1; }

id -u "$USER_NAME" >/dev/null 2>&1 || \
    useradd --system --home-dir "$DEST" --shell /usr/sbin/nologin "$USER_NAME"

mkdir -p "$DEST"
# Everything the services need, by glob — a hand maintained list is one
# forgotten entry away from a service that starts and then fails.
install -m 644 -t "$DEST" ./*.py ./*.sql requirements.txt
mkdir -p "$DEST/templates"
install -m 644 -t "$DEST/templates" templates/*.html

if [[ ! -x "$DEST/.venv/bin/python" ]]; then
    rm -rf "$DEST/.venv"
    if python3 -m venv "$DEST/.venv" 2>/dev/null; then
        :
    else
        # Debian ships python3 without ensurepip unless python3-venv is
        # installed. Bootstrap pip instead of requiring another package.
        echo "ensurepip unavailable, bootstrapping pip"
        python3 -m venv --without-pip "$DEST/.venv"
        python3 -c 'import urllib.request as u; open("/tmp/get-pip.py","wb").write(u.urlopen("https://bootstrap.pypa.io/get-pip.py", timeout=60).read())'
        "$DEST/.venv/bin/python" /tmp/get-pip.py -q
        rm -f /tmp/get-pip.py
    fi
fi

# Always sync dependencies: the venv may predate a new requirement.
"$DEST/.venv/bin/pip" -q install -r "$DEST/requirements.txt"

chown -R "$USER_NAME:$USER_NAME" "$DEST"
chown root:"$USER_NAME" "$ENV_FILE"
chmod 640 "$ENV_FILE"

install -m 644 homearchive.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable homearchive.service

# The web UI is optional: enabled only when its environment file exists.
UI_ENABLED=false
if [[ -f /etc/homearchive-ui.env ]]; then
    chown root:"$USER_NAME" /etc/homearchive-ui.env
    chmod 640 /etc/homearchive-ui.env
    install -m 644 homearchive-ui.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable homearchive-ui.service
    UI_ENABLED=true
fi

echo
echo "installed."
echo
echo "  collector   systemctl start homearchive"
echo "              journalctl -u homearchive -f"
if $UI_ENABLED; then
    UI_PORT=$(sed -n 's/^UI_PORT="\?\([0-9]*\)"\?/\1/p' /etc/homearchive-ui.env)
    echo
    echo "  web UI      systemctl start homearchive-ui"
    echo "              journalctl -u homearchive-ui -f"
    echo "              http://$(hostname -f):${UI_PORT:-8080}"
else
    echo
    echo "  web UI      not enabled — create /etc/homearchive-ui.env"
    echo "              (see setup_ui_role.py) and run this script again"
fi
echo
echo "Both services are already running? Restart them to pick up the changes:"
echo "  systemctl restart homearchive${UI_ENABLED:+ homearchive-ui}"
