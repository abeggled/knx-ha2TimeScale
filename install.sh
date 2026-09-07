#!/usr/bin/env bash
# Install the collector as a systemd service. Run with sudo on the target host.
#
#   sudo ./install.sh
#
# Expects the working copy in the current directory and an environment file
# at /etc/knx-ha2timescale.env (see .env.example). Creates a dedicated
# service user without a login shell.
set -euo pipefail

DEST=/opt/knx-ha2timescale
USER_NAME=collector
ENV_FILE=/etc/knx-ha2timescale.env

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE — see .env.example"; exit 1; }

id -u "$USER_NAME" >/dev/null 2>&1 || \
    useradd --system --home-dir "$DEST" --shell /usr/sbin/nologin "$USER_NAME"

mkdir -p "$DEST"
install -m 644 -t "$DEST" \
    db.py registry.py writer.py knx_source.py ha_source.py service.py ui.py \
    compare_dryrun.py import_knxproj.py seed_ga.py seed_units.py \
    setup_ui_role.py requirements.txt schema.sql schema_ha.sql schema_status.sql
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
    "$DEST/.venv/bin/pip" -q install -r "$DEST/requirements.txt"
fi

chown -R "$USER_NAME:$USER_NAME" "$DEST"
chown root:"$USER_NAME" "$ENV_FILE"
chmod 640 "$ENV_FILE"

install -m 644 knx-ha2timescale.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable knx-ha2timescale.service

# The web UI is optional: enabled only when its environment file exists.
if [[ -f /etc/knx-ha2timescale-ui.env ]]; then
    chown root:"$USER_NAME" /etc/knx-ha2timescale-ui.env
    chmod 640 /etc/knx-ha2timescale-ui.env
    install -m 644 knx-ha2timescale-ui.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable knx-ha2timescale-ui.service
    echo "web UI enabled"
fi

echo
echo "installed. Start with:  systemctl start knx-ha2timescale"
echo "Logs:                   journalctl -u knx-ha2timescale -f"
