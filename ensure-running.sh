#!/bin/bash
# Sanctum Sanctorum watchdog — keeps the bot alive across VM replacements.
# The project lives in the home directory (persists), but the systemd unit
# in /etc, the `sanctum` system user, and the home directory's traverse
# permission do NOT survive a VM replacement. This script restores all
# three, then restarts the service if it is down.
# Run it on a schedule (e.g. every 5 minutes via a cron job).
set -u
PROJECT="/home/hatch/workspace/sanctum-sanctorum"
UNIT="/etc/systemd/system/sanctum-sanctorum.service"
SRC="$PROJECT/sanctum-sanctorum.service"

# Reinstall the unit if it is gone (VM was replaced).
if [ ! -f "$UNIT" ]; then
    if [ -f "$SRC" ]; then
        cp "$SRC" "$UNIT" 2>/dev/null || sudo cp "$SRC" "$UNIT"
        systemctl daemon-reload 2>/dev/null || sudo systemctl daemon-reload
        systemctl enable sanctum-sanctorum 2>/dev/null || sudo systemctl enable sanctum-sanctorum
        echo "$(date -Iseconds) watchdog: reinstalled missing systemd unit"
    else
        echo "$(date -Iseconds) watchdog: ERROR project service file missing at $SRC"
        exit 1
    fi
fi

# Recreate the `sanctum` system user if a VM replacement wiped it.
# The service runs as this user (uid 999); without the account the unit
# fails with 217/USER and the bot never starts. The project files are
# already owned by uid 999, so recreating the user with the same uid
# restores access to them. The account has no login shell and no password.
if ! id sanctum >/dev/null 2>&1; then
    sudo useradd --system --uid 999 --home-dir /home/sanctum \
        --shell /usr/sbin/nologin sanctum 2>/dev/null \
        || sudo useradd -r -u 999 sanctum
    if id sanctum >/dev/null 2>&1; then
        echo "$(date -Iseconds) watchdog: recreated missing sanctum user (uid 999)"
    else
        echo "$(date -Iseconds) watchdog: ERROR could not recreate sanctum user"
        exit 1
    fi
fi

# The sanctum user must be able to reach the project through /home/hatch.
# A VM replacement can reset the home directory's permissions (the service
# then fails with 200/CHDIR). Traverse-only (o+x) restores access without
# opening the directory to listing. Idempotent: safe to run every time.
chmod o+x /home/hatch 2>/dev/null || sudo chmod o+x /home/hatch

# Start the service if it is not active.
if ! systemctl is-active --quiet sanctum-sanctorum 2>/dev/null; then
    systemctl start sanctum-sanctorum 2>/dev/null || sudo systemctl start sanctum-sanctorum
    sleep 3
    if systemctl is-active --quiet sanctum-sanctorum 2>/dev/null; then
        echo "$(date -Iseconds) watchdog: restarted sanctum-sanctorum (was down)"
    else
        echo "$(date -Iseconds) watchdog: ERROR failed to start sanctum-sanctorum"
        exit 1
    fi
fi
exit 0
