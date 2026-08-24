# SPDX-License-Identifier: GPL-3.0-or-later
#
# Lightweight stand-in for turing-smart-screen-python's library/log.py.
# The upstream version installs a RotatingFileHandler writing "log.log"
# into the current working directory and calls locale.setlocale() as an
# import-time side effect - both awkward for a script meant to be run
# from anywhere (e.g. MiSTer's Scripts/ launcher). This just logs to
# stderr.

import logging

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%x %X",
)

logger = logging.getLogger("mister_turing")
logger.setLevel(logging.INFO)
