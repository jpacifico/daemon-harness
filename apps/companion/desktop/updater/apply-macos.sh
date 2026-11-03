#!/bin/sh
# Runs detached once Daemons Companion has quit: the verified release's own
# install script replaces the app, then the app reopens, new or restored.
# Usage: apply-macos.sh <app pid> <install script> <package> <destination> <log>
set -u
[ "$#" -eq 5 ] || exit 2
pid=$1 installer=$2 package=$3 destination=$4 log=$5
waited=0
while /bin/kill -0 "$pid" 2>/dev/null; do
    waited=$((waited + 1))
    # The app did not quit: leave it untouched.
    [ "$waited" -lt 600 ] || exit 1
    /bin/sleep 0.1
done
/bin/sh "$installer" --package "$package" --destination "$destination" --no-launch >"$log" 2>&1
printf 'exit=%s\n' "$?" >>"$log"
/bin/rm -f "$package"
/usr/bin/open "$destination/Daemons Companion.app"
