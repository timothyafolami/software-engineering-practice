#!/bin/sh
# Layer 2 · Topic 7 - the socket-state report, run inside a LINUX CONTAINER.
#
# `ss` shows sockets as STATE; tcpdump shows them as EVENTS. This file is the
# first half. None of it works on macOS, which has no `ss` -- `lsof -i` and
# `netstat -an` [host] are the nearest equivalents and they do not report rtt,
# cwnd or retransmits at all, which is most of the value.
#
# Usage, from 02-network/lab:
#   docker compose cp ../07-see-it-on-the-wire/sniff/sockets.sh api:/sockets.sh
#   docker compose exec api sh /sockets.sh
set -eu

PORT="${PORT:-8000}"

echo "listeners and accept backlog"
ss -tln
echo

echo "established connections, total"
ss -tan state established | tail -n +2 | wc -l
echo

echo "established to :${PORT}"
ss -tan state established "( dport = :${PORT} or sport = :${PORT} )" | tail -n +2 | wc -l
echo

echo "TIME-WAIT sockets   <- churn. If this is large, pooling is not working."
ss -tan state time-wait | tail -n +2 | wc -l
echo

echo "per-socket transport detail (rtt, cwnd, retrans, congestion algorithm)"
echo "  This is Topic 6's entire mechanism, live, with no packet capture:"
echo "    rtt rising, retrans 0        -> the far end got slower"
echo "    retrans rising, cwnd falling -> the PATH got lossy, and a loss-based"
echo "                                    controller is halving your window"
ss -ti state established | head -40
