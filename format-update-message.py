#!/usr/bin/env python3
"""Turn a raw ansible-playbook run log into a short, scannable Telegram message.
Usage: format-update-message.py <run_log_path> <exit_code> <hostname> <command>
"""

import re
import sys
from collections import OrderedDict

run_log, rc, hostname, cmd = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]

# Routine bookkeeping that happens on every successful run — not interesting
# to see repeated per host, so it's filtered out of the "changes" section.
NOISE = {
    "Remove old snapshot in this slot (if present)",
    "Create snapshot",
    "Advance snapshot index",
    "Ensure snapshot state directory exists",
    "Initialize snapshot index if absent",
    "Read snapshot index",
    "Set snapshot name",
    "Ensure control node has an SSH keypair",
    "Check if host key is already known",
}

current_task = None
in_recap = False
recap = OrderedDict()
changes = OrderedDict()

with open(run_log, errors="replace") as f:
    for line in f:
        line = line.rstrip("\n")
        m = re.match(r"^TASK \[(.*?)\]", line)
        if m:
            current_task = m.group(1)
            in_recap = False
            continue
        if line.startswith("PLAY RECAP"):
            in_recap = True
            continue
        if in_recap:
            m = re.match(r"^(\S+)\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", line)
            if m:
                host, _ok, changed, unreachable, failed = m.groups()
                recap[host] = dict(changed=int(changed), unreachable=int(unreachable), failed=int(failed))
            continue
        m = re.match(r"^(changed|failed|fatal):\s*\[([^\]]+)\](.*)$", line)
        if m:
            kind, host_raw, rest = m.groups()
            host = host_raw.split(" -> ")[0]
            if kind == "changed" and current_task in NOISE:
                continue
            detail = ""
            item_m = re.search(r"item=([^\s)]+)", rest)
            if item_m:
                # Strip the common compose-root prefix but keep the rest (e.g.
                # "prod/card-games") — some hosts have multiple stacks that'd
                # otherwise collapse to the same basename and look identical.
                detail = re.sub(r"^/opt/docker-compose/", "", item_m.group(1))
            label = current_task or "?"
            text = f"{label} ({detail})" if detail else label
            if kind != "changed":
                text = f"⚠️ {text}"
            changes.setdefault(host, [])
            if text not in changes[host]:
                changes[host].append(text)

total_hosts = len(recap)
total_changed = sum(v["changed"] for v in recap.values())
total_failed = sum(v["failed"] for v in recap.values())
total_unreachable = sum(v["unreachable"] for v in recap.values())
bad_hosts = [h for h, v in recap.items() if v["failed"] or v["unreachable"]]

icon = "✅" if rc == 0 else "\U0001f6a8"
status = "succeeded" if rc == 0 else "FAILED"

lines = [f"{icon} {status}: {hostname}", cmd, ""]
summary = f"{total_hosts} hosts • {total_changed} changes • {total_failed} failed"
if total_unreachable:
    summary += f" • {total_unreachable} unreachable"
lines.append(summary)

if bad_hosts:
    lines.append("")
    lines.append("⚠️ Problem hosts:")
    for h in bad_hosts:
        lines.append(f"  {h}")

real_changes = {h: v for h, v in changes.items() if v}
if real_changes:
    lines.append("")
    lines.append("\U0001f4e6 Changes:")
    for h, items in real_changes.items():
        lines.append(f"{h}: {'; '.join(items)}")

msg = "\n".join(lines)
if len(msg) > 3800:
    msg = msg[:3800] + "\n… [truncated, see update.log]"
print(msg)
