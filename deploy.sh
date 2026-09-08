#!/usr/bin/env bash
# Copy the whole working tree to the target host, so no file can be forgotten.
#
#   ./deploy.sh admdaniel@iqsrv36.a38.ch
#
# Uses tar over ssh — no rsync needed on the target. Secrets and local state
# are excluded; installing into /opt still needs root and stays manual.
set -euo pipefail

TARGET=${1:?usage: deploy.sh user@host [remote-dir]}
REMOTE=${2:-homearchive}
KEY=${DEPLOY_KEY:-$HOME/.ssh/id_ed25519_knxmig}
SSH="ssh -i $KEY -o BatchMode=yes"

$SSH "$TARGET" "mkdir -p '$REMOTE/templates'"

tar czf - \
    --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='*.log' --exclude='.env*' --exclude='*.knxproj' \
    --exclude='*.pwd' --exclude='ui.env' --exclude='collector.env' \
    ./*.py ./*.sql ./*.sh ./*.service requirements.txt templates \
  | $SSH "$TARGET" "tar xzf - -C '$REMOTE' && chmod +x '$REMOTE'/*.sh"

# tar only adds. Remove remote files that no longer exist locally, otherwise a
# deleted template keeps being installed.
KEEP=$(cd templates && ls *.html | tr '\n' ' ')
$SSH "$TARGET" "cd '$REMOTE/templates' && for f in *.html; do
    case \" $KEEP \" in *\" \$f \"*) ;; *) rm -f \"\$f\"; echo \"entfernt: \$f\";; esac
done"

echo "synced to $TARGET:$REMOTE"
echo
echo "On the target, as root:"
echo "  cd ~/$REMOTE && ./install.sh"
echo "  systemctl daemon-reload"
echo "  systemctl restart homearchive homearchive-ui"
