#!/usr/bin/env python3
"""Regenerate containers-generated.yml from live Proxmox state and deliver it to ansible-host.
Runs as root on node1, triggered by proxmox-inventory-sync.path (or manually for the first run)."""
import json, os, subprocess, sys, tempfile; from pathlib import Path
import yaml

CACHE_PATH = Path("/var/lib/proxmox-inventory-sync/last-known-good.yml")
TELEGRAM_ENV_PATH = Path("/etc/proxmox-inventory-sync/telegram.env")
ANSIBLE_HOST = "ansible-host"
REMOTE_USER = "nihar"
REMOTE_KEY = "/root/.ssh/proxmox-inventory-sync_ed25519"


def parse_net0_ip(net0):
    """Extract the static IPv4 from a Proxmox net0 config string, or None if it's DHCP/unparseable."""
    if not net0:
        return None
    fields = dict(kv.split("=", 1) for kv in net0.split(",") if "=" in kv)
    ip_cidr = fields.get("ip")
    if not ip_cidr or ip_cidr.lower() == "dhcp":
        return None
    return ip_cidr.split("/")[0]


def build_inventory(raw_containers):
    """Turn pvesh's raw LXC entries into (hosts_dict, skipped, warnings). Raises ValueError on duplicate vmid."""
    hosts, seen_vmids, skipped, warnings = {}, {}, [], []
    for c in raw_containers:
        name, vmid, node, net0 = c["name"], c["vmid"], c["node"], c.get("net0")
        if vmid in seen_vmids:
            raise ValueError(f"duplicate vmid {vmid}: {seen_vmids[vmid]!r} and {name!r}")
        seen_vmids[vmid] = name
        ip = parse_net0_ip(net0)
        if ip is None:
            skipped.append(name)
            warnings.append(f"{name} (vmid {vmid}): no static IP in net0, skipped")
            continue
        hosts[name] = {"ansible_host": ip, "vmid": vmid, "node": node}
    return hosts, skipped, warnings


def render_yaml(hosts):
    """Render the hosts dict as containers-generated.yml content, sorted for stable diffs."""
    data = {"containers": {"hosts": {name: hosts[name] for name in sorted(hosts)}}}
    return yaml.safe_dump(data, default_flow_style=False, sort_keys=True)


def validate_result(new_count, last_known_good_count):
    """Raise ValueError if the new result looks like a suspicious wipe (Proxmox API hiccup, empty response)."""
    if last_known_good_count > 0 and new_count == 0:
        raise ValueError(f"regen produced 0 hosts but last known-good had {last_known_good_count} — refusing to overwrite")
    if last_known_good_count >= 5 and new_count < last_known_good_count / 2:
        raise ValueError(
            f"regen produced {new_count} hosts, less than half of last known-good's {last_known_good_count} — "
            "refusing to overwrite, looks like a partial API response"
        )
