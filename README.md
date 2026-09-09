# Kwispr

> Быстрая голосовая диктовка для Linux, Wayland и KDE.

Нажмите горячую клавишу, произнесите текст и нажмите её снова — Kwispr распознает речь, скопирует результат в буфер обмена и при необходимости сразу вставит его в активное окно.

- локальное распознавание без облака;
- OpenAI и OpenRouter;
- русский, английский и смешанная речь;
- KDE tray, уведомления и звуковые сигналы;
- архив записей и повторная отправка при ошибке.

![Kwispr demo](demo.gif)

## Быстрый старт

```bash
git clone https://github.com/blockedby/kwispr.git
cd kwispr
./setup.sh    # системные зависимости
./install.sh  # приложение в ~/.local
```

Откройте **Kwispr Settings**, выберите backend и назначьте глобальную горячую клавишу. Первое нажатие начинает запись, второе — останавливает её и запускает распознавание.

Настройки хранятся в `~/.config/kwispr/config.env`. Для запуска напрямую из репозитория можно скопировать `.env.example` в `.env`.

## Локальное распознавание

Для приватной офлайн-диктовки установите локальный runtime и модель из встроенного проверяемого каталога:

```bash
./install.sh --with-local-stt
./kwispr-models.py list
./kwispr-models.py download whisper-large-v3-turbo
systemctl --user enable --now kwispr-local-stt.service
```

Эквивалентная настройка через `~/.config/kwispr/config.env`:

```bash
KWISPR_BACKEND=openai-transcriptions
KWISPR_API_URL=http://127.0.0.1:19650/v1/audio/transcriptions
KWISPR_MODEL=whisper-large-v3-turbo
KWISPR_API_KEY=
KWISPR_AUTOPASTE=1
KWISPR_PASTE_HOTKEY=shift-insert

# Не обрезать тихий конец фразы слишком рано
KWISPR_VAD_ENABLED=1
KWISPR_VAD_PROVIDER=energy
KWISPR_VAD_THRESHOLD=0.01
KWISPR_VAD_PADDING_MS=1500
```

Подходящие модели:

| Задача | Модель |
|---|---|
| Русская речь | `gigaam-v3-e2e-ctc` |
| Русский + English | `whisper-large-v3-turbo` |
| Мультиязычная речь | `parakeet-tdt-0.6b-v3` |

## Облачные backend-ы

<details>
<summary><strong>OpenAI Whisper</strong></summary>

```bash
KWISPR_BACKEND=openai-transcriptions
KWISPR_API_URL=https://api.openai.com/v1/audio/transcriptions
KWISPR_MODEL=whisper-1
KWISPR_API_KEY=sk-...
```

</details>

<details>
<summary><strong>OpenRouter</strong></summary>

```bash
KWISPR_BACKEND=openrouter-chat
KWISPR_API_URL=https://openrouter.ai/api/v1/chat/completions
KWISPR_MODEL=google/gemini-2.5-flash
KWISPR_API_KEY=sk-or-...
KWISPR_AUDIO_FORMAT=wav
```

</details>

## Команды

```bash
./kwispr.sh toggle          # начать или закончить диктовку
./kwispr.sh retry file.wav  # повторить неудачное распознавание
./kwispr-models.py list     # показать локальные модели
```

Записи и расшифровки хранятся в `~/.cache/kwispr/` и удаляются через 30 дней.

## Как это устроено

```text
Горячая клавиша / KDE tray
          │
          ▼
      kwispr.sh ──► WAV ──► OpenAI / OpenRouter
                         └─► локальный Rust STT ──► GGUF-модель
          │
          └──────────────► clipboard + auto-paste
```

KDE-приложение использует тот же проверенный `kwispr.sh`; CLI остаётся рабочим независимо от tray.

## Если что-то не работает

| Симптом | Что проверить |
|---|---|
| Микрофон не записывается | `pactl list sources short` и `KWISPR_PULSE_SOURCE` |
| Конец фразы обрезается | Увеличить `KWISPR_VAD_PADDING_MS`, например до `1500` |
| Текст не вставляется | Запущен ли `ydotoold`; попробовать `shift-insert` |
| Локальный сервер недоступен | `curl http://127.0.0.1:19650/health` |
| Ошибка `unknown model` | Скачать модель через `kwispr-models.py download` |

## Разработка

```bash
python3 -m unittest discover
./kde-whisper/scripts/podman-test.sh
./rust-local-stt/build-in-podman.sh
```

Подробнее: [локальный STT](docs/local-stt.md) · [KDE-приложение](docs/kde-whisper.md)

## Лицензия

[MIT](LICENSE)
