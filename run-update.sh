#!/bin/bash
# Usage: run-update.sh <ansible-playbook args...>
# Wraps any ansible-playbook invocation, logs it, and sends a short,
# scannable Telegram message (summary + only the changes that matter) on
# both success and failure. Full raw output always goes to update.log.
cd /home/nihar/ansible

BOT_TOKEN=$(ansible-vault view playbooks/group_vars/all/vault.yml < /dev/null 2>/dev/null | grep vault_telegram_bot_token | awk -F'"' '{print $2}')
CHAT_ID="5050956829"

LOGFILE=/home/nihar/ansible/update.log
RUN_LOG=$(mktemp)
trap 'rm -f "$RUN_LOG"' EXIT

notify() {
    local text="$1"
    if [ -z "$BOT_TOKEN" ]; then
        echo "=== Telegram notify skipped: could not read bot token from vault ($(date)) ===" >> "$LOGFILE"
        return
    fi
    RESP=$(curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${CHAT_ID}" \
        --data-urlencode "text=${text}")
    if [[ "$RESP" != *'"ok":true'* ]]; then
        echo "=== Telegram notify failed ($(date)): $RESP ===" >> "$LOGFILE"
    fi
}

echo "=== Run started: $(date) — ansible-playbook $* ===" >> "$LOGFILE"

ansible-playbook "$@" > "$RUN_LOG" 2>&1
RC=$?
cat "$RUN_LOG" >> "$LOGFILE"

if [ "$RC" -eq 0 ]; then
    echo "=== Run succeeded: $(date) ===" >> "$LOGFILE"
else
    echo "=== Run FAILED: $(date) ===" >> "$LOGFILE"
fi

MSG=$(python3 /home/nihar/ansible/format-update-message.py "$RUN_LOG" "$RC" "$(hostname)" "ansible-playbook $*")
notify "$MSG"
