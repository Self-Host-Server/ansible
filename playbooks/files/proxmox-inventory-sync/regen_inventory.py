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


def fetch_container_net0(node, vmid):
    """Fetch one container's net0 string from its per-container config (cluster/resources doesn't include it)."""
    out = subprocess.run(
        ["pvesh", "get", f"/nodes/{node}/lxc/{vmid}/config", "--output-format", "json"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout).get("net0")


def fetch_raw_containers():
    """Call pvesh locally (root socket access, no API token needed) and return the raw LXC entries,
    each enriched with net0 from its own config (the cluster-wide resource list omits it)."""
    out = subprocess.run(
        ["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
        capture_output=True, text=True, check=True,
    )
    resources = json.loads(out.stdout)
    lxcs = [r for r in resources if r.get("type") == "lxc"]
    for c in lxcs:
        c["net0"] = fetch_container_net0(c["node"], c["vmid"])
    return lxcs


def last_known_good_count():
    """Host count from node1's local cache of the last successfully-delivered inventory, or 0 if none exists yet."""
    if not CACHE_PATH.exists():
        return 0
    data = yaml.safe_load(CACHE_PATH.read_text())
    return len(data.get("containers", {}).get("hosts", {})) if data else 0


def write_cache(text):
    """Atomically update node1's local last-known-good cache after a successful regen."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE_PATH.parent)
    os.write(fd, text.encode())
    os.close(fd)
    os.replace(tmp, CACHE_PATH)


def deliver(text):
    """Deliver plaintext YAML to ansible-host's staging path via the restricted key, atomically on the receiving end."""
    fd, tmp = tempfile.mkstemp()
    os.write(fd, text.encode())
    os.close(fd)
    try:
        subprocess.run(
            ["scp", "-O", "-i", REMOTE_KEY, "-o", "BatchMode=yes", tmp,
             f"{REMOTE_USER}@{ANSIBLE_HOST}:containers-generated.yml.plain"],
            check=True, capture_output=True, text=True,
        )
    finally:
        os.unlink(tmp)


def notify(text):
    """Best-effort Telegram notification using credentials deployed to this host by inventory-sync-bootstrap.yml."""
    if not TELEGRAM_ENV_PATH.exists():
        return
    env = dict(line.split("=", 1) for line in TELEGRAM_ENV_PATH.read_text().splitlines() if "=" in line)
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    subprocess.run(
        ["curl", "-s", "-X", "POST", f"https://api.telegram.org/bot{token}/sendMessage",
         "--data-urlencode", f"chat_id={chat_id}", "--data-urlencode", f"text={text}"],
        capture_output=True,
    )


def main():
    try:
        raw = fetch_raw_containers()
        hosts, skipped, warnings = build_inventory(raw)
        validate_result(len(hosts), last_known_good_count())
    except Exception as e:
        notify(f"proxmox-inventory-sync FAILED on node1: {e}")
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)

    text = render_yaml(hosts)
    write_cache(text)

    try:
        deliver(text)
    except subprocess.CalledProcessError as e:
        notify(f"proxmox-inventory-sync: regen OK ({len(hosts)} hosts) but delivery to ansible-host FAILED: {e}")
        sys.exit(1)

    msg = f"proxmox-inventory-sync: {len(hosts)} hosts delivered"
    if skipped:
        msg += f" ({len(skipped)} skipped: {', '.join(skipped)})"
    notify(msg)
    for w in warnings:
        print(w, file=sys.stderr)


if __name__ == "__main__":
    main()
