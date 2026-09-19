#!/usr/bin/env bash
# lucx-ui-backup.sh — backup and restore LucX-UI-PRO (panel + nginx + certs + AdGuard)
# Usage:
#   lucx-ui-backup backup
#   lucx-ui-backup list
#   lucx-ui-backup restore /var/backups/lucx-ui/....tar.gz
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

  local ts name staging dest est_kb need_kb have_kb
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
  local files_root="${staging}/files" path dst
  for path in "${BACKUP_PATHS[@]}"; do
    [[ -e "${path}" ]] || continue
    dst="${files_root}${path}"
    mkdir -p "$(dirname "${dst}")"
    cp -a "${path}" "${dst}"
  done

  mkdir -p "${files_root}/etc/systemd/system"
  local unit
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

  local size
  size=$(du -sh "${dest}" | cut -f1)
  green "==> Backup saved: ${dest} (${size})"
}

cmd_restore() {
  require_root

  local backup_file="${1:-}"
  [[ -n "${backup_file}" ]] || die "Usage: $0 restore <backup.tar.gz>"
  [[ -f "${backup_file}" ]] || die "File not found: ${backup_file}"

  local unpacked_kb have_kb staging
  unpacked_kb=$(( $(gzip -l "${backup_file}" | awk 'NR==2 {print $2}') / 1024 ))
  mkdir -p "${BACKUP_STORE}"
  have_kb=$(avail_kb "${BACKUP_STORE}")
  (( have_kb >= unpacked_kb * 2 + 102400 )) || \
    die "Not enough free space in ${BACKUP_STORE}: need ~$(( (unpacked_kb * 2 + 102400) / 1024 )) MB, have $(( have_kb / 1024 )) MB"

  staging=$(make_staging restore)
  trap 'rm -rf "${staging}"' EXIT

  blue "==> Extracting backup: ${backup_file}"
  tar -xzf "${backup_file}" -C "${staging}"

  if [[ -f "${staging}/meta.json" ]]; then
    blue "==> Backup metadata:"
    cat "${staging}/meta.json"
    echo
  fi

  blue "==> Installing packages..."
  apt-get update -qq
  # shellcheck disable=SC2086
  DEBIAN_FRONTEND=noninteractive apt-get install -y ${PACKAGES}

  blue "==> Stopping services..."
  local svc
  for svc in nginx x-ui AdGuardHome; do
    systemctl stop "${svc}" 2>/dev/null || true
  done

  blue "==> Restoring files..."
  if [[ -d "${staging}/files" ]]; then
    cp -a "${staging}/files/." /
  fi

  chown -R www-data:www-data /var/www/html 2>/dev/null || true
  [[ -f /usr/local/x-ui/x-ui ]] && chmod +x /usr/local/x-ui/x-ui
  [[ -f /usr/bin/x-ui ]] && chmod +x /usr/bin/x-ui
  [[ -x /opt/AdGuardHome/AdGuardHome ]] && chmod +x /opt/AdGuardHome/AdGuardHome
  [[ -f /root/.lucx-adguard-info ]] && chmod 600 /root/.lucx-adguard-info
  [[ -f /opt/AdGuardHome/AdGuardHome.yaml ]] && chmod 600 /opt/AdGuardHome/AdGuardHome.yaml

  local db=/etc/x-ui/x-ui.db web_cert cert_domain
  if [[ -f "${db}" ]] && command -v sqlite3 &>/dev/null; then
    web_cert=$(sqlite3 "${db}" "SELECT value FROM settings WHERE key='webCertFile';" 2>/dev/null || true)
    if [[ "${web_cert}" =~ ^/root/cert/([^/]+)/ ]]; then
      cert_domain="${BASH_REMATCH[1]}"
      if [[ ! -e "${web_cert}" && -d "/etc/letsencrypt/live/${cert_domain}" ]]; then
        blue "==> Recreating panel cert symlinks in /root/cert/${cert_domain}..."
        mkdir -p "/root/cert/${cert_domain}"
        chmod 755 /root/cert/* 2>/dev/null || true
        ln -sf "/etc/letsencrypt/live/${cert_domain}/fullchain.pem" "/root/cert/${cert_domain}/fullchain.pem"
        ln -sf "/etc/letsencrypt/live/${cert_domain}/privkey.pem"   "/root/cert/${cert_domain}/privkey.pem"
      fi
    fi
  fi

  blue "==> Enabling and starting services..."
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

  if nginx -t 2>/dev/null; then
    systemctl enable nginx
    systemctl restart nginx
    green " nginx restarted OK"
  else
    red " nginx config test failed — fix manually:"
    nginx -t
  fi

  blue "==> Restoring cron..."
  if [[ -s "${staging}/root-crontab" ]]; then
    crontab - < "${staging}/root-crontab"
    green " Root crontab restored"
  fi
  if [[ -d "${staging}/cron.d" ]]; then
    cp -a "${staging}/cron.d/." /etc/cron.d/
    green " /etc/cron.d restored"
  fi

  blue "==> Restoring UFW..."
  ufw --force enable 2>/dev/null || true
  green " UFW enabled"

  echo
  green "==> Restore complete."
  green " Check status with:"
  green "   systemctl status x-ui nginx AdGuardHome"
}

cmd_list() {
  if [[ ! -d "${BACKUP_STORE}" ]]; then
    echo "No backups found (${BACKUP_STORE} does not exist)"
    return
  fi
  local archives
  mapfile -t archives < <(ls -t "${BACKUP_STORE}"/*.tar.gz 2>/dev/null || true)
  if [[ ${#archives[@]} -eq 0 ]]; then
    echo "No backups in ${BACKUP_STORE}"
    return
  fi
  blue "Backups in ${BACKUP_STORE}:"
  local f
  for f in "${archives[@]}"; do
    printf "  %-55s %s\n" "$(basename "${f}")" "$(du -sh "${f}" | cut -f1)"
  done
}

case "${1:-}" in
  backup)  cmd_backup ;;
  restore) cmd_restore "${2:-}" ;;
  list)    cmd_list ;;
  *)
    cat <<EOF
Usage: $0 {backup|restore <file>|list}

  backup              create timestamped backup in ${BACKUP_STORE}/
  restore <file>      restore from backup archive (installs packages first)
  list                list available backups

What is backed up:
  /etc/nginx                 nginx config (incl. AdGuard snippet)
  /etc/x-ui                  panel DB + config
  /usr/local/x-ui            LucX panel + xray + geodata updater
  /usr/bin/x-ui              x-ui CLI
  /etc/letsencrypt           SSL certificates
  /root/cert                 panel cert symlinks
  /var/www/html              fake cover site
  /opt/AdGuardHome           AdGuard Home (if installed)
  /root/.lucx-adguard-info   AdGuard credentials
  systemd                    x-ui.service, AdGuardHome.service
  /etc/ufw/user*.rules       firewall rules
  root crontab + /etc/cron.d
EOF
    exit 1
    ;;
esac
