#!/bin/sh
# Layer 2 · Topic 7 - the captures, run inside the `sniff` sidecar.
#
# Everything in this file must run in a LINUX CONTAINER. macOS has no `ss`, and
# you cannot usefully tcpdump container traffic from the Mac: Docker Desktop
# runs containers inside a Linux VM, there is no veth on your host to attach
# to, and every `nsenter --net=/proc/<pid>/ns/net` recipe in the Linux blogs
# fails because that PID lives in the VM. The sidecar shares `api`'s network
# namespace (`network_mode: "service:api"`), which is the one pattern that
# works identically on macOS and in Linux CI.
#
# Usage, from 02-network/lab:
#   docker compose cp ../07-see-it-on-the-wire/sniff/capture.sh sniff:/capture.sh
#   docker compose exec sniff sh /capture.sh syns 60
#   docker compose exec sniff sh /capture.sh pcap 60
#   docker compose exec sniff sh /capture.sh dns
#   docker compose exec sniff sh /capture.sh fins 60
set -eu

MODE="${1:-syns}"
SECONDS_TO_RUN="${2:-60}"
PORT="${PORT:-8000}"

case "$MODE" in
  syns)
    # Connection INITIATIONS only: SYN set, ACK clear. This is the number you
    # want -- a plain "tcp-syn != 0" filter also matches every SYN/ACK and
    # doubles your count.
    echo "counting SYNs (no ACK) for ${SECONDS_TO_RUN}s..."
    tcpdump -i any -nn -q -l \
      'tcp[tcpflags] & tcp-syn != 0 and not tcp[tcpflags] & tcp-ack != 0' \
      > /tmp/syns.txt 2>/tmp/syns.err &
    pid=$!
    sleep "$SECONDS_TO_RUN"
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    n=$(wc -l < /tmp/syns.txt)
    echo "$n packets captured over ${SECONDS_TO_RUN} s  ->  $(awk "BEGIN{printf \"%.2f\", $n/$SECONDS_TO_RUN}") SYNs/s"
    echo "(divide by your request rate. Near 1 means you are not pooling at all.)"
    ;;

  pcap)
    # Full capture to the shared volume, readable from the host at lab/caps/
    # and openable in Wireshark [host].
    echo "capturing port ${PORT} to /caps/pool.pcap for ${SECONDS_TO_RUN}s..."
    tcpdump -i any -nn -w /caps/pool.pcap "port ${PORT}" &
    pid=$!
    sleep "$SECONDS_TO_RUN"
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    ls -lh /caps/pool.pcap
    ;;

  fins)
    # Topic 4's 502: a FIN from the backend, then a request on the SAME
    # four-tuple, then an RST. Match the four-tuple before concluding anything
    # -- an RST with no preceding FIN on the same tuple is a different bug.
    echo "capturing FIN/RST on port ${PORT} for ${SECONDS_TO_RUN}s..."
    tcpdump -i any -nn -tttt \
      "tcp port ${PORT} and (tcp[tcpflags] & (tcp-fin|tcp-rst) != 0)" \
      -w /caps/topic4.pcap &
    pid=$!
    sleep "$SECONDS_TO_RUN"
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    echo "written to /caps/topic4.pcap -- read it back with:"
    echo "  tcpdump -r /caps/topic4.pcap -nn -tttt | head -40"
    ;;

  dns)
    # Topic 5's ndots arithmetic, counted rather than believed.
    echo "resolv.conf as this container sees it:"
    sed 's/^/  | /' /etc/resolv.conf
    echo
    echo "40 DNS packets, with and without a trailing dot:"
    tcpdump -n -i any port 53 -c 40 &
    pid=$!
    sleep 1
    ( nslookup api.stripe.com  >/dev/null 2>&1 || true )
    ( nslookup api.stripe.com. >/dev/null 2>&1 || true )
    wait "$pid" 2>/dev/null || true
    echo "count the queries per name and compare with the derivation in Topic 5."
    ;;

  *)
    echo "unknown mode: $MODE (expected syns, pcap, fins or dns)" >&2
    exit 64
    ;;
esac
