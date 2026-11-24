"""Egress rules of the sidecars, one copy for both (iptables commands for the sidecar's own uid, applied at start as root with NET_ADMIN, then dropped).
`public_rules`: the blob-sidecar's (squid), public internet only. `pinned_rules`: the OCR sidecar's, only the configured endpoints, so a LAN-local
profile is possible. Resolved at start; an endpoint whose address changes needs a restart. Rules act at connect time, so DNS rebinding cannot get
around them. Pure functions: tests compare the command lists. (The browser backends keep their own copy of the public rules and may drift.)"""

from urllib.parse import urlparse

PRIVATE = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10", "127.0.0.0/8")


def _base(uid: int) -> tuple[list, list]:
    own = ["-m", "owner", "--uid-owner", str(uid)]
    cmds = [[t, "-F", "OUTPUT"] for t in ("iptables", "ip6tables")]
    cmds += [[t, "-A", "OUTPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"] for t in ("iptables", "ip6tables")]
    return own, cmds


def _accept(own: list, ip: str, port: int) -> list:
    return ["iptables", "-A", "OUTPUT", *own, "-d", ip, "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"]


def public_rules(uid: int, resolvers: list[str], allow: list[tuple[str, int]]) -> list[list[str]]:
    """Public internet only. `allow` (PARSE_PROXY_ALLOW, empty in production) are (ip, port) pairs accepted *before* the rejects: a dev store,
    exactly that address and port and not its whole private range."""
    own, cmds = _base(uid)
    # squid's own helper channel is UDP on loopback (v4 and v6); the container runs nothing else there, so a request for 127.x only reaches squid itself
    cmds.append(["iptables", "-A", "OUTPUT", *own, "-o", "lo", "-j", "ACCEPT"])
    for n in resolvers:  # the configured resolvers (private IPs on some hosts) stay reachable
        for proto in ("udp", "tcp"):
            cmds.append(["iptables", "-A", "OUTPUT", *own, "-d", n, "-p", proto, "--dport", "53", "-j", "ACCEPT"])
    cmds += [_accept(own, ip, port) for ip, port in allow]
    # public internet only; no RFC1918, link-local (the metadata server), CGNAT, loopback, no other stack service
    cmds += [["iptables", "-A", "OUTPUT", *own, "-d", net, "-j", "REJECT"] for net in PRIVATE]
    cmds.append(["iptables", "-A", "OUTPUT", *own, "-j", "ACCEPT"])
    cmds.insert(cmds.index(["iptables", "-A", "OUTPUT", *own, "-o", "lo", "-j", "ACCEPT"]) + 1, ["ip6tables", "-A", "OUTPUT", *own, "-o", "lo", "-j", "ACCEPT"])
    cmds.append(["ip6tables", "-A", "OUTPUT", *own, "-j", "REJECT"])  # ponytail: no IPv6 on the stack networks
    return cmds


def pinned_rules(uid: int, resolvers: list[str], endpoints: list[tuple[str, int]]) -> list[list[str]]:
    """Only `endpoints` (and the resolvers); everything else is rejected."""
    own, cmds = _base(uid)
    for n in resolvers:
        if n == "127.0.0.11":  # Docker's embedded DNS: NAT rewrites the port before this table sees the packet, so any port
            cmds.append(["iptables", "-A", "OUTPUT", *own, "-d", n, "-j", "ACCEPT"])
            continue
        for proto in ("udp", "tcp"):
            cmds.append(["iptables", "-A", "OUTPUT", *own, "-d", n, "-p", proto, "--dport", "53", "-j", "ACCEPT"])
    cmds += [_accept(own, ip, port) for ip, port in endpoints]
    cmds.append(["iptables", "-A", "OUTPUT", *own, "-j", "REJECT"])
    cmds.append(["ip6tables", "-A", "OUTPUT", *own, "-j", "REJECT"])
    return cmds


def endpoints(profiles: dict, resolve) -> list[tuple[str, int]]:
    """(ip, port) of every profile's base_url, plus the `token_url` of `oauth2` profiles and the metadata server for `google` profiles; `resolve(host) -> ip`."""
    out = []
    for p in profiles.values():
        u = urlparse(p["base_url"])
        out.append((resolve(u.hostname), u.port or (443 if u.scheme == "https" else 80)))
        if p.get("auth") == "oauth2":  # the token host too
            t = urlparse(p["token_url"])
            out.append((resolve(t.hostname), t.port or (443 if t.scheme == "https" else 80)))
        if p.get("auth") == "google":
            out.append(("169.254.169.254", 80))
    return list(dict.fromkeys(out))
