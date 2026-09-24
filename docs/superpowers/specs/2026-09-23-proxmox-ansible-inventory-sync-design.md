# Proxmox → Ansible inventory sync

Date: 2026-09-23
Status: design approved in chat, pending written-spec review

## Problem

`playbooks/inventory.yml` is hand-maintained. Every time a container is
created, reconfigured, or destroyed on the Proxmox cluster (node1/node2,
cluster "Eleno"), someone has to remember to edit it. This has already
caused a real bug ([[ansible_inventory_vmid_bug]] — `claude` and
`postgres` collided on vmid 127 because the file was edited by hand and
drifted from reality).

Goal: when an LXC container is created, modified, or deleted on the
cluster, the Ansible inventory reflects that automatically, without
manual editing — while still allowing the hand-maintained per-host vars
(`auto_reboot`, `update_method`, `makefile_dir`, `omniroute_client`, etc.)
that Proxmox has no concept of.

## Non-goals

- No new credential provisioning — reuses concepts already in
  `playbooks/group_vars/all/vars.yml` where useful.
- Not solving IP discovery via guest agent — every container's `net0`
  field already carries its static IP, so we parse that directly.
- Not changing anything about VMs (only LXC containers are in scope,
  matching the current `containers` group).

## Version control & secrets handling

Added mid-design: `/home/nihar/ansible` was not previously a git repo.
It now is, with `origin` set to the existing **public** GitHub repo
`https://github.com/Self-Host-Server/ansible` (empty at time of writing).
Because the remote is public, no internal IP address, hostname-to-IP
mapping, or credential may ever exist in git history in plaintext — not
"don't commit it now," but "never commit it unencrypted at any point,"
since git history is permanent even if a later commit fixes it.

Concretely:

- `playbooks/group_vars/all/vault.yml` — already `ansible-vault`
  encrypted; no change needed.
- `playbooks/group_vars/all/vars.yml` — contains `proxmox_api_host:
<node1 LAN IP>` in plaintext. Must be `ansible-vault encrypt`ed in place
  before it is ever `git add`ed.
- The inventory files (both the current `playbooks/inventory.yml` and,
  after migration, all three of `static.yml`, `overrides.yml`,
  `containers-generated.yml`) contain `ansible_host` IPs. Each must be
  `ansible-vault encrypt`ed in place before being committed. Ansible
  transparently decrypts vault-encrypted files at run time given
  `vault_password_file` (already configured in `ansible.cfg`), so
  encrypting in place requires no changes to `site.yml` or playbook
  logic.
- `.gitignore` covers build/runtime artifacts only (`node_modules/`,
  `update.log`, `*.retry`, `__pycache__/`) — it is not used as a
  substitute for encryption. A file either doesn't belong in git, or it's
  vault-encrypted before it goes in; nothing sensitive stays out solely
  via `.gitignore`.
- **The automated regen pipeline never touches the vault password on
  node1.** node1 generates `containers-generated.yml` in plaintext and
  pushes it to a _staging_ path on ansible-host over SSH. A local step on
  ansible-host — which already holds `~/.vault_pass` — re-encrypts the
  staged file into the real, git-tracked
  `playbooks/inventory/containers-generated.yml` path. This keeps
  decrypt/encrypt capability confined to ansible-host (and the human),
  never extending it to the hypervisor.
- The regen pipeline does **not** auto-commit to git after each run —
  its rollback safety net is the atomic-write/last-known-good mechanism
  already described below, which is independent of git. Committing the
  regenerated file to git (to keep drift history) is a manual/periodic
  action, not part of this design's automation.
- Pushing to the `Self-Host-Server/ansible` remote always requires an
  explicit go-ahead per push (standing convention — commits are fine
  autonomously, pushes are not).

## Architecture

```
┌─────────────────────────────┐
│ node1 (Proxmox hypervisor)  │
│                              │
│  /etc/pve/nodes/*/lxc/*.conf│◄── pmxcfs, cluster-wide, shared with node2
│         ▲                    │
│         │ inotify via        │
│  systemd path unit           │
│         │                    │
│         ▼                    │
│  regen script (Python)       │
│   - pvesh get /cluster/      │
│     resources --type vm      │
│     --output-format json     │
│   - parse net0 for static IP │
│   - build inventory YAML     │
│   - validate                 │
│   - scp/rsync over SSH  ─────┼──► ansible-host (LAN IP)
└─────────────────────────────┘      playbooks/inventory/.staging/
                                        containers-generated.yml.plain
                                      (ansible-host then vault-encrypts
                                       into containers-generated.yml)
```

**Why node1, not ansible-host:** `/etc/pve/nodes/*/lxc/*.conf` only
exists on PVE hypervisor nodes (pmxcfs, the cluster filesystem). Since
node1 and node2 are clustered ("Eleno", quorate), pmxcfs is replicated
between them — a watcher on node1 alone sees every container event
cluster-wide, so only one node needs the unit.

**Why a systemd path unit, not `pct hookscript`:** Proxmox's per-container
hookscript only fires on start/stop of an _existing_ container — it
cannot fire on create (container doesn't exist yet when it would need to
register) or destroy (container is already gone). A path unit watching
the pmxcfs directory itself is the only way to catch create/delete
events, not just modify.

**Why push over SSH, not polling or a pull-based cron:** the systemd path
unit already gives us event-driven triggering on node1; polling from
ansible-host would throw that away and reintroduce a delay. node1 pushing
the finished file over SSH keeps the whole pipeline event-driven end to
end.

## Components

### 1. systemd units on node1

- `proxmox-inventory-sync.path` — watches `/etc/pve/nodes/*/lxc/*.conf`
  (`PathModified=`, `Unit=proxmox-inventory-sync.service`)
- `proxmox-inventory-sync.service` — oneshot, runs the regen script

### 2. Regen script (Python, runs as root on node1)

- Calls `pvesh get /cluster/resources --type vm --output-format json`
  locally (no API token needed — local pvesh socket access as root)
- Filters to `type == "lxc"`
- For each container, parses `net0` for the static `ip=` field
- Builds `containers-generated.yml`: one entry per container under the
  `containers` group, with `ansible_host`, `vmid`, `node` — the fields
  Proxmox actually knows
- Writes to a temp file first; only proceeds to delivery if the YAML is
  valid and every entry has a resolvable IP (entries that fail parsing
  are skipped with a warning, not fatal to the whole run)
- Delivers the **plaintext** file via `scp`/`rsync` over SSH to a staging
  path, `ansible-host:/home/nihar/ansible/playbooks/inventory/.staging/containers-generated.yml.plain`,
  itself written to a temp name and atomically renamed on the receiving
  end, so a failed/partial transfer never clobbers the last staged file
- Logs outcome and sends a Telegram notification (reusing the existing
  pattern from [[ansible_telegram_notifications]]) — every run, success
  or failure, so a broken sync doesn't go unnoticed

### 3. Encrypt-on-receipt (ansible-host, local, holds the vault password)

A small watcher (systemd path unit, same mechanism as node1) on
ansible-host watches the staging path. On change, it runs
`ansible-vault encrypt --vault-password-file ~/.vault_pass
--output playbooks/inventory/containers-generated.yml
playbooks/inventory/.staging/containers-generated.yml.plain`,
using a temp file + atomic rename for the final write, then deletes the
staged plaintext. This is the only place in the whole pipeline that
touches the vault password — node1 never has it.

### 4. SSH trust: node1 → ansible-host

One-time setup: a dedicated keypair (not the existing `root@node1`↔fleet
key) authorized on ansible-host, scoped as tightly as practical (e.g.
`authorized_keys` `command=` restriction limiting it to writing only
under `playbooks/inventory/.staging/`).

### 5. Ansible inventory restructure

`playbooks/inventory.yml` (single file) becomes `playbooks/inventory/`
(directory) with three files — Ansible natively merges hostvars across
every file in a directory-based inventory, so no custom merge code is
needed:

- **`static.yml`** — the groups that aren't Proxmox-CT-derived, moved
  over unchanged: `selfhost`, `laptops`, `proxmox_nodes`.
- **`overrides.yml`** — hand-maintained, keyed by container name:
  `auto_reboot`, `update_method`, `makefile_dir`, `omniroute_client`, and
  any future per-host var Proxmox has no concept of. This file is never
  touched by the regen script.
- **`containers-generated.yml`** — fully machine-generated, overwritten
  on every regen run, never hand-edited. Contains only what Proxmox
  itself knows: `ansible_host`, `vmid`, `node`.

`ansible.cfg`'s `inventory =` changes from `playbooks/inventory.yml` to
`playbooks/inventory/`.

## Error handling

- **Atomic writes, both ends.** The regen script never overwrites a good
  file with a bad or partial one — temp file + validate + rename, on
  node1 before sending and again on ansible-host on receipt.
- **Per-host skip, not whole-run failure.** A container with an
  unparseable `net0` (e.g. DHCP instead of static, malformed config) is
  skipped with a warning; the rest of the inventory still regenerates.
- **Stale override warning.** If `overrides.yml` references a container
  name no longer present in `containers-generated.yml` (container was
  deleted), log a warning rather than failing — keeps hand-maintained
  overrides from silently orphaning without blocking the sync.
- **Notify every run.** Telegram message on both success and failure,
  matching the existing fleet-update notification convention.
- **No debounce.** Proxmox may write a container's `.conf` several times
  in quick succession (e.g. during creation). Rather than adding
  debounce/settle-time complexity, the regen is treated as cheap and
  idempotent — redundant runs are harmless.

## Migration of the existing `inventory.yml`

1. Create `playbooks/inventory/` with the three files described above.
2. `static.yml` — move `selfhost`, `laptops`, `proxmox_nodes` groups over
   verbatim.
3. `overrides.yml` — extract the per-container hand-maintained vars,
   keyed by container name.
4. `containers-generated.yml` — produced by a first **manual** run of the
   regen script (before the systemd units are installed), so its output
   can be diffed against the current `inventory.yml` before anything is
   trusted. Specifically re-verify vmids for every container, given the
   prior `claude`/`postgres` vmid-127 collision bug.
5. `ansible-vault encrypt` all three new files (`static.yml`,
   `overrides.yml`, `containers-generated.yml`) in place, and separately
   `ansible-vault encrypt` `playbooks/group_vars/all/vars.yml` — none of
   these may be `git add`ed while still plaintext, since the remote is
   public.
6. Point `ansible.cfg`'s `inventory =` at the new directory.
7. Run `ansible-inventory --list` against the new directory-based
   inventory and diff it against the old file's `ansible-inventory
--list` output. They must match exactly (aside from the source
   restructure) before proceeding. (`ansible-inventory` transparently
   decrypts vault-encrypted files given `vault_password_file`, so this
   diff works the same whether the files are encrypted or not.)
8. Rename the old `inventory.yml` to `inventory.yml.bak` (not delete) —
   this is the rollback point for the inventory side of the migration.
   `.bak` files stay out of git via `.gitignore`, since the old file is
   still plaintext.

## Rollback / safety net

The original ask was "snapshot before making changes so we can roll
back." Reconsidered against what's actually being modified:

- **node1** is a hypervisor, not a container — it can't be
  Proxmox-snapshotted. Its safety net is a plain file-level backup
  (`cp`) of anything touched there (the new systemd units, and nothing
  else — no existing node1 config is modified) before installation.
- **ansible-host** — the rollback point is `inventory.yml.bak` from step
  7 above, plus normal file backups of `ansible.cfg` before it's edited.
- No Proxmox CT snapshot is required by this design, since no container
  is modified by the deployment itself — only node1 (hypervisor-level)
  and ansible-host (config-level) change.

This becomes an explicit precondition step in the implementation plan,
not just a conversational promise.

## Testing plan

1. **Dry run the regen script manually on node1** against the live
   cluster (read-only: it only calls `pvesh get`), before any systemd
   units are installed. Confirm its output matches current
   `inventory.yml` reality.
2. **Live test with a throwaway container.** Create a low-stakes test
   LXC on node1, confirm the path unit fires and the container appears
   in `containers-generated.yml` on ansible-host within seconds. Modify
   its config, confirm regen fires again. Destroy it, confirm it
   disappears from the generated file and a stale-override warning does
   _not_ fire (since it won't be in `overrides.yml`).
3. **Full inventory diff.** After the migration (step 6 above),
   `ansible-inventory --list` must match pre-migration output exactly.
4. **Low-risk playbook run.** Following the existing convention
   ([[ansible_test_low_risk_host_first]]), run a real playbook (e.g. the
   laptop or a single low-priority container) against the new
   directory-based inventory before trusting it for the full fleet.

## Reused existing patterns

- Telegram run notifications ([[ansible_telegram_notifications]])
- Testing new Ansible logic on a low-risk host first
  ([[ansible_test_low_risk_host_first]])
- `community.general.proxmox_snap`-style rotation pattern exists in
  `playbooks/update.yml` but is not directly reused here, since this
  design doesn't modify any container that could be snapshotted — see
  Rollback section above.

## Open items for the implementation plan

- Exact restricted-command syntax for the node1→ansible-host SSH key
  (scoping it to just this file transfer).
- Where the regen script and systemd unit files live in this repo before
  deployment (e.g. `playbooks/files/proxmox-inventory-sync/`) so they're
  deployed via Ansible rather than hand-copied to node1.
- Whether deploying the regen script/units themselves should be its own
  Ansible playbook (e.g. `playbooks/inventory-sync-bootstrap.yml`, run
  once) — this seems like the natural fit given the existing
  `bootstrap-nodes.yml`/`bootstrap.yml` pattern in `site.yml`.
