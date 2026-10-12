#!/bin/bash
# update-report.sh [daily|weekly]
# Summarises automatic updates + health into /opt/homelab/update-report.log (appended)
# and the latest report as JSON in /opt/homelab/update-report.json.
# Runs as root from homelab-update-report-{daily,weekly}.timer. Read-only: changes nothing.
export LC_ALL=C
MODE=${1:-daily}
LOG=/opt/homelab/update-report.log
JSON=/opt/homelab/update-report.json
IGNORE_STOPPED=" nextcloud nextcloud-db nextcloud-redis "   # intentionally stopped
if [ "$MODE" = weekly ]; then HOURS=168; LABEL="last 7 days"; else HOURS=24; LABEL="last 24h"; fi
SINCE_EPOCH=$(date -d "$HOURS hours ago" +%s)
SINCE_TS=$(date -d "$HOURS hours ago" '+%Y-%m-%d %H:%M:%S')
NOW=$(date '+%Y-%m-%d %H:%M:%S %Z')
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
: > "$W/issues"; : > "$W/updated"; : > "$W/body"
issue() { echo "$*" >> "$W/issues"; }
sec() { printf '\n--- %s ---\n' "$1" >> "$W/body"; }

# 1. Container updates (Watchtower) -------------------------------------------
sec "Container updates ($LABEL)"
docker logs watchtower --since "${HOURS}h" > "$W/wt" 2>&1
grep -E 'Started new container' "$W/wt" | sed -E 's/.*time=([^ ]+).*container=([^ ]+).*image=([^ ]+).*/\2  \3  (\1)/' > "$W/updated"
if [ -s "$W/updated" ]; then sed 's/^/updated: /' "$W/updated" >> "$W/body"; else echo "no containers were updated by Watchtower" >> "$W/body"; fi
grep -E 'Update session completed' "$W/wt" | sed -E 's/.*time=([^ ]+).*(failed=[0-9]+).*(scanned=[0-9]+).*(skipped=[0-9]+).*(updated=[0-9]+).*/session \1 \3 \5 \2 \4/' | tail -n 8 >> "$W/body"
if grep -E 'Update session completed' "$W/wt" | grep -Eq 'failed=[1-9]'; then issue "Watchtower reported failed container updates"; fi
if grep -Eq 'level=(error|fatal)' "$W/wt"; then issue "Watchtower logged errors (see log below)"; grep -E 'level=(error|fatal)' "$W/wt" | tail -n 5 >> "$W/body"; fi
if ! grep -Eq 'Update session completed|Next scheduled run' "$W/wt" && [ "$MODE" = daily ]; then issue "Watchtower has no log lines in the last 24h (is it running?)"; fi
echo "containers (re)created in window (any cause):" >> "$W/body"
RECR=0
for n in $(docker ps -a --format '{{.Names}}'); do
  c=$(docker inspect -f '{{.Created}}' "$n" 2>/dev/null) || continue
  ce=$(date -d "$c" +%s 2>/dev/null) || continue
  if [ "$ce" -ge "$SINCE_EPOCH" ]; then echo "  $n  ($(docker inspect -f '{{.Config.Image}}' "$n"))  created $(date -d "$c" '+%F %T')" >> "$W/body"; RECR=1; fi
done
[ $RECR = 0 ] && echo "  none" >> "$W/body"

# 2. OS updates ---------------------------------------------------------------
sec "OS updates: packages upgraded ($LABEL)"
zcat -f /var/log/dpkg.log* 2>/dev/null | awk -v s="$SINCE_TS" '($1" "$2)>=s && $3=="upgrade"{print "  "$1" "$2"  "$4"  "$5" -> "$6}' | sort | tail -n 60 > "$W/pk"
if [ -s "$W/pk" ]; then cat "$W/pk" >> "$W/body"; else echo "  none" >> "$W/body"; fi
sec "unattended-upgrades log ($LABEL)"
awk -v s="$SINCE_TS" '($1" "$2)>=s && ($3=="INFO"||$3=="WARNING"||$3=="ERROR")' /var/log/unattended-upgrades/unattended-upgrades.log 2>/dev/null \
  | grep -vE 'Checking if system is running on battery|The list of kept packages' | cut -c1-220 | tail -n 40 > "$W/ua"
if [ -s "$W/ua" ]; then cat "$W/ua" >> "$W/body"; else echo "  (no entries)" >> "$W/body"; fi
if grep -E ' (ERROR|WARNING) ' "$W/ua" | grep -vE 'Found /var/run/reboot-required, rebooting|Shutdown msg' | grep -q .; then issue "unattended-upgrades logged warnings/errors"; fi
if grep -Eq 'Package.*(failed|could not)|dpkg.*error|Installing the upgrades failed' "$W/ua"; then issue "unattended-upgrades install problem"; fi
PEND=$(apt-get -s upgrade 2>/dev/null | awk '/^Inst/{print $2}' | tr '\n' ' ')
sec "Pending upgrades NOT installed automatically (e.g. third-party repos)"
echo "  ${PEND:-none}" >> "$W/body"

# 3. Reboot -------------------------------------------------------------------
sec "Reboot"
BOOT=$(uptime -s); BOOTE=$(date -d "$BOOT" +%s)
if [ "$BOOTE" -ge "$SINCE_EPOCH" ]; then echo "rebooted in window: boot at $BOOT" >> "$W/body"; REBOOTED=true; else echo "no reboot in window (up since $BOOT)" >> "$W/body"; REBOOTED=false; fi
if [ -f /var/run/reboot-required ]; then issue "reboot still required ($(tr '\n' ' ' < /var/run/reboot-required)) - auto-reboot did not happen"; echo "REBOOT STILL REQUIRED" >> "$W/body"; fi

# 4. Containers -----------------------------------------------------------------
sec "Containers"
docker ps -a --format '{{.Names}}|{{.State}}|{{.Status}}' | sort > "$W/ps"
NRUN=0
while IFS='|' read -r name state status; do
  [ "$state" = running ] && NRUN=$((NRUN+1))
  case "$IGNORE_STOPPED" in *" $name "*) continue;; esac
  if [ "$state" != running ]; then issue "container $name is $state ($status)"; fi
  case "$status" in *unhealthy*) issue "container $name is unhealthy";; esac
  case "$status" in *Restarting*) issue "container $name is restarting";; esac
done < "$W/ps"
echo "$NRUN running; problems listed under ATTENTION if any" >> "$W/body"
awk -F'|' '{printf "  %-24s %s\n",$1,$3}' "$W/ps" >> "$W/body"

# 5. systemd --------------------------------------------------------------------
sec "Failed systemd units"
systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' > "$W/failed"
if [ -s "$W/failed" ]; then sed 's/^/  /' "$W/failed" >> "$W/body"; while read -r u; do issue "systemd unit failed: $u"; done < "$W/failed"; else echo "  none" >> "$W/body"; fi

# 6. Backup + disk -----------------------------------------------------------------
sec "Backup and disk"
LB=$(ls -1t /opt/homelab-backups/homelab-config-*.tar.gz 2>/dev/null | head -n1)
if [ -n "$LB" ]; then
  AGE=$(( ($(date +%s) - $(stat -c %Y "$LB")) / 86400 ))
  echo "latest backup: $(basename "$LB") $(du -h "$LB" | cut -f1), ${AGE} day(s) old; $(ls -1 /opt/homelab-backups/homelab-config-*.tar.gz | wc -l) kept" >> "$W/body"
  [ "$AGE" -gt 8 ] && issue "latest config backup is $AGE days old"
else echo "no backups found" >> "$W/body"; issue "no config backup found in /opt/homelab-backups"; fi
USE=$(df --output=pcent / | tail -n1 | tr -dc 0-9)
echo "root disk: ${USE}% used" >> "$W/body"
[ "$USE" -ge 90 ] && issue "root disk is ${USE}% full"

# assemble ----------------------------------------------------------------------
NISS=$(wc -l < "$W/issues")
if [ "$NISS" -eq 0 ]; then STATUS=OK; else STATUS="ATTENTION ($NISS issue(s))"; fi
{
  echo "=================================================================="
  echo "$NOW  $MODE report ($LABEL)  STATUS: $STATUS"
  if [ "$NISS" -gt 0 ]; then echo "ATTENTION:"; sed 's/^/  ! /' "$W/issues"; fi
  cat "$W/body"
  echo
} >> "$LOG"
# keep the log bounded (~1 MB)
if [ "$(stat -c %s "$LOG")" -gt 1048576 ]; then tail -n 5000 "$LOG" > "$W/log.new" && cat "$W/log.new" > "$LOG"; fi
chmod 644 "$LOG"

REBOOTED=$REBOOTED MODE=$MODE STATUS_OK=$([ "$NISS" -eq 0 ] && echo true || echo false) NOW="$NOW" HOURS=$HOURS NRUN=$NRUN PEND="$PEND" W="$W" JSON="$JSON" python3 - <<'PY'
import json, os
W = os.environ["W"]
rd = lambda n: [l.rstrip("\n") for l in open(os.path.join(W, n))]
out = {
    "generated": os.environ["NOW"],
    "mode": os.environ["MODE"],
    "window_hours": int(os.environ["HOURS"]),
    "ok": os.environ["STATUS_OK"] == "true",
    "issues": rd("issues"),
    "containers_updated": [l.split("  ")[0] for l in rd("updated")],
    "packages_upgraded": len(rd("pk")),
    "rebooted_in_window": os.environ["REBOOTED"] == "true",
    "containers_running": int(os.environ["NRUN"]),
    "pending_not_auto_installed": os.environ["PEND"].split(),
    "failed_units": rd("failed"),
}
p = os.environ["JSON"]
with open(p + ".tmp", "w") as f:
    json.dump(out, f, indent=2)
os.replace(p + ".tmp", p)
os.chmod(p, 0o644)
PY
exit 0
