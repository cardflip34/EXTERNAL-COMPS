#!/bin/bash
# External Comps V2 keep-alive (PRD V2 §46/§49) — run by launchd (com.mazi.extcomps-keepalive, every 600 s).
#
# WHY THE ssh-localhost HOP: a launchd GUI agent on this Mini cannot read/write the 6 TB volume (TCC; verified
# 2026-08-19: even `ls` -> "Operation not permitted"), but sshd-spawned processes inherit Remote Login's Full
# Disk Access. So launchd only checks liveness and, when needed, relaunches the supervisor THROUGH
# `ssh localhost` (key auth: ~/.ssh/id_ed25519 is in ~/.ssh/authorized_keys). Survives reboots; no operator step.
#
# Logs: ~/Library/Logs/mazi/launchd/extcomps_keepalive.log (this), supervisor log in
# ~/Library/Logs/mazi_external_comps/extcomps_supervisor.out (written by the supervisor itself).
set -uo pipefail
LOGD="$HOME/Library/Logs/mazi/launchd"; mkdir -p "$LOGD" "$HOME/Library/Logs/mazi_external_comps"
LOG="$LOGD/extcomps_keepalive.log"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ROOT="$HOME/whatnot-sniper"

# liveness: PID file written by the supervisor (fcntl-locked) + kill -0 + command check.
# (NOT pgrep -f: any shell whose command line mentions the script path would self-match.)
LOCK="$HOME/mazi_local_evidence/extcomps_supervisor.lock"
if [ -s "$LOCK" ]; then
  PID="$(tr -dc '0-9' < "$LOCK")"
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null && /bin/ps -o command= -p "$PID" 2>/dev/null | grep -q "external_engine/supervisor.py"; then
    echo "$TS alive pid=$PID" >> "$LOG"
    exit 0
  fi
fi

# relaunch through sshd so the child gets FDA. BatchMode: never prompt. 20 s budget.
OUT="$(/usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new localhost \
  "cd '$ROOT' && nohup /usr/bin/python3 -u external_engine/supervisor.py >> '$HOME/Library/Logs/mazi_external_comps/extcomps_supervisor.out' 2>&1 < /dev/null & echo launched_pid=\$!" 2>&1)"
RC=$?
echo "$TS relaunch rc=$RC $OUT" >> "$LOG"
sleep 4
PID="$(tr -dc '0-9' < "$LOCK" 2>/dev/null)"
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  echo "$TS supervisor up pid=$PID" >> "$LOG"
else
  echo "$TS WARN supervisor not up after relaunch (check ssh localhost key auth / Remote Login)" >> "$LOG"
fi
