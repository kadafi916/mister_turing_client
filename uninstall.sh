#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# mister_turing_client — uninstaller
#
# Usage:
#   bash /media/fat/Scripts/.config/mister_turing_client/uninstall.sh

TARGET_DIR="/media/fat/Scripts/.config/mister_turing_client"
STARTUP_FILE="/media/fat/linux/user-startup.sh"
STARTUP_LINE="${TARGET_DIR}/start_turing_client.sh start"

echo "mister_turing_client uninstaller"
echo "================================="
echo

if [ -f "${TARGET_DIR}/start_turing_client.sh" ]; then
    echo "Stopping running client..."
    "${TARGET_DIR}/start_turing_client.sh" stop || true
fi

if [ -f "${STARTUP_FILE}" ] && grep -qF "${STARTUP_LINE}" "${STARTUP_FILE}"; then
    echo "Removing auto-start line from ${STARTUP_FILE}..."
    grep -vF "${STARTUP_LINE}" "${STARTUP_FILE}" \
        | grep -vF "# mister_turing_client — added by install.sh" \
        > "${STARTUP_FILE}.tmp"
    mv "${STARTUP_FILE}.tmp" "${STARTUP_FILE}"
fi

# Non-interactive-safe: no stdin (e.g. run over a plain SSH exec) reads as
# empty, which the regex below treats as "no" - deletion never happens
# without an explicit 'y', including when this runs unattended.
read -p "Remove ${TARGET_DIR} entirely, including config.ini and artwork_cache/? [y/N] " -n 1 -r REPLY
echo
if [[ "$REPLY" =~ ^[Yy]$ ]]; then
    rm -rf "${TARGET_DIR}"
    echo "Removed ${TARGET_DIR}"
else
    echo "Left ${TARGET_DIR} in place (config.ini and cached artwork preserved)."
fi

echo "Uninstall complete."
