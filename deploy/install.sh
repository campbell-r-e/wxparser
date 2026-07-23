#!/usr/bin/env bash
# wxparser interactive installer.
#
# Run this on EACH machine and tell it which role(s) the machine plays:
#
#   db     — PostgreSQL (the store both services talk through)
#   radio  — the capture/STT pipeline (needs the sound card + whisper.cpp)
#   api    — the LAN query API
#
# Pick all three for the classic single-box deployment, or one per machine for
# the split topology (docs/DEPLOY.md §13). The script asks every question up
# front, shows a summary, and only then touches the system: installs packages,
# clones the repo, builds whisper.cpp (radio), configures PostgreSQL (db),
# writes /etc/wxparser.env, installs patched systemd units, and starts things.
# Idempotent — safe to re-run to change answers.
#
# The script clones the repo itself, so it only needs to reach the new machine —
# copy it from any existing clone:
#   scp deploy/install.sh newbox:  &&  ssh newbox bash install.sh
set -euo pipefail

# ---------- helpers ---------------------------------------------------------

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
note() { printf '   %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

ask() {  # ask "prompt" "default" -> echoes the answer
    local v
    read -rp "$1 [$2]: " v </dev/tty
    printf '%s' "${v:-$2}"
}

ask_secret() {  # hidden input
    local v
    read -rsp "$1: " v </dev/tty; printf '\n' >&2
    printf '%s' "$v"
}

confirm() {  # confirm "prompt" "Y|N" -> exit status
    local d=${2:-Y} v hint
    if [ "$d" = Y ]; then hint="Y/n"; else hint="y/N"; fi
    read -rp "$1 [$hint]: " v </dev/tty
    v=${v:-$d}
    [[ $v =~ ^[Yy] ]]
}

if [ "$(id -u)" = 0 ]; then die "run as a regular user; the script uses sudo where needed"; fi
command -v sudo >/dev/null || die "sudo is required"

PKG_MGR=none  # dnf (Fedora/RHEL) or apt (Debian/Ubuntu/Pi OS); both are supported
if command -v dnf >/dev/null; then
    PKG_MGR=dnf
elif command -v apt-get >/dev/null; then
    PKG_MGR=apt
fi
pkg() {  # install packages; on an unknown distro just tell the user what's needed
    case $PKG_MGR in
        dnf) sudo dnf -y install "$@" ;;
        apt) sudo apt-get update -qq
             sudo DEBIAN_FRONTEND=noninteractive apt-get -y install "$@" ;;
        *)   note "no dnf/apt — install these yourself before continuing: $*"
             confirm "installed?" Y || die "install the packages and re-run" ;;
    esac
}
open_port() {  # open a TCP port in whichever firewall this box runs
    if command -v firewall-cmd >/dev/null; then
        sudo firewall-cmd --permanent --add-port="$1"/tcp
        sudo firewall-cmd --reload
    elif command -v ufw >/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
        sudo ufw allow "$1"/tcp
    else
        note "no active firewalld/ufw — make sure port $1/tcp is reachable"
    fi
}

# ---------- gather answers --------------------------------------------------

say "wxparser installer — what does THIS machine run?"
cat <<'EOF'
   1) db     PostgreSQL store
   2) radio  capture/STT pipeline (sound card + whisper.cpp live here)
   3) api    LAN query API
EOF
ROLES=$(ask "roles (numbers/names, space-separated, or 'all')" "all")
WANT_DB=0 WANT_RADIO=0 WANT_API=0
if [[ $ROLES =~ all|1|db    ]]; then WANT_DB=1;    fi
if [[ $ROLES =~ all|2|radio ]]; then WANT_RADIO=1; fi
if [[ $ROLES =~ all|3|api   ]]; then WANT_API=1;   fi
if [ $((WANT_DB + WANT_RADIO + WANT_API)) -eq 0 ]; then die "no role selected"; fi

RUN_USER=$(ask "service account (existing user the services run as)" "$USER")
RUN_HOME=$(eval echo "~$RUN_USER")
if [ ! -d "$RUN_HOME" ]; then die "no home directory for $RUN_USER"; fi
INSTALL_DIR=$(ask "install directory (the git clone)" "$RUN_HOME/wxparser")
REPO_URL=$(ask "repo URL (private repo: use an ssh deploy key or a token URL)" \
                "git@github.com:campbell-r-e/wxparser.git")

# -- DB connectivity ---------------------------------------------------------
PG_HOST=127.0.0.1 PG_PASS="" DB_EXPOSE=0 DB_CIDR=""
if [ "$WANT_DB" = 1 ] && [ "$WANT_RADIO" = 1 ] && [ "$WANT_API" = 1 ]; then
    note "all roles on one box — PostgreSQL stays localhost-trust, no password needed"
elif [ "$WANT_DB" = 1 ]; then
    say "db role: other machines will connect to this PostgreSQL"
    DB_EXPOSE=1
    DEFAULT_CIDR=$(ip -4 route 2>/dev/null | awk '/proto kernel/ {print $1; exit}' || true)
    DB_CIDR=$(ask "LAN CIDR allowed to connect" "${DEFAULT_CIDR:-192.168.0.0/16}")
    PG_PASS=$(ask_secret "password to set on the 'wxparser' DB role")
    if [ -z "$PG_PASS" ]; then die "a password is required for network access"; fi
fi
if [ "$WANT_DB" = 0 ]; then
    say "no db role here — where is PostgreSQL?"
    PG_HOST=$(ask "PostgreSQL host" "192.168.1.10")
    PG_PASS=$(ask_secret "password for the 'wxparser' DB role")
fi

# -- radio questions ---------------------------------------------------------
ALSA_DEV="" PROFILE="" WHISPER_DIR="" MODEL="" TIMERS=0
if [ "$WANT_RADIO" = 1 ]; then
    say "radio role: audio + STT"
    if command -v arecord >/dev/null; then
        note "capture devices:"
        arecord -l 2>/dev/null | sed 's/^/     /' || true
    fi
    ALSA_DEV=$(ask "ALSA capture device" "plughw:0,0")
    PROFILE=$(ask "station profile (bundled name or /path/to/profile.json)" "kjy93_muncie")
    WHISPER_DIR=$(ask "whisper.cpp directory (built here if missing)" "$RUN_HOME/whisper.cpp")
    MODEL=$(ask "whisper model: small.en-q5_1 (accurate) or base.en-q5_1 (fast)" "small.en-q5_1")
    if confirm "install the maintenance timers (AGC + nightly cleanup)?" Y; then TIMERS=1; fi
fi

# -- api questions -----------------------------------------------------------
API_PORT=8080
if [ "$WANT_API" = 1 ]; then
    say "api role"
    API_PORT=$(ask "API port" "8080")
fi

# -- CD ----------------------------------------------------------------------
CD=0 CD_SERVICES=""
if [ $((WANT_RADIO + WANT_API)) -gt 0 ]; then
    if [ "$WANT_RADIO" = 1 ]; then CD_SERVICES="wxparser"; fi
    if [ "$WANT_API" = 1 ]; then CD_SERVICES="${CD_SERVICES:+$CD_SERVICES }wxparser-api"; fi
    if confirm "enable pull-based CD (auto-deploy green pushes every 10 min)?" Y; then CD=1; fi
fi

# -- summary -----------------------------------------------------------------
say "summary"
ROLE_LIST=""
if [ "$WANT_DB" = 1 ]; then ROLE_LIST="db"; fi
if [ "$WANT_RADIO" = 1 ]; then ROLE_LIST="${ROLE_LIST:+$ROLE_LIST }radio"; fi
if [ "$WANT_API" = 1 ]; then ROLE_LIST="${ROLE_LIST:+$ROLE_LIST }api"; fi
note "roles:      $ROLE_LIST"
note "user/dir:   $RUN_USER  $INSTALL_DIR"
if [ "$WANT_DB" = 0 ]; then note "postgres:   $PG_HOST (password auth)"; fi
if [ "$DB_EXPOSE" = 1 ]; then note "postgres:   exposed to $DB_CIDR (scram)"; fi
if [ "$WANT_RADIO" = 1 ]; then note "radio:      $ALSA_DEV  profile=$PROFILE  model=$MODEL  timers=$TIMERS"; fi
if [ "$WANT_API" = 1 ]; then note "api:        port $API_PORT"; fi
if [ "$CD" = 1 ]; then note "CD:         on ($CD_SERVICES)"; else note "CD:         off"; fi
confirm "apply?" Y || die "aborted — nothing was changed"

# ---------- execute ---------------------------------------------------------

say "packages"
PKGS="git python3"
if [ $((WANT_RADIO + WANT_API)) -gt 0 ]; then
    if [ "$PKG_MGR" = apt ]; then
        # Debian's python3-pg8000 is ancient (1.10, no pg8000.native) — pip below
        PKGS="$PKGS python3-numpy python3-pip"
    else
        PKGS="$PKGS python3-numpy python3-pg8000"
    fi
fi
if [ "$WANT_RADIO" = 1 ]; then
    if [ "$PKG_MGR" = apt ]; then
        PKGS="$PKGS alsa-utils build-essential cmake"
    else
        PKGS="$PKGS alsa-utils gcc-c++ cmake make"
    fi
fi
# shellcheck disable=SC2086
pkg $PKGS
if [ "$PKG_MGR" = apt ] && [ $((WANT_RADIO + WANT_API)) -gt 0 ]; then
    # system-wide on purpose: the services run the system python3
    sudo python3 -m pip install --break-system-packages --quiet 'pg8000>=1.31'
fi

say "code"
if [ -d "$INSTALL_DIR/.git" ]; then
    note "$INSTALL_DIR exists — leaving it as-is (CD keeps it current)"
else
    sudo -u "$RUN_USER" git clone "$REPO_URL" "$INSTALL_DIR"
fi

# -- db ----------------------------------------------------------------------
if [ "$WANT_DB" = 1 ]; then
    say "postgresql"
    if [ "$PKG_MGR" = apt ]; then
        bash "$INSTALL_DIR/deploy/setup-postgres-debian.sh"
    else
        bash "$INSTALL_DIR/deploy/setup-postgres.sh"
    fi
    if [ "$DB_EXPOSE" = 1 ]; then
        sudo -u postgres psql -c "ALTER ROLE wxparser WITH LOGIN PASSWORD '$PG_PASS' CREATEDB;"
        sudo -u postgres psql -c "ALTER SYSTEM SET listen_addresses = '*';"
        PGDATA=$(sudo -u postgres psql -tAc "SHOW data_directory")
        if ! sudo grep -q "wxparser-lan" "$PGDATA/pg_hba.conf"; then
            printf '# wxparser-lan\nhost wxparser,wxparser_test wxparser %s scram-sha-256\n' "$DB_CIDR" \
                | sudo tee -a "$PGDATA/pg_hba.conf" >/dev/null
        fi
        sudo systemctl restart postgresql
        open_port 5432
    fi
fi

# -- whisper.cpp (radio) -----------------------------------------------------
WHISPER_BIN="" MODEL_PATH=""
if [ "$WANT_RADIO" = 1 ]; then
    say "whisper.cpp"
    WHISPER_BIN="$WHISPER_DIR/build/bin/whisper-cli"
    if [ -x "$WHISPER_DIR/build-blas/bin/whisper-cli" ]; then
        WHISPER_BIN="$WHISPER_DIR/build-blas/bin/whisper-cli"
    fi
    if [ ! -x "$WHISPER_BIN" ]; then
        if [ ! -d "$WHISPER_DIR" ]; then
            sudo -u "$RUN_USER" git clone https://github.com/ggml-org/whisper.cpp "$WHISPER_DIR"
        fi
        sudo -u "$RUN_USER" bash -c "cd '$WHISPER_DIR' && cmake -B build && cmake --build build -j --config Release"
        WHISPER_BIN="$WHISPER_DIR/build/bin/whisper-cli"
    fi
    MODEL_PATH="$WHISPER_DIR/models/ggml-$MODEL.bin"
    if [ ! -f "$MODEL_PATH" ]; then
        sudo -u "$RUN_USER" bash "$WHISPER_DIR/models/download-ggml-model.sh" "$MODEL"
    fi
fi

# -- /etc/wxparser.env -------------------------------------------------------
say "environment file (/etc/wxparser.env)"
ENV_TMP=$(mktemp)
{
    echo "# generated by deploy/install.sh $(date -Is) — safe to edit, mode 600"
    if [ "$PG_HOST" != 127.0.0.1 ]; then echo "WX_PG_HOST=$PG_HOST"; fi
    if [ -n "$PG_PASS" ] && [ "$WANT_DB" = 0 ]; then echo "WX_PG_PASSWORD=$PG_PASS"; fi
    if [ "$WANT_RADIO" = 1 ]; then
        echo "WX_ALSA_DEVICE=$ALSA_DEV"
        if [ "$PROFILE" != kjy93_muncie ]; then echo "WX_PROFILE=$PROFILE"; fi
        echo "WX_WHISPER_BIN=$WHISPER_BIN"
        echo "WX_WHISPER_MODEL=$MODEL_PATH"
    fi
    if [ "$WANT_API" = 1 ] && [ "$API_PORT" != 8080 ]; then echo "WX_API_PORT=$API_PORT"; fi
    if [ "$CD" = 1 ]; then
        echo "WX_DEPLOY_SERVICES=$CD_SERVICES"
        echo "WX_DEPLOY_REPO=$INSTALL_DIR"
        echo "WX_DEPLOY_VENV=$RUN_HOME/wxparser-testenv"
        echo "WX_DEPLOY_LOG=$RUN_HOME/wxparser-deploy.log"
    fi
} > "$ENV_TMP"
sudo install -m 600 -o root -g root "$ENV_TMP" /etc/wxparser.env
rm -f "$ENV_TMP"
note "$(sudo grep -c . /etc/wxparser.env) lines written"

# -- systemd units -----------------------------------------------------------
install_unit() {  # patch the repo unit for this account/layout and install it
    local tmp
    tmp=$(mktemp)
    sed -e "s|/home/creed/wxparser|$INSTALL_DIR|g" \
        -e "s|/home/creed|$RUN_HOME|g" \
        -e "s|^User=.*|User=$RUN_USER|" \
        -e "s|^Group=.*|Group=$RUN_USER|" \
        "$INSTALL_DIR/deploy/$1" \
        | awk '{print} /^\[Service\]$/ {print "EnvironmentFile=-/etc/wxparser.env"}' > "$tmp"
    sudo install -m 644 "$tmp" "/etc/systemd/system/$1"
    rm -f "$tmp"
}

say "systemd units"
UNITS=() TIMERS_ON=()
if [ "$WANT_RADIO" = 1 ]; then
    install_unit wxparser.service; UNITS+=(wxparser)
    if [ "$TIMERS" = 1 ]; then
        for t in agc fixspelling fixterms prune reprocess; do
            install_unit "wxparser-$t.service"
            install_unit "wxparser-$t.timer"
            TIMERS_ON+=("wxparser-$t.timer")
        done
    fi
fi
if [ "$WANT_API" = 1 ]; then
    install_unit wxparser-api.service; UNITS+=(wxparser-api)
fi
if [ "$CD" = 1 ]; then
    install_unit wxparser-deploy.service
    install_unit wxparser-deploy.timer
    TIMERS_ON+=(wxparser-deploy.timer)
fi
sudo systemctl daemon-reload
if [ ${#UNITS[@]} -gt 0 ]; then sudo systemctl enable --now "${UNITS[@]}"; fi
if [ ${#TIMERS_ON[@]} -gt 0 ]; then sudo systemctl enable --now "${TIMERS_ON[@]}"; fi

# -- firewall (api) ----------------------------------------------------------
if [ "$WANT_API" = 1 ]; then
    open_port "$API_PORT"
fi

# -- verify ------------------------------------------------------------------
say "verify"
for u in "${UNITS[@]}"; do
    note "$u: $(systemctl is-active "$u" || true)"
done
if [ "$WANT_API" = 1 ]; then
    sleep 3
    CODE=$(curl -fsS -o /dev/null -w '%{http_code}' "http://localhost:$API_PORT/health" || true)
    note "/health: ${CODE:-unreachable} (503 = API up but no pipeline heartbeat yet — fine if the radio box isn't running)"
fi
say "done"
note "next: run this script on the other machine(s) with their roles."
note "guide: $INSTALL_DIR/docs/DEPLOY.md (§13 for the split topology)"
