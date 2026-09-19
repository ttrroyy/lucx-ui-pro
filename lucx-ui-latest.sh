#!/bin/bash
[[ $EUID -ne 0 ]] && { echo "Run as root: sudo bash $0"; exit 1; }

# ─── Output helpers ──────────────────────────────────────────────────────────
msg_ok()  { echo -e "\e[1;42m $1 \e[0m"; }
msg_err() { echo -e "\e[1;41m $1 \e[0m"; }
msg_inf() { echo -e "\e[1;34m$1\e[0m"; }

echo
msg_inf '        __           ___    _   _   _  '
msg_inf      '|  | | /   \/ __ | |  | __ |_) |_) / \ '
msg_inf      '|_ |_| \__ /\    |_| _|_   |   | \ \_/ '
echo
_os_id=$(grep -oP '(?<=^ID=).+' /etc/os-release 2>/dev/null | tr -d '"')
_os_ver=$(grep -oP '(?<=^VERSION_ID=).+' /etc/os-release 2>/dev/null | tr -d '"')
_os_id_cap=$(echo "${_os_id}" | awk '{print toupper(substr($0,1,1)) substr($0,2)}')
echo "The OS release is: ${_os_id_cap} ${_os_ver}"
echo

# ─── Pre-flight checks ───────────────────────────────────────────────────────
check_os() {
    local os_id os_version
    os_id=$(grep -oP '(?<=^ID=).+' /etc/os-release 2>/dev/null | tr -d '"')
    os_version=$(grep -oP '(?<=^VERSION_ID=").+(?=")' /etc/os-release 2>/dev/null)

    case "${os_id}" in
        ubuntu)
            [[ "$os_version" == "24.04" || "$os_version" == "26.04" ]] && return 0
            ;;
        debian)
            [[ "$os_version" == "12" || "$os_version" == "13" ]] && return 0
            ;;
    esac

    msg_err "Unsupported OS: ${os_id} ${os_version}"
    echo -e "\nThis script supports:\n  Ubuntu 24.04 / 26.04\n  Debian 12 / 13"
    echo -e "\nPlease reinstall your server with one of the supported OS versions and try again."
    exit 1
}

check_cpu() {
    local cpu_model
    cpu_model=$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2-)

    if echo "$cpu_model" | grep -qi 'QEMU'; then
        msg_err "QEMU virtual CPU detected!"
        echo -e "\nYour VPS is running with an emulated QEMU processor."
        echo -e "Please contact your hosting provider and ask them to switch the CPU type"
        echo -e "to \e[1;33mhost-passthrough\e[0m (expose real CPU model to the VM)."
        echo -e "\nThis is required for correct operation of the Xray core."
        exit 1
    fi
}

check_os
check_cpu

# ─── Constants ───────────────────────────────────────────────────────────────
XUIDB="/etc/x-ui/x-ui.db"
GITHUB_RAW="https://raw.githubusercontent.com/ttrroyy/lucx-ui-pro/main"
FAKE_SITE_COUNT=50

# ─── Default argument values ─────────────────────────────────────────────────
domain=""
reality_domain=""
UNINSTALL="x"
INSTALL="y"
AUTODOMAIN="n"
CFALLOW="n"
PANEL_VERSION=""
DNS_CHOICE=""
EXTRA_INBOUND="1"
DEPLOY_AGH=""
DEPLOY_HY2=""
ADGUARD_ONLY=""
ADGUARD_UNINSTALL=""
CUSTOM_DOH_URL=""
CUSTOM_DOH_HOST=""
CUSTOM_DOH_IP=""

# ─── Stop & clean previous install (called from main, after domain validation) ─
clean_previous_install() {
    uninstall_adguard 2>/dev/null || true
    systemctl stop x-ui 2>/dev/null || true
    rm -rf /etc/systemd/system/x-ui.service
    rm -rf /usr/local/x-ui
    rm -rf /etc/x-ui
    rm -rf /etc/nginx/sites-enabled/*
    rm -rf /etc/nginx/sites-available/*
    rm -rf /etc/nginx/stream-enabled/*
}

# ─── Port / path generators ──────────────────────────────────────────────────
get_port() {
    echo $(( ((RANDOM<<15)|RANDOM) % 49152 + 10000 ))
}

gen_random_string() {
    local length="$1"
    head -c 4096 /dev/urandom | tr -dc 'a-zA-Z0-9' | head -c "$length"
    echo
}

# Matches the panel's host group_id format (16 lowercase alphanumerics)
gen_group_id() {
    head -c 4096 /dev/urandom | tr -dc 'a-z0-9' | head -c 16
    echo
}

check_free() {
    nc -z 127.0.0.1 "$1" &>/dev/null
    return $?
}

make_port() {
    while true; do
        local PORT
        PORT=$(get_port)
        if ! check_free "$PORT"; then
            echo "$PORT"
            break
        fi
    done
}

# ─── Generate ports & paths (done once at startup) ───────────────────────────
sub_port=$(make_port)
panel_port=$(make_port)
hy2_port=""

sub_path=$(gen_random_string 10)
json_path=$(gen_random_string 10)
panel_path=$(gen_random_string 10)
xhttp_path=$(gen_random_string 10)
config_username=$(gen_random_string 10)
config_password=$(gen_random_string 10)

# ─── Argument parsing ────────────────────────────────────────────────────────
while [ "$#" -gt 0 ]; do
    case "$1" in
        -install)          INSTALL="$2";           shift 2 ;;
        -subdomain)        domain="$2";            shift 2 ;;
        -reality_domain)   reality_domain="$2";    shift 2 ;;
        -ONLY_CF_IP_ALLOW) CFALLOW="$2";           shift 2 ;;
        -version)          PANEL_VERSION="$2";     shift 2 ;;
        -adguard)          ADGUARD_ONLY="$2";      shift 2 ;;
        -adguard-uninstall) ADGUARD_UNINSTALL="$2"; shift 2 ;;
        -uninstall)        UNINSTALL="$2";         shift 2 ;;
        *)                 shift 1 ;;
    esac
done

# ─── Detect package manager ───────────────────────────────────────────────────
Pak=$(type apt &>/dev/null && echo "apt" || echo "yum")

download_one_geo() {
    local dest="$1" name="$2" url="$3" fallback="${4:-}" min_bytes="${5:-50000}"
    local tmp http size head
    tmp="${dest}/${name}.tmp.$$"
    rm -f "$tmp"
    http=$(curl -sSfLRo "$tmp" --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 180 -w '%{http_code}' "$url" || true)
    if [[ "$http" != "200" || ! -s "$tmp" ]]; then
        rm -f "$tmp"
        if [[ -n "$fallback" ]]; then
            http=$(curl -sSfLRo "$tmp" --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 180 -w '%{http_code}' "$fallback" || true)
        fi
    fi
    if [[ "$http" != "200" || ! -s "$tmp" ]]; then
        rm -f "$tmp"
        echo "Failed to download ${name}"
        return 1
    fi
    size=$(wc -c < "$tmp"); size=${size// /}
    head=$(head -c 64 "$tmp" | tr -d '\0' || true)
    if printf '%s' "$head" | grep -qiE '<html|<!doctype|^Not Found|^404'; then
        rm -f "$tmp"
        echo "${name} looks like HTML, not a .dat"
        return 1
    fi
    if (( size < min_bytes )); then
        rm -f "$tmp"
        echo "${name} too small (${size} bytes)"
        return 1
    fi
    mv -f "$tmp" "${dest}/${name}"
    echo "  ${name}: ${size} bytes"
}
ensure_stock_geo() {
    local dest="$1" name="$2" url="$3" fallback="${4:-}" min_bytes="${5:-50000}"
    if [[ -s "${dest}/${name}" ]]; then
        echo "  ${name}: already present, leave stock file"
        return 0
    fi
    download_one_geo "$dest" "$name" "$url" "$fallback" "$min_bytes"
}
fetch_lucx_geofiles() {
    local dest="${1:-/usr/local/x-ui/bin}"
    mkdir -p "$dest"
    local GH='https://github.com'
    local RAW='https://raw.githubusercontent.com'
    local CDN='https://cdn.jsdelivr.net/gh'
    echo "Ensuring stock LucX/3x-ui geodata (no overwrite) ..."
    ensure_stock_geo "$dest" geoip.dat "$GH/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat" "" 100000 || return 1
    ensure_stock_geo "$dest" geosite.dat "$GH/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat" "" 100000 || return 1
    ensure_stock_geo "$dest" geoip_IR.dat "$GH/chocolate4u/Iran-v2ray-rules/releases/latest/download/geoip.dat" "" 50000 || true
    ensure_stock_geo "$dest" geosite_IR.dat "$GH/chocolate4u/Iran-v2ray-rules/releases/latest/download/geosite.dat" "" 50000 || true
    ensure_stock_geo "$dest" geoip_RU.dat "$GH/runetfreedom/russia-v2ray-rules-dat/releases/latest/download/geoip.dat" "$RAW/runetfreedom/russia-v2ray-rules-dat/release/geoip.dat" 50000 || true
    ensure_stock_geo "$dest" geosite_RU.dat "$GH/runetfreedom/russia-v2ray-rules-dat/releases/latest/download/geosite.dat" "$RAW/runetfreedom/russia-v2ray-rules-dat/release/geosite.dat" 50000 || true
    ensure_stock_geo "$dest" geoip_ROSCOM.dat "$GH/hydraponique/roscomvpn-geoip/releases/latest/download/geoip.dat" "$CDN/hydraponique/roscomvpn-geoip/release/geoip.dat" 50000 || true
    ensure_stock_geo "$dest" geosite_ROSCOM.dat "$GH/hydraponique/roscomvpn-geosite/releases/latest/download/geosite.dat" "$CDN/hydraponique/roscomvpn-geosite/release/geosite.dat" 50000 || true
    echo "Placing runetfreedom NEXT TO ROSCOM as geoip_RUNET.dat / geosite_RUNET.dat ..."
    download_one_geo "$dest" geoip_RUNET.dat "$RAW/runetfreedom/russia-v2ray-rules-dat/release/geoip.dat" "$GH/runetfreedom/russia-v2ray-rules-dat/raw/release/geoip.dat" 50000 || return 1
    download_one_geo "$dest" geosite_RUNET.dat "$RAW/runetfreedom/russia-v2ray-rules-dat/release/geosite.dat" "$GH/runetfreedom/russia-v2ray-rules-dat/raw/release/geosite.dat" 50000 || return 1
}

normalize_dns_choice() {
    local c
    c=$(echo "$1" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
    case "$c" in
        1|default|none) echo 1 ;;
        2|cloudflare|cf|doh-cloudflare) echo 2 ;;
        3|google|doh-google) echo 3 ;;
        4|quad9|q9|doh-quad9) echo 4 ;;
        5|self|selfhosted|adguard|agh) echo 5 ;;
        6|custom|own) echo 6 ;;
        *) echo "" ;;
    esac
}
has_global_ipv6() { ip -6 addr show scope global 2>/dev/null | grep -q 'inet6'; }
resolve_doh_for_hosts() {
    python3 - "$1" <<'PY'
import socket, sys, urllib.parse
raw = (sys.argv[1] or "").strip()
p = urllib.parse.urlparse(raw)
if p.scheme != "https" or not p.hostname:
    raise SystemExit(1)
host = p.hostname
try:
    infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
except Exception:
    raise SystemExit(1)
ip = infos[0][4][0] if infos else ""
if not ip:
    raise SystemExit(1)
print(host); print(ip); print(raw)
PY
}
choose_xray_dns() {
    local ans mapped tty doh_url resolved host ip
    DNS_CHOICE=""; CUSTOM_DOH_URL=""; CUSTOM_DOH_HOST=""; CUSTOM_DOH_IP=""
    tty="/dev/tty"; [[ -r /dev/tty ]] || tty=""
    while true; do
        echo
        msg_inf '────────────────────────────────────────────────────────────────────────────────'
        msg_inf 'Настройка DNS в X-UI:'
        echo '  1) Оставить по умолчанию'
        echo '  2) DoH Cloudflare'
        echo '  3) DoH Google'
        echo '  4) DoH Quad9'
        echo '  5) Использовать self-hosted DoH'
        echo '  6) Свой DoH'
        msg_inf '────────────────────────────────────────────────────────────────────────────────'
        echo -en 'Выбор [1-6]: '
        if [[ -n "$tty" ]]; then read -r ans <"$tty" || ans=""; else read -r ans || ans=""; fi
        mapped=$(normalize_dns_choice "$ans")
        [[ -n "$mapped" ]] || continue
        if [[ "$mapped" == "5" && "${DEPLOY_AGH:-}" != "1" ]]; then
            choose_adguard
            [[ "${DEPLOY_AGH}" == "1" ]] || continue
        fi
        if [[ "$mapped" == "6" ]]; then
            echo -en 'DoH URL: '
            if [[ -n "$tty" ]]; then read -r doh_url <"$tty" || doh_url=""; else read -r doh_url || doh_url=""; fi
            resolved=$(resolve_doh_for_hosts "$doh_url" 2>/dev/null || true)
            [[ -n "$resolved" ]] || continue
            host=$(printf '%s\n' "$resolved" | sed -n '1p')
            ip=$(printf '%s\n' "$resolved" | sed -n '2p')
            CUSTOM_DOH_URL=$(printf '%s\n' "$resolved" | sed -n '3p')
            CUSTOM_DOH_HOST="$host"; CUSTOM_DOH_IP="$ip"
        fi
        DNS_CHOICE="$mapped"; break
    done
    echo
}
apply_xray_template() {
    [[ ! -f $XUIDB ]] && { msg_err "x-ui.db not found — cannot apply Xray template."; return 1; }
    local qstrat freedom_strat enable_he=0 dns_mode="${DNS_CHOICE:-1}"
    if [[ "$dns_mode" != "1" ]]; then
        if has_global_ipv6; then qstrat='UseIP'; freedom_strat='ForceIP'; enable_he=1
        else qstrat='UseIPv4'; freedom_strat='ForceIPv4'; fi
    fi
    [[ -z "${IP4:-}" ]] && get_server_ip
    x-ui stop >/dev/null 2>&1 || true; sleep 2; pkill -x x-ui >/dev/null 2>&1 || true; sleep 1
    python3 - "$XUIDB" "$dns_mode" "$qstrat" "$freedom_strat" "$enable_he" \
        "${domain:-}" "${IP4:-}" "${CUSTOM_DOH_URL:-}" "${CUSTOM_DOH_HOST:-}" "${CUSTOM_DOH_IP:-}" <<'PY'
import json, sqlite3, sys
db, choice, qstrat, freedom_strat, enable_he, domain, ip4, custom_url, custom_host, custom_ip = sys.argv[1:11]
GH = "https://github.com"; RAW = "https://raw.githubusercontent.com"
GEODATA = {"cron": "0 4 * * 0", "assets": [
    {"url": GH + "/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat", "file": "geoip.dat"},
    {"url": GH + "/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat", "file": "geosite.dat"},
    {"url": GH + "/chocolate4u/Iran-v2ray-rules/releases/latest/download/geoip.dat", "file": "geoip_IR.dat"},
    {"url": GH + "/chocolate4u/Iran-v2ray-rules/releases/latest/download/geosite.dat", "file": "geosite_IR.dat"},
    {"url": GH + "/runetfreedom/russia-v2ray-rules-dat/releases/latest/download/geoip.dat", "file": "geoip_RU.dat"},
    {"url": GH + "/runetfreedom/russia-v2ray-rules-dat/releases/latest/download/geosite.dat", "file": "geosite_RU.dat"},
    {"url": GH + "/hydraponique/roscomvpn-geoip/releases/latest/download/geoip.dat", "file": "geoip_ROSCOM.dat"},
    {"url": GH + "/hydraponique/roscomvpn-geosite/releases/latest/download/geosite.dat", "file": "geosite_ROSCOM.dat"},
    {"url": RAW + "/runetfreedom/russia-v2ray-rules-dat/release/geoip.dat", "file": "geoip_RUNET.dat"},
    {"url": RAW + "/runetfreedom/russia-v2ray-rules-dat/release/geosite.dat", "file": "geosite_RUNET.dat"},
]}
DEFAULT = {"api": {"services": ["HandlerService", "LoggerService", "StatsService", "RoutingService"], "tag": "api"},
    "inbounds": [{"listen": "127.0.0.1", "port": 62789, "protocol": "tunnel", "settings": {"rewriteAddress": "127.0.0.1"}, "tag": "api"}],
    "log": {"access": "none", "dnsLog": False, "error": "", "loglevel": "warning", "maskAddress": ""},
    "metrics": {"listen": "127.0.0.1:11111", "tag": "metrics_out"},
    "outbounds": [{"protocol": "freedom", "tag": "direct", "settings": {"finalRules": [{"action": "block", "ip": ["geoip:private"]}, {"action": "allow"}]}},
                  {"protocol": "blackhole", "settings": {}, "tag": "blocked"}],
    "policy": {"levels": {"0": {"statsUserDownlink": True, "statsUserUplink": True}},
               "system": {"statsInboundDownlink": True, "statsInboundUplink": True, "statsOutboundDownlink": False, "statsOutboundUplink": False}},
    "routing": {"domainStrategy": "AsIs", "rules": [{"inboundTag": ["api"], "outboundTag": "api", "type": "field"},
        {"ip": ["geoip:private"], "outboundTag": "blocked", "type": "field"},
        {"outboundTag": "blocked", "protocol": ["bittorrent"], "type": "field"}]},
    "stats": {}, "geodata": GEODATA}
profiles = {"2": {"address": "https://cloudflare-dns.com/dns-query", "hosts": {"cloudflare-dns.com": "1.1.1.1", "one.one.one.one": "1.1.1.1"}},
            "3": {"address": "https://dns.google/dns-query", "hosts": {"dns.google": "8.8.8.8"}},
            "4": {"address": "https://dns.quad9.net/dns-query", "hosts": {"dns.quad9.net": "9.9.9.9"}}}
if choice == "5":
    if not domain or not ip4: raise SystemExit("self-hosted DoH needs panel domain and IP")
    profiles["5"] = {"address": "https://%s/dns-query" % domain, "hosts": {domain: ip4}}
if choice == "6":
    if not custom_url or not custom_host or not custom_ip: raise SystemExit("custom DoH missing host/ip")
    profiles["6"] = {"address": custom_url, "hosts": {custom_host: custom_ip}}
def unwrap(cfg):
    for _ in range(8):
        if not isinstance(cfg, dict): return cfg
        if "xraySetting" in cfg and not any(k in cfg for k in ("inbounds", "outbounds", "routing", "dns", "api")):
            inner = cfg["xraySetting"]
            cfg = json.loads(inner) if isinstance(inner, str) else inner if isinstance(inner, dict) else cfg
        else: return cfg
    return cfg
def set_direct_freedom_strategy(cfg, strategy, he):
    outs = cfg.get("outbounds")
    if not isinstance(outs, list): outs = []; cfg["outbounds"] = outs
    direct = None; tag_taken = False
    for ob in outs:
        if not isinstance(ob, dict): continue
        if str(ob.get("protocol", "")).lower() == "freedom" and ob.get("tag") == "direct":
            direct = ob; break
        if ob.get("tag") == "direct": tag_taken = True
    if direct is None:
        if tag_taken: raise SystemExit("tag 'direct' is taken by a non-freedom outbound")
        direct = {"protocol": "freedom", "tag": "direct", "settings": {}}; outs.append(direct)
    settings = direct.get("settings") if isinstance(direct.get("settings"), dict) else {}
    for k in ("domainStrategy", "targetStrategy"): settings.pop(k, None)
    direct["settings"] = settings; direct.pop("targetStrategy", None)
    stream = direct.get("streamSettings") if isinstance(direct.get("streamSettings"), dict) else {}
    sockopt = stream.get("sockopt") if isinstance(stream.get("sockopt"), dict) else {}
    if strategy == "AsIs": sockopt.pop("domainStrategy", None)
    else: sockopt["domainStrategy"] = strategy
    if he: sockopt["happyEyeballs"] = {"tryDelayMs": 250, "prioritizeIPv6": False, "interleave": 1, "maxConcurrentTry": 4}
    else: sockopt.pop("happyEyeballs", None)
    if sockopt: stream["sockopt"] = sockopt
    else: stream.pop("sockopt", None)
    if stream: direct["streamSettings"] = stream
    else: direct.pop("streamSettings", None)
con = sqlite3.connect(db, timeout=30); cur = con.cursor()
row = cur.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig' ORDER BY id DESC LIMIT 1").fetchone()
if row and row[0]:
    try:
        cfg = json.loads(row[0])
        if isinstance(cfg, str): cfg = json.loads(cfg)
    except Exception:
        cfg = json.loads(json.dumps(DEFAULT))
else:
    cfg = json.loads(json.dumps(DEFAULT))
cfg = unwrap(cfg)
if not isinstance(cfg, dict): cfg = json.loads(json.dumps(DEFAULT))
geo = cfg.get("geodata") if isinstance(cfg.get("geodata"), dict) else {}
assets = geo.get("assets") if isinstance(geo.get("assets"), list) else []
have = {a.get("file") for a in assets if isinstance(a, dict)}
for extra in GEODATA["assets"]:
    if extra["file"] not in have:
        assets.append(extra); have.add(extra["file"])
geo["assets"] = assets
cron = str(geo.get("cron") or "").strip()
geo["cron"] = cron if cron else "0 4 * * 0"
cfg["geodata"] = geo
if choice != "1":
    prof = profiles[choice]
    routing = cfg.get("routing") if isinstance(cfg.get("routing"), dict) else {}
    routing["domainStrategy"] = "IPIfNonMatch"; cfg["routing"] = routing
    set_direct_freedom_strategy(cfg, freedom_strat, enable_he == "1")
    cfg["dns"] = {"tag": "dns_inbound", "queryStrategy": qstrat, "disableCache": False, "disableFallback": True, "disableFallbackIfMatch": True,
        "useSystemHosts": False, "enableParallelQuery": False, "serveStale": False, "serveExpiredTTL": 0,
        "hosts": prof["hosts"],
        "servers": [{"address": prof["address"], "skipFallback": True, "queryStrategy": qstrat, "timeoutMs": 4000}]}
    cfg["fakedns"] = None
val = json.dumps(cfg, ensure_ascii=False, indent=2)
cur.execute("DELETE FROM settings WHERE key='xrayTemplateConfig'")
cur.execute("INSERT INTO settings (key, value) VALUES ('xrayTemplateConfig', ?)", (val,))
con.commit()
try:
    cur.execute("PRAGMA wal_checkpoint(TRUNCATE)"); con.commit()
except Exception:
    pass
con.close()
print("ok")
PY
    rc=$?; x-ui start >/dev/null 2>&1 || true
    [[ $rc -eq 0 ]] || { msg_err "Failed to write xrayTemplateConfig (DNS/geodata)."; return 1; }
    msg_ok "Xray template saved (DNS ${dns_mode})."
}
apply_xray_dns() { apply_xray_template; }

choose_hysteria2() {
    local ans mapped tty p
    DEPLOY_HY2=""
    hy2_port=""
    tty="/dev/tty"
    [[ -r /dev/tty ]] || tty=""
    while true; do
        echo
        msg_inf '────────────────────────────────────────────────────────────────────────────────'
        msg_inf 'Ставить Hysteria2?'
        echo '  1) Да'
        echo '  2) Нет'
        msg_inf '────────────────────────────────────────────────────────────────────────────────'
        echo -en 'Выбор [1-2]: '
        if [[ -n "$tty" ]]; then
            read -r ans <"$tty" || ans=""
        else
            read -r ans || ans=""
        fi
        mapped=$(echo "$ans" | tr -d '[:space:]')
        case "$mapped" in
            1) DEPLOY_HY2="1"; break ;;
            2) DEPLOY_HY2="2"; break ;;
        esac
    done
    if [[ "$DEPLOY_HY2" != "1" ]]; then
        echo
        return 0
    fi
    while true; do
        echo
        msg_inf '────────────────────────────────────────────────────────────────────────────────'
        msg_inf 'Выберите порт для Hysteria2:'
        msg_inf '────────────────────────────────────────────────────────────────────────────────'
        echo -en 'Порт: '
        if [[ -n "$tty" ]]; then
            read -r p <"$tty" || p=""
        else
            read -r p || p=""
        fi
        p=$(echo "$p" | tr -d '[:space:]')
        if [[ ! "$p" =~ ^[0-9]+$ ]] || (( p < 1 || p > 65535 )); then
            msg_err "Некорректный порт."
            continue
        fi
        case "$p" in
            80|46000|56000|56001|56003)
                msg_err "Порт ${p} занят."
                continue
                ;;
        esac
        if ss -Hlnu "sport = :$p" 2>/dev/null | grep -q .; then
            msg_err "Порт ${p} занят."
            continue
        fi
        hy2_port="$p"
        break
    done
    echo
}

insert_hy2_inbound() {
    [[ "${DEPLOY_HY2}" == "1" && -n "${hy2_port}" ]] || return 0
    [[ ! -f $XUIDB ]] && { msg_err "x-ui.db not found — cannot add Hysteria2 inbound."; return 1; }
    local salamander_pass gid_col gid_hy2 flag
    salamander_pass=$(gen_random_string 16 | tr '[:upper:]' '[:lower:]')
    flag="${EMOJI_FLAG:-}"
    if [[ -z "$flag" ]]; then
        flag=$(LC_ALL=en_US.UTF-8 curl -s --max-time 10 https://ipwho.is/ | jq -r '.flag.emoji' 2>/dev/null)
        [[ -z "$flag" || "$flag" == "null" ]] && flag="🌐"
    fi
    gid_col=""
    gid_hy2=""
    if sqlite3 "$XUIDB" "PRAGMA table_info(hosts);" | grep -qw "group_id"; then
        gid_col='"group_id",'
        gid_hy2="'$(gen_group_id)',"
    fi
    python3 - "$XUIDB" "$hy2_port" "$salamander_pass" "$domain" "$gid_col" "$gid_hy2" "$flag" <<'PY'
import json, sqlite3, sys
db, port, salamander, domain, gid_col, gid_hy2, flag = sys.argv[1:8]
port = int(port)
remark = ("%s hy2" % flag).strip()
cert = "/root/cert/%s/fullchain.pem" % domain
key = "/root/cert/%s/privkey.pem" % domain
settings = {"version": 2, "clients": []}
stream = {
    "network": "hysteria",
    "security": "tls",
    "hysteriaSettings": {"version": 2, "udpIdleTimeout": 60},
    "tlsSettings": {
        "serverName": domain,
        "minVersion": "1.2",
        "maxVersion": "1.3",
        "cipherSuites": "",
        "rejectUnknownSni": False,
        "disableSystemRoot": False,
        "enableSessionResumption": False,
        "alpn": ["h3"],
        "certificates": [{
            "useFile": True,
            "certificateFile": cert,
            "keyFile": key,
            "certificate": [],
            "key": [],
            "ocspStapling": 0,
            "oneTimeLoading": False,
            "usage": "encipherment",
            "buildChain": False
        }],
        "settings": {
            "fingerprint": "firefox",
            "echConfigList": "",
            "pinnedPeerCertSha256": [],
            "verifyPeerCertByName": ""
        }
    },
    "finalmask": {
        "tcp": [],
        "udp": [{"type": "salamander", "settings": {"password": salamander}}]
    }
}
sniffing = {"enabled": True, "destOverride": ["http", "tls", "quic", "fakedns"], "metadataOnly": False, "routeOnly": False}
tag = "inbound-%s" % port
con = sqlite3.connect(db, timeout=30)
cur = con.cursor()
row = cur.execute("SELECT id FROM inbounds WHERE protocol='hysteria' OR tag=? LIMIT 1", (tag,)).fetchone()
if row:
    con.close()
    print("exists")
    raise SystemExit(0)
cur.execute(
    "INSERT INTO inbounds (user_id, up, down, total, remark, enable, expiry_time, listen, port, protocol, settings, stream_settings, tag, sniffing) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (1, 0, 0, 0, remark, 1, 0, "", port, "hysteria",
     json.dumps(settings, ensure_ascii=False),
     json.dumps(stream, ensure_ascii=False),
     tag, json.dumps(sniffing, ensure_ascii=False)),
)
inbound_id = cur.lastrowid
cols = '"inbound_id",%s"sort_order","remark","address","port","security","fingerprint","alpn"' % gid_col
vals = [inbound_id]
if gid_col:
    vals.append(gid_hy2.strip("',"))
vals.extend([0, "hy2", domain, port, "same", "", "[]"])
placeholders = ",".join(["?"] * len(vals))
cur.execute("INSERT INTO hosts (%s) VALUES (%s)" % (cols, placeholders), vals)
con.commit()
con.close()
print("ok", port)
PY
    [[ $? -eq 0 ]] || { msg_err "Failed to insert Hysteria2 inbound."; return 1; }
    msg_ok "Inbound Hysteria2 created (UDP ${hy2_port})."
}

choose_extra_inbound() {
    local ans mapped tty
    EXTRA_INBOUND=""
    tty="/dev/tty"
    [[ -r /dev/tty ]] || tty=""
    while true; do
        echo
        msg_inf '────────────────────────────────────────────────────────────────────────────────'
        msg_inf 'Дополнительные инбаунды (только один):'
        echo '  1) Без дополнительного инбаунда'
        echo '  2) qWDTT'
        echo '  3) CSQTT'
        msg_inf '────────────────────────────────────────────────────────────────────────────────'
        echo -en 'Выбор [1-3]: '
        if [[ -n "$tty" ]]; then
            read -r ans <"$tty" || ans=""
        else
            read -r ans || ans=""
        fi
        mapped=$(echo "$ans" | tr -d '[:space:]')
        case "$mapped" in
            1|2|3) EXTRA_INBOUND="$mapped"; break ;;
        esac
    done
    echo
}

install_shareonly_client_sync() {
    [[ ! -f $XUIDB ]] && return 0
    sqlite3 "$XUIDB" <<'SQL'
DROP TRIGGER IF EXISTS lucx_shareonly_clients_ins;
DROP TRIGGER IF EXISTS lucx_shareonly_clients_del;
CREATE TRIGGER lucx_shareonly_clients_ins
AFTER INSERT ON client_inbounds
WHEN EXISTS (SELECT 1 FROM inbounds WHERE id = NEW.inbound_id AND protocol IN ('qwdtt','csqtt'))
BEGIN
  UPDATE inbounds SET settings = json_insert(
    json_set(
      settings,
      '$.clients',
      CASE WHEN json_type(json_extract(settings, '$.clients')) = 'array'
           THEN json_extract(settings, '$.clients')
           ELSE json('[]') END
    ),
    '$.clients[#]',
    json_object(
      'email', COALESCE((SELECT email FROM clients WHERE id = NEW.client_id), ''),
      'enable', json('true')
    )
  )
  WHERE id = NEW.inbound_id
    AND COALESCE((SELECT email FROM clients WHERE id = NEW.client_id), '') != ''
    AND NOT EXISTS (
      SELECT 1 FROM json_each(
        CASE WHEN json_type(json_extract(inbounds.settings, '$.clients')) = 'array'
             THEN json_extract(inbounds.settings, '$.clients')
             ELSE json('[]') END
      )
      WHERE json_extract(value, '$.email') = (SELECT email FROM clients WHERE id = NEW.client_id)
    );
END;
CREATE TRIGGER lucx_shareonly_clients_del
AFTER DELETE ON client_inbounds
WHEN EXISTS (SELECT 1 FROM inbounds WHERE id = OLD.inbound_id AND protocol IN ('qwdtt','csqtt'))
BEGIN
  UPDATE inbounds SET settings = json_set(
    settings,
    '$.clients',
    (
      SELECT json_group_array(json(value))
      FROM json_each(
        CASE WHEN json_type(json_extract(inbounds.settings, '$.clients')) = 'array'
             THEN json_extract(inbounds.settings, '$.clients')
             ELSE json('[]') END
      )
      WHERE json_extract(value, '$.email') != (SELECT email FROM clients WHERE id = OLD.client_id)
         OR (SELECT email FROM clients WHERE id = OLD.client_id) IS NULL
    )
  )
  WHERE id = OLD.inbound_id;
END;
SQL
}

insert_extra_inbound() {
    [[ "${EXTRA_INBOUND:-1}" == "1" ]] && return 0
    [[ ! -f $XUIDB ]] && { msg_err "x-ui.db not found — cannot add extra inbound."; return 1; }
    [[ -z "${IP4:-}" ]] && get_server_ip
    local proto remark port listen_addr sub_host pass web_pass
    pass=$(gen_random_string 16)
    web_pass=$(gen_random_string 24)
    if [[ "$EXTRA_INBOUND" == "2" ]]; then
        proto='qwdtt'; remark='qWDTT'; port=56000
        listen_addr='0.0.0.0:56000'
        sub_host="${IP4}:56000"
    else
        proto='csqtt'; remark='CSQTT'; port=46000
        listen_addr='0.0.0.0:46000'
        sub_host="${IP4}"
    fi
    python3 - "$XUIDB" "$proto" "$remark" "$port" "$listen_addr" "$sub_host" "$pass" "$web_pass" <<'PY'
import json, sqlite3, sys
db, proto, remark, port, listen_addr, sub_host, password, web_pass = sys.argv[1:9]
port = int(port)
if proto == "qwdtt":
    settings = {
        "clients": [],
        "remark": remark,
        "enabled": True,
        "listenAddr": listen_addr,
        "wgPort": 56001,
        "password": password,
        "dns": "8.8.8.8",
        "configDir": "",
        "listenRaw": "0.0.0.0:56003",
        "listenDirect": "",
        "subHost": sub_host,
        "vkHashes": "",
        "clientPort": 9000,
        "workers": 16,
        "routeThroughXray": True,
        "outboundTag": ""
    }
else:
    settings = {
        "clients": [],
        "remark": remark,
        "enabled": True,
        "listenAddr": listen_addr,
        "password": password,
        "deviceId": "",
        "webPass": web_pass,
        "subHost": sub_host,
        "vkHashes": "",
        "configDir": "",
        "routeThroughXray": True,
        "outboundTag": ""
    }
stream = {"security": "none"}
sniffing = {"enabled": True, "destOverride": ["http", "tls", "quic", "fakedns"], "metadataOnly": False, "routeOnly": False}
tag = "inbound-qwdtt" if proto == "qwdtt" else "inbound-csqtt"
con = sqlite3.connect(db, timeout=30)
cur = con.cursor()
row = cur.execute("SELECT id FROM inbounds WHERE protocol=? OR tag=? LIMIT 1", (proto, tag)).fetchone()
if row:
    con.close()
    print("exists")
    raise SystemExit(0)
cur.execute(
    "INSERT INTO inbounds (user_id, up, down, total, remark, enable, expiry_time, listen, port, protocol, settings, stream_settings, tag, sniffing) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (1, 0, 0, 0, remark, 1, 0, "", port, proto,
     json.dumps(settings, ensure_ascii=False),
     json.dumps(stream, ensure_ascii=False),
     tag, json.dumps(sniffing, ensure_ascii=False)),
)
con.commit()
con.close()
print("ok", proto, port)
PY
    [[ $? -eq 0 ]] || { msg_err "Failed to insert ${remark} inbound."; return 1; }
    install_shareonly_client_sync
    msg_ok "Inbound ${remark} created (port ${port})."
}

choose_adguard() {
    local ans mapped tty
    DEPLOY_AGH=""
    tty="/dev/tty"
    [[ -r /dev/tty ]] || tty=""
    while true; do
        echo
        msg_inf '────────────────────────────────────────────────────────────────────────────────'
        msg_inf 'Развернуть self-hosted DNS-over-HTTPS (DoH) на домене панели?'
        echo '  1) Да'
        echo '  2) Нет'
        msg_inf '────────────────────────────────────────────────────────────────────────────────'
        echo -en 'Выбор [1-2]: '
        if [[ -n "$tty" ]]; then read -r ans <"$tty" || ans=""; else read -r ans || ans=""; fi
        mapped=$(echo "$ans" | tr -d '[:space:]')
        case "$mapped" in
            1|2) DEPLOY_AGH="$mapped"; break ;;
        esac
    done
    echo
}
uninstall_adguard() {
    systemctl stop AdGuardHome 2>/dev/null || true
    [[ -x /opt/AdGuardHome/AdGuardHome ]] && /opt/AdGuardHome/AdGuardHome -s uninstall 2>/dev/null || true
    rm -rf /opt/AdGuardHome /etc/nginx/snippets/adguard.conf /root/.lucx-adguard-info
    for f in /etc/nginx/sites-available/*; do
        [[ -f "$f" ]] || continue
        sed -i '\|snippets/adguard.conf|d' "$f"
    done
    nginx -t &>/dev/null && systemctl reload nginx
    msg_ok "AdGuard Home removed."
}
install_adguard() {
    local AGH_DIR=/opt/AdGuardHome AGH_YAML=/opt/AdGuardHome/AdGuardHome.yaml
    local AGH_SNIPPET=/etc/nginx/snippets/adguard.conf AGH_SERVICE=AdGuardHome
    local vhost agh_path agh_web_port agh_dns_port agh_arch agh_user agh_pass agh_hash
    local new_credentials=0 agh_up=0 GH p
    GH='https://github.com'
    [[ -f $XUIDB ]] || { msg_err "x-ui.db not found — install the panel first."; return 1; }
    command -v sqlite3 >/dev/null || apt-get install -y -q sqlite3
    if [[ -z "${domain:-}" ]]; then
        local web_cert
        web_cert=$(sqlite3 "$XUIDB" "SELECT value FROM settings WHERE key='webCertFile';" 2>/dev/null || true)
        if [[ "$web_cert" =~ /root/cert/([^/]+)/ || "$web_cert" =~ /etc/letsencrypt/live/([^/]+)/ ]]; then domain="${BASH_REMATCH[1]}"; fi
    fi
    if [[ -z "${domain:-}" ]]; then
        for f in /etc/nginx/sites-available/*; do
            [[ -f "$f" ]] || continue
            case "$(basename "$f")" in 80.conf|00-maps.conf) continue;; esac
            if grep -q 'listen 7443' "$f" 2>/dev/null; then domain=$(awk '/server_name/{print $2; exit}' "$f" | tr -d ';'); break; fi
        done
    fi
    [[ -n "${domain:-}" ]] || { msg_err "Could not determine panel domain."; return 1; }
    [[ -z "${IP4:-}" ]] && get_server_ip
    vhost="/etc/nginx/sites-available/${domain}"
    if [[ ! -f "$vhost" ]] || ! grep -q 'listen 7443' "$vhost"; then
        vhost=""
        for f in /etc/nginx/sites-available/*; do
            [[ -f "$f" ]] || continue
            grep -q 'listen 7443' "$f" 2>/dev/null && { vhost="$f"; break; }
        done
    fi
    [[ -n "$vhost" ]] || { msg_err "Panel vhost (listen 7443) not found."; return 1; }
    agh_path=""
    [[ -f "$AGH_SNIPPET" ]] && agh_path=$(grep -oP 'location /\Kadg-[a-zA-Z0-9]+' "$AGH_SNIPPET" | head -1 || true)
    [[ -n "$agh_path" ]] || agh_path="adg-$(gen_random_string 12)"
    agh_web_port=""
    [[ -f "$AGH_YAML" ]] && agh_web_port=$(grep -oP '^\s*address:\s*127\.0\.0\.1:\K\d+' "$AGH_YAML" | head -1 || true)
    if [[ -z "$agh_web_port" ]]; then
        while true; do p=$(( ((RANDOM<<15)|RANDOM) % 49152 + 10000 )); ss -Hln "sport = :$p" 2>/dev/null | grep -q . || { agh_web_port="$p"; break; }; done
    fi
    while true; do p=$(( ((RANDOM<<15)|RANDOM) % 49152 + 10000 )); ss -Hln "sport = :$p" 2>/dev/null | grep -q . || { agh_dns_port="$p"; break; }; done
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q curl tar ca-certificates apache2-utils
    case "$(uname -m)" in x86_64) agh_arch="amd64";; aarch64) agh_arch="arm64";; armv7l) agh_arch="armv7";; *) msg_err "Unsupported architecture"; return 1;; esac
    if [[ ! -x "${AGH_DIR}/AdGuardHome" ]]; then
        mkdir -p /opt
        curl -fsSL "${GH}/AdguardTeam/AdGuardHome/releases/latest/download/AdGuardHome_linux_${agh_arch}.tar.gz" | tar -xz -C /opt
        [[ -x "${AGH_DIR}/AdGuardHome" ]] || { msg_err "AdGuard Home download failed."; return 1; }
    fi
    agh_user="admin"; agh_pass=""
    if [[ -f "$AGH_YAML" ]]; then new_credentials=0
    else
        new_credentials=1
        agh_pass=$(gen_random_string 20)
        agh_hash=$(htpasswd -nbB x "$agh_pass" | cut -d: -f2)
        [[ "$agh_hash" == \$2* ]] || { msg_err "bcrypt hash generation failed."; return 1; }
        systemctl stop "$AGH_SERVICE" 2>/dev/null || true
        mkdir -p "$AGH_DIR"
        cat > "$AGH_YAML" <<EOF
http:
  address: 127.0.0.1:${agh_web_port}
users:
  - name: ${agh_user}
    password: ${agh_hash}
auth_attempts: 5
block_auth_min: 15
theme: auto
dns:
  bind_hosts:
    - 127.0.0.1
  port: ${agh_dns_port}
  upstream_dns:
    - https://dns.cloudflare.com/dns-query
    - https://dns.google/dns-query
    - https://dns.quad9.net/dns-query
  bootstrap_dns:
    - 1.1.1.1
    - 8.8.8.8
    - 9.9.9.9
  trusted_proxies:
    - 127.0.0.0/8
tls:
  enabled: false
  allow_unencrypted_doh: true
filters:
  - enabled: true
    url: https://adguardteam.github.io/HostlistsRegistry/assets/filter_1.txt
    name: AdGuard DNS filter
    id: 1
schema_version: 28
EOF
        chmod 600 "$AGH_YAML"
        umask 077
        cat > /root/.lucx-adguard-info <<INFO
AGH_USER=${agh_user}
AGH_PASS=${agh_pass}
AGH_PATH=${agh_path}
AGH_DOMAIN=${domain}
AGH_IP=${IP4}
INFO
        chmod 600 /root/.lucx-adguard-info
    fi
    if [[ -f /root/.lucx-adguard-info ]]; then
        grep -q '^AGH_PATH=' /root/.lucx-adguard-info 2>/dev/null || echo "AGH_PATH=${agh_path}" >> /root/.lucx-adguard-info
        grep -q '^AGH_DOMAIN=' /root/.lucx-adguard-info 2>/dev/null || echo "AGH_DOMAIN=${domain}" >> /root/.lucx-adguard-info
        grep -q '^AGH_IP=' /root/.lucx-adguard-info 2>/dev/null || echo "AGH_IP=${IP4}" >> /root/.lucx-adguard-info
    fi
    if systemctl list-unit-files --type=service 2>/dev/null | grep -q "^${AGH_SERVICE}\.service"; then systemctl restart "$AGH_SERVICE"
    else "${AGH_DIR}/AdGuardHome" -s install; fi
    for _ in $(seq 1 20); do curl -fso /dev/null "http://127.0.0.1:${agh_web_port}/" && { agh_up=1; break; }; sleep 0.5; done
    [[ $agh_up -eq 1 ]] || { msg_err "AdGuard Home did not start"; return 1; }
    mkdir -p /etc/nginx/snippets
    cat > "$AGH_SNIPPET" <<EOF
    location /dns-query {
        proxy_pass http://127.0.0.1:${agh_web_port};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        access_log off;
    }
    location /${agh_path}/ {
        proxy_pass http://127.0.0.1:${agh_web_port}/;
        proxy_redirect / /${agh_path}/;
        proxy_cookie_path / /${agh_path}/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
    }
    location = /${agh_path} { return 302 /${agh_path}/; }
EOF
    if ! grep -q 'snippets/adguard.conf' "$vhost"; then
        if grep -q 'include /etc/nginx/snippets/includes.conf;' "$vhost"; then
            sed -i 's|^\(\s*\)include /etc/nginx/snippets/includes.conf;|\1include /etc/nginx/snippets/adguard.conf;\n\1include /etc/nginx/snippets/includes.conf;|' "$vhost"
        else
            sed -i '$ s|^}$|    include /etc/nginx/snippets/adguard.conf;\n}|' "$vhost"
        fi
    fi
    nginx -t 2>&1 | grep -q successful && systemctl reload nginx
    AGH_PATH="$agh_path"; AGH_USER="$agh_user"; AGH_PASS="$agh_pass"
    msg_ok "AdGuard Home installed."
}
print_adguard_results() {
    local agh_path agh_user agh_pass agh_domain agh_ip H
    H='https://'
    [[ -f /etc/nginx/snippets/adguard.conf ]] || return 0
    agh_domain="${domain:-}"; agh_ip="${IP4:-}"; agh_path=""; agh_user="admin"; agh_pass=""
    if [[ -f /root/.lucx-adguard-info ]]; then
        . /root/.lucx-adguard-info
        agh_path="${AGH_PATH:-$agh_path}"; agh_user="${AGH_USER:-$agh_user}"
        agh_pass="${AGH_PASS:-}"; agh_domain="${AGH_DOMAIN:-$agh_domain}"; agh_ip="${AGH_IP:-$agh_ip}"
    fi
    [[ -z "$agh_path" ]] && agh_path=$(grep -oP 'location /\Kadg-[a-zA-Z0-9]+' /etc/nginx/snippets/adguard.conf | head -1 || true)
    [[ -n "$agh_domain" ]] || return 0
    echo
    msg_inf '────────────────────────────────────────────────────────────────────────────────'
    echo -e "AdGuard Home:  ${H}${agh_domain}/${agh_path}/\n"
    echo -e "Login:         ${agh_user}\n"
    if [[ -n "$agh_pass" ]]; then echo -e "Password:      ${agh_pass}\n"
    else echo -e "Password:      (already set / bcrypt in AdGuardHome.yaml)\n"; fi
    echo -e "DoH:           ${H}${agh_domain}/dns-query\n"
    echo -e "Hosts:         ${agh_domain} ${agh_ip}\n"
    msg_inf '────────────────────────────────────────────────────────────────────────────────'
}
# ─────────────────────────────────────────────────────────────────────────────
# UNINSTALL
# ─────────────────────────────────────────────────────────────────────────────
uninstall_xui() {
    printf 'y\n' | x-ui uninstall >/dev/null 2>&1 || true
    uninstall_adguard 2>/dev/null || true
    systemctl stop x-ui nginx mtr-backend AdGuardHome 2>/dev/null || true
    systemctl disable x-ui nginx mtr-backend AdGuardHome 2>/dev/null || true
    pkill -f 'mtg-linux-' >/dev/null 2>&1 || true
    crontab -l 2>/dev/null | grep -vE 'certbot|x-ui|cloudflareips|nginx -s reload|update-geodata' | crontab - || true
    rm -rf /etc/x-ui/ /usr/local/x-ui/ /usr/local/lib/3x-ui-pro/ /root/cert/ /opt/AdGuardHome /root/.lucx-adguard-info /var/www/diagnostics
    rm -f /usr/bin/x-ui /etc/systemd/system/x-ui.service /etc/systemd/system/mtr-backend.service /etc/default/x-ui /etc/nginx/snippets/adguard.conf
    $Pak -y remove nginx nginx-common nginx-core nginx-full python3-certbot-nginx
    $Pak -y purge  nginx nginx-common nginx-core nginx-full python3-certbot-nginx
    $Pak -y autoremove
    $Pak -y autoclean
    rm -rf /var/www/html/ /var/www/diagnostics/ /var/www/subpage/ /etc/nginx/ /usr/share/nginx/
    systemctl daemon-reload 2>/dev/null || true
}

if [[ ${UNINSTALL} == *"y"* ]]; then
    uninstall_xui
    clear && msg_ok "Completely Uninstalled!" && exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
# GET SERVER IP
# ─────────────────────────────────────────────────────────────────────────────
IP4_REGEX="^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$"
IP6_REGEX="([a-f0-9:]+:+)+[a-f0-9]+"

get_server_ip() {
    IP4=$(ip route get 8.8.8.8 2>&1 | grep -Po -- 'src \K\S*')
    IP6=$(ip route get 2620:fe::fe 2>&1 | grep -Po -- 'src \K\S*')
    [[ $IP4 =~ $IP4_REGEX ]] || IP4=$(curl -s ipv4.icanhazip.com | tr -d '[:space:]')
    [[ $IP6 =~ $IP6_REGEX ]] || IP6=$(curl -s ipv6.icanhazip.com | tr -d '[:space:]')
}

# Early IP fetch for auto-domain
IP4=$(ip route get 8.8.8.8 2>&1 | grep -Po -- 'src \K\S*')
[[ $IP4 =~ $IP4_REGEX ]] || IP4=$(curl -s ipv4.icanhazip.com | tr -d '[:space:]')


# ─────────────────────────────────────────────────────────────────────────────
# DOMAIN VALIDATION
# ─────────────────────────────────────────────────────────────────────────────
validate_domains() {
    while true; do
        [[ -n "$domain" ]] && break
        echo -en "Enter available subdomain (sub.domain.tld): " && read -r domain
    done
    domain=$(echo "$domain" | tr -d '[:space:]')
    SubDomain=$(echo "$domain"   | sed 's/^[^ ]* \|\..*//g')
    MainDomain=$(echo "$domain"  | sed 's/.*\.\([^.]*\..*\)$/\1/')
    [[ "${SubDomain}.${MainDomain}" != "${domain}" ]] && MainDomain=${domain}

    while true; do
        [[ -n "$reality_domain" ]] && break
        echo -en "Enter available subdomain for REALITY (sub.domain.tld): " && read -r reality_domain
    done
    reality_domain=$(echo "$reality_domain" | tr -d '[:space:]')
    RealitySubDomain=$(echo "$reality_domain" | sed 's/^[^ ]* \|\..*//g')
    RealityMainDomain=$(echo "$reality_domain" | sed 's/.*\.\([^.]*\..*\)$/\1/')
    [[ "${RealitySubDomain}.${RealityMainDomain}" != "${reality_domain}" ]] && RealityMainDomain=${reality_domain}

    if [[ "$domain" == "$reality_domain" ]]; then
        msg_err "Panel domain and REALITY domain must be different! Got: ${domain}"
        exit 1
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# INSTALL PACKAGES
# ─────────────────────────────────────────────────────────────────────────────
install_packages() {
    ufw disable 2>/dev/null || true

    if [[ ${INSTALL} == *"y"* ]]; then
        local version
        version=$(grep -oP '(?<=VERSION_ID=")[0-9]+' /etc/os-release)
        [[ "$version" == "20" || "$version" == "22" ]] && echo "System: Ubuntu $version"

        $Pak -y update
        $Pak -y install curl wget jq bash sudo nginx-full certbot python3-certbot-nginx sqlite3 ufw netcat-openbsd mtr python3 libcap2-bin
        systemctl daemon-reload && systemctl enable --now nginx
    fi

    apt-get install -yqq --no-install-recommends ca-certificates
}

# ─────────────────────────────────────────────────────────────────────────────
# SSL CERTIFICATES
# ─────────────────────────────────────────────────────────────────────────────
get_ssl_certs() {
    systemctl stop nginx 2>/dev/null || true
    fuser -k 80/tcp 80/udp 443/tcp 443/udp 2>/dev/null || true

    if [[ ${AUTODOMAIN} == *"y"* ]]; then
        local resolve_ok=true
        for d in "$domain" "$reality_domain"; do
            local a
            a=$(getent ahostsv4 "$d" 2>/dev/null | awk 'NR==1{print $1}')
            if [[ "$a" != "$IP4" ]]; then
                msg_err "Auto-domain $d does not resolve to $IP4. Fix DNS and retry."
                resolve_ok=false
            fi
        done
        [[ $resolve_ok == false ]] && exit 1
    fi

    certbot certonly --standalone --non-interactive --agree-tos \
        --register-unsafely-without-email -d "$domain"
    if [[ ! -d "/etc/letsencrypt/live/${domain}/" ]]; then
        systemctl start nginx >/dev/null 2>&1
        msg_err "$domain SSL could not be generated! Check Domain/IP." && exit 1
    fi

    certbot certonly --standalone --non-interactive --agree-tos \
        --register-unsafely-without-email -d "$reality_domain"
    if [[ ! -d "/etc/letsencrypt/live/${reality_domain}/" ]]; then
        systemctl start nginx >/dev/null 2>&1
        msg_err "$reality_domain SSL could not be generated! Check Domain/IP." && exit 1
    fi

    mkdir -p /root/cert/${domain}
    chmod 755 /root/cert/*
    ln -sf /etc/letsencrypt/live/${domain}/fullchain.pem /root/cert/${domain}/fullchain.pem
    ln -sf /etc/letsencrypt/live/${domain}/privkey.pem   /root/cert/${domain}/privkey.pem
}

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURE NGINX
# ─────────────────────────────────────────────────────────────────────────────
configure_nginx() {
    mkdir -p /etc/nginx/stream-enabled /etc/nginx/snippets

    # nginx >= 1.25.1 deprecates "listen ... http2" in favor of "http2 on;";
    # older versions (Debian 12 / Ubuntu 24.04) don't know the new directive
    local ngx_ver http2_listen="" http2_on=""
    ngx_ver=$(nginx -v 2>&1 | grep -oP '[0-9]+\.[0-9]+\.[0-9]+' || echo 0)
    if [[ "$(printf '%s\n' 1.25.1 "$ngx_ver" | sort -V | head -1)" == "1.25.1" ]]; then
        http2_on="http2 on;"
    else
        http2_listen=" http2"
    fi

    # SNI-based stream: reality → 8443, domain → 7443
    cat > /etc/nginx/stream-enabled/stream.conf <<EOF
map \$ssl_preread_server_name \$sni_name {
    hostnames;
    ${reality_domain}    xray;
    ${domain}            www;
    default              xray;
}

upstream xray { server 127.0.0.1:8443; }
upstream www  { server 127.0.0.1:7443; }

server {
    proxy_protocol on;
    set_real_ip_from unix:;
    listen     443;
    listen     [::]:443;
    proxy_pass \$sni_name;
    ssl_preread on;
}
EOF

    grep -xqFR "stream { include /etc/nginx/stream-enabled/*.conf; }" /etc/nginx/* \
        || echo "stream { include /etc/nginx/stream-enabled/*.conf; }" >> /etc/nginx/nginx.conf
    grep -xqFR "load_module modules/ngx_stream_module.so;" /etc/nginx/* \
        || sed -i '1s/^/load_module \/usr\/lib\/nginx\/modules\/ngx_stream_module.so; /' /etc/nginx/nginx.conf
    grep -xqFR "worker_rlimit_nofile 16384;" /etc/nginx/* \
        || echo "worker_rlimit_nofile 16384;" >> /etc/nginx/nginx.conf
    sed -i "/worker_connections/c\worker_connections 4096;" /etc/nginx/nginx.conf

    # HTTP → HTTPS redirect
    cat > /etc/nginx/sites-available/80.conf <<EOF
server {
    listen 80;
    server_name ${domain} ${reality_domain};
    return 301 https://\$host\$request_uri;
}
EOF

    # Shared proxy locations for xray inbounds (included by both vhosts)
    cat > /etc/nginx/snippets/includes.conf <<EOF
    #Subscription — prefix location covers all sub-paths (assets, JS, etc.)
    location /${sub_path}/ {
        if (\$hack = 1) { return 404; }
        proxy_redirect off;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_pass https://127.0.0.1:${sub_port};
    }
    location = /${sub_path} {
        if (\$hack = 1) { return 404; }
        proxy_redirect off;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_pass https://127.0.0.1:${sub_port};
    }
    location /assets  { proxy_pass https://127.0.0.1:${sub_port}; }
    location /assets/ { proxy_pass https://127.0.0.1:${sub_port}; }

    #Subscription (json)
    location /${json_path} {
        if (\$hack = 1) { return 404; }
        proxy_redirect off;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_pass https://127.0.0.1:${sub_port};
    }
    location /${json_path}/ {
        if (\$hack = 1) { return 404; }
        proxy_redirect off;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_pass https://127.0.0.1:${sub_port};
    }

    #XHTTP
    location /${xhttp_path} {
        grpc_pass grpc://unix:/dev/shm/uds2023.sock;
        grpc_buffer_size      16k;
        grpc_socket_keepalive on;
        grpc_read_timeout     1h;
        grpc_send_timeout     1h;
        grpc_set_header Connection        "";
        grpc_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        grpc_set_header X-Forwarded-Proto \$scheme;
        grpc_set_header X-Forwarded-Port  \$server_port;
        grpc_set_header Host              \$host;
        grpc_set_header X-Forwarded-Host  \$host;
    }

    #Xray generic proxy (WS / gRPC by port+path)
    location ~ ^/(?<fwdport>\d+)/(?<fwdpath>.*)\$ {
        if (\$hack = 1) { return 404; }
        client_max_body_size 0;
        client_body_timeout 1d;
        grpc_read_timeout 1d;
        grpc_socket_keepalive on;
        proxy_read_timeout 1d;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_socket_keepalive on;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        if (\$content_type ~* "GRPC") {
            grpc_pass grpc://127.0.0.1:\$fwdport\$is_args\$args;
            break;
        }
        if (\$http_upgrade ~* "(WEBSOCKET|WS)") {
            proxy_pass http://127.0.0.1:\$fwdport\$is_args\$args;
            break;
        }
        if (\$request_method ~* ^(PUT|POST|GET)\$) {
            proxy_pass http://127.0.0.1:\$fwdport\$is_args\$args;
            break;
        }
    }

    location / { try_files \$uri \$uri/ =404; }
EOF

    # Main domain vhost (TLS termination at 7443, proxy_protocol)
    cat > "/etc/nginx/sites-available/${domain}" <<EOF
server {
    server_tokens off;
    server_name ${domain};
    listen 7443 ssl${http2_listen} proxy_protocol;
    listen [::]:7443 ssl${http2_listen} proxy_protocol;
    ${http2_on}
    index index.html index.htm index.php;
    root /var/www/html/;
    real_ip_header proxy_protocol;
    set_real_ip_from 127.0.0.1;
    # This vhost listens on 7443 behind the SNI stream (public port 443). Without
    # this, nginx bakes :7443 into redirect Location headers (return/error_page),
    # so browsers get sent to an unreachable port. Keep redirects relative.
    absolute_redirect off;
    # Larger h2 preread window improves single-stream upload throughput
    http2_body_preread_size 128k;
    client_body_buffer_size 512k;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!eNULL:!MD5:!DES:!RC4:!ADH:!SSLv3:!EXP:!PSK:!DSS;
    ssl_certificate     /etc/letsencrypt/live/${domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${domain}/privkey.pem;
    if (\$host !~* ^(.+\.)?${domain}\$)            { return 444; }
    if (\$scheme ~* https)                          { set \$safe 1; }
    if (\$ssl_server_name !~* ^(.+\.)?${domain}\$) { set \$safe "\${safe}0"; }
    if (\$safe = 10)                                { return 444; }
    if (\$request_uri ~ "(\"|'|\`|~|,|:|;|%|\\$|&&|\?\?|0x00|0X00|\||\\|\{|\}|\[|\]|<|>|\.\.\.|\.\.\/|\/\/\/)") { set \$hack 1; }
    error_page 400 401 402 403 500 501 502 503 504 =404 /404;
    proxy_intercept_errors on;

    location /${panel_path}/ {
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_pass https://127.0.0.1:${panel_port};
    }
    location /${panel_path} {
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_pass https://127.0.0.1:${panel_port};
    }

    include /etc/nginx/snippets/includes.conf;
}
EOF

    # Reality domain vhost (plain TLS at 9443, no proxy_protocol)
    cat > "/etc/nginx/sites-available/${reality_domain}" <<EOF
server {
    server_tokens off;
    server_name ${reality_domain};
    listen 9443 ssl${http2_listen};
    listen [::]:9443 ssl${http2_listen};
    ${http2_on}
    index index.html index.htm index.php;
    root /var/www/html/;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!eNULL:!MD5:!DES:!RC4:!ADH:!SSLv3:!EXP:!PSK:!DSS;
    ssl_certificate     /etc/letsencrypt/live/${reality_domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${reality_domain}/privkey.pem;
    if (\$host !~* ^(.+\.)?${reality_domain}\$)            { return 444; }
    if (\$scheme ~* https)                                  { set \$safe 1; }
    if (\$ssl_server_name !~* ^(.+\.)?${reality_domain}\$) { set \$safe "\${safe}0"; }
    if (\$safe = 10)                                        { return 444; }
    if (\$request_uri ~ "(\"|'|\`|~|,|:|;|%|\\$|&&|\?\?|0x00|0X00|\||\\|\{|\}|\[|\]|<|>|\.\.\.|\.\.\/|\/\/\/)") { set \$hack 1; }
    error_page 400 401 402 403 500 501 502 503 504 =404 /404;
    proxy_intercept_errors on;

    location /${panel_path}/ {
        proxy_redirect off;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_pass http://127.0.0.1:${panel_port};
    }
    location /${panel_path} {
        proxy_redirect off;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_pass http://127.0.0.1:${panel_port};
    }

    include /etc/nginx/snippets/includes.conf;
}
EOF

    # Activate configs
    if [[ -f "/etc/nginx/sites-available/${domain}" ]]; then
        rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-available/default
        ln -sf "/etc/nginx/sites-available/${domain}"          /etc/nginx/sites-enabled/
        ln -sf "/etc/nginx/sites-available/${reality_domain}"  /etc/nginx/sites-enabled/
        ln -sf "/etc/nginx/sites-available/80.conf"            /etc/nginx/sites-enabled/
    else
        msg_err "${domain} nginx config not found!" && exit 1
    fi

    if [[ $(nginx -t 2>&1 | grep -o 'successful') != "successful" ]]; then
        msg_err "nginx config check failed!" && exit 1
    fi

    systemctl start nginx
}

# ─────────────────────────────────────────────────────────────────────────────
# INSTALL PANEL (LucX-UI)
# ─────────────────────────────────────────────────────────────────────────────
_arch() {
    case "$(uname -m)" in
        x86_64|x64|amd64)          echo 'amd64'  ;;
        i*86|x86)                  echo '386'    ;;
        armv8*|armv8|arm64|aarch64) echo 'arm64' ;;
        armv7*|armv7|arm)          echo 'armv7'  ;;
        armv6*|armv6)              echo 'armv6'  ;;
        armv5*|armv5)              echo 'armv5'  ;;
        s390x)                     echo 's390x'  ;;
        *) echo "Unsupported CPU architecture!" && exit 1 ;;
    esac
}

_panel_initial_config() {
    /usr/local/x-ui/x-ui setting -username "asdfasdf" -password "asdfasdf" -port "2096" -webBasePath "asdfasdf"
    /usr/local/x-ui/x-ui migrate
}

install_panel() {
    local tag_version dest script_ref GH RAW
    GH='https://github.com'
    RAW='https://raw.githubusercontent.com'
    apt-get update && apt-get install -y -q wget curl tar tzdata
    cd /usr/local/
    if [[ -n "$PANEL_VERSION" && "$PANEL_VERSION" != "latest" ]]; then
        tag_version="${PANEL_VERSION#v}"
        tag_version="v${tag_version}"
        if ! curl -fsILo /dev/null "$GH/AlexeyLCP/lucx-ui/releases/download/${tag_version}/x-ui-linux-$(_arch).tar.gz" \
           && ! curl -4 -fsILo /dev/null "$GH/AlexeyLCP/lucx-ui/releases/download/${tag_version}/x-ui-linux-$(_arch).tar.gz"; then
            echo "LucX-UI release ${tag_version} not found." && exit 1
        fi
    else
        tag_version=$(curl -Ls "https://api.github.com/repos/AlexeyLCP/lucx-ui/releases/latest" | grep -m1 '"tag_name":' | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/')
        if [[ -z "$tag_version" || "$tag_version" == "null" ]]; then
            tag_version=$(curl -4 -Ls "https://api.github.com/repos/AlexeyLCP/lucx-ui/releases/latest" | grep -m1 '"tag_name":' | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/')
        fi
        if [[ -z "$tag_version" || "$tag_version" == "null" ]]; then
            echo "Failed to fetch LucX-UI version." && exit 1
        fi
    fi
    echo "Installing LucX-UI ${tag_version} ..."
    wget -N -O /usr/local/x-ui-linux-$(_arch).tar.gz "$GH/AlexeyLCP/lucx-ui/releases/download/${tag_version}/x-ui-linux-$(_arch).tar.gz"
    [[ $? -ne 0 ]] && echo "Download failed." && exit 1
    script_ref="${tag_version}"
    [[ "$script_ref" == "dev-latest" ]] && script_ref="main"
    wget -O /usr/bin/x-ui-temp "$RAW/AlexeyLCP/lucx-ui/${script_ref}/x-ui.sh"
    if [[ $? -ne 0 ]]; then wget -O /usr/bin/x-ui-temp "$RAW/AlexeyLCP/lucx-ui/main/x-ui.sh"; fi
    [[ $? -ne 0 ]] && echo "Failed to download x-ui.sh" && exit 1
    [[ -d /usr/local/x-ui/ ]] && systemctl stop x-ui 2>/dev/null; rm -rf /usr/local/x-ui/
    tar zxvf x-ui-linux-$(_arch).tar.gz
    rm -f x-ui-linux-$(_arch).tar.gz
    cd x-ui
    chmod +x x-ui x-ui.sh
    if [[ $(_arch) == "armv5" || $(_arch) == "armv6" || $(_arch) == "armv7" ]]; then
        mv bin/xray-linux-$(_arch) bin/xray-linux-arm32
        chmod +x bin/xray-linux-arm32
        if [[ -f bin/mtg-linux-$(_arch) ]]; then mv bin/mtg-linux-$(_arch) bin/mtg-linux-arm; chmod +x bin/mtg-linux-arm; fi
    fi
    chmod +x bin/* 2>/dev/null || true
    fetch_lucx_geofiles /usr/local/x-ui/bin || { echo "Failed to download LucX geodata."; exit 1; }
    mv -f /usr/bin/x-ui-temp /usr/bin/x-ui
    chmod +x /usr/bin/x-ui
    _panel_initial_config
    cp -f x-ui.service.debian /etc/systemd/system/x-ui.service
    systemctl daemon-reload
    systemctl enable x-ui
    systemctl start x-ui
    msg_ok "LucX-UI ${tag_version} installed."
}

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURE X-UI DATABASE
# ─────────────────────────────────────────────────────────────────────────────
configure_xui_db() {
    if [[ ! -f $XUIDB ]]; then
        msg_err "x-ui.db not found — panel may not be installed." && exit 1
    fi

    x-ui stop 2>/dev/null || true

    local output private_key public_key emoji_flag xray_bin
    xray_bin="/usr/local/x-ui/bin/xray-linux-$(_arch)"
    [[ -f "$xray_bin" ]] || xray_bin="/usr/local/x-ui/bin/xray-linux-arm32"
    [[ -f "$xray_bin" ]] || xray_bin="/usr/local/x-ui/bin/xray-linux-arm"
    output=$("$xray_bin" x25519)
    private_key=$(echo "$output" | grep "^PrivateKey:" | awk '{print $2}')
    public_key=$(echo "$output"  | grep "^Password"   | awk '{print $3}')
    local gid_col="" gid_reality="" gid_xhttp=""
    if sqlite3 "$XUIDB" "PRAGMA table_info(hosts);" | grep -qw "group_id"; then
        gid_col='"group_id",'
        gid_reality="'$(gen_group_id)',"
        gid_xhttp="'$(gen_group_id)',"
    fi
    emoji_flag=$(LC_ALL=en_US.UTF-8 curl -s --max-time 10 https://ipwho.is/ | jq -r '.flag.emoji' 2>/dev/null)
    [[ -z "$emoji_flag" || "$emoji_flag" == "null" ]] && emoji_flag="🌐"
    EMOJI_FLAG="$emoji_flag"

    local sub_uri="https://${domain}/${sub_path}/"
    local json_uri="https://${domain}/${json_path}?name="

    # Prepare short IDs for REALITY
    local shor
    shor=($(openssl rand -hex 8) $(openssl rand -hex 8) $(openssl rand -hex 8) $(openssl rand -hex 8) \
           $(openssl rand -hex 8) $(openssl rand -hex 8) $(openssl rand -hex 8) $(openssl rand -hex 8))

    sqlite3 $XUIDB <<EOF
DELETE FROM "settings" WHERE "key" IN ("webCertFile","webKeyFile");

INSERT INTO "settings" ("key","value") VALUES ("subPort",             '${sub_port}');
UPDATE "settings" SET "value" = '/${sub_path}/' WHERE "key" = 'subPath';
INSERT INTO "settings" ("key","value") VALUES ("subURI",              '${sub_uri}');
UPDATE "settings" SET "value" = '/${json_path}/' WHERE "key" = 'subJsonPath';
INSERT INTO "settings" ("key","value") VALUES ("subJsonURI",          '${json_uri}');
INSERT INTO "settings" ("key","value") VALUES ("subClashEnable",      'false');
INSERT INTO "settings" ("key","value") VALUES ("subEnableRouting",    'false');
INSERT INTO "settings" ("key","value") VALUES ("subEnable",           'true');
INSERT INTO "settings" ("key","value") VALUES ("webListen",           '');
INSERT INTO "settings" ("key","value") VALUES ("webDomain",           '');
INSERT INTO "settings" ("key","value") VALUES ("webCertFile",         '');
INSERT INTO "settings" ("key","value") VALUES ("webKeyFile",          '');
INSERT INTO "settings" ("key","value") VALUES ("sessionMaxAge",       '60');
INSERT INTO "settings" ("key","value") VALUES ("pageSize",            '50');
INSERT INTO "settings" ("key","value") VALUES ("expireDiff",          '0');
INSERT INTO "settings" ("key","value") VALUES ("trafficDiff",         '0');
INSERT INTO "settings" ("key","value") VALUES ("remarkModel",         '-ieo');
INSERT INTO "settings" ("key","value") VALUES ("tgBotEnable",         'false');
INSERT INTO "settings" ("key","value") VALUES ("tgBotToken",          '');
INSERT INTO "settings" ("key","value") VALUES ("tgBotProxy",          '');
INSERT INTO "settings" ("key","value") VALUES ("tgBotAPIServer",      '');
INSERT INTO "settings" ("key","value") VALUES ("tgBotChatId",         '');
INSERT INTO "settings" ("key","value") VALUES ("tgRunTime",           '@daily');
INSERT INTO "settings" ("key","value") VALUES ("tgBotBackup",         'false');
INSERT INTO "settings" ("key","value") VALUES ("tgBotLoginNotify",    'true');
INSERT INTO "settings" ("key","value") VALUES ("tgCpu",               '80');
INSERT INTO "settings" ("key","value") VALUES ("tgLang",              'en-US');
INSERT INTO "settings" ("key","value") VALUES ("timeLocation",        'Europe/Moscow');
INSERT INTO "settings" ("key","value") VALUES ("secretEnable",        'false');
INSERT INTO "settings" ("key","value") VALUES ("subDomain",           '');
INSERT INTO "settings" ("key","value") VALUES ("subCertFile",         '');
INSERT INTO "settings" ("key","value") VALUES ("subKeyFile",          '');
INSERT INTO "settings" ("key","value") VALUES ("subUpdates",          '12');
INSERT INTO "settings" ("key","value") VALUES ("subEncrypt",          'true');
INSERT INTO "settings" ("key","value") VALUES ("subShowInfo",         'true');
INSERT INTO "settings" ("key","value") VALUES ("subJsonFragment",     '');
INSERT INTO "settings" ("key","value") VALUES ("subJsonNoises",       '');
INSERT INTO "settings" ("key","value") VALUES ("subJsonMux",          '');
INSERT INTO "settings" ("key","value") VALUES ("subJsonRules",        '');
INSERT INTO "settings" ("key","value") VALUES ("datepicker",          'gregorian');

INSERT INTO "inbounds"
    ("user_id","up","down","total","remark","enable","expiry_time","listen","port","protocol","settings","stream_settings","tag","sniffing")
VALUES (
    '1','0','0','0','${emoji_flag} tcp-reality','1','0','','8443','vless',
    '{
  "clients": [],
  "decryption": "none",
  "encryption": "none",
  "fallbacks": []
}',
    '{
  "network": "tcp",
  "security": "reality",
  "realitySettings": {
    "show": false,
    "xver": 0,
    "target": "127.0.0.1:9443",
    "serverNames": ["${reality_domain}"],
    "privateKey": "${private_key}",
    "minClient": "",
    "maxClient": "",
    "maxTimediff": 0,
    "shortIds": [
      "${shor[0]}","${shor[1]}","${shor[2]}","${shor[3]}",
      "${shor[4]}","${shor[5]}","${shor[6]}","${shor[7]}"
    ],
    "settings": {
      "publicKey": "${public_key}",
      "fingerprint": "firefox",
      "serverName": "",
      "spiderX": "/"
    }
  },
  "tcpSettings": {
    "acceptProxyProtocol": true,
    "header": {"type":"none"}
  }
}',
    'inbound-8443',
    '{"enabled":true,"destOverride":["http","tls","quic","fakedns"],"metadataOnly":false,"routeOnly":false}'
);

INSERT INTO "inbounds"
    ("user_id","up","down","total","remark","enable","expiry_time","listen","port","protocol","settings","stream_settings","tag","sniffing")
VALUES (
    '1','0','0','0','${emoji_flag} xhttp-tls','1','0','/dev/shm/uds2023.sock,0666','0','vless',
    '{
  "clients": [],
  "decryption": "none",
  "encryption": "none",
  "fallbacks": []
}',
    '{
  "network": "xhttp",
  "security": "none",
  "xhttpSettings": {
    "path": "/${xhttp_path}",
    "host": "${domain}",
    "headers": {},
    "scMaxBufferedPosts": 30,
    "scMaxEachPostBytes": "1000000",
    "noSSEHeader": false,
    "xPaddingBytes": "100-1000",
    "mode": "packet-up"
  },
  "sockopt": {
    "acceptProxyProtocol": false,
    "tcpFastOpen": true,
    "mark": 0,
    "tproxy": "off",
    "tcpMptcp": true,
    "tcpNoDelay": true,
    "domainStrategy": "UseIP",
    "tcpMaxSeg": 1440,
    "dialerProxy": "",
    "tcpKeepAliveInterval": 0,
    "tcpKeepAliveIdle": 300,
    "tcpUserTimeout": 10000,
    "tcpcongestion": "bbr",
    "V6Only": false,
    "tcpWindowClamp": 600,
    "interface": ""
  }
}',
    'inbound-/dev/shm/uds2023.sock,0666:0|',
    '{"enabled":true,"destOverride":["http","tls","quic","fakedns"],"metadataOnly":false,"routeOnly":false}'
);

INSERT INTO "hosts" ("inbound_id",${gid_col}"sort_order","remark","address","port","security","fingerprint","alpn")
VALUES
    ((SELECT id FROM inbounds WHERE tag='inbound-8443'),           ${gid_reality} 0, 'tcp-reality', '${domain}', 443, 'same', '',        '[]'),
    ((SELECT id FROM inbounds WHERE tag='inbound-/dev/shm/uds2023.sock,0666:0|'), ${gid_xhttp} 0, 'xhttp-tls', '${domain}', 443, 'tls', 'firefox', '["h2","http/1.1"]');
EOF

    /usr/local/x-ui/x-ui setting \
        -username  "${config_username}" \
        -password  "${config_password}" \
        -port      "${panel_port}"      \
        -webBasePath "${panel_path}"

    /usr/local/x-ui/x-ui cert \
        -webCert    "/root/cert/${domain}/fullchain.pem" \
        -webCertKey "/root/cert/${domain}/privkey.pem"

    x-ui start
}

# ─────────────────────────────────────────────────────────────────────────────
# INSTALL FAKE SITE
# ─────────────────────────────────────────────────────────────────────────────
install_fake_site() {
    local idx=$(( (RANDOM % FAKE_SITE_COUNT) + 1 ))
    local site_id
    site_id=$(printf "site-%02d" "$idx")
    local url="${GITHUB_RAW}/assets/fake-sites/${site_id}/index.html"

    mkdir -p /var/www/html
    if curl -fsSL "$url" -o /var/www/html/index.html; then
        chown -R www-data:www-data /var/www/html 2>/dev/null || true
        chmod 644 /var/www/html/index.html
        msg_ok "Fake cover site '${site_id}' installed."
    else
        msg_err "Failed to download fake site ${site_id} from GitHub."
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM TUNING (BBR + kernel params)
# ─────────────────────────────────────────────────────────────────────────────
tune_system() {
    local params=(
        "net.core.default_qdisc=fq"
        "net.ipv4.tcp_congestion_control=bbr"
        "fs.file-max=2097152"
        "net.ipv4.tcp_timestamps=1"
        "net.ipv4.tcp_sack=1"
        "net.ipv4.tcp_window_scaling=1"
        "net.core.rmem_max=16777216"
        "net.core.wmem_max=16777216"
        "net.ipv4.tcp_rmem=4096 87380 16777216"
        "net.ipv4.tcp_wmem=4096 65536 16777216"
    )
    for p in "${params[@]}"; do
        grep -qxF "$p" /etc/sysctl.conf || echo "$p" >> /etc/sysctl.conf
    done
    sysctl -p
}

# ─────────────────────────────────────────────────────────────────────────────
# CRON JOBS
# ─────────────────────────────────────────────────────────────────────────────
install_geodata_updater() {
    local dest=/usr/local/x-ui/update-geodata.sh
    mkdir -p /usr/local/x-ui
    cat > "$dest" << 'GEOUPD'
#!/bin/bash
set -euo pipefail
DEST=/usr/local/x-ui/bin
mkdir -p "$DEST"
RAW='https://raw.githubusercontent.com'
GH='https://github.com'
download_one_geo() {
    local dest="$1" name="$2" url="$3" fallback="${4:-}" min_bytes="${5:-50000}"
    local tmp http size head
    tmp="${dest}/${name}.tmp.$$"
    rm -f "$tmp"
    http=$(curl -sSfLRo "$tmp" -z "${dest}/${name}" --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 180 -w '%{http_code}' "$url" || true)
    if [[ "$http" == "304" ]]; then rm -f "$tmp"; return 0; fi
    if [[ "$http" != "200" || ! -s "$tmp" ]]; then
        rm -f "$tmp"
        if [[ -n "$fallback" ]]; then
            http=$(curl -sSfLRo "$tmp" --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 180 -w '%{http_code}' "$fallback" || true)
        fi
    fi
    [[ "$http" == "200" && -s "$tmp" ]] || { rm -f "$tmp"; return 0; }
    size=$(wc -c < "$tmp"); size=${size// /}
    head=$(head -c 64 "$tmp" | tr -d '\0' || true)
    if printf '%s' "$head" | grep -qiE '<html|<!doctype|^Not Found|^404'; then rm -f "$tmp"; return 0; fi
    (( size >= min_bytes )) || { rm -f "$tmp"; return 0; }
    mv -f "$tmp" "${dest}/${name}"
}
download_one_geo "$DEST" geoip_RUNET.dat "$RAW/runetfreedom/russia-v2ray-rules-dat/release/geoip.dat" "$GH/runetfreedom/russia-v2ray-rules-dat/raw/release/geoip.dat" 50000 || true
download_one_geo "$DEST" geosite_RUNET.dat "$RAW/runetfreedom/russia-v2ray-rules-dat/release/geosite.dat" "$GH/runetfreedom/russia-v2ray-rules-dat/raw/release/geosite.dat" 50000 || true
GEOUPD
    chmod +x "$dest"
}

setup_cron() {
    crontab -l 2>/dev/null | grep -vE 'certbot|x-ui|cloudflareips|update-geodata|nginx -s reload' | crontab - || true
    install_geodata_updater
    (crontab -l 2>/dev/null; echo '0 4 * * 0 /usr/local/x-ui/update-geodata.sh >/dev/null 2>&1') | crontab -
    (crontab -l 2>/dev/null; echo '0 1 * * * certbot renew --non-interactive --pre-hook "systemctl stop nginx" --deploy-hook "x-ui restart" --post-hook "systemctl start nginx" >/dev/null 2>&1') | crontab -
}

# ─────────────────────────────────────────────────────────────────────────────
# FIREWALL
# ─────────────────────────────────────────────────────────────────────────────
setup_firewall() {
    ufw disable
    ufw allow 22/tcp
    ufw allow 80/tcp
    ufw allow 443/tcp
    if [[ "${DEPLOY_HY2}" == "1" && -n "${hy2_port}" ]]; then
        ufw allow ${hy2_port}/udp
    fi
    if [[ "${EXTRA_INBOUND:-1}" == "2" ]]; then
        ufw allow 56000/udp
        ufw allow 56001/udp
        ufw allow 56003/udp
    elif [[ "${EXTRA_INBOUND:-1}" == "3" ]]; then
        ufw allow 46000/udp
    fi
    ufw --force enable
}

# ─────────────────────────────────────────────────────────────────────────────
# SHOW RESULTS
# ─────────────────────────────────────────────────────────────────────────────
show_results() {
    clear
    if systemctl is-active --quiet x-ui; then
        printf '0\n' | x-ui | grep --color=never -i ':'
        msg_inf "────────────────────────────────────────────────────────────────────────────────"
        msg_inf "X-UI Secure Panel: https://${domain}/${panel_path}/\n"
        echo -e "Username:  ${config_username}\n"
        echo -e "Password:  ${config_password}\n"
        msg_inf "────────────────────────────────────────────────────────────────────────────────"
        print_adguard_results
        msg_inf "Please save this screen!"
    else
        nginx -t
        printf '0\n' | x-ui | grep --color=never -i ':'
        msg_err "x-ui or nginx check failed. Try on a clean Linux install."
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
main() {
    choose_adguard
    choose_xray_dns
    choose_hysteria2
    choose_extra_inbound
    validate_domains
    clean_previous_install
    install_packages
    get_server_ip
    get_ssl_certs

    if systemctl is-active --quiet x-ui; then
        x-ui restart
    else
        install_panel
    fi

    configure_nginx
    if [[ "${DEPLOY_AGH}" == "1" ]]; then
        install_adguard
    fi
    configure_xui_db
    install_fake_site
    tune_system
    setup_cron
    setup_firewall

    if ! systemctl is-enabled --quiet x-ui; then
        systemctl daemon-reload && systemctl enable x-ui.service
    fi
    x-ui restart

    apply_xray_dns
    insert_hy2_inbound
    insert_extra_inbound
    x-ui restart

    show_results
}

if [[ "${ADGUARD_UNINSTALL}" == "y" ]]; then
    uninstall_adguard
    exit 0
fi
if [[ "${ADGUARD_ONLY}" == "y" ]]; then
    install_adguard
    print_adguard_results
    exit 0
fi

main
