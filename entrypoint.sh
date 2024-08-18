#!/bin/bash

# Default interface
IFACE="eth0"

# Default bandwidth limit (10 MB/s = 80 mbit/s)
LIMIT=${BANDWIDTH_LIMIT:-80mbit}

# Clear any existing qdisc on the interface
tc qdisc del dev $IFACE root || true
tc qdisc del dev $IFACE ingress || true

# Apply egress (outgoing) bandwidth limit
tc qdisc add dev $IFACE root tbf rate $LIMIT burst 32kbit latency 400ms

# Apply ingress (incoming) bandwidth limit using IFB (Intermediate Functional Block)
modprobe ifb
ip link add ifb0 type ifb
ip link set ifb0 up

tc qdisc add dev $IFACE handle ffff: ingress
tc filter add dev $IFACE parent ffff: protocol ip u32 match u32 0 0 action mirred egress redirect dev ifb0

tc qdisc add dev ifb0 root tbf rate $LIMIT burst 32kbit latency 400ms

# Run the main application
exec "$@"