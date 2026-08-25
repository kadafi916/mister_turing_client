#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# mister_turing_client — installer
#
# Mirrors MiSTer_monitor's own MiSTer/install.sh pattern (proven working on
# this device for the server half). Doesn't assemble the vendored pylibs/ +
# vendor_libs/ bundle itself - run build_pylibs.sh first (once, needs
# network access) if it's not already in place; see README.md for why
# that's a separate step.
#
# Usage: copy this whole repo to your MiSTer (e.g. to
#   /media/fat/mister_turing_client_install/), then run:
#
#   bash /media/fat/mister_turing_client_install/build_pylibs.sh   # once
#   bash /media/fat/mister_turing_client_install/install.sh
#

set -e

SCRIPTS_DIR="/media/fat/Scripts"
CONFIG_DIR="${SCRIPTS_DIR}/.config/mister_monitor"
TARGET_DIR="${CONFIG_DIR}/turing_client"
STARTUP_FILE="/media/fat/linux/user-startup.sh"
STARTUP_LINE="${TARGET_DIR}/start_turing_client.sh start"

INSTALLER_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

echo "mister_turing_client installer"
echo "==============================="
echo

if [ ! -d "/media/fat" ]; then
    echo "ERROR: /media/fat not found. Are you running this on a MiSTer?"
    exit 1
fi

if [ ! -f "${INSTALLER_DIR}/mister_turing_client.py" ]; then
    echo "ERROR: cannot find mister_turing_client.py next to install.sh."
    echo "Make sure you copied the entire repo, not just this file."
    exit 1
fi

# ===== Stop any running instance first =====
if [ -f "${TARGET_DIR}/start_turing_client.sh" ]; then
    echo "Existing installation found, stopping current client..."
    "${TARGET_DIR}/start_turing_client.sh" stop 2>/dev/null || true
    sleep 1
fi

# ===== Create directory structure =====
echo "Creating ${TARGET_DIR}..."
mkdir -p "${TARGET_DIR}"

# ===== Copy source files (not the vendored binary bundle - see below) =====
echo "Installing source files..."
for f in mister_turing_client.py screenscraper.py screenscraper_systems.py \
         libretro_thumbs.py retroachievements.py config.py \
         build_pylibs.sh uninstall.sh README.md TROUBLESHOOTING.md LICENSE; do
    cp "${INSTALLER_DIR}/${f}" "${TARGET_DIR}/"
done
chmod +x "${TARGET_DIR}/build_pylibs.sh" "${TARGET_DIR}/uninstall.sh"
cp -r "${INSTALLER_DIR}/turing_lcd" "${TARGET_DIR}/"
cp -r "${INSTALLER_DIR}/fonts" "${TARGET_DIR}/"

if [ ! -f "${TARGET_DIR}/config.ini" ]; then
    cp "${INSTALLER_DIR}/config.ini.example" "${TARGET_DIR}/config.ini"
    echo "Created config.ini from the template - fill in ScreenScraper"
    echo "credentials to enable artwork (see README.md)."
else
    echo "Existing config.ini found, leaving it untouched."
fi

echo "Installing start_turing_client.sh..."
cp "${INSTALLER_DIR}/mister/start_turing_client.sh" "${TARGET_DIR}/"
chmod +x "${TARGET_DIR}/start_turing_client.sh"

# ===== Vendored binary dependencies =====
# pylibs/ (Pillow/numpy/pyserial) and vendor_libs/ (the .so files those need
# beyond what MiSTer's Buildroot Python ships) are gitignored - they're
# large prebuilt binaries, not source. build_pylibs.sh assembles them (run
# it once, needs network access) - see README.md "Why the odd pylibs/ +
# vendor_libs/ layout" for the full reasoning. An existing bundle is left
# untouched (never overwritten by this script); a missing one just gets a
# loud warning rather than a silent later failure.
if [ ! -d "${TARGET_DIR}/pylibs" ] || [ ! -d "${TARGET_DIR}/vendor_libs" ]; then
    echo
    echo "WARNING: pylibs/ and/or vendor_libs/ not found at ${TARGET_DIR}."
    echo "         The client will not run without them. Run:"
    echo "           bash ${TARGET_DIR}/build_pylibs.sh"
    echo "         then run start_turing_client.sh start."
    echo
fi

# ===== Configure auto-start =====
if [ ! -f "${STARTUP_FILE}" ]; then
    echo "Creating ${STARTUP_FILE}..."
    mkdir -p "$(dirname "${STARTUP_FILE}")"
    cat > "${STARTUP_FILE}" <<'EOF'
#!/bin/bash
# user-startup.sh — runs at MiSTer boot.

EOF
    chmod +x "${STARTUP_FILE}"
fi

if grep -qF "${STARTUP_LINE}" "${STARTUP_FILE}"; then
    echo "Auto-start already configured in ${STARTUP_FILE}"
else
    echo "Adding auto-start line to ${STARTUP_FILE}..."
    echo "" >> "${STARTUP_FILE}"
    echo "# mister_turing_client — added by install.sh" >> "${STARTUP_FILE}"
    echo "${STARTUP_LINE}" >> "${STARTUP_FILE}"
fi

# ===== Start it now, if the runtime bundle is actually there =====
if [ -d "${TARGET_DIR}/pylibs" ] && [ -d "${TARGET_DIR}/vendor_libs" ]; then
    echo
    echo "Starting mister_turing_client..."
    "${TARGET_DIR}/start_turing_client.sh" start
else
    echo "Skipping start - runtime bundle missing (see warning above)."
fi

echo
echo "========================================"
echo "Installation complete."
echo "========================================"
echo
echo "The client will start automatically on boot (via ${STARTUP_FILE})"
echo "and respawns on its own if the process ever exits unexpectedly"
echo "(e.g. a USB replug outlasting its reconnect window)."
echo
echo "To check status:  ${TARGET_DIR}/start_turing_client.sh status"
echo "To stop:          ${TARGET_DIR}/start_turing_client.sh stop"
echo "To uninstall:     bash ${TARGET_DIR}/uninstall.sh"
echo

# ===== Self-cleanup =====
# Everything needed has been copied to its permanent location; remove the
# staging copy the same way MiSTer_monitor's own install.sh does.
echo "Cleaning up installation folder..."
rm -rf "${INSTALLER_DIR}"
