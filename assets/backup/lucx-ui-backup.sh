#!/usr/bin/env bash
# lucx-ui-backup.sh — backup and restore LucX-UI-PRO (panel + nginx + certs + AdGuard)
set -Eeuo pipefail

BACKUP_STORE="/var/backups/lucx-ui"
PACKAGES="nginx-full certbot python3 sqlite3 curl wget jq ufw apache2-utils ca-certificates tar"

BACKUP_PATHS=(
  /etc/nginx
  /etc/x-ui
  /usr/local/x-ui
  /usr/bin/x-ui
  /etc/letsencrypt
  /root/cert
  /var/www/html
  /opt/AdGuardHome
  /root/.lucx-adguard-info
  /etc/ufw/user.rules
  /etc/ufw/user6.rules
)
SYSTEMD_UNITS=(x-ui.service AdGuardHome.service)

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
blue()  { printf '\033[34m%s\033[0m\n' "$*"; }
die()   { red "ERROR: $*" >&2; exit 1; }
require_root() { [[ $EUID -eq 0 ]] || die "Run as root (sudo $0 $*)"; }
avail_kb() { df -Pk "$1" | awk 'NR==2 {print $4}'; }
make_staging() {
  local prefix="$1"
  mkdir -p "${BACKUP_STORE}"
  mktemp -d "${BACKUP_STORE}/.${prefix}-XXXXXX"
}

cmd_backup() {
  require_root
  local ts name staging dest est_kb need_kb have_kb path dst unit size
  ts=$(date +%Y%m%d-%H%M%S)
  name="lucx-ui-backup-${ts}"
  est_kb=$(du -skc "${BACKUP_PATHS[@]}" 2>/dev/null | awk 'END {print $1}')
  need_kb=$(( est_kb * 2 + 102400 ))
  mkdir -p "${BACKUP_STORE}"
  have_kb=$(avail_kb "${BACKUP_STORE}")
  (( have_kb >= need_kb )) || die "Not enough free space in ${BACKUP_STORE}: need ~$(( need_kb / 1024 )) MB, have $(( have_kb / 1024 )) MB"
  staging=$(make_staging staging)
  trap 'rm -rf "${staging}"' EXIT
  dest="${BACKUP_STORE}/${name}.tar.gz"
  blue "==> Stopping x-ui and AdGuard Home for a consistent snapshot..."
  systemctl stop x-ui 2>/dev/null || true
  systemctl stop AdGuardHome 2>/dev/null || true
  blue "==> Collecting files..."
  local files_root="${staging}/files"
  for path in "${BACKUP_PATHS[@]}"; do
    [[ -e "${path}" ]] || continue
    dst="${files_root}${path}"
    mkdir -p "$(dirname "${dst}")"
    cp -a "${path}" "${dst}"
  done
  mkdir -p "${files_root}/etc/systemd/system"
  for unit in "${SYSTEMD_UNITS[@]}"; do
    [[ -f "/etc/systemd/system/${unit}" ]] && cp "/etc/systemd/system/${unit}" "${files_root}/etc/systemd/system/"
  done
  crontab -l 2>/dev/null > "${staging}/root-crontab" || true
  [[ -d /etc/cron.d ]] && cp -a /etc/cron.d "${staging}/cron.d"
  local xui_ver
  xui_ver=$(x-ui version 2>/dev/null | grep -oP '[0-9]+\.[0-9]+[^[:space:]]*' | head -1 || echo unknown)
  cat > "${staging}/meta.json" <<EOF
{
  "created": "${ts}",
  "panel": "lucx-ui",
  "xui_version": "${xui_ver}",
  "hostname": "$(hostname 2>/dev/null || true)",
  "adguard": $([ -d /opt/AdGuardHome ] && echo true || echo false)
}
EOF
  blue "==> Compressing..."
  tar -czf "${dest}" -C "${staging}" .
  systemctl start x-ui 2>/dev/null || true
  systemctl start AdGuardHome 2>/dev/null || true
  size=$(du -sh "${dest}" | cut -f1)
  green "==> Backup saved: ${dest} (${size})"
}

cmd_restore() {
  require_root
  local backup_file="${1:-}" svc staging unpacked_kb have_kb db web_cert cert_domain
  [[ -n "${backup_file}" ]] || die "Usage: $0 restore <backup.tar.gz>"
  [[ -f "${backup_file}" ]] || die "File not found: ${backup_file}"
  unpacked_kb=$(( $(gzip -l "${backup_file}" | awk 'NR==2 {print $2}') / 1024 ))
  mkdir -p "${BACKUP_STORE}"
  have_kb=$(avail_kb "${BACKUP_STORE}")
  (( have_kb >= unpacked_kb * 2 + 102400 )) || die "Not enough free space in ${BACKUP_STORE}"
  staging=$(make_staging restore)
  trap 'rm -rf "${staging}"' EXIT
  blue "==> Extracting backup: ${backup_file}"
  tar -xzf "${backup_file}" -C "${staging}"
  [[ -f "${staging}/meta.json" ]] && { blue "==> Backup metadata:"; cat "${staging}/meta.json"; echo; }
  blue "==> Installing packages..."
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y ${PACKAGES}
  blue "==> Stopping services..."
  for svc in nginx x-ui AdGuardHome; do systemctl stop "${svc}" 2>/dev/null || true; done
  blue "==> Restoring files..."
  [[ -d "${staging}/files" ]] && cp -a "${staging}/files/." /
  chown -R www-data:www-data /var/www/html 2>/dev/null || true
  [[ -f /usr/local/x-ui/x-ui ]] && chmod +x /usr/local/x-ui/x-ui
  [[ -f /usr/bin/x-ui ]] && chmod +x /usr/bin/x-ui
  [[ -x /opt/AdGuardHome/AdGuardHome ]] && chmod +x /opt/AdGuardHome/AdGuardHome
  [[ -f /root/.lucx-adguard-info ]] && chmod 600 /root/.lucx-adguard-info
  [[ -f /opt/AdGuardHome/AdGuardHome.yaml ]] && chmod 600 /opt/AdGuardHome/AdGuardHome.yaml
  db=/etc/x-ui/x-ui.db
  if [[ -f "${db}" ]] && command -v sqlite3 &>/dev/null; then
    web_cert=$(sqlite3 "${db}" "SELECT value FROM settings WHERE key='webCertFile';" 2>/dev/null || true)
    if [[ "${web_cert}" =~ ^/root/cert/([^/]+)/ ]]; then
      cert_domain="${BASH_REMATCH[1]}"
      if [[ ! -e "${web_cert}" && -d "/etc/letsencrypt/live/${cert_domain}" ]]; then
        mkdir -p "/root/cert/${cert_domain}"
        ln -sf "/etc/letsencrypt/live/${cert_domain}/fullchain.pem" "/root/cert/${cert_domain}/fullchain.pem"
        ln -sf "/etc/letsencrypt/live/${cert_domain}/privkey.pem"   "/root/cert/${cert_domain}/privkey.pem"
      fi
    fi
  fi
  systemctl daemon-reload
  systemctl enable x-ui 2>/dev/null || true
  systemctl start x-ui 2>/dev/null || true
  if [[ -x /opt/AdGuardHome/AdGuardHome ]]; then
    if ! systemctl list-unit-files --type=service 2>/dev/null | grep -q '^AdGuardHome\.service'; then
      /opt/AdGuardHome/AdGuardHome -s install 2>/dev/null || true
    fi
    systemctl enable AdGuardHome 2>/dev/null || true
    systemctl start AdGuardHome 2>/dev/null || true
  fi
  if nginx -t 2>/dev/null; then systemctl enable nginx; systemctl restart nginx; green " nginx restarted OK"
  else red " nginx config test failed"; nginx -t; fi
  [[ -s "${staging}/root-crontab" ]] && crontab - < "${staging}/root-crontab"
  [[ -d "${staging}/cron.d" ]] && cp -a "${staging}/cron.d/." /etc/cron.d/
  ufw --force enable 2>/dev/null || true
  green "==> Restore complete."
}

cmd_list() {
  if [[ ! -d "${BACKUP_STORE}" ]]; then echo "No backups found (${BACKUP_STORE} does not exist)"; return; fi
  local archives f
  mapfile -t archives < <(ls -t "${BACKUP_STORE}"/*.tar.gz 2>/dev/null || true)
  if [[ ${#archives[@]} -eq 0 ]]; then echo "No backups in ${BACKUP_STORE}"; return; fi
  blue "Backups in ${BACKUP_STORE}:"
  for f in "${archives[@]}"; do printf "  %-55s %s\n" "$(basename "${f}")" "$(du -sh "${f}" | cut -f1)"; done
}

case "${1:-}" in
  backup)  cmd_backup ;;
  restore) cmd_restore "${2:-}" ;;
  list)    cmd_list ;;
  *) echo "Usage: $0 {backup|restore <file>|list}"; exit 1 ;;
esac
