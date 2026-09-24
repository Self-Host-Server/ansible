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
