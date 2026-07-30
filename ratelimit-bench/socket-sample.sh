#!/usr/bin/env bash
# Sample the client socket table while a benchmark runs.
#
# The gateway is a single IP:port, so every in-flight request needs a distinct
# local ephemeral port and every closed one holds that port in TIME_WAIT for
# 2*MSL. That budget (net.inet.ip.portrange.first..last) is what runs out first
# on a laptop driving thousands of concurrent requests, and it fails as
# ConnectError rather than a timeout, so it is easy to misread as a server fault.
#
# Usage: ./socket-sample.sh <peer-ip> <seconds> [interval]

set -euo pipefail

PEER="${1:?peer ip required}"
DURATION="${2:-120}"
INTERVAL="${3:-3}"

FIRST=$(sysctl -n net.inet.ip.portrange.first)
LAST=$(sysctl -n net.inet.ip.portrange.last)
BUDGET=$((LAST - FIRST + 1))
MSL=$(sysctl -n net.inet.tcp.msl)

echo "ephemeral ports ${FIRST}-${LAST} (${BUDGET} total), msl ${MSL}ms -> TIME_WAIT $((2 * MSL / 1000))s"
printf '%-9s %10s %10s %10s %10s %8s\n' elapsed established time_wait syn_sent other used_pct

START=$(date +%s)
while :; do
    NOW=$(date +%s)
    ELAPSED=$((NOW - START))
    (( ELAPSED > DURATION )) && break

    netstat -an -p tcp 2>/dev/null | awk -v peer="${PEER}.443" -v budget="$BUDGET" -v elapsed="$ELAPSED" '
        $5 == peer || index($5, peer) {
            state = $6
            if (state == "ESTABLISHED") est++
            else if (state == "TIME_WAIT") tw++
            else if (state == "SYN_SENT") syn++
            else other++
        }
        END {
            total = est + tw + syn + other
            printf "%-9s %10d %10d %10d %10d %7.1f%%\n", elapsed "s", est, tw, syn, other, total * 100 / budget
        }'
    sleep "$INTERVAL"
done
