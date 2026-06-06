#!/usr/bin/env bash
# YouHub — запуск grid_demo.py в отдельном окне kitty (Kitty graphics protocol → превью).
# Путь к репозиторию определяется по расположению этого файла — вручную ничего не подставляйте.
# Использование:  ./scripts/run-youthub.sh
#                 youthub   (после install-arch.sh: symlink ~/.local/bin/youthub)

set -euo pipefail

# Через symlink (~/.local/bin/youthub) BASH_SOURCE[0] — путь к ссылке, не к репо.
SCRIPT="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SCRIPT")/.." && pwd)"
FFPLAY_BIN="$ROOT/ffplay-yt/bin/ffplay-yt"
VENV_PY="$ROOT/.venv/bin/python3"

die() { echo "youthub: $*" >&2; exit 1; }

command -v kitty >/dev/null || die "kitty не найден. Установите: sudo pacman -S kitty"

[[ -x "$VENV_PY" ]] || die "нет $VENV_PY — сначала: $ROOT/scripts/install-arch.sh"

[[ -x "$FFPLAY_BIN" ]] || die "нет $FFPLAY_BIN — сначала: $ROOT/scripts/install-arch.sh"

[[ -d "$ROOT/node_modules" ]] || die "нет node_modules — в $ROOT выполните: npm install"

# Отдельное окно kitty, cwd = репозиторий (как в README_ARCH).
exec kitty --directory="$ROOT" bash -lc '
  set -euo pipefail
  source .venv/bin/activate
  exec python3 grid_demo.py
'
