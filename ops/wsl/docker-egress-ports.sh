#!/usr/bin/env bash
# Restore the default Docker bridge TCP port mapping after WSL starts.
set -euo pipefail

read -r first last < /proc/sys/net/ipv4/ip_local_port_range
[[ $first =~ ^[0-9]+$ && $last =~ ^[0-9]+$ ]]
(( first > 0 && first < last && last <= 65535 ))
subnet=$(docker network inspect bridge --format '{{range .IPAM.Config}}{{println .Subnet}}{{end}}' | awk '/^[0-9]+\./ {print; exit}')
[[ $subnet =~ ^[0-9.]+/[0-9]+$ ]]
interface=$(ip -4 route get 1.1.1.1 | awk '{for (i=1;i<NF;i++) if ($i=="dev") {print $(i+1); exit}}')
[[ -n $interface ]]

rule=(-s "$subnet" -o "$interface" -p tcp
      -m comment --comment previewmesh-wsl-ports
      -j MASQUERADE --to-ports "$first-$last")
if ! iptables -w 5 -t nat -C POSTROUTING "${rule[@]}"; then
    iptables -w 5 -t nat -I POSTROUTING 1 "${rule[@]}"
fi
printf 'Docker TCP egress: %s via %s, source ports %s-%s\n' "$subnet" "$interface" "$first" "$last"
