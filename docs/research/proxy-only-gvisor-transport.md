# Proxy-only transport for gVisor analyzer jobs

Research for [Find enforceable proxy-only transport for gVisor jobs](https://github.com/Mitravasu/playgrounds/issues/9), 2026-09-22. This investigates transport for the policy in [Define the shared sandbox isolation contract](https://github.com/Mitravasu/playgrounds/issues/4); it is not an implementation.

## Finding

Use **one Docker `internal` bridge per public analyzer job with IPv4 gateway mode `isolated`**, attach only that job and its short-lived trusted proxy, and attach the proxy separately to a normal egress network. Keep the analyzer under `playgrounds-runsc`; the proxy is the only connected peer. Docker Engine 28 introduced `isolated` gateway mode specifically to remove the host-side bridge address that ordinary `internal` bridges retain. The mode is accepted only with `internal`. [Docker Engine 28 release notes](https://docs.docker.com/engine/release-notes/28/#networking).

Docker's bridge model allows containers on one bridge to communicate while isolating them from containers on other bridges. An `internal` bridge lacks external routing; the `isolated` mode additionally removes the bridge's host address. These are properties of Docker Engine running on Linux, so the same network setup API is available to the Docker daemon inside Colima and to a supported Linux Docker Engine. This is an inference about portability from one shared daemon implementation, **not** a Linux workload test. [Docker bridge driver](https://docs.docker.com/engine/network/drivers/bridge/), [Docker Engine 28 release notes](https://docs.docker.com/engine/release-notes/28/#networking).

Create the network with the equivalent of:

```sh
docker network create --driver bridge --internal \
  --opt com.docker.network.bridge.gateway_mode_ipv4=isolated \
  --opt com.docker.network.bridge.gateway_mode_ipv6=isolated \
  <unique-job-network>
```

IPv6 should remain disabled unless it has its own verified policy. The orchestrator should reject a daemon older than 28 or a network whose inspected driver, `Internal`, gateway mode, IP family, or attached endpoints differ from the expected values. Do not fall back to a plain `internal` bridge. The proxy must be ready before starting the analyzer; derive its address from the proxy's endpoint on this job's bridge rather than a fixed subnet. The analyzer's existing proxy configuration can then point to that address. The proxy's separate egress attachment must never be shared with the analyzer. On any failure or timeout, remove the analyzer, proxy, and job bridge; label resources so an interrupted run can be reconciled later. Per-job networks consume Docker address pools, so pool exhaustion must be a clear setup/job error and cleanup must be reliable.

## Disposable Colima/gVisor probe

On this Mac's Colima Docker Engine 29.5.2 with `playgrounds-runsc`, a disposable `internal` bridge with both gateway-mode options set to `isolated` reported `Internal=true`, `EnableIPv6=false`, IPv4 subnet `172.18.0.0/16`, and no gateway in its IPAM config or endpoint inspect. `colima ssh -- ip -4 addr show dev <bridge>` returned no address. A trusted proxy on that bridge had `172.18.0.1`; its separate public bridge had `172.19.0.2` and gateway `172.19.0.1`. The analyzer joined only the isolated bridge.

From a disposable gVisor analyzer container, TCP `connect_ex` returned:

| Destination | Result |
| --- | --- |
| Own proxy `172.18.0.1:8080` | `0` (connected) |
| Another job's isolated endpoint `172.20.0.1:8080` | `101` (`ENETUNREACH`) |
| Proxy public-side gateway `172.19.0.1:80` | `101` |
| Public `1.1.1.1:443` | `101` |
| Private `192.168.5.1:443` | `101` |

An HTTP `CONNECT www.mitravasu.com:443` from that gVisor container through the real proxy returned `200 Connection Established`. The full repository public analyzer then succeeded for `https://www.mitravasu.com/`, returning all three declared artifacts: screenshot (605,257 bytes), page metadata (97 bytes), and observations (280,696 bytes). The harness changed only the in-memory public profile's network name, proxy IP, and known analyzer PID limit from 64 to 128; no repository implementation was edited. The analyzer itself still used gVisor and the repository's normal runner restrictions. This validates one ARM64 Colima VM, not Linux or Intel macOS, and it is a connectivity probe rather than a formal adversarial proof. All disposable containers and networks were removed afterward.

## Other transports considered

- **Ordinary `internal` bridge:** insufficient. Docker documents that its bridge normally has a host address, so containers can reach host services on that address. The earlier Colima probe in [Design controlled analyzer egress across hosts](https://github.com/Mitravasu/playgrounds/issues/6#issuecomment-5787122112) observed a TCP response from that gateway. [Docker Engine 28 release notes](https://docs.docker.com/engine/release-notes/28/#networking).
- **Host firewall allowlist:** possible in principle but more host-specific and easier to misapply. Docker's `DOCKER-USER` chain processes forwarded packets, whereas traffic addressed to the bridge's host IP is host-directed and needs an `INPUT` policy as well. Docker can use iptables or nftables, and its docs warn against changing Docker-managed rules. A per-job firewall design would need privileged daemon-host operations in Linux or inside Colima, atomic installation before attachment, rule cleanup, IPv4/IPv6 handling, and a separate preflight. The isolated gateway mode removes the relevant host IP without these moving parts. [Docker iptables chains](https://docs.docker.com/engine/network/firewall-iptables/), [Docker packet filtering](https://docs.docker.com/engine/network/packet-filtering-firewalls/).
- **Unix socket plus `network_mode=none`:** attractive in concept, but the existing proxy is an HTTP TCP proxy and would need a Unix-socket listener or relay. gVisor's first-party examples explicitly enable `--host-uds=all` for cross-boundary Unix socket access. That is a runtime-wide capability change needing careful mount and security review, and a Colima VM socket cannot simply be addressed by the macOS Docker client path. This route was not prototyped after the Docker-native bridge passed. [gVisor observability guide](https://gvisor.dev/docs/user_guide/observability/), [gVisor multi-agent example](https://gvisor.dev/blog/2026/04/15/magi-multi-agent-gvisor-isolation/).

## Remaining acceptance work

Implement the per-job network and proxy lifecycle, including deterministic cleanup after crashes. Verify the same topology on a supported Linux Docker Engine; run adversarial checks for host bridge addresses, other jobs, DNS, IPv6, direct public/private targets, and proxy policy. Confirm the analyzer never receives an egress network attachment and fails closed if network creation or inspection differs from the expected mode. Run full workload and cleanup tests at concurrency. The existing 64-PID analyzer limit remains a separate measured workload gap. [Colima validation](https://github.com/Mitravasu/playgrounds/issues/8#issuecomment-5786296868).
