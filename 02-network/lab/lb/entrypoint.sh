#!/bin/sh
# Pick the nginx config for the profile, then hand over to the image's own
# entrypoint. Also replace the image's access.log -> /dev/stdout symlink with
# a real file, because Topic 4 counts 502s with
#   docker compose exec lb sh -c "grep ' 502 ' /var/log/nginx/access.log | wc -l"
# and you cannot grep a pipe.
set -e

PROFILE="${KEEPALIVE_PROFILE:-mismatched}"
SRC="/etc/nginx/profiles/${PROFILE}.conf"
[ -f "$SRC" ] || { echo "no nginx profile for KEEPALIVE_PROFILE=$PROFILE" >&2; exit 64; }

cp "$SRC" /etc/nginx/conf.d/default.conf
rm -f /var/log/nginx/access.log /var/log/nginx/error.log
touch /var/log/nginx/access.log /var/log/nginx/error.log

echo "lb starting with profile ${PROFILE}"
grep -E 'keepalive' "$SRC" | sed 's/^/  /'

exec nginx -g 'daemon off;'
