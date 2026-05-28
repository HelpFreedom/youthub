#!/usr/bin/env bash
# YouHub — установка зависимостей и сборка ffplay-yt на Arch Linux.
# Запуск из корня репозитория:  ./scripts/install-arch.sh
# Опции:  --skip-pacman   не вызывать sudo pacman
#         --skip-ffplay   не собирать ffplay-yt
#         --rebuild-ffplay  пересобрать ffplay-yt (make distclean + configure)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SKIP_PACMAN=0
SKIP_FFPLAY=0
REBUILD_FFPLAY=0
INSTALL_LAUNCHER=1
INSTALL_ALIAS=0

for arg in "$@"; do
  case "$arg" in
    --skip-pacman) SKIP_PACMAN=1 ;;
    --skip-ffplay) SKIP_FFPLAY=1 ;;
    --rebuild-ffplay) REBUILD_FFPLAY=1 ;;
    --no-launcher) INSTALL_LAUNCHER=0 ;;
    --install-alias) INSTALL_ALIAS=1 ;;
    -h|--help)
      sed -n '2,6p' "$0" | sed 's/^# \?//'
      echo "  --skip-pacman      только venv, pip, npm (без pacman)"
      echo "  --skip-ffplay      без сборки ffplay-yt"
      echo "  --rebuild-ffplay   заново configure + make ffplay"
      echo "  --no-launcher      не создавать ~/.local/bin/youthub"
      echo "  --install-alias    дополнительно прописать alias в ~/.bashrc / ~/.zshrc"
      exit 0
      ;;
    *) echo "Неизвестный аргумент: $arg" >&2; exit 1 ;;
  esac
done

RUN_SCRIPT="$ROOT/scripts/run-youthub.sh"
LAUNCHER_BIN="$HOME/.local/bin/youthub"
ALIAS_MARKER="# youthub-launcher (install-arch.sh)"

FFPLAY_BIN="$ROOT/ffplay-yt/bin/ffplay-yt"
FFMPEG_SRC="$ROOT/ffplay-yt/src/ffmpeg-4.3.9"
FFMPEG_TAR="$ROOT/ffplay-yt/src/ffmpeg-4.3.9.tar.xz"
FFMPEG_URL="https://ffmpeg.org/releases/ffmpeg-4.3.9.tar.xz"
MIN_FREE_MB=2048

if [[ -f /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
  if [[ "${ID:-}" != "arch" && "${ID_LIKE:-}" != *arch* ]]; then
    echo "[!] Похоже, это не Arch (${ID:-unknown}). Скрипт всё равно можно пробовать."
  fi
fi

step() { echo; echo "==> $*"; }
ok()   { echo "    ✓ $*"; }
warn() { echo "    ! $*" >&2; }
die()  { echo "    ✗ $*" >&2; exit 1; }

check_disk() {
  local avail_kb
  avail_kb="$(df -k "$ROOT" | awk 'NR==2 {print $4}')"
  if [[ -z "$avail_kb" ]]; then return; fi
  if (( avail_kb < MIN_FREE_MB * 1024 )); then
    die "Мало места на диске ($(df -h "$ROOT" | awk 'NR==2 {print $4}') свободно). Нужно ~${MIN_FREE_MB} МБ для сборки ffplay."
  fi
  ok "Свободно на разделе: $(df -h "$ROOT" | awk 'NR==2 {print $4}')"
}

PACMAN_PKGS=(
  base-devel nasm wget curl git
  python
  nodejs npm
  ffmpeg
  sdl2 opus alsa-lib
  kitty
  xdotool wmctrl
  ttf-dejavu
)

step "YouHub — установка на Arch (корень: $ROOT)"

if (( SKIP_PACMAN == 0 )); then
  step "Системные пакеты (pacman)"
  if ! command -v pacman >/dev/null; then
    die "pacman не найден"
  fi
  echo "    Пакеты: ${PACMAN_PKGS[*]}"
  sudo pacman -S --needed "${PACMAN_PKGS[@]}"
  ok "pacman готов"
else
  warn "Пропуск pacman (--skip-pacman)"
fi

step "Python venv (.venv)"
if ! command -v python3 >/dev/null; then
  die "python3 не найден — установите пакет python"
fi
echo "    Версия: $(python3 --version)"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  ok "venv создан"
else
  ok "venv уже есть"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python3 -m pip install --upgrade pip -q
python3 -m pip install curl_cffi "httpx[http2]" Pillow
ok "pip: curl_cffi, httpx[http2], Pillow"

step "Node.js (npm install)"
if ! command -v npm >/dev/null; then
  die "npm не найден"
fi
echo "    node: $(node --version 2>/dev/null || echo '?')"
npm install
ok "node_modules готовы"

if (( SKIP_FFPLAY == 0 )); then
  if [[ -x "$FFPLAY_BIN" && $REBUILD_FFPLAY -eq 0 ]]; then
    step "ffplay-yt"
    ok "уже собран: $FFPLAY_BIN (для пересборки: --rebuild-ffplay)"
  else
  step "Сборка ffplay-yt (FFmpeg 4.3.9, без libdav1d — совместимость с Arch)"
  check_disk

  mkdir -p ffplay-yt/src
  if [[ ! -f "$FFMPEG_TAR" ]]; then
    echo "    Скачиваю $FFMPEG_URL"
    wget -O "$FFMPEG_TAR" "$FFMPEG_URL"
  else
    ok "tarball уже есть: ffmpeg-4.3.9.tar.xz"
  fi

  if [[ ! -d "$FFMPEG_SRC" ]]; then
    echo "    Распаковка…"
    tar -xf "$FFMPEG_TAR" -C ffplay-yt/src
  fi

  cp -f "$ROOT/ffplay-yt/ffplay.c" "$FFMPEG_SRC/fftools/ffplay.c"
  ok "патч ffplay.c применён"

  cd "$FFMPEG_SRC"
  export TMPDIR="${TMPDIR:-/tmp}"

  if (( REBUILD_FFPLAY == 1 )); then
    echo "    make distclean…"
    make distclean 2>/dev/null || true
  fi

  if [[ ! -f ffbuild/config.mak ]] || (( REBUILD_FFPLAY == 1 )); then
    echo "    ./configure (это может занять минуту)…"
    ./configure \
      --disable-everything \
      --enable-gpl --enable-version3 \
      --enable-decoder=h264,vp9,opus,aac,mp3,mjpeg,png \
      --enable-demuxer=matroska,mov,webm \
      --enable-protocol=file,pipe,unix,fd \
      --enable-filter=aresample,scale,atempo,volume \
      --enable-parser=h264,vp9,opus,aac \
      --enable-libopus \
      --enable-sdl2 --enable-ffplay \
      --enable-indev=alsa \
      --disable-doc --disable-htmlpages --disable-manpages \
      --disable-ffmpeg --disable-ffprobe
  else
    ok "configure уже выполнен (используй --rebuild-ffplay для пересборки с нуля)"
  fi

  echo "    make ffplay -j$(nproc) (TMPDIR=$TMPDIR)…"
  make ffplay -j"$(nproc)"

  mkdir -p "$ROOT/ffplay-yt/bin"
  cp -f ffplay "$FFPLAY_BIN"
  chmod +x "$FFPLAY_BIN"
  cd "$ROOT"
  ok "ffplay-yt: $FFPLAY_BIN"
  fi
else
  warn "Пропуск сборки ffplay (--skip-ffplay)"
fi

step "Проверка"
[[ -d .venv ]] || die "нет .venv"
[[ -d node_modules ]] || die "нет node_modules"
if (( SKIP_FFPLAY == 0 )) || [[ -x "$FFPLAY_BIN" ]]; then
  [[ -x "$FFPLAY_BIN" ]] || die "нет исполняемого $FFPLAY_BIN"
  ok "ffplay-yt исполняемый"
fi
command -v kitty >/dev/null && ok "kitty: $(kitty --version 2>/dev/null | head -1)" || warn "kitty не в PATH"

install_launcher() {
  step "Команда youthub в PATH (путь подставляется автоматически)"
  mkdir -p "$HOME/.local/bin"
  ln -sf "$RUN_SCRIPT" "$LAUNCHER_BIN"
  ok "symlink: $LAUNCHER_BIN → $RUN_SCRIPT"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ok '~/.local/bin уже в PATH' ;;
    *)
      warn '~/.local/bin нет в PATH — добавьте в ~/.bashrc или ~/.zshrc:'
      echo "      export PATH=\"\$HOME/.local/bin:\$PATH\""
      ;;
  esac
}

install_shell_alias() {
  local line="alias youthub='$RUN_SCRIPT'"
  local rc
  for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    [[ -f "$rc" ]] || continue
    if grep -qF "$ALIAS_MARKER" "$rc" 2>/dev/null; then
      ok "алиас уже есть в $rc"
      continue
    fi
    {
      echo ""
      echo "$ALIAS_MARKER"
      echo "$line"
    } >>"$rc"
    ok "алиас добавлен в $rc"
  done
}

if (( INSTALL_LAUNCHER )); then
  install_launcher
fi
if (( INSTALL_ALIAS )); then
  install_shell_alias
fi

echo
echo "Готово."
echo
if (( INSTALL_LAUNCHER )) && [[ -x "$LAUNCHER_BIN" ]]; then
  echo "  Запуск из любой папки:"
  echo "    youthub"
  echo
fi
echo "  Или напрямую (путь к репо не нужен — скрипт сам найдёт себя):"
echo "    $RUN_SCRIPT"
echo
if (( INSTALL_ALIAS == 0 && INSTALL_LAUNCHER )); then
  echo "  Алиас в shell (опционально): ./scripts/install-arch.sh --install-alias"
  echo
fi
echo "  Подробности: README_ARCH.md"
