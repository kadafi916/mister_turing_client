#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# mister_turing_client startup script - mirrors MiSTer_monitor's own
# MiSTer/Scripts/start_monitor.sh pattern (proven working on this device),
# with one addition: a respawn loop, since a USB replug/hard-exit
# (see README.md "A mister_status_server.py bug this surfaced" and the
# session notes on os._exit() after 10 failed reconnect attempts) should
# come back on its own rather than need a manual SSH restart.

SCRIPT_DIR="/media/fat/Scripts/.config/mister_monitor/turing_client"
PID_FILE="/tmp/mister_turing_client.pid"
LOG_FILE="/tmp/mister_turing_client.log"

start_client() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "The client is already running (supervisor PID: $PID)"
            return 0
        fi
    fi

    echo "Starting MiSTer Turing Client..."
    cd "$SCRIPT_DIR"
    # -u: unbuffered stdout, so a hard exit (os._exit(), which skips the
    # normal flush) doesn't lose the last few log lines - exactly what made
    # an earlier crash impossible to diagnose from the log alone.
    # The while-loop respawns on any exit (clean or crashed) after a short
    # pause, so a USB dropout that outlasts the client's own 10-attempt
    # reconnect window recovers on its own instead of staying dead.
    nohup bash -c 'while true; do python3 -u mister_turing_client.py --port AUTO; sleep 2; done' \
        > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Client started (supervisor PID: $(cat "$PID_FILE"))"
}

stop_client() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            # Kill the supervisor loop FIRST so it stops respawning, then
            # clean up whichever python instance it currently has running -
            # busybox has no pkill, so match by command line like the rest
            # of this session's SSH work has been doing.
            kill "$PID" 2>/dev/null
            rm -f "$PID_FILE"
            ps aux | grep 'mister_turing_client.py' | grep -v grep | awk '{print $1}' | xargs -r kill
            echo "Client stopped"
        else
            rm -f "$PID_FILE"
            ps aux | grep 'mister_turing_client.py' | grep -v grep | awk '{print $1}' | xargs -r kill
            echo "Supervisor was not running (cleaned up any orphaned client process)"
        fi
    else
        echo "PID file not found"
        ps aux | grep 'mister_turing_client.py' | grep -v grep | awk '{print $1}' | xargs -r kill
    fi
}

case "$1" in
    start)
        start_client
        ;;
    stop)
        stop_client
        ;;
    restart)
        stop_client
        sleep 2
        start_client
        ;;
    status)
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "Supervisor running (PID: $(cat "$PID_FILE"))"
            ps aux | grep 'mister_turing_client.py' | grep -v grep || echo "  (no client process currently active - respawning?)"
        else
            echo "Client is not running"
        fi
        ;;
    *)
        echo "Use: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
