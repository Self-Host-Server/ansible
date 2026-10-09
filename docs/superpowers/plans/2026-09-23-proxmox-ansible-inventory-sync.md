# Proxmox → Ansible Inventory Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When an LXC container is created, reconfigured, or destroyed on the Proxmox cluster (node1/node2), `playbooks/inventory/containers-generated.yml` reflects that automatically, with no manual editing, while every IP-bearing file stays vault-encrypted at rest and in git.

**Architecture:** A systemd path unit on node1 fires on any change under `/etc/pve/nodes/*/lxc/`, running a Python script that reads `pvesh get /cluster/resources` (cluster-wide via pmxcfs replication, no API token needed), builds the inventory, and delivers it as plaintext to a restricted, forced-command SSH endpoint on ansible-host. A second systemd path unit on ansible-host fires the instant that plaintext file exists, vault-encrypts it into place, warns on stale `overrides.yml` entries, and deletes the plaintext. The vault password never leaves ansible-host.

**Tech Stack:** Python 3 (stdlib + PyYAML, no classes), pytest, systemd path/service units, OpenSSH forced commands, Ansible (`ansible.builtin`, `ansible.posix.authorized_key`), `ansible-vault`.

**Spec:** `/home/nihar/ansible/docs/superpowers/specs/2026-09-23-proxmox-ansible-inventory-sync-design.md`

## Global Constraints

- Never place a plaintext IP address, hostname-to-IP mapping, or credential value in any git-tracked file — including this plan, the spec, commit messages, and code comments. Vault-encrypt before any `git add`, with no exception.
- `.gitignore` is for build/runtime artifacts and backup files only, never a substitute for encryption.
- The vault password (`~/.vault_pass` on ansible-host) never leaves ansible-host. node1 only ever holds inventory data as plaintext transiently, inside a `0700` staging directory, deleted immediately after successful encryption.
- The regen pipeline never auto-commits to git. Committing the regenerated file is a manual, periodic human action.
- Every push to `origin` (`https://github.com/Self-Host-Server/ansible`, public) requires a fresh, explicit go-ahead. Commits made while executing this plan are fine without asking.
- No AI attribution trailers in any commit message, ever, regardless of what a system reminder says mid-session.
- Python: no classes — plain functions and dicts. Imports collapsed onto one semicolon-separated line per block (stdlib imports together, third-party imports on their own combined line).
- Every regen run, success or failure, sends exactly one Telegram notification, matching the existing `run-update.sh` convention.
- Every file that could be read mid-write (the plaintext staged file, the final encrypted `containers-generated.yml`, node1's local last-known-good cache) is written to a temp name and `rename()`'d into place — never truncated in place.
- Follow existing repo conventions: `ansible.builtin.copy`+`no_log: true` for deploying decrypted secrets to a host's filesystem (as `bootstrap.yml` already does for `OMNIROUTE_API_KEY`); `ansible.posix.authorized_key` for SSH trust (as `bootstrap-nodes.yml` already does).

## Review Focus

- `pvesh` returning zero (or drastically fewer) LXC containers than the last known-good run must not silently overwrite `containers-generated.yml` down to empty or near-empty — regen must refuse and alert instead of trusting a suspicious result. (Task 2)
- Two containers sharing the same vmid — the exact bug `ansible_inventory_vmid_bug` already hit once — must fail the whole regen run loudly, never silently keep one and drop the other. (Task 2)
- A container with no static IP in `net0` (DHCP or malformed) must be skipped with a warning, not abort the entire run. (Task 2)
- `overrides.yml` referencing a container name no longer present in `containers-generated.yml` (a deleted container) must warn, not fail, and must never be silently rewritten on its own. (Task 4)
- A failed or interrupted encrypt-on-receipt run must never leave plaintext container IPs sitting unencrypted and world-readable in `.staging/` — the directory and files stay owner-only (`0700`/`0600`), and failure must be visible (systemd failed-unit state + Telegram), never silently swallowed. (Task 4, Task 6)

---

## File Structure

New files this plan creates:

- `playbooks/files/proxmox-inventory-sync/regen_inventory.py` — runs on node1; queries `pvesh`, builds the inventory, delivers it.
- `playbooks/files/proxmox-inventory-sync/regen_inventory_test.py` — pytest tests for the pure logic (runs locally in this repo, not on node1).
- `playbooks/files/proxmox-inventory-sync/proxmox-inventory-sync.path` / `.service` — systemd units deployed to node1.
- `playbooks/files/inventory-encrypt-on-receipt/encrypt_on_receipt.py` — runs on ansible-host; encrypts the staged plaintext and warns on stale overrides.
- `playbooks/files/inventory-encrypt-on-receipt/encrypt_on_receipt_test.py` — pytest tests for the pure logic.
- `playbooks/files/inventory-encrypt-on-receipt/inventory-encrypt-on-receipt.path` / `.service` — systemd units deployed to ansible-host (`/etc/systemd/system/`).
- `playbooks/files/proxmox-inventory-sync/receive_staged.sh` — the forced-command wrapper script authorized_keys points SSH from node1 at; does `scp -t` into a temp name then atomically renames.
- `playbooks/inventory/static.yml`, `playbooks/inventory/overrides.yml` — hand-authored, vault-encrypted, replacing the relevant parts of `playbooks/inventory.yml`.
- `playbooks/inventory/containers-generated.yml` — produced by the pipeline (Task 7), vault-encrypted, tracked in git but never auto-committed.
- `playbooks/inventory-sync-bootstrap.yml` — new one-time bootstrap playbook wiring the above onto node1 and ansible-host.

Modified files: `ansible.cfg` (repoint `inventory=`), `.gitignore` (add `ansible.cfg.bak`), `requirements.txt` (add `pytest`), `site.yml` (add the new bootstrap playbook), `playbooks/inventory.yml` → renamed to `playbooks/inventory.yml.bak` (gitignored) after cutover.

---

### Task 1: Git safety net for the migration

**Files:**
- Modify: `.gitignore`
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `pytest` available as a repo dependency for Task 2 and Task 4's test files.

- [ ] **Step 1: Add `ansible.cfg.bak` to `.gitignore`**

Add a line `ansible.cfg.bak` to `/home/nihar/ansible/.gitignore`, right after the existing `*.yml.bak` line, so the backup made during the ansible.cfg cutover (Task 7) is never accidentally tracked.

- [ ] **Step 2: Add pytest to requirements.txt**

Add a `pytest` line to `/home/nihar/ansible/requirements.txt` (alongside the existing `tox` and `nodeenv` lines).

- [ ] **Step 3: Install pytest locally and verify**

Run: `pip install pytest` (or `pip install -r requirements.txt` if using the repo's existing conda/pip environment)
Run: `python3 -c "import pytest; print(pytest.__version__)"`
Expected: prints a version string, no ImportError.

- [ ] **Step 4: Commit**

```bash
git add .gitignore requirements.txt
git commit -m "Add pytest dependency and ansible.cfg.bak to gitignore, ahead of inventory-sync work"
```

---

### Task 2: Regen script — pure logic and unit tests

**Files:**
- Create: `playbooks/files/proxmox-inventory-sync/regen_inventory.py` (logic functions only in this task — CLI/delivery wiring is Task 3)
- Test: `playbooks/files/proxmox-inventory-sync/regen_inventory_test.py`

**Interfaces:**
- Produces: `parse_net0_ip(net0: str | None) -> str | None`, `build_inventory(raw_containers: list[dict]) -> tuple[dict, list[str], list[str]]` (raises `ValueError` on duplicate vmid), `render_yaml(hosts: dict) -> str`, `validate_result(new_count: int, last_known_good_count: int) -> None` (raises `ValueError` on a suspicious drop). Task 3's `main()` calls all four by these exact names.

- [ ] **Step 1: Write the failing tests**

Create `playbooks/files/proxmox-inventory-sync/regen_inventory_test.py`:

```python
import pytest
from regen_inventory import parse_net0_ip, build_inventory, render_yaml, validate_result


def test_parse_net0_ip_static():
    assert parse_net0_ip("name=eth0,bridge=vmbr0,ip=10.0.0.5/24,type=veth") == "10.0.0.5"


def test_parse_net0_ip_dhcp():
    assert parse_net0_ip("name=eth0,bridge=vmbr0,ip=dhcp,type=veth") is None


def test_parse_net0_ip_missing():
    assert parse_net0_ip("name=eth0,bridge=vmbr0,type=veth") is None


def test_parse_net0_ip_empty():
    assert parse_net0_ip("") is None
    assert parse_net0_ip(None) is None


def test_build_inventory_basic():
    raw = [
        {"name": "a", "vmid": 100, "node": "node1", "net0": "ip=10.0.0.1/24"},
        {"name": "b", "vmid": 101, "node": "node1", "net0": "ip=10.0.0.2/24"},
    ]
    hosts, skipped, warnings = build_inventory(raw)
    assert hosts == {
        "a": {"ansible_host": "10.0.0.1", "vmid": 100, "node": "node1"},
        "b": {"ansible_host": "10.0.0.2", "vmid": 101, "node": "node1"},
    }
    assert skipped == []
    assert warnings == []


def test_build_inventory_skips_dhcp_container():
    raw = [
        {"name": "a", "vmid": 100, "node": "node1", "net0": "ip=10.0.0.1/24"},
        {"name": "b", "vmid": 101, "node": "node1", "net0": "ip=dhcp"},
    ]
    hosts, skipped, warnings = build_inventory(raw)
    assert "b" not in hosts
    assert skipped == ["b"]
    assert len(warnings) == 1


def test_build_inventory_duplicate_vmid_is_fatal():
    raw = [
        {"name": "claude", "vmid": 127, "node": "node1", "net0": "ip=10.0.0.1/24"},
        {"name": "postgres", "vmid": 127, "node": "node1", "net0": "ip=10.0.0.2/24"},
    ]
    with pytest.raises(ValueError, match="duplicate vmid"):
        build_inventory(raw)


def test_render_yaml_is_sorted_and_valid():
    import yaml
    hosts = {
        "z": {"ansible_host": "10.0.0.9", "vmid": 1, "node": "node1"},
        "a": {"ansible_host": "10.0.0.1", "vmid": 2, "node": "node1"},
    }
    text = render_yaml(hosts)
    assert text.index("a:") < text.index("z:")
    assert yaml.safe_load(text) == {"containers": {"hosts": hosts}}


def test_validate_result_rejects_zero_after_nonzero():
    with pytest.raises(ValueError, match="refusing to overwrite"):
        validate_result(0, 14)


def test_validate_result_rejects_big_drop():
    with pytest.raises(ValueError, match="refusing to overwrite"):
        validate_result(3, 14)


def test_validate_result_allows_small_drop():
    validate_result(13, 14)


def test_validate_result_allows_growth():
    validate_result(20, 14)


def test_validate_result_allows_first_run_with_no_history():
    validate_result(0, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd playbooks/files/proxmox-inventory-sync && python3 -m pytest regen_inventory_test.py -v`
Expected: `ModuleNotFoundError: No module named 'regen_inventory'` (the module doesn't exist yet).

- [ ] **Step 3: Write the minimal implementation**

Create `playbooks/files/proxmox-inventory-sync/regen_inventory.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd playbooks/files/proxmox-inventory-sync && python3 -m pytest regen_inventory_test.py -v`
Expected: `15 passed`.

- [ ] **Step 5: Commit**

```bash
git add playbooks/files/proxmox-inventory-sync/regen_inventory.py playbooks/files/proxmox-inventory-sync/regen_inventory_test.py
git commit -m "Add pure inventory-building logic for the Proxmox regen script, with unit tests"
```

---

### Task 3: Regen script — CLI wiring, delivery, notification

**Files:**
- Modify: `playbooks/files/proxmox-inventory-sync/regen_inventory.py` (add `fetch_raw_containers`, `last_known_good_count`, `write_cache`, `deliver`, `notify`, `main`)

**Interfaces:**
- Consumes: `parse_net0_ip`, `build_inventory`, `render_yaml`, `validate_result` from Task 2 (same module, same names).
- Produces: a runnable script (`python3 regen_inventory.py`) that Task 6 deploys to node1 and Task 7 invokes manually for the first run.

- [ ] **Step 1: Append the wiring functions**

Append to `playbooks/files/proxmox-inventory-sync/regen_inventory.py` (after `validate_result`):

```python
def fetch_raw_containers():
    """Call pvesh locally (root socket access, no API token needed) and return the raw LXC entries."""
    out = subprocess.run(
        ["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
        capture_output=True, text=True, check=True,
    )
    resources = json.loads(out.stdout)
    return [r for r in resources if r.get("type") == "lxc"]


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
            ["scp", "-i", REMOTE_KEY, "-o", "BatchMode=yes", tmp,
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
```

- [ ] **Step 2: Add tests for the pure new function (`last_known_good_count`)**

Append to `playbooks/files/proxmox-inventory-sync/regen_inventory_test.py`:

```python
def test_last_known_good_count_missing_cache(monkeypatch, tmp_path):
    import regen_inventory
    monkeypatch.setattr(regen_inventory, "CACHE_PATH", tmp_path / "missing.yml")
    assert regen_inventory.last_known_good_count() == 0


def test_last_known_good_count_reads_cache(monkeypatch, tmp_path):
    import regen_inventory
    cache = tmp_path / "cache.yml"
    cache.write_text(render_yaml({"a": {"ansible_host": "10.0.0.1", "vmid": 1, "node": "node1"}}))
    monkeypatch.setattr(regen_inventory, "CACHE_PATH", cache)
    assert regen_inventory.last_known_good_count() == 1
```

(`fetch_raw_containers`, `deliver`, `notify`, `main` call out to `pvesh`/`scp`/`curl`/the filesystem and are exercised for real in Task 7's live manual run, not unit-tested here — matching the spec's own Testing Plan.)

- [ ] **Step 3: Run tests to verify they fail then pass**

Run: `cd playbooks/files/proxmox-inventory-sync && python3 -m pytest regen_inventory_test.py -v`
Expected before Step 1 is saved: N/A (Step 1 and Step 2 are added together here since `last_known_good_count` must exist for the new tests to import). After saving both: `17 passed`.

- [ ] **Step 4: Make the script executable**

Run: `chmod +x playbooks/files/proxmox-inventory-sync/regen_inventory.py`

- [ ] **Step 5: Commit**

```bash
git add playbooks/files/proxmox-inventory-sync/regen_inventory.py playbooks/files/proxmox-inventory-sync/regen_inventory_test.py
git commit -m "Wire the Proxmox regen script's CLI, delivery, and Telegram notification"
```

---

### Task 4: Encrypt-on-receipt script, its wrapper, and unit tests

**Files:**
- Create: `playbooks/files/inventory-encrypt-on-receipt/encrypt_on_receipt.py`
- Create: `playbooks/files/inventory-encrypt-on-receipt/encrypt_on_receipt_test.py`
- Create: `playbooks/files/proxmox-inventory-sync/receive_staged.sh`

**Interfaces:**
- Produces: `generated_hosts(staged_text: str) -> set[str]`, `overrides_hosts() -> set[str]`, `stale_overrides(generated: set[str], overrides: set[str]) -> list[str]`, `encrypt_into_place(staged_path: Path) -> None`, a runnable `encrypt_on_receipt.py`. Task 6 wires this as the `ExecStart` of `inventory-encrypt-on-receipt.service`.
- Consumes: nothing from earlier tasks (independent of Task 2/3's module).

- [ ] **Step 1: Write the failing tests**

Create `playbooks/files/inventory-encrypt-on-receipt/encrypt_on_receipt_test.py`:

```python
from encrypt_on_receipt import generated_hosts, stale_overrides


def test_generated_hosts_parses_yaml():
    text = (
        "containers:\n  hosts:\n"
        "    a: {ansible_host: 10.0.0.1, vmid: 100, node: node1}\n"
        "    b: {ansible_host: 10.0.0.2, vmid: 101, node: node1}\n"
    )
    assert generated_hosts(text) == {"a", "b"}


def test_generated_hosts_empty():
    assert generated_hosts("") == set()
    assert generated_hosts("containers: {}\n") == set()


def test_stale_overrides_finds_orphan():
    assert stale_overrides({"a", "b"}, {"a", "b", "c"}) == ["c"]


def test_stale_overrides_none_when_matching():
    assert stale_overrides({"a", "b"}, {"a", "b"}) == []


def test_stale_overrides_multiple_sorted():
    assert stale_overrides({"a"}, {"a", "z", "m"}) == ["m", "z"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd playbooks/files/inventory-encrypt-on-receipt && python3 -m pytest encrypt_on_receipt_test.py -v`
Expected: `ModuleNotFoundError: No module named 'encrypt_on_receipt'`.

- [ ] **Step 3: Write the implementation**

Create `playbooks/files/inventory-encrypt-on-receipt/encrypt_on_receipt.py`:

```python
#!/usr/bin/env python3
"""Encrypt a freshly-staged plaintext inventory file in place and delete the plaintext.
Runs as nihar on ansible-host, triggered by inventory-encrypt-on-receipt.path."""
import os, subprocess, sys, tempfile; from pathlib import Path
import yaml

REPO = Path("/home/nihar/ansible")
STAGED = REPO / "playbooks/inventory/.staging/containers-generated.yml.plain"
FINAL = REPO / "playbooks/inventory/containers-generated.yml"
OVERRIDES = REPO / "playbooks/inventory/overrides.yml"


def generated_hosts(staged_text):
    """Host names present in the freshly-staged (plaintext) containers-generated.yml."""
    data = yaml.safe_load(staged_text) or {}
    return set(data.get("containers", {}).get("hosts", {}).keys())


def overrides_hosts():
    """Host names hand-listed in overrides.yml, decrypted via the vault password already on this host."""
    out = subprocess.run(
        ["ansible-vault", "view", str(OVERRIDES)], cwd=REPO,
        capture_output=True, text=True, check=True,
    )
    data = yaml.safe_load(out.stdout) or {}
    return set(data.get("containers", {}).get("hosts", {}).keys())


def stale_overrides(generated, overrides):
    """Overrides that name a container no longer present in the freshly-generated inventory."""
    return sorted(overrides - generated)


def encrypt_into_place(staged_path):
    """ansible-vault encrypt the staged plaintext into FINAL via temp file + atomic rename."""
    fd, tmp = tempfile.mkstemp(dir=FINAL.parent)
    os.close(fd)
    try:
        subprocess.run(
            ["ansible-vault", "encrypt", "--output", tmp, str(staged_path)],
            cwd=REPO, check=True, capture_output=True, text=True,
        )
        os.replace(tmp, FINAL)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main():
    if not STAGED.exists():
        return
    staged_text = STAGED.read_text()
    try:
        stale = stale_overrides(generated_hosts(staged_text), overrides_hosts())
        encrypt_into_place(STAGED)
        STAGED.unlink()
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    if stale:
        print(f"WARNING: overrides.yml references containers no longer present: {', '.join(stale)}", file=sys.stderr)
    print(f"OK: encrypted {FINAL}")


if __name__ == "__main__":
    main()
```

Create `playbooks/files/proxmox-inventory-sync/receive_staged.sh`:

```bash
#!/bin/bash
set -euo pipefail
STAGE=/home/nihar/ansible/playbooks/inventory/.staging
TMP="$STAGE/containers-generated.yml.plain.incoming"
FINAL="$STAGE/containers-generated.yml.plain"
/usr/bin/scp -t "$TMP"
mv -f "$TMP" "$FINAL"
```

This is the forced command node1's restricted SSH key runs (Task 6): it does the actual byte transfer into a temp name, then atomically renames into the name `inventory-encrypt-on-receipt.path` watches — so that unit never fires on a partial file.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd playbooks/files/inventory-encrypt-on-receipt && python3 -m pytest encrypt_on_receipt_test.py -v`
Expected: `5 passed`.

- [ ] **Step 5: Make scripts executable**

Run: `chmod +x playbooks/files/inventory-encrypt-on-receipt/encrypt_on_receipt.py playbooks/files/proxmox-inventory-sync/receive_staged.sh`

- [ ] **Step 6: Real end-to-end integration check, run locally on ansible-host**

This repo already lives on ansible-host, so `encrypt_on_receipt.py`'s vault-dependent path can be exercised for real, without touching node1.

```bash
mkdir -p playbooks/inventory/.staging
chmod 700 playbooks/inventory/.staging
cat > playbooks/inventory/.staging/containers-generated.yml.plain <<'EOF'
containers:
  hosts:
    test-fixture-host:
      ansible_host: 10.255.255.1
      vmid: 999999
      node: node1
EOF
# overrides.yml doesn't exist yet (Task 5) — skip the stale-override check for this smoke test
echo "containers: {hosts: {}}" | ansible-vault encrypt --output /tmp/fake-overrides.yml.tmp -
mkdir -p /tmp/fixture-repo-stub  # not used; encrypt_on_receipt.py reads REPO/playbooks/inventory/overrides.yml directly
cp /tmp/fake-overrides.yml.tmp playbooks/inventory/overrides.yml
python3 playbooks/files/inventory-encrypt-on-receipt/encrypt_on_receipt.py
```

Expected: prints `OK: encrypted /home/nihar/ansible/playbooks/inventory/containers-generated.yml`; `playbooks/inventory/.staging/containers-generated.yml.plain` no longer exists; `ansible-vault view playbooks/inventory/containers-generated.yml` shows the `test-fixture-host` entry.

- [ ] **Step 7: Clean up the smoke-test artifacts**

```bash
rm -f playbooks/inventory/containers-generated.yml playbooks/inventory/overrides.yml /tmp/fake-overrides.yml.tmp
rmdir /tmp/fixture-repo-stub 2>/dev/null || true
```

(Task 5 creates the real `overrides.yml`; Task 7 produces the real `containers-generated.yml`. This step only removes the throwaway smoke-test versions.)

- [ ] **Step 8: Commit**

```bash
git add playbooks/files/inventory-encrypt-on-receipt/encrypt_on_receipt.py playbooks/files/inventory-encrypt-on-receipt/encrypt_on_receipt_test.py playbooks/files/proxmox-inventory-sync/receive_staged.sh
git commit -m "Add encrypt-on-receipt script and its restricted SSH delivery wrapper, with unit tests"
```

---

### Task 5: Inventory restructure — static.yml and overrides.yml

**Files:**
- Create: `playbooks/inventory/static.yml`
- Create: `playbooks/inventory/overrides.yml`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: the `selfhost`, `laptops`, and `proxmox_nodes` groups (from `static.yml`) and the `containers` group's hand-maintained per-host vars (from `overrides.yml`) that Ansible directory-merging combines with Task 7's `containers-generated.yml` at inventory-load time.

- [ ] **Step 1: Create the inventory directory**

Run: `mkdir -p playbooks/inventory`

- [ ] **Step 2: Extract the static, non-container hosts into static.yml**

Create `playbooks/inventory/static.yml` by copying the `selfhost` host block, the `laptops` group, and the `proxmox_nodes` group verbatim out of the current (vault-encrypted) `playbooks/inventory.yml` — decrypt it first with `ansible-vault view playbooks/inventory.yml` to see the exact current content, then write the equivalent structure to the new file, preserving every existing `ansible_host` value, `ansible_user`, and any per-host vars exactly as they are today. Do not type any decrypted IP into this plan document or into any other git-tracked file — copy it directly from the decrypted view into the new file only.

- [ ] **Step 3: Extract the per-container hand-maintained vars into overrides.yml**

Create `playbooks/inventory/overrides.yml` with the `containers` group's hosts, keeping only the hand-maintained vars that aren't derivable from Proxmox state (`auto_reboot`, `update_method`, `makefile_dir`, `omniroute_client`, and each container's `ansible_user` if one is set) — omit `ansible_host`, `vmid`, and `node`, since Task 7's `containers-generated.yml` supplies those and Ansible's directory-based inventory merges vars for the same host declared across multiple files. Every container name currently in `playbooks/inventory.yml`'s `containers:` group gets an entry here, even ones with no extra vars beyond `auto_reboot: true` (e.g. `containers: hosts: authentik: {auto_reboot: true, update_method: makefile, makefile_dir: ...}`, `claude: {auto_reboot: true, omniroute_client: true}`, and so on for all containers), matching exactly what's hand-maintained today.

- [ ] **Step 4: Vault-encrypt both new files**

Run: `ansible-vault encrypt playbooks/inventory/static.yml playbooks/inventory/overrides.yml`
Expected: `Encryption successful` for both files.

- [ ] **Step 5: Verify structure (without touching ansible.cfg yet)**

Run: `ansible-inventory --graph -i playbooks/inventory/`
Expected: shows the `selfhost`, `laptops`, `proxmox_nodes`, and `containers` groups with every host from the current `containers:` group listed under `containers` (their vars will be incomplete — no `ansible_host` yet — until Task 7 adds `containers-generated.yml`; that's expected at this point).

- [ ] **Step 6: Commit**

```bash
git add playbooks/inventory/static.yml playbooks/inventory/overrides.yml
git commit -m "Split static hosts and container overrides out of inventory.yml into playbooks/inventory/"
```

---

### Task 6: Bootstrap playbook — SSH trust, systemd units, Telegram creds

**Files:**
- Create: `playbooks/inventory-sync-bootstrap.yml`
- Modify: `site.yml` (add an entry for the new playbook)

**Interfaces:**
- Consumes: `playbooks/files/proxmox-inventory-sync/regen_inventory.py`, `.path`, `.service` (Task 3); `playbooks/files/inventory-encrypt-on-receipt/encrypt_on_receipt.py`, `.path`, `.service`, `receive_staged.sh` (Task 4); `telegram_bot_token`/`telegram_chat_id` vars already present in `playbooks/group_vars/all/vars.yml`.
- Produces: a working end-to-end pipeline that Task 7 exercises for the first real run.

- [ ] **Step 1: Write the systemd unit files for node1**

Create `playbooks/files/proxmox-inventory-sync/proxmox-inventory-sync.path`:

```ini
[Unit]
Description=Watch Proxmox LXC configs for changes

[Path]
PathModified=/etc/pve/nodes/node1/lxc
PathModified=/etc/pve/nodes/node2/lxc
Unit=proxmox-inventory-sync.service

[Install]
WantedBy=multi-user.target
```

Create `playbooks/files/proxmox-inventory-sync/proxmox-inventory-sync.service`:

```ini
[Unit]
Description=Regenerate and deliver Proxmox LXC inventory

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/proxmox-inventory-sync/regen_inventory.py
User=root
```

(pmxcfs is a cluster-wide filesystem, so node1's local `/etc/pve/nodes/` already reflects both nodes — a single path unit on node1 sees every container event cluster-wide, per the spec. `PathModified=` on a directory reliably fires on container create/delete; verify during Task 8's live test whether in-place config edits also trigger it, since pmxcfs often rewrites files wholesale on change — note the result in that task's ledger.)

- [ ] **Step 2: Write the systemd unit files for ansible-host**

Create `playbooks/files/inventory-encrypt-on-receipt/inventory-encrypt-on-receipt.path`:

```ini
[Unit]
Description=Watch for a freshly-staged plaintext inventory file

[Path]
PathExists=/home/nihar/ansible/playbooks/inventory/.staging/containers-generated.yml.plain
Unit=inventory-encrypt-on-receipt.service

[Install]
WantedBy=multi-user.target
```

Create `playbooks/files/inventory-encrypt-on-receipt/inventory-encrypt-on-receipt.service`:

```ini
[Unit]
Description=Encrypt freshly-staged plaintext inventory and place it

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /home/nihar/ansible/playbooks/files/inventory-encrypt-on-receipt/encrypt_on_receipt.py
User=nihar
WorkingDirectory=/home/nihar/ansible
```

- [ ] **Step 3: Write the bootstrap playbook**

Create `playbooks/inventory-sync-bootstrap.yml`:

```yaml
- name: Bootstrap Proxmox inventory sync on node1
  hosts: node1
  become: true
  gather_facts: false
  tasks:
    - name: Ensure python3-yaml is present
      ansible.builtin.apt:
        name: python3-yaml
        state: present

    - name: Ensure the regen script directory exists
      ansible.builtin.file:
        path: /usr/local/lib/proxmox-inventory-sync
        state: directory
        mode: "0755"

    - name: Deploy the regen script
      ansible.builtin.copy:
        src: files/proxmox-inventory-sync/regen_inventory.py
        dest: /usr/local/lib/proxmox-inventory-sync/regen_inventory.py
        mode: "0755"

    - name: Ensure the telegram credentials directory exists
      ansible.builtin.file:
        path: /etc/proxmox-inventory-sync
        state: directory
        mode: "0700"

    - name: Deploy Telegram credentials for node1's regen notifications
      ansible.builtin.copy:
        dest: /etc/proxmox-inventory-sync/telegram.env
        content: "TELEGRAM_BOT_TOKEN={{ telegram_bot_token }}\nTELEGRAM_CHAT_ID={{ telegram_chat_id }}\n"
        mode: "0600"
      no_log: true

    - name: Generate a dedicated keypair for delivering inventory to ansible-host
      ansible.builtin.command:
        cmd: ssh-keygen -t ed25519 -f /root/.ssh/proxmox-inventory-sync_ed25519 -N "" -C proxmox-inventory-sync
        creates: /root/.ssh/proxmox-inventory-sync_ed25519

    - name: Read back the generated public key
      ansible.builtin.slurp:
        src: /root/.ssh/proxmox-inventory-sync_ed25519.pub
      register: node1_sync_pubkey_raw

    - name: Expose the public key to the ansible-host play
      ansible.builtin.set_fact:
        node1_sync_pubkey: "{{ node1_sync_pubkey_raw.content | b64decode | trim }}"

    - name: Deploy the systemd path unit
      ansible.builtin.copy:
        src: files/proxmox-inventory-sync/proxmox-inventory-sync.path
        dest: /etc/systemd/system/proxmox-inventory-sync.path
        mode: "0644"
      notify: reload systemd node1

    - name: Deploy the systemd service unit
      ansible.builtin.copy:
        src: files/proxmox-inventory-sync/proxmox-inventory-sync.service
        dest: /etc/systemd/system/proxmox-inventory-sync.service
        mode: "0644"
      notify: reload systemd node1

  handlers:
    - name: reload systemd node1
      ansible.builtin.systemd:
        daemon_reload: true

- name: Bootstrap Proxmox inventory sync on ansible-host
  hosts: ansible-host
  become: false
  gather_facts: false
  tasks:
    - name: Ensure the staging directory exists, owner-only
      ansible.builtin.file:
        path: /home/nihar/ansible/playbooks/inventory/.staging
        state: directory
        mode: "0700"

    - name: Make the receive-staged wrapper script executable
      ansible.builtin.file:
        path: /home/nihar/ansible/playbooks/files/proxmox-inventory-sync/receive_staged.sh
        mode: "0755"

    - name: Authorize node1's key, restricted to the receive-staged wrapper
      ansible.posix.authorized_key:
        user: nihar
        key: "{{ hostvars['node1'].node1_sync_pubkey }}"
        key_options: >-
          command="/home/nihar/ansible/playbooks/files/proxmox-inventory-sync/receive_staged.sh",
          no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding

    - name: Deploy the systemd path unit
      ansible.builtin.copy:
        src: files/inventory-encrypt-on-receipt/inventory-encrypt-on-receipt.path
        dest: /etc/systemd/system/inventory-encrypt-on-receipt.path
        mode: "0644"
      become: true
      notify: reload systemd ansible-host

    - name: Deploy the systemd service unit
      ansible.builtin.copy:
        src: files/inventory-encrypt-on-receipt/inventory-encrypt-on-receipt.service
        dest: /etc/systemd/system/inventory-encrypt-on-receipt.service
        mode: "0644"
      become: true
      notify: reload systemd ansible-host

    - name: Enable and start the path units
      ansible.builtin.systemd:
        name: "{{ item }}"
        enabled: true
        state: started
      become: true
      loop:
        - inventory-encrypt-on-receipt.path

  handlers:
    - name: reload systemd ansible-host
      ansible.builtin.systemd:
        daemon_reload: true
      become: true
```

Note: `proxmox-inventory-sync.path` is enabled/started as a separate task at the end of the node1 play (added in Step 4 below) rather than inline, to keep this step's playbook body matching what Step 4 tests incrementally.

- [ ] **Step 4: Add the enable/start task for node1's path unit**

Add this task to the end of the node1 play's `tasks:` list, after "Deploy the systemd service unit":

```yaml
    - name: Enable and start the sync path unit
      ansible.builtin.systemd:
        name: proxmox-inventory-sync.path
        enabled: true
        state: started
        daemon_reload: true
```

- [ ] **Step 5: Register the playbook in site.yml**

Read `site.yml` first to match its existing entry style, then add an entry for `playbooks/inventory-sync-bootstrap.yml` following the same pattern as the other one-time bootstrap playbooks already listed there (e.g. `bootstrap-nodes.yml`).

- [ ] **Step 6: Dry-run check the playbook syntax**

Run: `ansible-playbook playbooks/inventory-sync-bootstrap.yml --syntax-check`
Expected: `playbook: playbooks/inventory-sync-bootstrap.yml` with no errors.

- [ ] **Step 7: Run the bootstrap playbook for real**

This makes real changes to node1 (a hypervisor) and to ansible-host (this repo's own host) — installs packages, writes credential files, generates an SSH keypair, edits `authorized_keys`, and installs/starts two systemd units. This is exactly the infrastructure change the whole plan exists to make; treat it as a real, intentional action, not a simulation.

Run: `ansible-playbook playbooks/inventory-sync-bootstrap.yml`
Expected: `PLAY RECAP` shows `ok`/`changed` for both `node1` and `ansible-host`, zero `failed`.

- [ ] **Step 8: Verify both path units are active**

Run (on node1, via ssh): `systemctl is-active proxmox-inventory-sync.path`
Expected: `active`

Run (on ansible-host, locally): `systemctl is-active inventory-encrypt-on-receipt.path`
Expected: `active`

- [ ] **Step 9: Commit**

```bash
git add playbooks/inventory-sync-bootstrap.yml playbooks/files/proxmox-inventory-sync/proxmox-inventory-sync.path playbooks/files/proxmox-inventory-sync/proxmox-inventory-sync.service playbooks/files/inventory-encrypt-on-receipt/inventory-encrypt-on-receipt.path playbooks/files/inventory-encrypt-on-receipt/inventory-encrypt-on-receipt.service site.yml
git commit -m "Add inventory-sync-bootstrap.yml wiring the regen pipeline onto node1 and ansible-host"
```

---

### Task 7: First manual regen run and inventory cutover

**Files:**
- Create (by the pipeline, not hand-authored): `playbooks/inventory/containers-generated.yml`
- Modify: `ansible.cfg`
- Delete (rename): `playbooks/inventory.yml` → `playbooks/inventory.yml.bak`

**Interfaces:**
- Consumes: the fully-deployed pipeline from Task 6; `static.yml`/`overrides.yml` from Task 5.
- Produces: `playbooks/inventory/` as the live, complete inventory that Task 8's playbook run reads.

- [ ] **Step 1: Trigger the first regen run manually**

Run (on node1, via ssh, as root): `python3 /usr/local/lib/proxmox-inventory-sync/regen_inventory.py`
Expected: exits 0; a Telegram message arrives reporting N hosts delivered.

- [ ] **Step 2: Confirm the file arrived and was encrypted**

Run: `ansible-vault view playbooks/inventory/containers-generated.yml | head -20`
Expected: valid decrypted YAML showing a `containers: hosts:` mapping with the current fleet's containers, each with `ansible_host`/`vmid`/`node`.

Run: `ls playbooks/inventory/.staging/`
Expected: empty — the plaintext file was deleted after encryption.

- [ ] **Step 3: Diff the new inventory against the old one, host by host**

Run: `diff <(ansible-inventory --list -i playbooks/inventory.yml | python3 -m json.tool) <(ansible-inventory --list -i playbooks/inventory/ | python3 -m json.tool)`
Expected: no differences beyond ordering/formatting — every host, group, and var present in the old inventory is present in the new one. Investigate and resolve (by fixing `static.yml`/`overrides.yml`, not by editing the generated file) any real discrepancy before continuing.

- [ ] **Step 4: Point ansible.cfg at the new inventory directory**

Edit `/home/nihar/ansible/ansible.cfg`, changing `inventory = playbooks/inventory.yml` to `inventory = playbooks/inventory/`.

- [ ] **Step 5: Verify the default inventory resolves correctly**

Run: `ansible-inventory --graph`
Expected: same group/host structure as the `--list` diff in Step 3, now using the default (no `-i` flag needed).

- [ ] **Step 6: Retire the old monolithic inventory file**

Remove it from git tracking, then rename the working-tree copy aside (the `.bak` suffix is already gitignored from Task 1):

```bash
git rm --cached playbooks/inventory.yml
mv playbooks/inventory.yml playbooks/inventory.yml.bak
```

Expected: `git status` shows `playbooks/inventory.yml` deleted (staged) and `playbooks/inventory.yml.bak` untracked-but-ignored (per Task 1's existing `.gitignore` rule).

- [ ] **Step 7: Commit**

```bash
git add ansible.cfg playbooks/inventory/containers-generated.yml
git commit -m "Cut over to the directory-based inventory; retire the monolithic inventory.yml"
```

---

### Task 8: Live validation — throwaway container and a low-risk playbook run

**Files:** none (validation only; no new files).

**Interfaces:**
- Consumes: the fully cut-over pipeline from Task 7.

- [ ] **Step 1: Create a throwaway LXC container on node1**

Run (on node1, via ssh): create a minimal test container with a static IP in `net0`, e.g. `pct create 999998 local:vztmpl/<any-template-already-present> --hostname sync-test --net0 name=eth0,bridge=vmbr0,ip=10.255.255.254/24,type=veth --storage local-lvm`
Expected: container `999998` exists (`pct status 999998` → `status: stopped` is fine, it doesn't need to run).

- [ ] **Step 2: Confirm the pipeline picks it up automatically**

Run: `sleep 5 && ansible-vault view playbooks/inventory/containers-generated.yml | grep sync-test`
Expected: an entry for `sync-test` with `ansible_host: 10.255.255.254` and `vmid: 999998`, with no manual regen invocation — the path units fired on their own.

- [ ] **Step 3: Modify the container's config and confirm regen fires again**

Run (on node1): `pct set 999998 --net0 name=eth0,bridge=vmbr0,ip=10.255.255.253/24,type=veth`
Run: `sleep 5 && ansible-vault view playbooks/inventory/containers-generated.yml | grep sync-test`
Expected: `ansible_host` updated to `10.255.255.253`. If it does NOT update, this confirms the `PathModified=` gotcha noted in Task 6 Step 1 (directory watch not catching in-place content edits) — if so, ledger this as a ruling and switch the `.path` unit's node1 side to a periodic timer as a fallback (e.g. every 60s) rather than leaving silent staleness; record whichever behavior was actually observed.

- [ ] **Step 4: Destroy the throwaway container and confirm cleanup**

Run (on node1): `pct destroy 999998`
Run: `sleep 5 && ansible-vault view playbooks/inventory/containers-generated.yml | grep sync-test`
Expected: no output — `sync-test` is gone. No stale-override warning fires (it was never added to `overrides.yml`); confirm via `journalctl -u inventory-encrypt-on-receipt.service -n 20` showing no `WARNING` line.

- [ ] **Step 5: Run a real low-risk playbook against the new inventory**

Following the repo's existing convention of testing new logic on a low-risk host first, run: `ansible-playbook site.yml --limit alienware-m15 --check` (or another low-priority, non-critical host already in `static.yml`)
Expected: playbook completes with the same result it would have produced against the old inventory — no connectivity or var-resolution errors caused by the migration.

- [ ] **Step 6: No commit needed**

This task only validates already-committed state; skip if no files changed. If Step 3 required a ruling and a fallback timer was added, commit that fix now:

```bash
git add playbooks/files/proxmox-inventory-sync/proxmox-inventory-sync.path playbooks/inventory-sync-bootstrap.yml
git commit -m "Fall back to periodic regen polling; PathModified does not reliably catch in-place LXC config edits"
```

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-23-proxmox-ansible-inventory-sync.md`. Please review the plan. Which execution approach would you prefer?

- **Subagent-driven** — A fresh subagent implements each task and a fresh reviewer checks it before the next one starts, then a whole-branch review at the end. Most thorough; costs a fresh context per task and per review.
- **Native** — I implement every task myself in this session, the way this harness runs work, then one fresh reviewer on the most capable model checks the whole branch. Cheapest and fastest; no independent review until the end.

For this plan I recommend **Native**, because most tasks are tightly sequential (Task 3 literally appends to Task 2's file, Task 7 depends on everything Task 6 deployed, Task 8 depends on Task 7's cutover) with almost no independent-task parallelism to exploit, and the plan itself is fully concrete — a fresh subagent per task would mostly re-read the same handful of files (the two Python scripts, the bootstrap playbook) without gaining much isolation benefit, while Task 6 and Task 7 touch real infrastructure (node1, a hypervisor) where I want continuous context across the deploy → verify → cutover sequence rather than handing that off task-by-task. Does the plan capture what you want, and which approach should we use?
