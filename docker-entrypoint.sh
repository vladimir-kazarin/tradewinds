#!/bin/sh
# Fly volumes mount root-owned: fix ownership, then run the app as the unprivileged user.
set -e
if [ "$(id -u)" = "0" ]; then
  mkdir -p /data/runs
  chown -R app:app /data
  exec setpriv --reuid=app --regid=app --init-groups "$@"
fi
exec "$@"
