#!/usr/bin/env bash
# Bagian `inspire setup web` yang butuh root. Dijalankan oleh bin/inspire lewat sudo.
#   setup-web.sh <folder data/ssh>
# Membuat user `inspire`, memasang key SSH agent, dan sudoers terbatas:
#   - jalan sebagai PEMILIK tiap folder situs (bukan root)
#   - systemctl reload/restart nginx, apache2, php*-fpm; nginx -t / apache2ctl -t
set -euo pipefail

SSH_DIR="$1"
PUB="$SSH_DIR/id_ed25519.pub"
SITES="$SSH_DIR/sites.txt"
OWNERS="$SSH_DIR/owners.txt"
[ -f "$PUB" ] && [ -f "$SITES" ] || { echo "❌ $PUB / $SITES tidak ada. Jalankan: inspire setup web"; exit 1; }

run() { echo "  \$ $*"; "$@"; }

echo "→ user inspire"
if ! id inspire >/dev/null 2>&1; then
  run useradd --create-home --shell /bin/bash --comment "inspire AI agent" inspire
fi
run install -d -m 700 -o inspire -g inspire /home/inspire/.ssh
echo "no-port-forwarding,no-agent-forwarding,no-X11-forwarding $(cat "$PUB")" > /home/inspire/.ssh/authorized_keys
run chown inspire:inspire /home/inspire/.ssh/authorized_keys
run chmod 600 /home/inspire/.ssh/authorized_keys

echo "→ situs"
: > "$OWNERS"
OWNER_LIST=()
while IFS='|' read -r name path; do
  [ -n "$name" ] || continue
  if [ ! -d "$path" ]; then echo "  ⚠️  lewati $name: $path tidak ada"; continue; fi
  owner="$(stat -c %U "$path")"
  if [ "$owner" = root ]; then
    echo "  ⚠️  lewati $name: $path milik root. Agent tak boleh bekerja sebagai root."
    echo "      Pindahkan kepemilikan ke user deploy (mis. chown -R deploy:www-data $path), lalu ulangi."
    continue
  fi
  echo "$name|$owner" >> "$OWNERS"
  OWNER_LIST+=("$owner")
  echo "  ✅ $name → $path (pemilik: $owner)"
done < "$SITES"
chmod 644 "$OWNERS"

echo "→ sudoers"
SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"
TMP="$(mktemp)"
{
  echo "# Dibuat oleh 'inspire setup web'. Hapus file ini untuk mencabut semua akses agent."
  for o in $(printf '%s\n' "${OWNER_LIST[@]:-}" | sort -u); do
    [ -n "$o" ] && echo "inspire ALL=($o) NOPASSWD: ALL"
  done
  for svc in nginx apache2 'php*-fpm'; do
    echo "inspire ALL=(root) NOPASSWD: $SYSTEMCTL reload $svc, $SYSTEMCTL restart $svc"
  done
  command -v nginx >/dev/null && echo "inspire ALL=(root) NOPASSWD: $(command -v nginx) -t"
  command -v apache2ctl >/dev/null && echo "inspire ALL=(root) NOPASSWD: $(command -v apache2ctl) -t"
  true
} > "$TMP"
cat "$TMP" | sed 's/^/  /'
visudo -cf "$TMP" >/dev/null
run install -m 440 -o root -g root "$TMP" /etc/sudoers.d/inspire
rm -f "$TMP"

if grep -qiE '^\s*AllowUsers' /etc/ssh/sshd_config 2>/dev/null && ! grep -qiE '^\s*AllowUsers.*\binspire\b' /etc/ssh/sshd_config; then
  echo "  ⚠️  sshd_config punya AllowUsers tanpa 'inspire'. Tambahkan 'inspire', lalu: systemctl reload ssh"
fi
echo "✅ Selesai. Cabut akses kapan saja: sudo rm /etc/sudoers.d/inspire /home/inspire/.ssh/authorized_keys"
