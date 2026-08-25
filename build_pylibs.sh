#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# mister_turing_client — pylibs/vendor_libs bundle builder
#
# Assembles the two vendored, gitignored directories mister_turing_client.py
# needs to run (Pillow, numpy, pyserial + the .so files they need beyond
# what MiSTer's own Buildroot ships) - see README.md "Why the odd
# pylibs/ + vendor_libs/ layout" for the full reasoning: no compiler and
# no pip on-device, so this downloads prebuilt armv7l wheels from
# piwheels.org and .so files extracted from Debian bullseye armhf .debs,
# no build step involved anywhere.
#
# Every URL below is pinned to an exact version already verified end-to-end
# on real MiSTer hardware (glibc floor checked via objdump -T, NEEDED
# entries confirmed to resolve, the whole stack round-tripped: JPEG
# encode/decode, TrueType rendering, RGB->RGB565 packing via numpy). This
# script deliberately does not fetch "latest" - see "Bumping a pinned
# version" at the bottom before changing any of these.
#
# Usage: run this ONCE on the MiSTer itself (needs network access to
# piwheels.org and snapshot.debian.org), before install.sh:
#
#   bash build_pylibs.sh

set -e

HERE="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
PYLIBS="${HERE}/pylibs"
VENDOR_LIBS="${HERE}/vendor_libs"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# MiSTer's own curl doesn't use this as its default CA bundle even though
# it's right there and valid (verified: piwheels.org's cert resolves fine
# against it, and plain `curl https://...` fails identically against
# github.com too - not a piwheels-specific cert problem, just curl not
# being pointed at it). Point at it explicitly rather than falling back to
# --insecure; harmless/no-op on a system where curl's own default already
# works (this file just won't exist there).
CURL_OPTS=(--retry 3 --retry-delay 2)
[ -f /etc/ssl/certs/cacert.pem ] && CURL_OPTS+=(--cacert /etc/ssl/certs/cacert.pem)
curl() { command curl "${CURL_OPTS[@]}" "$@"; }

echo "mister_turing_client — pylibs/vendor_libs builder"
echo "==================================================="
echo

if [ -d "$PYLIBS" ] || [ -d "$VENDOR_LIBS" ]; then
    echo "${PYLIBS} and/or ${VENDOR_LIBS} already exist."
    echo "Remove them first if you want to rebuild from scratch:"
    echo "  rm -rf '${PYLIBS}' '${VENDOR_LIBS}'"
    exit 0
fi

mkdir -p "$PYLIBS" "$VENDOR_LIBS"

# ===== pylibs/ - pure unzip, no pip needed (MiSTer has neither pip nor a
# compiler, but these are already-built wheels - just archives) =====

echo "Fetching Pillow..."
curl -sL -o "${WORK}/pillow.whl" \
    "https://www.piwheels.org/simple/pillow/pillow-11.2.1-cp39-cp39-linux_armv7l.whl"
unzip -q "${WORK}/pillow.whl" 'PIL/*' -d "$PYLIBS"

echo "Fetching numpy..."
curl -sL -o "${WORK}/numpy.whl" \
    "https://www.piwheels.org/simple/numpy/numpy-2.0.2-cp39-cp39-linux_armv7l.whl"
unzip -q "${WORK}/numpy.whl" 'numpy/*' -d "$PYLIBS"

echo "Fetching pyserial..."
curl -sL -o "${WORK}/pyserial.whl" \
    "https://files.pythonhosted.org/packages/07/bc/587a445451b253b285629263eb51c2d8e9bcea4fc97826266d186f96f558/pyserial-3.5-py2.py3-none-any.whl"
unzip -q "${WORK}/pyserial.whl" 'serial/*' -d "$PYLIBS"

# ===== vendor_libs/ - .so files extracted from Debian bullseye armhf
# .debs, fetched from snapshot.debian.org by content hash (a permanent
# archive - unlike deb.debian.org/security.debian.org, which drop old
# point-release versions once superseded, so this is the reliable source
# for exactly these pinned versions going forward). Each entry:
# "package version hash" - the hash is what's actually fetched; version
# is recorded alongside purely for anyone auditing/bumping this later. =====

DEBS='
libjpeg62-turbo         1:2.0.6-4                    acb1e07fdab147b4a45cfda33a128e09b7134f42
libopenjp2-7            2.4.0-3+deb11u3              def26c67f5fa3e4f7b067c0838dad5d009ddb208
libxcb1                 1.14-3                       2180a9349d28e722b19bbd11b1ebdb436fb83d53
libxau6                 1:1.0.9-1                    b5de33a6ec293bc134277751c11a4588335c9b9d
libxdmcp6               1:1.1.2-3                    77874cb05ce0f96fff894a838d05728948b208bb
libbsd0                 0.11.3-1+deb11u1             4ba216b5aebe7d5fe89f17972249a94544f1a4d1
libmd0                  1.0.3-3                      74531b6dd28797f5f98fd264a715aa992f36b66f
libopenblas0-pthread    0.3.13+ds-3+deb11u1          2f8e1ed9f1f56250103910906e5f46ecfeda42cb
'

echo "$DEBS" | while read -r pkg ver hash; do
    [ -z "$pkg" ] && continue
    echo "Fetching ${pkg} ${ver} (armhf)..."
    debdir="${WORK}/${pkg}"
    mkdir -p "$debdir"
    curl -sL -o "${debdir}/pkg.deb" "https://snapshot.debian.org/file/${hash}"
    ( cd "$debdir" && ar x pkg.deb && tar xf data.tar.* )
    # .so files land under usr/lib/arm-linux-gnueabihf/ (both the real
    # versioned file and its unversioned-SONAME symlink) - copy both,
    # preserving the symlink, into vendor_libs/ flat.
    find "$debdir" -name '*.so*' -exec cp -P {} "$VENDOR_LIBS/" \;
done

echo
echo "========================================"
echo "Done."
echo "========================================"
echo
echo "pylibs/:"
ls "$PYLIBS"
echo
echo "vendor_libs/:"
ls "$VENDOR_LIBS"
echo
echo "Next: bash install.sh"

# ===== Bumping a pinned version =====
#
# Before trusting a newer wheel or .deb:
#   1. Its glibc floor must be <= MiSTer's own (`ldd --version` on-device,
#      2.31 as of this writing):
#      objdump -T <file>.so | grep -oE 'GLIBC_[0-9]+\.[0-9]+' | sort -V | tail -1
#      (objdump isn't on MiSTer itself - run this check on a Linux box with
#      binutils, against the downloaded file, before deploying it.)
#   2. Its NEEDED entries must all resolve against either MiSTer's own
#      /usr/lib or vendor_libs/:
#      objdump -p <file>.so | grep NEEDED
#   3. For a new Debian package version: look it up at
#      https://snapshot.debian.org/package/<name>/ for the version + hash,
#      matching the pattern above (mr/binary/<pkg>/<version>/binfiles
#      returns per-architecture hashes; the file itself is at
#      https://snapshot.debian.org/file/<hash>).
