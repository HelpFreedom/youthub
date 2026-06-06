# YouHub на Arch Linux

Дополнение к основному [README.md](README.md).  
Все общие разделы (управление, архитектура, OAuth, переменные окружения) — в основном README; здесь только то, что отличается на Arch (включая Omarchy и другие дистрибутивы на базе Arch).

### Быстрый старт (скрипты)

Из корня репозитория:

```bash
./scripts/install-arch.sh    # pacman, venv, pip, npm, ffplay-yt, команда youthub в PATH
youthub                      # из любой папки (после установки)
```

Пути **не нужно** прописывать вручную: скрипты сами находят корень репозитория; установщик создаёт `~/.local/bin/youthub` → `scripts/run-youthub.sh`.

Опции: `./scripts/install-arch.sh --help` (`--skip-pacman`, `--skip-ffplay`, `--rebuild-ffplay`, `--install-alias`, `--no-launcher`).

---

## Требования (Arch)

- **Arch Linux** (или производный: Omarchy, EndeavourOS и т.д.)
- **X11** — upstream тестировал под X11; Wayland в README не заявлен (`xdotool` / `wmctrl` завязаны на X11)
- **kitty** >= 0.26 (тестировалось на 0.46.2)
- **Python 3.11+** — в README указан именно 3.11; на Arch в `extra` часто новее (3.12/3.13). Если что-то ломается, поставьте `python311` из AUR или соберите venv с нужной версией
- **Node.js** >= 18 (`nodejs` в extra)
- **FFmpeg** — системный, для мультиплексирования (`ffmpeg`)
- **SDL2**, **ALSA** — для сборки ffplay-yt

### Пакеты одной командой

```bash
sudo pacman -S --needed \
  base-devel nasm wget curl git \
  python python-pip \
  nodejs npm \
  ffmpeg \
  sdl2 dav1d opus alsa-lib \
  kitty \
  xdotool wmctrl \
  ttf-dejavu
```

Опционально для cookie watchstats: **Firefox** (скрипт `extract_cookies.py` читает профиль Firefox).

---

## Установка

### 1. Клонирование

```bash
git clone https://github.com/HelpFreedom/youthub.git
cd youthub
```

### 2. Python-окружение

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install curl_cffi "httpx[http2]" Pillow
```

Если в репозитории жёстко ожидается 3.11:

```bash
# при наличии python3.11 в системе или AUR
python3.11 -m venv .venv
source .venv/bin/activate
python3.11 -m pip install curl_cffi "httpx[http2]" Pillow
```

### 3. Node-зависимости

```bash
npm install
```

### 4. Сборка ffplay-yt

Патченный исходник: `ffplay-yt/ffplay.c`.  
На Arch нет `apt source` — берём официальный tarball FFmpeg **4.3.9**:

```bash
# Зависимости сборки (если ещё не ставили)
sudo pacman -S --needed base-devel nasm sdl2 dav1d opus alsa-lib

mkdir -p ffplay-yt/src && cd ffplay-yt/src
wget https://ffmpeg.org/releases/ffmpeg-4.3.9.tar.xz
tar xf ffmpeg-4.3.9.tar.xz
cd ffmpeg-4.3.9

cp ../../ffplay.c fftools/ffplay.c

# Важно для Arch: системный dav1d (1.5.x) НЕ совместим с FFmpeg 4.3.9
# (ошибка libdav1d.c: n_tile_threads / DAV1D_MAX_TILE_THREADS).
# YouHub в основном крутит H.264/VP9 — libdav1d (AV1) для сборки не обязателен.
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

make ffplay -j$(nproc)

mkdir -p ../../bin
cp ffplay ../../bin/ffplay-yt
cd ../../..
```

Проверка (бинарник лежит в `ffplay-yt/bin/`, не в корневом `bin/`):

```bash
test -x ffplay-yt/bin/ffplay-yt && ffplay-yt/bin/ffplay-yt -version | head -1
```

### 5. Системные утилиты

```bash
sudo pacman -S --needed xdotool wmctrl ttf-dejavu kitty
```

### 6. Прокси (опционально)

```bash
export HTTPS_PROXY="socks5://127.0.0.1:1080"
```

---

## Запуск (Arch / Omarchy)

Нужен **kitty** с работающим graphics protocol (превью в сетке).

**Рекомендуемый способ** — скрипт `scripts/run-youthub.sh` (отдельное окно kitty, cwd = репозиторий):

```bash
~/youthub/scripts/run-youthub.sh
# то же вручную:
# kitty --directory=~/youthub -e bash -lc 'source .venv/bin/activate && python3 grid_demo.py'
```

Или в уже открытом kitty:

```bash
cd ~/youthub
source .venv/bin/activate
python3 grid_demo.py
```

Перед запуском проверка (в **том же** окне):

```bash
echo "TERM=$TERM TERM_PROGRAM=${TERM_PROGRAM:-}"
# желательно TERM_PROGRAM=kitty; если пусто, но окно визуально kitty —
# всё равно проверьте тест превью в разделе ниже
```

Первый запуск, OAuth, cookie, горячие клавиши — см. [README.md](README.md).

---

## Превью не отображаются

Метаданные (название, длительность) есть, а картинок нет — JPEG обычно **уже скачаны** (`cache/thumbnails/*.jpg`), но терминал **не рисует** Kitty graphics protocol.

Проверьте **в том же окне**, где запускаете `grid_demo.py`:

```bash
echo "TERM=$TERM TERM_PROGRAM=${TERM_PROGRAM:-}"
# нужно:  TERM_PROGRAM=kitty   (TERM часто xterm-kitty или xterm-256color)
```

| Что видите | Значение |
|------------|----------|
| `TERM_PROGRAM=kitty` | Ок для graphics protocol |
| `TERM_PROGRAM=` пусто | Часто **другой** эмулятор (foot/ghostty) или оболочка без переменных kitty; **иногда** бывает и внутри kitty — ориентируйтесь на **тест картинки** ниже, а не только на `echo` |
| внутри **tmux** | Graphics protocol часто не проходит — запустите вне tmux |

Если превью нет, но вы уверены, что это kitty — попробуйте явный запуск:

```bash
kitty --directory=~/youthub -e bash -lc 'source .venv/bin/activate && python3 grid_demo.py'
```

(у части пользователей Omarchy это сразу включает отрисовку превью).

Тест картинки в kitty:

```bash
cd ~/youthub && source .venv/bin/activate
python3 -c "
from pathlib import Path
from youthub import graphics, thumbnails
vid = next(Path('cache/thumbnails').glob('*.jpg')).stem
png = thumbnails.get_png(vid)
graphics.transmit_and_place(1, png, width_cells=20, height_cells=10)
print('Должно быть превью выше')
"
```

Если тест **не** показывает картинку — проблема в терминале/конфиге kitty, не в YouHub.  
Если тест **показывает**, а сетка нет — напишите в issue (редкий случай).

**Ghostty** (если хотите оставить его): в конфиге включите поддержку Kitty graphics protocol (см. документацию Ghostty) — код YouHub шлёт именно `\033_G`, не Sixel.

---

## Omarchy

- Для YouHub нужен **kitty** (graphics protocol). Если превью пустые — `kitty --directory=~/youthub -e ...` из раздела «Запуск»; пустой `TERM_PROGRAM` при `echo` не всегда значит «не kitty», но тест с `transmit_and_place` не врёт.
- Убедитесь, что сессия **X11**, если используете Hyprland/Wayland: для позиционирования окна ffplay может понадобиться XWayland и рабочие `xdotool`/`wmctrl` (как в upstream).
- Версия **kitty 0.46.2** полностью покрывает graphics protocol, который использует `youthub/graphics.py`.

---

## Другие терминалы

### Что сейчас в коде

| Компонент | Терминал | Протокол |
|-----------|----------|----------|
| `grid_demo.py`, `youthub/graphics.py`, превью, поиск | **kitty** (обязательно для картинок в сетке) | Kitty graphics (`\033_G`, PNG `f=100`) |
| `bridge_player.py` + `bin/ffplay-yt` | Любой (отдельное SDL-окно) | — |
| `tui_player.py` (прототип) | kitty | Тот же graphics protocol |
| `player.py` (legacy) | kitty через `mpv --vo=kitty` | mpv → kitty |

Другие эмуляторы **не проверяются** и **не выбираются** автоматически: в коде нет веток под iTerm2, Sixel, sixel-кит и т.д.

### Можно ли «заточить» под другие

**Реалистично без полной переделки UI:**

1. **WezTerm / Ghostty** — часто поддерживают **тот же** Kitty graphics protocol (у WezTerm может понадобиться `enable_kitty_graphics = true` в конфиге). Теоретически достаточно запустить `grid_demo.py` там и проверить; официально проект это не тестирует.

2. **Sixel** (foot, некоторые другие) — другой формат вывода; нужен отдельный бэкенд в `graphics.py` (конвертация PNG → sixel, другие размеры/позиционирование). Объём работы: средний.

3. **iTerm2 inline images** (macOS/Linux) — свой OSC-протокол; отдельный бэкенд.

4. **Без графики в терминале** — только текстовая сетка (без превью) или вынести UI в GUI/web; это уже другой продукт.

**Практический совет на Arch:** оставить **kitty** для сетки (как задумано upstream) — у вас он уже есть и подходит по версии.

---

## Типичные проблемы на Arch

| Симптом | Что проверить |
|---------|----------------|
| `libdav1d.c: n_tile_threads` / `DAV1D_MAX_TILE_THREADS` | Системный **dav1d 1.5.x** слишком новый для **FFmpeg 4.3.9**. Пересоберите **без** `--enable-libdav1d` (см. блок `./configure` выше) или соберите старый dav1d в отдельный prefix (ниже) |
| `No space left on device` при `make` | Раздел `/` полон; освободите **2–5 ГБ**, затем `make distclean`, `./configure`, `export TMPDIR=/tmp`, `make ffplay` |
| `make: ffbuild/config.mak: No such file` | После `make distclean` нужен снова `./configure` |
| `cp: cannot stat 'ffplay'` | `make ffplay` не завершился — бинарник не создался |
| Нет картинок в сетке, только текст | Нет graphics protocol: см. [Превью не отображаются](#превью-не-отображаются); JPEG в `cache/thumbnails/` при этом могут быть |
| `./configure` не находит sdl2/opus | `pacman -S sdl2 opus alsa-lib` |
| ffplay не собирается | `base-devel`, `nasm`, версия исходников именно **4.3.9** |
| Окно плеера не встаёт как надо | X11/XWayland, `xdotool`, `wmctrl` |
| `ImportError: ... 'h2' package is not installed` | `pip install "httpx[http2]"` (нужен для `http2=True` в innertube/thumbnails) |
| `No module named 'playwright'` при Enter | В старом `bridge_player.py` был лишний `import bootstrap` — возьмите версию без него; **playwright для просмотра не нужен** |
| Сетка есть, **превью пустые** | [Превью не отображаются](#превью-не-отображаются) |
| Ошибки npm/node | `nodejs` >= 18, повторить `npm install` |
| `Cache entry deserialization failed` (pip) | Не критично; при желании: `pip cache purge` |

### Опционально: libdav1d (AV1) со старым dav1d

Если принципиально нужен декодер AV1 в ffplay-yt, соберите **dav1d 1.0.0** в локальный prefix (не путать с `pacman -S dav1d`):

```bash
DEPS=$HOME/.local/youthub-ffmpeg-deps
mkdir -p "$DEPS" && cd /tmp
curl -LO https://code.videolan.org/videolan/dav1d/-/archive/1.0.0/dav1d-1.0.0.tar.bz2
tar xf dav1d-1.0.0.tar.bz2 && cd dav1d-1.0.0
meson setup build --prefix="$DEPS" --buildtype=release
meson compile -C build && meson install -C build

cd /path/to/youthub/ffplay-yt/src/ffmpeg-4.3.9
make distclean 2>/dev/null || true
PKG_CONFIG_PATH="$DEPS/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}" \
  ./configure ... --enable-libdav1d --enable-decoder=...,libdav1d,...
```

Для YouHub обычно достаточно сборки **без** libdav1d.

### Место на диске при сборке

`make ffplay` может занять **1–3 ГБ**. Если `/` заполнен:

```bash
df -h /
# освободить место (yay -Sc, pip cache purge, …)
cd ffplay-yt/src/ffmpeg-4.3.9
make distclean
./configure   # те же флаги, что в разделе 4
export TMPDIR=/tmp
make ffplay -j$(nproc)
```

---

## Чеклист перед PR / отчётом «работает на Arch»

- [ ] `ffplay-yt/bin/ffplay-yt` собран (`test -x ffplay-yt/bin/ffplay-yt`)
- [ ] `npm install` без ошибок
- [ ] venv: `curl_cffi`, `"httpx[http2]"`, `Pillow`
- [ ] Превью: тест `transmit_and_place` или сетка с картинками в kitty
- [ ] `python3 grid_demo.py` — лента + превью
- [ ] Enter — ffplay-yt, звук/картинка (без `playwright`)
- [ ] (опционально) OAuth и cookie по README

Если что-то падает — версии: `pacman -Q kitty python nodejs ffmpeg`, вывод ошибки, `echo $TERM $TERM_PROGRAM`.

---

## Кратко: что покрывает этот файл

| Тема | Где в документе |
|------|-----------------|
| Скрипты `install-arch.sh`, `run-youthub.sh` | Быстрый старт |
| `pacman`, venv, `httpx[http2]` | Установка §1–3 |
| FFmpeg 4.3.9 без `libdav1d`, путь `ffplay-yt/bin/ffplay-yt` | §4 |
| Диск / `TMPDIR`, `configure` после `distclean` | Типичные проблемы |
| Запуск kitty, превью, `TERM_PROGRAM` | Запуск, § «Превью» |
| `playwright` не нужен | Типичные проблемы |
| Ghostty / другие терминалы | § «Другие терминалы» |
