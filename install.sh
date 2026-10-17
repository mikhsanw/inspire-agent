#!/usr/bin/env bash
# Installer inspire.
#   curl -fsSL https://raw.githubusercontent.com/mikhsanw/inspire-agent/main/install.sh | bash
#
# Variabel opsional: INSPIRE_REPO (URL git), INSPIRE_DIR (folder, bawaan ~/inspire),
# INSPIRE_MODE=docker|native (lewati deteksi).
set -euo pipefail

REPO="${INSPIRE_REPO:-https://github.com/mikhsanw/inspire-agent.git}"
DIR="${INSPIRE_DIR:-$HOME/inspire}"
OS="$(uname -s)"
TTY=/dev/tty

c()   { printf '\033[%sm%s\033[0m\n' "$1" "$2"; }
say() { printf '%s\n' "$*"; }
step(){ printf '\n'; c "1;36" "── $*"; }
die() { c 31 "❌ $*"; exit 1; }
tty_ok() { ( : < "$TTY" ) 2>/dev/null; }  # bisa dibuka sungguhan (bukan cuma -r)
ask() {  # ask "pertanyaan" "bawaan" — baca dari terminal walau skrip di-pipe dari curl
  local a=""
  if tty_ok; then read -r -p "$1 [$2]: " a < "$TTY" || true; fi
  printf '%s' "${a:-$2}"
}
yes() { [[ "$(ask "$1 (y/n)" "${2:-y}")" =~ ^[Yy] ]]; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then command -v sudo >/dev/null && SUDO="sudo"; fi

c "1;36" "inspire installer"
say "Folder: $DIR"
[ "$OS" = Darwin ] && c 33 "macOS terdeteksi: mode dev (tanpa service otomatis)."

# --- 1. git + kode ------------------------------------------------------------
step "Ambil kode"
if ! command -v git >/dev/null; then
  if [ "$OS" = Linux ] && command -v apt-get >/dev/null; then
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq git curl ca-certificates
  else
    die "git belum ada. Pasang git dulu, lalu jalankan installer lagi."
  fi
fi
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only -q && say "Diperbarui."
else
  git clone -q --depth 1 "$REPO" "$DIR" && say "Diunduh."
fi
cd "$DIR"
mkdir -p data workspace/inbox

# --- 2. mode: docker atau native ------------------------------------------------
step "Pilih mode"
DOCKER=""
docker_ok() { docker info >/dev/null 2>&1 && DOCKER="docker" && return 0
              [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1 && DOCKER="$SUDO docker" && return 0
              return 1; }
MODE="${INSPIRE_MODE:-}"
if [ -z "$MODE" ]; then
  if command -v docker >/dev/null && docker_ok; then
    MODE=docker
  elif [ "$OS" = Linux ]; then
    say "Docker belum ada. Docker dibutuhkan untuk shell sandbox (agent menjalankan perintah dengan aman)."
    say "Tanpa Docker: semua fitur lain tetap jalan, shell dimatikan (bisa nanti)."
    if yes "Pasang Docker sekarang?" y; then
      curl -fsSL https://get.docker.com | $SUDO sh
      [ -n "$SUDO" ] && $SUDO usermod -aG docker "$USER" || true
      docker_ok || die "Docker terpasang tapi belum bisa dipakai. Coba login ulang lalu jalankan installer lagi."
      MODE=docker
    else
      MODE=native
    fi
  else
    if command -v docker >/dev/null; then
      c 33 "Docker Desktop ada tapi belum jalan. Nyalakan untuk mode Docker, atau lanjut native."
    fi
    MODE=native
  fi
elif [ "$MODE" = docker ]; then
  docker_ok || die "INSPIRE_MODE=docker tapi Docker tak bisa dipakai."
fi
echo "$MODE" > .mode
c 32 "Mode: $MODE"

# --- 3. pasang --------------------------------------------------------------------
step "Pasang"
if [ "$MODE" = docker ]; then
  if ! grep -q '^SANDBOX_TOKEN=' .env 2>/dev/null; then
    TOKEN="$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    echo "SANDBOX_TOKEN=$TOKEN" >> .env
  fi
  chmod 600 .env
  $DOCKER compose build -q
  c 32 "Image siap."
else
  if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
  uv sync --frozen --no-dev -q
  c 32 "Paket Python siap."
fi

# Perintah `inspire`
chmod +x bin/inspire
mkdir -p "$HOME/.local/bin"
ln -sf "$DIR/bin/inspire" "$HOME/.local/bin/inspire"
export PATH="$HOME/.local/bin:$PATH"
if ! grep -qs '.local/bin' "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile" 2>/dev/null; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.profile"
fi

# --- 4. setup ---------------------------------------------------------------------
step "Setup"
if tty_ok; then
  "$DIR/bin/inspire" setup < "$TTY" || true
else
  c 33 "Tak ada terminal interaktif. Jalankan nanti: inspire setup"
fi

# --- 5. service ---------------------------------------------------------------------
step "Nyalakan"
if [ "$MODE" = docker ]; then
  $DOCKER compose up -d
  c 32 "Jalan di Docker (nyala lagi otomatis setelah reboot)."
elif [ "$OS" = Linux ] && command -v systemctl >/dev/null; then
  UV="$(command -v uv)"
  $SUDO tee /etc/systemd/system/inspire.service >/dev/null <<EOF
[Unit]
Description=inspire AI agent
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$DIR
Environment=INSPIRE_HOME=$DIR/data INSPIRE_MODE=native
ExecStart=$UV run --project $DIR --no-dev inspire run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now inspire
  c 32 "Jalan sebagai service systemd (nyala lagi otomatis setelah reboot)."
else
  say "Mode dev: jalankan bot dengan  inspire run"
fi

step "Selesai"
say "  inspire doctor     cek semua fitur"
say "  inspire setup      ubah setelan (atau /setup di Telegram)"
say "  inspire logs       lihat log"
say "  inspire update     versi terbaru"
command -v inspire >/dev/null || c 33 "Buka terminal baru (atau: source ~/.profile) supaya perintah 'inspire' dikenal."
