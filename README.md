# Subgen

Subgen generates subtitles for personal media, but this fork no longer runs
Whisper locally. It keeps the media-server integrations, queueing, skip logic,
audio-track selection, and subtitle file writing, then sends transcription work
to an OpenAI-compatible audio endpoint.

Supported entry points:

- Bazarr Whisper provider via `POST /asr`
- Plex, Jellyfin, Emby, and Tautulli webhooks
- Folder scanning with `TRANSCRIBE_FOLDERS`
- Manual folder/file queueing with `POST /batch`
- Language detection with `POST /detect-language`

## Quick Start

```yaml
services:
  subgen:
    build: .
    ports:
      - "9000:9000"
    environment:
      OPENAI_BASE_URL: "https://api.openai.com/v1"
      OPENAI_API_KEY: "${OPENAI_API_KEY}"
      OPENAI_TRANSCRIPTION_MODEL: "whisper-1"
      OPENAI_TRANSLATION_MODEL: "whisper-1"
      OPENAI_AUDIO_FORMAT: "wav"
      WEBHOOK_PORT: "9000"
    volumes:
      - /path/to/tv:/tv
      - /path/to/movies:/movies
```

For a local OpenAI-compatible service, set `OPENAI_BASE_URL` to that service's
base URL, usually something like `http://host.docker.internal:8000/v1`.

## Bazarr

1. In Bazarr, go to `Settings > Whisper Provider`.
2. Select `Whisper`.
3. Set the Docker Endpoint to `http://<subgen-host>:9000`.
4. Save.

For the Amazon WEB-DL audio offset fix, mount media paths into Subgen exactly as
Bazarr sees them and enable Bazarr's `Pass Video Name` option. Subgen uses
`ffprobe` to detect audio `start_time` and shifts returned SRT timestamps.

Bazarr's `encode=false` upload is raw 16 kHz mono PCM, not a WAV container.
Subgen wraps that payload into a real audio file before sending it to the
remote transcription endpoint.

## Configuration

### Endpoint

| Variable | Default | Description |
|---|---:|---|
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base URL for the OpenAI-compatible API. |
| `OPENAI_TRANSCRIPTIONS_URL` | empty | Full override for the transcriptions endpoint. |
| `OPENAI_TRANSLATIONS_URL` | empty | Full override for the translations endpoint. |
| `OPENAI_API_KEY` | empty | Bearer token. Leave empty only if your compatible endpoint does not require auth. |
| `OPENAI_ORGANIZATION` | empty | Optional OpenAI organization header. |
| `OPENAI_PROJECT` | empty | Optional OpenAI project header. |
| `OPENAI_TRANSCRIPTION_MODEL` | `whisper-1` | Model sent to `/audio/transcriptions`. |
| `OPENAI_TRANSLATION_MODEL` | `whisper-1` | Model sent to `/audio/translations`. |
| `OPENAI_API_TIMEOUT` | `18000` | Request timeout and `/asr` wait timeout in seconds. |
| `OPENAI_EXTRA_PARAMS` | `{}` | Optional JSON object merged into every audio API request. |

### Audio Preparation

| Variable | Default | Description |
|---|---:|---|
| `OPENAI_AUDIO_FORMAT` | `wav` | Format used when Subgen extracts or normalizes audio before upload. WAV is the safest default for whisper.cpp-compatible servers. |
| `OPENAI_AUDIO_BITRATE` | `64k` | Bitrate for compressed upload formats. |
| `MIN_REMOTE_AUDIO_SECONDS` | `0.1` | Reject extracted WAV payloads shorter than this before sending them to the endpoint. |
| `AUDIO_DEBUG` | `False` | Log audio stream discovery, selected track, ffmpeg command/stderr, payload byte size, hash, and WAV duration. |
| `AUDIO_DEBUG_SAVE` | `False` | Save the exact upload payload for inspection. |
| `AUDIO_DEBUG_DIR` | `/tmp/subgen-audio-debug` | Directory used when `AUDIO_DEBUG_SAVE=True`. |
| `DETECT_LANGUAGE_LENGTH` | `30` | Seconds to send for language detection. |
| `DETECT_LANGUAGE_OFFSET` | `0` | Seconds to skip before language detection. |

Subgen still requires `ffmpeg` and `ffprobe`. They are used only for media
inspection and audio extraction, not for local speech recognition.

For extraction problems, run with:

```yaml
environment:
  AUDIO_DEBUG: "True"
  AUDIO_DEBUG_SAVE: "True"
```

Then inspect the saved payload:

```bash
docker exec subgen ls -lh /tmp/subgen-audio-debug
docker exec subgen ffprobe -hide_banner /tmp/subgen-audio-debug/<saved-file>.wav
```

### Queueing and Triggers

| Variable | Default | Description |
|---|---:|---|
| `CONCURRENT_TRANSCRIPTIONS` | `2` | Number of worker threads sending requests to the endpoint. |
| `PROCESS_ADDED_MEDIA` | `True` | Process newly added media from webhook events. |
| `PROCESS_MEDIA_ON_PLAY` | `True` | Process media when playback starts. |
| `TRANSCRIBE_FOLDERS` | empty | Pipe-separated paths to scan, for example `/tv\|/movies`. |
| `MONITOR` | `False` | Watch `TRANSCRIBE_FOLDERS` for new files. |
| `WEBHOOK_URL_COMPLETED` | empty | Optional POST callback after a file subtitle is generated. |

### Subtitle Output

| Variable | Default | Description |
|---|---:|---|
| `TRANSCRIBE_OR_TRANSLATE` | `transcribe` | Use `transcribe` or `translate`. |
| `SUBTITLE_LANGUAGE_NAME` | empty | Override the language part of generated subtitle filenames. |
| `SUBTITLE_LANGUAGE_NAMING_TYPE` | `ISO_639_2_B` | Filename language style: `ISO_639_1`, `ISO_639_2_T`, `ISO_639_2_B`, `NAME`, or `NATIVE`. |
| `LRC_FOR_AUDIO_FILES` | `True` | Write `.lrc` instead of `.srt` for pure audio files. |
| `APPEND` | `False` | Append a short Subgen watermark to generated SRT files. |
| `SHOW_IN_SUBNAME_SUBGEN` | `True` | Include `.subgen` in generated subtitle names. |
| `SHOW_IN_SUBNAME_MODEL` | `True` | Include the transcription model in generated subtitle names. |

### Skip Logic and Audio Selection

| Variable | Default | Description |
|---|---:|---|
| `SKIP_IF_TARGET_SUBTITLES_EXIST` | `True` | Skip when a matching generated subtitle already exists. |
| `SKIP_IF_EXTERNAL_SUBTITLES_EXIST` | `False` | Skip when a matching external subtitle exists. |
| `SKIP_IF_INTERNAL_SUBTITLES_LANGUAGE` | empty | Skip when an embedded subtitle language exists. |
| `SKIP_SUBTITLE_LANGUAGES` | empty | Pipe-separated embedded subtitle languages that should skip processing. |
| `SKIP_IF_AUDIO_LANGUAGES` | empty | Pipe-separated audio languages that should skip processing. |
| `PREFERRED_AUDIO_LANGUAGES` | `eng` | Pipe-separated audio languages to prefer for multi-track media. |
| `LIMIT_TO_PREFERRED_AUDIO_LANGUAGE` | `False` | Skip files without a preferred audio track. |
| `FORCE_DETECTED_LANGUAGE_TO` | empty | Force a two-letter source language. |
| `SHOULD_DETECT_AUDIO_LANGUAGE` | `False` | Use the endpoint to detect language when media metadata is unknown. |
| `SKIP_UNKNOWN_LANGUAGE` | `False` | Skip when no language can be determined. |
| `SKIP_ONLY_SUBGEN_SUBTITLES` | `False` | Only treat filenames containing `subgen` as generated subtitles. |
| `SKIP_IF_NO_LANGUAGE_BUT_SUBTITLES_EXIST` | `False` | Skip unknown-language media if any subtitle already exists. |

The legacy variable `SHOULD_WHISPER_DETECT_AUDIO_LANGUAGE` is still accepted as
a fallback for `SHOULD_DETECT_AUDIO_LANGUAGE`.

### Paths and Server Integrations

| Variable | Default | Description |
|---|---:|---|
| `WEBHOOK_PORT` | `9000` | Port for the FastAPI service. |
| `PUID` / `PGID` | `99` / `100` | Container user/group IDs. |
| `DEBUG` | `True` | Enable verbose logs. |
| `USE_PATH_MAPPING` | `False` | Rewrite media paths before queueing. |
| `PATH_MAPPING_FROM` | `/tv` | Prefix seen by the media server. |
| `PATH_MAPPING_TO` | `/Volumes/TV` | Prefix seen by Subgen. |
| `PLEX_SERVER` / `PLEX_TOKEN` | empty | Plex API details for webhooks and metadata refresh. |
| `JELLYFIN_SERVER` / `JELLYFIN_TOKEN` | empty | Jellyfin API details for webhooks and metadata refresh. |

## Standalone

```bash
python3 -m pip install -r requirements.txt
OPENAI_API_KEY=... python3 -u subgen.py
```

`launcher.py` remains as a small convenience wrapper for loading `subgen.env`
and optionally installing requirements:

```bash
python3 launcher.py --install
python3 launcher.py
```

It no longer downloads code from upstream at runtime.

## Notes

- OpenAI's public API currently has per-file upload limits. If you use the
  official endpoint for long media files, configure `OPENAI_AUDIO_FORMAT` and
  `OPENAI_AUDIO_BITRATE` conservatively, or use a compatible endpoint designed
  for longer uploads.
- `whisper-1` is the default because it supports `srt`, `vtt`, and
  `verbose_json`, which Subgen uses for subtitle and language metadata.
- GPU, CUDA, ROCm, Torch, faster-whisper, stable-ts, local model caches, and
  VRAM cleanup settings are intentionally gone from this fork.
