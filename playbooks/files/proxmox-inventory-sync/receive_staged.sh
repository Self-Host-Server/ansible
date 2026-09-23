#!/bin/bash
set -euo pipefail
STAGE=/home/nihar/ansible/playbooks/inventory/.staging
TMP="$STAGE/containers-generated.yml.plain.incoming"
FINAL="$STAGE/containers-generated.yml.plain"
/usr/bin/scp -t "$TMP"
mv -f "$TMP" "$FINAL"
