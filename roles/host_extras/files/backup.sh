#!/bin/bash
# Weekly backup of /opt/homelab configs (no media/downloads/caches) -> /opt/homelab-backups
# Runs as root from homelab-backup.timer (Sunday 01:00). Keeps the newest 4.
set -u
umask 077
DEST=/opt/homelab-backups
KEEP=4
mkdir -p "$DEST"; chmod 700 "$DEST"
ts=$(date +%Y%m%d-%H%M%S)
out="$DEST/homelab-config-$ts.tar.gz"
part="$out.partial"

# extra (outside /opt/homelab) files that define the update setup
extra=()
for p in etc/apt/apt.conf.d/20auto-upgrades etc/apt/apt.conf.d/52unattended-upgrades-local \
         etc/systemd/system/apt-daily.timer.d etc/systemd/system/apt-daily-upgrade.timer.d \
         etc/systemd/system/homelab-backup.service etc/systemd/system/homelab-backup.timer \
         etc/systemd/system/homelab-update-report-daily.service etc/systemd/system/homelab-update-report-daily.timer \
         etc/systemd/system/homelab-update-report-weekly.service etc/systemd/system/homelab-update-report-weekly.timer \
         etc/docker/daemon.json; do
  [ -e "/$p" ] && extra+=("$p")
done

# Paperless originals live in paperless/media, which the global 'media' exclude skips:
# use the official exporter into paperless/export (included) instead
if docker ps --format '{{.Names}}' | grep -qx paperless; then
  docker exec paperless document_exporter ../export --delete > /dev/null 2>&1 || echo "warning: paperless export failed"
fi

tar -czf "$part" \
  --exclude='media' --exclude='cache' --exclude='.cache' --exclude='__pycache__' \
  --exclude='transcodes' --exclude='*.swp' --exclude='*.swo' \
  --exclude='homelab/downloads' \
  --exclude='homelab/mediasorter/venv' \
  --exclude='homelab/nextcloud/data' --exclude='homelab/nextcloud/html' \
  --exclude='homelab/nextcloud/db' --exclude='homelab/nextcloud/redis' \
  --exclude='homelab/pihole/etc-pihole/listsCache' \
  --exclude='homelab/pihole/etc-pihole/gravity_old.db' \
  --exclude='homelab/homeassistant/config/deps' \
  --exclude='homelab/jellyfin/config/data/metadata' \
  --exclude='homelab/qbittorrent/config/qBittorrent/GeoDB' \
  --exclude='homelab/immich/library/upload' --exclude='homelab/immich/library/library' \
  --exclude='homelab/immich/library/thumbs' --exclude='homelab/immich/library/encoded-video' \
  --exclude='homelab/immich/library/profile' --exclude='homelab/immich/postgres' \
  --exclude='homelab/paperless/redis' \
  -C /opt homelab -C / "${extra[@]}" 2> "$DEST/.last-tar-warnings"
rc=$?
# rc 1 = "file changed as we read it" (live databases); the archive is still usable
if [ $rc -gt 1 ]; then
  echo "backup FAILED (tar rc=$rc)"; cat "$DEST/.last-tar-warnings"; rm -f "$part"; exit 1
fi
if ! tar -tzf "$part" > /dev/null 2>&1; then
  echo "backup FAILED (archive unreadable)"; rm -f "$part"; exit 1
fi
mv "$part" "$out"; chmod 600 "$out"
# rotate: keep newest $KEEP
ls -1t "$DEST"/homelab-config-*.tar.gz 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f
echo "backup OK: $out ($(du -h "$out" | cut -f1), tar rc=$rc)"
