subgen_version = '2026.05.3'

"""
ENVIRONMENT VARIABLES DOCUMENTATION

This application supports both new standardized environment variable names and legacy names for backwards compatibility. The new names follow a consistent naming convention: 

STANDARDIZED NAMING CONVENTION:
- Use UPPERCASE with underscores for separation
- Group related variables with consistent prefixes: 
  * PLEX_* for Plex server integration
  * JELLYFIN_* for Jellyfin server integration
  * PROCESS_* for media processing triggers
  * SKIP_* for all skip conditions
  * SUBTITLE_* for subtitle-related settings
  * OPENAI_* for OpenAI-compatible transcription endpoint settings
  * TRANSCRIBE_* for transcription settings

BACKWARDS COMPATIBILITY: 
Legacy environment variable names are still supported. If both new and old names are set,
the new standardized name takes precedence. 

NEW NAME → OLD NAME (for backwards compatibility):
- PLEX_TOKEN → PLEXTOKEN
- PLEX_SERVER → PLEXSERVER
- JELLYFIN_TOKEN → JELLYFINTOKEN
- JELLYFIN_SERVER → JELLYFINSERVER
- PROCESS_ADDED_MEDIA → PROCADDEDMEDIA
- PROCESS_MEDIA_ON_PLAY → PROCMEDIAONPLAY
- SUBTITLE_LANGUAGE_NAME → NAMESUBLANG
- WEBHOOK_PORT → WEBHOOKPORT
- SKIP_IF_EXTERNAL_SUBTITLES_EXIST → SKIPIFEXTERNALSUB
- SKIP_IF_TARGET_SUBTITLES_EXIST → SKIP_IF_TO_TRANSCRIBE_SUB_ALREADY_EXIST
- SKIP_IF_INTERNAL_SUBTITLES_LANGUAGE → SKIPIFINTERNALSUBLANG
- SKIP_SUBTITLE_LANGUAGES → SKIP_LANG_CODES
- SKIP_IF_AUDIO_LANGUAGES → SKIP_IF_AUDIO_TRACK_IS
- SKIP_ONLY_SUBGEN_SUBTITLES → ONLY_SKIP_IF_SUBGEN_SUBTITLE
- SKIP_IF_NO_LANGUAGE_BUT_SUBTITLES_EXIST → SKIP_IF_LANGUAGE_IS_NOT_SET_BUT_SUBTITLES_EXIST

MIGRATION GUIDE:
Users can gradually migrate to the new names. Both will work simultaneously during the
transition period. The old names may be deprecated in future versions. 
"""

import asyncio
import hashlib
import io
import json
import logging
import mimetypes
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime
from threading import Event, Lock
from typing import List, Union

import av
import ffmpeg
import requests
from fastapi import Body, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver as Observer

from language_code import LanguageCode


def convert_to_bool(in_bool):
    # Convert the input to string and lower case, then check against true values
    return str(in_bool).lower() in ('true', 'on', '1', 'y', 'yes')

def get_env_with_fallback(new_name: str, old_name: str, default_value=None, convert_func=None):
    """
    Get environment variable with backwards compatibility fallback.
    
    Args:
        new_name: The new standardized environment variable name
        old_name: The legacy environment variable name for backwards compatibility
        default_value: Default value if neither variable is set
        convert_func: Optional function to convert the value (e.g., convert_to_bool, int)
    
    Returns:
        The environment variable value, converted if convert_func is provided
    """
    # Try new name first, then fall back to old name
    value = os.getenv(new_name) or os.getenv(old_name)
    
    if value is None:
        value = default_value
    
    # Apply conversion function if provided
    if convert_func and value is not None:
        return convert_func(value)
    
    return value
    
# Server Integration - with backwards compatibility
plextoken = get_env_with_fallback('PLEX_TOKEN', 'PLEXTOKEN', 'token here')
plexserver = get_env_with_fallback('PLEX_SERVER', 'PLEXSERVER', 'http://192.168.1.111:32400')
jellyfintoken = get_env_with_fallback('JELLYFIN_TOKEN', 'JELLYFINTOKEN', 'token here')
jellyfinserver = get_env_with_fallback('JELLYFIN_SERVER', 'JELLYFINSERVER', 'http://192.168.1.111:8096')

# OpenAI-compatible transcription endpoint configuration
openai_base_url = os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1')
openai_transcriptions_url = os.getenv('OPENAI_TRANSCRIPTIONS_URL', '')
openai_translations_url = os.getenv('OPENAI_TRANSLATIONS_URL', '')
openai_api_key = os.getenv('OPENAI_API_KEY', '')
openai_organization = os.getenv('OPENAI_ORGANIZATION', '')
openai_project = os.getenv('OPENAI_PROJECT', '')
transcription_model = os.getenv('OPENAI_TRANSCRIPTION_MODEL', os.getenv('TRANSCRIPTION_MODEL', 'whisper-1'))
translation_model = os.getenv('OPENAI_TRANSLATION_MODEL', 'whisper-1')
openai_api_timeout = int(os.getenv('OPENAI_API_TIMEOUT', os.getenv('ASR_TIMEOUT', 18000)))
openai_audio_format = os.getenv('OPENAI_AUDIO_FORMAT', 'wav').lower().lstrip('.')
openai_audio_bitrate = os.getenv('OPENAI_AUDIO_BITRATE', '64k')
openai_temperature = os.getenv('OPENAI_TEMPERATURE', '')
min_remote_audio_seconds = float(os.getenv('MIN_REMOTE_AUDIO_SECONDS', '0.1'))
audio_debug = convert_to_bool(os.getenv('AUDIO_DEBUG', False))
audio_debug_save = convert_to_bool(os.getenv('AUDIO_DEBUG_SAVE', False))
audio_debug_dir = os.getenv('AUDIO_DEBUG_DIR', '/tmp/subgen-audio-debug')
try:
    openai_extra_params = json.loads(os.getenv('OPENAI_EXTRA_PARAMS', '{}') or '{}')
    if not isinstance(openai_extra_params, dict):
        raise ValueError("OPENAI_EXTRA_PARAMS must be a JSON object")
except ValueError:
    openai_extra_params = {}
    logging.info("OPENAI_EXTRA_PARAMS is invalid, defaulting to empty '{}'")

concurrent_transcriptions = int(os.getenv('CONCURRENT_TRANSCRIPTIONS', 2))

# Processing Control - with backwards compatibility
procaddedmedia = get_env_with_fallback('PROCESS_ADDED_MEDIA', 'PROCADDEDMEDIA', True, convert_to_bool)
procmediaonplay = get_env_with_fallback('PROCESS_MEDIA_ON_PLAY', 'PROCMEDIAONPLAY', True, convert_to_bool)

# Subtitle Configuration - with backwards compatibility
subtitle_language_name = get_env_with_fallback('SUBTITLE_LANGUAGE_NAME', 'NAMESUBLANG', '')

# System Configuration - with backwards compatibility
webhookport = get_env_with_fallback('WEBHOOK_PORT', 'WEBHOOKPORT', 9000, int)
debug = convert_to_bool(os.getenv('DEBUG', True))
use_path_mapping = convert_to_bool(os.getenv('USE_PATH_MAPPING', False))
path_mapping_from = os.getenv('PATH_MAPPING_FROM', r'/tv')
path_mapping_to = os.getenv('PATH_MAPPING_TO', r'/Volumes/TV')
monitor = convert_to_bool(os.getenv('MONITOR', False))
transcribe_folders = os.getenv('TRANSCRIBE_FOLDERS', '')
transcribe_or_translate = os.getenv('TRANSCRIBE_OR_TRANSLATE', 'transcribe').lower()
append = convert_to_bool(os.getenv('APPEND', False))
reload_script_on_change = convert_to_bool(os.getenv('RELOAD_SCRIPT_ON_CHANGE', False))
lrc_for_audio_files = convert_to_bool(os.getenv('LRC_FOR_AUDIO_FILES', True))
detect_language_length = int(os.getenv('DETECT_LANGUAGE_LENGTH', 30))
detect_language_offset = int(os.getenv('DETECT_LANGUAGE_OFFSET', 0))
asr_timeout = openai_api_timeout
webhook_url_completed = os.getenv('WEBHOOK_URL_COMPLETED', '')

# Skip Configuration - with backwards compatibility
skip_if_external_sub_exists = get_env_with_fallback('SKIP_IF_EXTERNAL_SUBTITLES_EXIST', 'SKIPIFEXTERNALSUB', False, convert_to_bool)
skip_if_target_subtitle_exists = get_env_with_fallback('SKIP_IF_TARGET_SUBTITLES_EXIST', 'SKIP_IF_TO_TRANSCRIBE_SUB_ALREADY_EXIST', True, convert_to_bool)
skip_if_internal_sub_language = LanguageCode.from_string(get_env_with_fallback('SKIP_IF_INTERNAL_SUBTITLES_LANGUAGE', 'SKIPIFINTERNALSUBLANG', ''))
plex_queue_next_episode = convert_to_bool(os.getenv('PLEX_QUEUE_NEXT_EPISODE', False))
plex_queue_season = convert_to_bool(os.getenv('PLEX_QUEUE_SEASON', False))
plex_queue_series = convert_to_bool(os.getenv('PLEX_QUEUE_SERIES', False))
# Language and Skip Configuration - with backwards compatibility
skip_subtitle_languages = ([LanguageCode.from_string(code) for code in get_env_with_fallback('SKIP_SUBTITLE_LANGUAGES', 'SKIP_LANG_CODES', '').split("|")]
        if get_env_with_fallback('SKIP_SUBTITLE_LANGUAGES', 'SKIP_LANG_CODES')
    else[]
)
force_detected_language_to = LanguageCode.from_string(os.getenv('FORCE_DETECTED_LANGUAGE_TO', ''))
preferred_audio_languages =[
    LanguageCode.from_string(code) 
    for code in os.getenv('PREFERRED_AUDIO_LANGUAGES', 'eng').split("|")
] # in order of preference
limit_to_preferred_audio_languages = convert_to_bool(os.getenv('LIMIT_TO_PREFERRED_AUDIO_LANGUAGE', False))
skip_audio_languages = ([LanguageCode.from_string(code) for code in get_env_with_fallback('SKIP_IF_AUDIO_LANGUAGES', 'SKIP_IF_AUDIO_TRACK_IS', '').split("|")]
    if get_env_with_fallback('SKIP_IF_AUDIO_LANGUAGES', 'SKIP_IF_AUDIO_TRACK_IS')
    else[]
)

# Additional Subtitle Configuration - with backwards compatibility
subtitle_language_naming_type = os.getenv('SUBTITLE_LANGUAGE_NAMING_TYPE', 'ISO_639_2_B')
only_match_subgen_subtitles = get_env_with_fallback('SKIP_ONLY_SUBGEN_SUBTITLES', 'ONLY_SKIP_IF_SUBGEN_SUBTITLE', False, convert_to_bool)
skip_unknown_language = convert_to_bool(os.getenv('SKIP_UNKNOWN_LANGUAGE', False))
skip_if_no_audio_language_but_subtitles_exist = get_env_with_fallback('SKIP_IF_NO_LANGUAGE_BUT_SUBTITLES_EXIST', 'SKIP_IF_LANGUAGE_IS_NOT_SET_BUT_SUBTITLES_EXIST', False, convert_to_bool)
should_whisper_detect_audio_language = get_env_with_fallback('SHOULD_DETECT_AUDIO_LANGUAGE', 'SHOULD_WHISPER_DETECT_AUDIO_LANGUAGE', False, convert_to_bool)
show_in_subname_subgen = convert_to_bool(os.getenv('SHOW_IN_SUBNAME_SUBGEN', True))
show_in_subname_model = convert_to_bool(os.getenv('SHOW_IN_SUBNAME_MODEL', True))

VIDEO_EXTENSIONS = (
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mpg", ".mpeg", 
    ".3gp", ".ogv", ".vob", ".rm", ".rmvb", ".ts", ".m4v", ".f4v", ".svq3", 
    ".asf", ".m2ts", ".divx", ".xvid"
)

AUDIO_EXTENSIONS = (
    ".mp3", ".wav", ".aac", ".flac", ".ogg", ".wma", ".alac", ".m4a", ".opus", 
    ".aiff", ".aif", ".pcm", ".ra", ".ram", ".mid", ".midi", ".ape", ".wv", 
    ".amr", ".vox", ".tak", ".spx", ".m4b", ".mka"
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    if transcribe_folders:
        threading.Thread(target=transcribe_existing, args=(transcribe_folders,), daemon=True).start()
    yield

app = FastAPI(lifespan=lifespan)

in_docker = os.path.exists('/.dockerenv')
docker_status = "Docker" if in_docker else "Standalone"

# ============================================================================
# TASK RESULT STORAGE (for blocking endpoints)
# ============================================================================

class TaskResult:
    """Stores the result of a queued task for blocking retrieval"""
    def __init__(self):
        self.result = None
        self.error = None
        self.done = Event()
    
    def set_result(self, result):
        self.result = result
        self.done.set()
    
    def set_error(self, error):
        self.error = error
        self.done.set()
    
    def wait(self, timeout=None):
        """Block until result is ready. Returns True if completed, False if timeout."""
        return self.done.wait(timeout)

# Dictionary to store task results keyed by task_id
# Entries are cleaned up in /asr endpoint finally block to prevent unbounded growth
task_results = {}
task_results_lock = Lock()

# ============================================================================
# HASH GENERATION FOR DEDUPLICATION
# ============================================================================

def generate_audio_hash(audio_content: bytes, task: str = None, language: str = None) -> str:
    """
    Generate a deterministic hash from audio content and optional parameters. 
    
    Same audio + same task + same language = always same hash. 
    This ensures duplicate requests are caught by the queue. 
    
    Args:
        audio_content: Raw audio bytes from uploaded file
        task: Optional task type ('transcribe' or 'translate')
        language: Optional target language code
        
    Returns: 
        SHA256 hash (first 16 chars for brevity in logs)
    """
    hash_input = audio_content
    
    # Include task and language for fine-grained deduplication
    if task:
        hash_input += task.encode('utf-8')
    if language:
        hash_input += language.encode('utf-8')
    
    full_hash = hashlib.sha256(hash_input).hexdigest()
    return full_hash[:16] # Use first 16 chars for shorter IDs in logs

# ============================================================================
# REFACTORED DEDUPLICATED QUEUE WITH BETTER TRACKING
# ============================================================================

class DeduplicatedQueue(queue.PriorityQueue):
    """Queue that prevents duplicates, handles priority, and tracks status."""
    def __init__(self):
        super().__init__()
        self._queued = set()     # Tracks task IDs waiting in queue
        self._processing = set() # Tracks task IDs currently being handled
        self._lock = Lock()

    def put(self, item, block=True, timeout=None):
        with self._lock:
            task_id = item["path"]
            if task_id not in self._queued and task_id not in self._processing:
                # Priority: 0 (Detect), 1 (ASR), 2 (Transcribe)
                task_type = item.get("type", "transcribe")
                priority = 0 if task_type == "detect_language" else (1 if task_type == "asr" else 2)
                
                # PriorityQueue requires a tuple: (priority, tie_breaker, item)
                super().put((priority, time.time(), item), block, timeout)
                self._queued.add(task_id)
                return True
            return False

    def get(self, block=True, timeout=None):
        # PriorityQueue returns the tuple, we want just the item
        priority, timestamp, item = super().get(block, timeout)
        with self._lock:
            task_id = item["path"]
            self._queued.discard(task_id)
            self._processing.add(task_id)
        return item

    def mark_done(self, item):
        with self._lock:
            task_id = item["path"]
            self._processing.discard(task_id)

    def is_idle(self):
        with self._lock:
            return self.empty() and len(self._processing) == 0

    def is_active(self, task_id):
        """Checks if a task_id is currently queued or processing."""
        with self._lock:
            return task_id in self._queued or task_id in self._processing

    def get_queued_tasks(self):
        with self._lock:
            return list(self._queued)

    def get_processing_tasks(self):
        with self._lock:
            return list(self._processing)

# Start queue
task_queue = DeduplicatedQueue()

# ============================================================================
# TRANSCRIPTION WORKER
# ============================================================================

def transcription_worker():
    """Main worker thread with centralized logging and status tracking."""
    while True:
        task = None
        next_task = None
        try:
            task = task_queue.get(block=True, timeout=1)
            task_type = task.get("type", "transcribe")
            path = task.get("path", "unknown")
            display_name = os.path.basename(path) if ("/" in str(path) or "\\" in str(path)) else path
            
            # Status for START log
            proc_count = len(task_queue.get_processing_tasks())
            queue_count = len(task_queue.get_queued_tasks())
            logging.info(f"WORKER START :[{task_type.upper():<10}] {display_name:^40} | Jobs: {proc_count} processing, {queue_count} queued")
            
            start_time = time.time()
            if task_type == "detect_language": 
                if "audio_content" in task: 
                    detect_language_from_upload(task)
                else: 
                    # Capture the transcription task to queue later
                    next_task = detect_language_task(task['path'], original_task_data=task)
            elif task_type == "asr":
                asr_task_worker(task)
            else: # transcribe
                gen_subtitles(task['path'], task['transcribe_or_translate'], task['force_language'], audio_tracks=task.get('audio_tracks'))
                
                # --- METADATA REFRESH LOGIC ---
                if 'plex_item_id' in task:
                    try:
                        logging.info(f"Refreshing Plex Metadata for item {task['plex_item_id']}")
                        refresh_plex_metadata(task['plex_item_id'], task['plex_server'], task['plex_token'])
                    except Exception as e:
                        logging.error(f"Failed to refresh Plex metadata: {e}")
                
                if 'jellyfin_item_id' in task:
                    try:
                        logging.info(f"Refreshing Jellyfin Metadata for item {task['jellyfin_item_id']}")
                        refresh_jellyfin_metadata(task['jellyfin_item_id'], task['jellyfin_server'], task['jellyfin_token'])
                    except Exception as e:
                        logging.error(f"Failed to refresh Jellyfin metadata: {e}")
                # ------------------------------
            
            # Status for FINISH log
            elapsed = time.time() - start_time
            m, s = divmod(int(elapsed), 60)
            remaining_queued = len(task_queue.get_queued_tasks())
            logging.info(f"WORKER FINISH: [{task_type.upper():<10}] {display_name:^40} in {m}m {s}s | Remaining: {remaining_queued} queued")

        except queue.Empty:
            continue
        except Exception as e:
            logging.error(f"Error processing task: {e}", exc_info=True)
        finally:
            if task:
                task_queue.task_done()
                task_queue.mark_done(task)
                
                # Now that the detect task is removed from processing, it's safe to queue the transcription
                if next_task:
                    if task_queue.put(next_task):
                        logging.debug(f"Queued transcription for detected language: {next_task['path']}")
                    else:
                        logging.debug(f"Transcription already queued/processing for: {next_task['path']}")

# Create worker threads
for _ in range(concurrent_transcriptions):
    threading.Thread(target=transcription_worker, daemon=True).start()

# Define a filter class to hide common logging we don't want to see
class MultiplePatternsFilter(logging.Filter):
    def filter(self, record):
        # Define the patterns to search for
        patterns =[
            "Compression ratio threshold is not met",
            "Processing segment at",
            "Log probability threshold is",
            "Reset prompt",
            "Attempting to release",
            "released on ",
            "Attempting to acquire",
            "acquired on",
            "header parsing failed",
            "timescale not set",
            "misdetection possible",
            "srt was added",
            "doesn't have any audio to transcribe",
            "Calling on_"
        ]
        # Return False if any of the patterns are found, True otherwise
        return not any(pattern in record.getMessage() for pattern in patterns)

# Configure logging
if debug:
    level = logging.DEBUG
else:
    level = logging.INFO

logging.basicConfig(
    stream=sys.stderr, 
    level=level, 
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S" # This removes the ,123 part
)

# Get the root logger
logger = logging.getLogger()
logger.setLevel(level) # Set the logger level

for handler in logger.handlers:
    handler.addFilter(MultiplePatternsFilter())

logging.getLogger("multipart").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("watchfiles").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


TIME_OFFSET = 5
RAW_PCM_UPLOAD_SAMPLE_RATE = 16000
RAW_PCM_UPLOAD_CHANNELS = 1
RAW_PCM_UPLOAD_SAMPLE_WIDTH = 2
RAW_PCM_UPLOAD_BYTES_PER_SECOND = (
    RAW_PCM_UPLOAD_SAMPLE_RATE * RAW_PCM_UPLOAD_CHANNELS * RAW_PCM_UPLOAD_SAMPLE_WIDTH
)

SRT_TIME_RE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})"
)


def seconds_to_srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    millis = int(round((seconds - int(seconds)) * 1000))
    total_seconds = int(seconds)
    if millis == 1000:
        total_seconds += 1
        millis = 0
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def srt_time_to_seconds(value: str) -> float:
    time_part, millis = value.replace('.', ',').split(',')
    hours, minutes, seconds = [int(part) for part in time_part.split(':')]
    return hours * 3600 + minutes * 60 + seconds + int(millis) / 1000


def shift_srt_timestamps(srt_text: str, offset: float) -> str:
    if offset <= 0:
        return srt_text

    def replace(match):
        start = seconds_to_srt_time(srt_time_to_seconds(match.group("start")) + offset)
        end = seconds_to_srt_time(srt_time_to_seconds(match.group("end")) + offset)
        return f"{start} --> {end}"

    shifted = SRT_TIME_RE.sub(replace, srt_text)
    logging.info(f"Applied +{offset:.3f}s timestamp offset")
    return shifted


def append_srt_watermark(srt_text: str) -> str:
    if not append or not srt_text.strip():
        return srt_text

    matches = list(SRT_TIME_RE.finditer(srt_text))
    last_end = srt_time_to_seconds(matches[-1].group("end")) if matches else 0.0
    start = last_end + TIME_OFFSET
    end = start + 4
    blocks = [block for block in re.split(r"\n\s*\n", srt_text.strip()) if block.strip()]
    next_index = len(blocks) + 1
    date_time_str = datetime.now().strftime("%d %b %Y - %H:%M:%S")
    appended_text = f"Transcribed by Subgen using {transcription_model} on {date_time_str}"
    block = f"{next_index}\n{seconds_to_srt_time(start)} --> {seconds_to_srt_time(end)}\n{appended_text}"
    return f"{srt_text.rstrip()}\n\n{block}\n"


def openai_audio_url(task: str) -> str:
    if task == "translate" and openai_translations_url:
        return openai_translations_url
    if task != "translate" and openai_transcriptions_url:
        return openai_transcriptions_url

    base = openai_base_url.rstrip("/")
    if base.endswith("/audio/transcriptions") or base.endswith("/audio/translations"):
        return re.sub(r"/audio/(transcriptions|translations)$", f"/audio/{'translations' if task == 'translate' else 'transcriptions'}", base)
    return f"{base}/audio/{'translations' if task == 'translate' else 'transcriptions'}"


def openai_headers() -> dict:
    headers = {}
    if openai_api_key:
        headers["Authorization"] = f"Bearer {openai_api_key}"
    if openai_organization:
        headers["OpenAI-Organization"] = openai_organization
    if openai_project:
        headers["OpenAI-Project"] = openai_project
    return headers


def guess_mime_type(filename: str) -> str:
    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or "application/octet-stream"


def remote_audio_request(
    audio_content: bytes,
    filename: str,
    task: str = "transcribe",
    language: str | None = None,
    prompt: str | None = None,
    response_format: str = "srt",
):
    model_name = translation_model if task == "translate" else transcription_model
    data = {
        "model": model_name,
        "response_format": response_format,
    }
    if task != "translate" and language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt
    if openai_temperature != "":
        data["temperature"] = openai_temperature
    data.update(openai_extra_params)

    files = {
        "file": (filename, audio_content, guess_mime_type(filename)),
    }
    url = openai_audio_url(task)

    logging.info(f"Sending {task} request to {url} with model {model_name}")
    log_audio_debug(
        f"AUDIO DEBUG posting request task={task} filename={filename} content_type={guess_mime_type(filename)} "
        f"bytes={len(audio_content)} response_format={response_format} language={language}"
    )
    response = requests.post(
        url,
        headers=openai_headers(),
        data=data,
        files=files,
        timeout=openai_api_timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Transcription endpoint returned HTTP {response.status_code}: {response.text[:1000]}")

    content_type = response.headers.get("content-type", "")
    if response_format in {"json", "verbose_json"} or "application/json" in content_type:
        return response.json()
    return response.text


def response_text(response) -> str:
    if isinstance(response, dict):
        return response.get("text", json.dumps(response))
    return str(response)


def response_language(response, fallback: LanguageCode = LanguageCode.NONE, task: str = "transcribe") -> LanguageCode:
    if task == "translate":
        return LanguageCode.ENGLISH
    if isinstance(response, dict):
        detected = LanguageCode.from_string(response.get("language"))
        if detected:
            return detected
    return fallback or LanguageCode.NONE


def segments_to_srt(segments: list) -> str:
    lines = []
    for index, segment in enumerate(segments, start=1):
        start = segment.get("start", 0)
        end = segment.get("end", start + 5)
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        lines.append(f"{index}\n{seconds_to_srt_time(start)} --> {seconds_to_srt_time(end)}\n{text}")
    return "\n\n".join(lines) + ("\n" if lines else "")


def segments_to_lrc(segments: list) -> str:
    lines = []
    for segment in segments:
        text = str(segment.get("text", "")).strip().replace("\n", " ")
        if not text:
            continue
        start = float(segment.get("start", 0))
        minutes, seconds = divmod(int(start), 60)
        fraction = int((start - int(start)) * 100)
        lines.append(f"[{minutes:02d}:{seconds:02d}.{fraction:02d}]{text}")
    return "\n".join(lines) + ("\n" if lines else "")


def segments_to_tsv(segments: list) -> str:
    lines = ["start\tend\ttext"]
    for segment in segments:
        text = str(segment.get("text", "")).strip().replace("\t", " ").replace("\n", " ")
        lines.append(f"{int(float(segment.get('start', 0)) * 1000)}\t{int(float(segment.get('end', 0)) * 1000)}\t{text}")
    return "\n".join(lines) + "\n"


def response_to_srt(response) -> str:
    if isinstance(response, dict):
        if isinstance(response.get("segments"), list):
            return segments_to_srt(response["segments"])
        return f"1\n00:00:00,000 --> 00:00:05,000\n{response.get('text', '').strip()}\n"
    return str(response)


def response_to_lrc(response) -> str:
    if isinstance(response, dict) and isinstance(response.get("segments"), list):
        return segments_to_lrc(response["segments"])
    text = response_text(response).strip()
    return f"[00:00.00]{text}\n" if text else ""


def output_from_response(response, output: str) -> str:
    if output == "json":
        return json.dumps(response, ensure_ascii=False)
    if output == "tsv":
        if isinstance(response, dict) and isinstance(response.get("segments"), list):
            return segments_to_tsv(response["segments"])
        return f"start\tend\ttext\n0\t0\t{response_text(response).strip()}\n"
    if output == "srt":
        return response_to_srt(response)
    return response_text(response)


def log_audio_debug(message: str) -> None:
    if audio_debug:
        logging.info(message)
    else:
        logging.debug(message)


def audio_output_kwargs() -> dict:
    if openai_audio_format == "wav":
        return {"format": "wav", "acodec": "pcm_s16le", "ac": 1, "ar": 16000, "vn": None}
    kwargs = {"format": openai_audio_format, "ac": 1, "ar": 16000, "vn": None}
    if openai_audio_bitrate:
        kwargs["audio_bitrate"] = openai_audio_bitrate
    return kwargs


def remote_audio_filename(source_name: str) -> str:
    base = os.path.splitext(os.path.basename(source_name or "audio"))[0] or "audio"
    extension = "wav" if openai_audio_format == "wav" else openai_audio_format
    return f"{base}.{extension}"


def extension_from_content_type(content_type: str | None = None) -> str:
    normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if not normalized_content_type:
        return ""

    extension = mimetypes.guess_extension(normalized_content_type, strict=False)
    if extension:
        return extension

    return {
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "video/mp4": ".mp4",
    }.get(normalized_content_type, "")


def upload_source_name(
    upload_filename: str | None = None,
    video_file: str | None = None,
    content_type: str | None = None,
    fallback: str = "audio.wav",
) -> str:
    inferred_extension = extension_from_content_type(content_type) or os.path.splitext(fallback)[1]
    for candidate in (upload_filename, video_file):
        if candidate:
            candidate_name = os.path.basename(candidate)
            if candidate_name:
                if inferred_extension and not os.path.splitext(candidate_name)[1]:
                    return f"{candidate_name}{inferred_extension}"
                return candidate_name
    return fallback


def upload_media_kind(filename: str | None = None, content_type: str | None = None) -> str:
    normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized_content_type.startswith("audio/"):
        return "audio"
    if normalized_content_type.startswith("video/"):
        return "video"

    extension = os.path.splitext(filename or "")[1].casefold()
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    return "unknown"


def safe_debug_filename(filename: str) -> str:
    base = os.path.basename(filename or "audio")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)


def save_audio_debug_payload(audio_content: bytes, remote_name: str, source_name: str) -> None:
    if not audio_debug_save:
        return

    try:
        os.makedirs(audio_debug_dir, exist_ok=True)
        digest = hashlib.sha256(audio_content).hexdigest()[:12]
        output_path = os.path.join(audio_debug_dir, f"{digest}-{safe_debug_filename(remote_name)}")
        with open(output_path, "wb") as debug_file:
            debug_file.write(audio_content)
        logging.info(f"AUDIO DEBUG saved upload payload for {source_name} to {output_path}")
    except Exception as e:
        logging.warning(f"AUDIO DEBUG could not save upload payload for {source_name}: {e}")


def format_audio_track(track: dict) -> str:
    if not track:
        return "none"
    language = track.get("language")
    language_name = language.to_iso_639_2_b() if isinstance(language, LanguageCode) else str(language)
    return (
        f"index={track.get('index')} codec={track.get('codec')} channels={track.get('channels')} "
        f"language={language_name} default={track.get('default')} start_time={track.get('start_time')} "
        f"duration={track.get('duration')} title={track.get('title')}"
    )


def log_ffmpeg_command(stream, context: str) -> None:
    if not audio_debug:
        return
    try:
        command = " ".join(ffmpeg.compile(stream))
        logging.info(f"AUDIO DEBUG {context} ffmpeg command: {command}")
    except Exception as e:
        logging.info(f"AUDIO DEBUG {context} could not compile ffmpeg command: {e}")


def log_ffmpeg_stderr(stderr: bytes, context: str) -> None:
    if not audio_debug or not stderr:
        return
    decoded = stderr.decode(errors="ignore").strip()
    if decoded:
        logging.info(f"AUDIO DEBUG {context} ffmpeg stderr:\n{decoded[-4000:]}")


def wav_duration_seconds(audio_content: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(audio_content), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            if frame_rate <= 0:
                return None
            return wav_file.getnframes() / float(frame_rate)
    except (wave.Error, EOFError):
        return None


def validate_remote_audio(audio_content: bytes, source_name: str) -> bytes:
    digest = hashlib.sha256(audio_content).hexdigest()[:12]
    log_audio_debug(
        f"AUDIO DEBUG prepared payload source={source_name} remote_format={openai_audio_format} "
        f"bytes={len(audio_content)} sha256={digest}"
    )

    if openai_audio_format != "wav":
        return audio_content

    duration = wav_duration_seconds(audio_content)
    if duration is None:
        logging.warning(f"Could not determine WAV duration for {source_name}")
        return audio_content

    if duration < min_remote_audio_seconds:
        raise ValueError(
            f"Extracted audio for {source_name} is too short "
            f"({duration:.3f}s < {min_remote_audio_seconds:.3f}s). "
            "Check that ffmpeg can read the input file and that the selected audio stream is correct."
        )

    log_audio_debug(f"AUDIO DEBUG WAV duration for {source_name}: {duration:.3f}s")
    return audio_content


def passthrough_upload_for_remote(audio_content: bytes, source_name: str) -> tuple[bytes, str]:
    remote_name = upload_source_name(source_name, fallback=remote_audio_filename(source_name or "audio"))
    log_audio_debug(
        f"AUDIO DEBUG passing through uploaded payload source={source_name} remote_name={remote_name} "
        f"bytes={len(audio_content)}"
    )

    if os.path.splitext(remote_name)[1].casefold() == ".wav":
        audio_content = validate_remote_audio(audio_content, source_name)

    save_audio_debug_payload(audio_content, remote_name, source_name)
    return audio_content, remote_name


def slice_raw_pcm_upload(
    audio_content: bytes,
    start_time: int | None = None,
    duration: int | None = None,
) -> bytes:
    start_byte = 0 if start_time is None else max(0, int(start_time * RAW_PCM_UPLOAD_BYTES_PER_SECOND))
    end_byte = len(audio_content)
    if duration is not None:
        end_byte = min(len(audio_content), start_byte + max(0, int(duration * RAW_PCM_UPLOAD_BYTES_PER_SECOND)))
    return audio_content[start_byte:end_byte]


def pcm_bytes_to_wav(audio_content: bytes) -> bytes:
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wav_file:
        wav_file.setnchannels(RAW_PCM_UPLOAD_CHANNELS)
        wav_file.setsampwidth(RAW_PCM_UPLOAD_SAMPLE_WIDTH)
        wav_file.setframerate(RAW_PCM_UPLOAD_SAMPLE_RATE)
        wav_file.writeframes(audio_content)
    return wav_buffer.getvalue()


def normalize_raw_pcm_upload_for_remote(
    audio_content: bytes,
    source_name: str,
    start_time: int | None = None,
    duration: int | None = None,
) -> tuple[bytes, str]:
    raw_audio_content = slice_raw_pcm_upload(audio_content, start_time=start_time, duration=duration)
    remote_name = remote_audio_filename(source_name)
    log_audio_debug(
        f"AUDIO DEBUG normalizing raw PCM upload source={source_name} input_bytes={len(audio_content)} "
        f"sliced_bytes={len(raw_audio_content)} start_time={start_time} duration={duration} "
        f"output_name={remote_name} output_kwargs={audio_output_kwargs()}"
    )

    if openai_audio_format == "wav":
        out = pcm_bytes_to_wav(raw_audio_content)
        out = validate_remote_audio(out, source_name)
        save_audio_debug_payload(out, remote_name, source_name)
        return out, remote_name

    try:
        stream = (
            ffmpeg
            .input(
                "pipe:0",
                format="s16le",
                ac=RAW_PCM_UPLOAD_CHANNELS,
                ar=RAW_PCM_UPLOAD_SAMPLE_RATE,
            )
            .output("pipe:1", **audio_output_kwargs())
        )
        log_ffmpeg_command(stream, f"raw PCM upload source={source_name}")
        out, err = stream.run(input=raw_audio_content, capture_stdout=True, capture_stderr=True)
        log_ffmpeg_stderr(err, f"raw PCM upload source={source_name}")
        if not out:
            raise ValueError(f"FFmpeg output is empty while normalizing raw PCM upload for {source_name}")
        out = validate_remote_audio(out, source_name)
        save_audio_debug_payload(out, remote_name, source_name)
        return out, remote_name
    except ffmpeg.Error as e:
        stderr = e.stderr.decode(errors="ignore") if e.stderr else str(e)
        raise RuntimeError(f"FFmpeg raw PCM transcode failed for {source_name}: {stderr}") from e


def transcode_upload_pipe_for_remote(
    audio_content: bytes,
    source_name: str,
    start_time: int | None = None,
    duration: int | None = None,
) -> tuple[bytes, str]:
    input_kwargs = {}
    if start_time is not None:
        input_kwargs["ss"] = start_time
    if duration is not None:
        input_kwargs["t"] = duration

    remote_name = remote_audio_filename(source_name)
    log_audio_debug(
        f"AUDIO DEBUG transcoding uploaded payload source={source_name} input_bytes={len(audio_content)} "
        f"input_kwargs={input_kwargs} output_name={remote_name} output_kwargs={audio_output_kwargs()}"
    )

    try:
        stream = (
            ffmpeg
            .input("pipe:0", **input_kwargs)
            .output("pipe:1", **audio_output_kwargs())
        )
        log_ffmpeg_command(stream, f"upload source={source_name}")
        out, err = stream.run(input=audio_content, capture_stdout=True, capture_stderr=True)
        log_ffmpeg_stderr(err, f"upload source={source_name}")
        if not out:
            raise ValueError(f"FFmpeg output is empty while transcoding uploaded media for {source_name}")
        out = validate_remote_audio(out, source_name)
        save_audio_debug_payload(out, remote_name, source_name)
        return out, remote_name
    except ffmpeg.Error as e:
        stderr = e.stderr.decode(errors="ignore") if e.stderr else str(e)
        raise RuntimeError(f"FFmpeg pipe transcode failed for {source_name}: {stderr}") from e


def write_upload_payload_to_tempfile(audio_content: bytes, source_name: str) -> str:
    suffix = os.path.splitext(source_name or "")[1] or ".bin"
    temp_file = tempfile.NamedTemporaryFile(prefix="subgen-upload-", suffix=suffix, delete=False)
    try:
        temp_file.write(audio_content)
        return temp_file.name
    finally:
        temp_file.close()


def transcode_upload_file_for_remote(
    file_path: str,
    source_name: str,
    start_time: int | None = None,
    duration: int | None = None,
) -> tuple[bytes, str]:
    input_kwargs = {}
    if start_time is not None:
        input_kwargs["ss"] = start_time
    if duration is not None:
        input_kwargs["t"] = duration

    remote_name = remote_audio_filename(source_name)
    log_audio_debug(
        f"AUDIO DEBUG transcoding temp upload file source={source_name} temp_path={file_path} "
        f"input_kwargs={input_kwargs} output_name={remote_name} output_kwargs={audio_output_kwargs()}"
    )

    try:
        stream = (
            ffmpeg
            .input(file_path, **input_kwargs)
            .output("pipe:1", **audio_output_kwargs())
        )
        log_ffmpeg_command(stream, f"temp upload source={source_name}")
        out, err = stream.run(capture_stdout=True, capture_stderr=True)
        log_ffmpeg_stderr(err, f"temp upload source={source_name}")
        if not out:
            raise ValueError(f"FFmpeg output is empty while transcoding temp upload file for {source_name}")
        out = validate_remote_audio(out, source_name)
        save_audio_debug_payload(out, remote_name, source_name)
        return out, remote_name
    except ffmpeg.Error as e:
        stderr = e.stderr.decode(errors="ignore") if e.stderr else str(e)
        raise RuntimeError(f"FFmpeg temp-file transcode failed for {source_name}: {stderr}") from e


def transcode_upload_for_remote(audio_content: bytes, source_name: str, start_time: int | None = None, duration: int | None = None) -> tuple[bytes, str]:
    try:
        return transcode_upload_pipe_for_remote(
            audio_content,
            source_name,
            start_time=start_time,
            duration=duration,
        )
    except Exception as pipe_error:
        logging.warning(f"FFmpeg pipe transcode failed for {source_name}; retrying via temp file: {pipe_error}")

    temp_path = write_upload_payload_to_tempfile(audio_content, source_name)
    try:
        return transcode_upload_file_for_remote(
            temp_path,
            source_name,
            start_time=start_time,
            duration=duration,
        )
    except Exception as temp_error:
        raise ValueError(
            f"Could not transcode uploaded media for {source_name}. "
            f"Pipe input failed: {pipe_error}. Temp-file retry failed: {temp_error}"
        ) from temp_error
    finally:
        try:
            os.unlink(temp_path)
        except OSError as cleanup_error:
            logging.debug(f"Could not remove temporary upload file {temp_path}: {cleanup_error}")


def extract_audio_for_remote(
    file_path: str,
    language: LanguageCode | None = None,
    audio_tracks=None,
    start_time: int | None = None,
    duration: int | None = None,
) -> tuple[bytes | None, str]:
    remote_name = remote_audio_filename(file_path)
    file_size = None
    try:
        file_size = os.path.getsize(file_path)
    except OSError:
        pass

    if audio_tracks is None:
        audio_tracks = get_audio_tracks(file_path)

    selected_track = None
    if audio_tracks:
        if language:
            selected_track = get_audio_track_by_language(audio_tracks, language)
        if selected_track is None:
            selected_track = audio_tracks[0]

    input_kwargs = {}
    if start_time is not None:
        input_kwargs["ss"] = start_time
    if duration is not None:
        input_kwargs["t"] = duration

    output_kwargs = audio_output_kwargs()
    if selected_track is not None:
        output_kwargs["map"] = f"0:{selected_track['index']}"

    log_audio_debug(
        f"AUDIO DEBUG extracting file={file_path} exists={os.path.exists(file_path)} size={file_size} "
        f"tracks={len(audio_tracks)} selected_track=({format_audio_track(selected_track)}) "
        f"input_kwargs={input_kwargs} output_name={remote_name} output_kwargs={output_kwargs}"
    )

    try:
        stream = (
            ffmpeg
            .input(file_path, **input_kwargs)
            .output("pipe:1", **output_kwargs)
        )
        log_ffmpeg_command(stream, f"file={file_path}")
        out, err = stream.run(capture_stdout=True, capture_stderr=True)
        log_ffmpeg_stderr(err, f"file={file_path}")
        if not out:
            raise ValueError("FFmpeg output is empty")
        out = validate_remote_audio(out, file_path)
        save_audio_debug_payload(out, remote_name, file_path)
        return out, remote_name
    except ffmpeg.Error as e:
        stderr = e.stderr.decode(errors="ignore") if e.stderr else str(e)
        logging.error(f"FFmpeg error extracting audio from {file_path}: {stderr}")
        return None, remote_name
    except Exception as e:
        logging.error(f"Error extracting audio from {file_path}: {e}")
        return None, remote_name


def prepare_upload_for_remote(
    file_content: bytes,
    encode: bool,
    upload_filename: str | None = None,
    upload_content_type: str | None = None,
    video_file: str | None = None,
    language: LanguageCode | None = None,
    start_time: int | None = None,
    duration: int | None = None,
) -> tuple[bytes, str]:
    fallback_name = remote_audio_filename(upload_filename or video_file or "audio")
    source_name = upload_source_name(upload_filename, video_file, upload_content_type, fallback_name)
    upload_kind = upload_media_kind(upload_filename or source_name, upload_content_type)
    video_file_exists = bool(video_file and os.path.isfile(video_file))
    upload_wav_duration = wav_duration_seconds(file_content) if not encode and upload_kind == "unknown" else None
    requires_transcode = encode or upload_kind == "video" or start_time is not None or duration is not None
    language_name = language.to_iso_639_2_b() if language else None

    log_audio_debug(
        "AUDIO DEBUG preparing upload payload "
        f"source_name={source_name} upload_name={upload_filename} upload_content_type={upload_content_type} "
        f"upload_kind={upload_kind} encode={encode} video_file={video_file} video_exists={video_file_exists} "
        f"language={language_name} start_time={start_time} duration={duration}"
    )

    if video_file_exists:
        audio_content, remote_name = extract_audio_for_remote(
            video_file,
            language=language,
            start_time=start_time,
            duration=duration,
        )
        if audio_content is not None:
            return audio_content, remote_name
        logging.warning(
            f"Falling back to uploaded payload for {source_name} because extraction from {video_file} failed"
        )

    if not encode and upload_kind == "unknown":
        if upload_wav_duration is not None:
            log_audio_debug(
                f"AUDIO DEBUG treating encode=false upload as WAV source={source_name} duration={upload_wav_duration:.3f}s"
            )
            if start_time is not None or duration is not None:
                return transcode_upload_for_remote(file_content, source_name, start_time=start_time, duration=duration)
            return passthrough_upload_for_remote(file_content, source_name)

        logging.warning(
            f"encode=false upload {source_name} has no media metadata and is not a valid WAV file; "
            "treating it as raw s16le PCM"
        )
        return normalize_raw_pcm_upload_for_remote(
            file_content,
            source_name,
            start_time=start_time,
            duration=duration,
        )

    if requires_transcode:
        try:
            return transcode_upload_for_remote(file_content, source_name, start_time=start_time, duration=duration)
        except Exception as transcode_error:
            if upload_kind == "audio" and start_time is None and duration is None:
                logging.warning(
                    f"Could not normalize uploaded audio for {source_name}; passing original audio through: {transcode_error}"
                )
                return passthrough_upload_for_remote(file_content, source_name)
            raise

    return passthrough_upload_for_remote(file_content, source_name)


def handle_multiple_audio_tracks(file_path: str, language: LanguageCode | None = None, audio_tracks=None) -> bytes | None:
    """
    Backwards-compatible helper for callers/tests that only need extraction when
    a file has multiple audio tracks. New transcription code uses
    extract_audio_for_remote() so every upload is in an endpoint-friendly format.
    """
    if audio_tracks is None:
        audio_tracks = get_audio_tracks(file_path)

    if len(audio_tracks) <= 1:
        return None

    audio_track = get_audio_track_by_language(audio_tracks, language) if language else None
    if audio_track is None:
        audio_track = audio_tracks[0]

    return extract_audio_track_to_memory(file_path, audio_track["index"])


def extract_audio_track_to_memory(input_video_path, track_index) -> bytes | None:
    if track_index is None:
        logging.warning(f"Skipping audio track extraction for {input_video_path} because track index is None")
        return None

    try:
        out, _ = (
            ffmpeg.input(input_video_path)
            .output("pipe:", map=f"0:{track_index}", **audio_output_kwargs())
            .run(capture_stdout=True, capture_stderr=True)
        )
        return out
    except ffmpeg.Error as e:
        stderr = e.stderr.decode(errors="ignore") if e.stderr else str(e)
        logging.error(f"FFmpeg error: {stderr}")
        return None

@app.get("/plex")
@app.get("/webhook")
@app.get("/jellyfin")
@app.get("/asr")
@app.get("/emby")
@app.get("/detect-language")
@app.get("/tautulli")
async def handle_get_request(request: Request):
    return {"You accessed this request incorrectly via a GET request. See https://github.com/McCloudS/subgen for proper configuration"}

@app.get("/")
async def webui():
    return {"The webui for configuration was removed on 1 October 2024, please configure via environment variables or in your Docker settings. "}

@app.get("/status")
async def status():
    return {
        "version": f"Subgen {subgen_version}, OpenAI-compatible transcription ({docker_status})",
        "transcription_model": transcription_model,
        "endpoint": openai_audio_url("transcribe"),
    }

@app.post("/tautulli")
async def receive_tautulli_webhook(
        source: Union[str, None] = Header(None),
        event: str = Body(None),
        file: str = Body(None),
):
    if source == "Tautulli":
        logging.debug(f"Tautulli event detected is: {event}")
        if((event == "added" and procaddedmedia) or (event == "played" and procmediaonplay)):
            fullpath = file
            logging.debug(f"Full file path: {fullpath}")

            gen_subtitles_queue(path_mapping(fullpath), transcribe_or_translate)
    else:
        return {
            "message": "This doesn't appear to be a properly configured Tautulli webhook, please review the instructions again!"}

    return ""

@app.post("/plex")
async def receive_plex_webhook(
        user_agent: Union[str] = Header(None),
        payload: Union[str] = Form(),
):
    try:
        plex_json = json.loads(payload)
        if "PlexMediaServer" not in user_agent:
            return {"message": "This doesn't appear to be a properly configured Plex webhook, please review the instructions again"}

        event = plex_json["event"]
        logging.debug(f"Plex event detected is: {event}")

        if (event == "library.new" and procaddedmedia) or (event == "media.play" and procmediaonplay):
            rating_key = plex_json['Metadata']['ratingKey']
            fullpath = get_plex_file_name(rating_key, plexserver, plextoken)
            logging.debug(f"Full file path: {fullpath}")

            # Queue the current item with its specific ID for refreshing
            gen_subtitles_queue(
                path_mapping(fullpath), 
                transcribe_or_translate, 
                plex_item_id=rating_key, 
                plex_server=plexserver, 
                plex_token=plextoken
            )
            
            # Note: refresh_plex_metadata is removed here; it is now handled by the worker thread.

            if plex_queue_next_episode:
                next_key = get_next_plex_episode(plex_json['Metadata']['ratingKey'], stay_in_season=False)
                if next_key:
                    next_file = get_plex_file_name(next_key, plexserver, plextoken)
                    gen_subtitles_queue(
                        path_mapping(next_file), 
                        transcribe_or_translate,
                        plex_item_id=next_key, # Pass the NEXT ID so it refreshes when done
                        plex_server=plexserver,
                        plex_token=plextoken
                    )

            if plex_queue_series or plex_queue_season:
                current_rating_key = plex_json['Metadata']['ratingKey']
                stay_in_season = plex_queue_season # Determine if we're staying in the season or not

                while current_rating_key is not None:
                    try:
                        # Queue the current episode
                        file_path = path_mapping(get_plex_file_name(current_rating_key, plexserver, plextoken))
                        
                        gen_subtitles_queue(
                            file_path, 
                            transcribe_or_translate,
                            plex_item_id=current_rating_key, # Pass the specific loop ID for refreshing
                            plex_server=plexserver,
                            plex_token=plextoken
                        )
                        
                        logging.debug(f"Queued episode with ratingKey {current_rating_key}")

                        # Get the next episode
                        next_episode_rating_key = get_next_plex_episode(current_rating_key, stay_in_season=stay_in_season)
                        if next_episode_rating_key is None:
                            break # Exit the loop if no next episode
                        current_rating_key = next_episode_rating_key

                    except Exception as e:
                        logging.error(f"Error processing episode with ratingKey {current_rating_key} or reached end of series: {e}")
                        break # Stop processing on error

                logging.info("All episodes in the series (or season) have been queued.")

    except Exception as e:
        logging.error(f"Failed to process Plex webhook: {e}")

    return ""
 
@app.post("/jellyfin")
async def receive_jellyfin_webhook(
        user_agent: str = Header(None),
        NotificationType: str = Body(None),
        file: str = Body(None),
        ItemId: str = Body(None),
):
    if "Jellyfin-Server" in user_agent:
        logging.debug(f"Jellyfin event detected is: {NotificationType}")
        logging.debug(f"itemid is: {ItemId}")

        if (NotificationType == "ItemAdded" and procaddedmedia) or (NotificationType == "PlaybackStart" and procmediaonplay):
            fullpath = get_jellyfin_file_name(ItemId, jellyfinserver, jellyfintoken)
            logging.debug(f"Full file path: {fullpath}")

            # Queue item with Jellyfin metadata ID for delayed refresh
            gen_subtitles_queue(
                path_mapping(fullpath), 
                transcribe_or_translate,
                jellyfin_item_id=ItemId,
                jellyfin_server=jellyfinserver,
                jellyfin_token=jellyfintoken
            )
            
            # Note: refresh_jellyfin_metadata removed here; handled by worker.
    else:
        return {
            "message": "This doesn't appear to be a properly configured Jellyfin webhook, please review the instructions again!"}

    return ""

@app.post("/emby")
async def receive_emby_webhook(
        user_agent: Union[str, None] = Header(None),
        data: Union[str, None] = Form(None),
):
    if not data:
        return ""

    data_dict = json.loads(data)
    event = data_dict['Event']
    logging.debug("Emby event detected is: " + event)

    # Check if it's a notification test event
    if event == "system.notificationtest":
        logging.info("Emby test message received!")
        return {"message": "Notification test received successfully!"}

    if (event == "library.new" and procaddedmedia) or (event == "playback.start" and procmediaonplay):
        fullpath = data_dict['Item']['Path']
        logging.debug(f"Full file path: {fullpath}")
        gen_subtitles_queue(path_mapping(fullpath), transcribe_or_translate)

    return ""
    
@app.post("/batch")
async def batch(
        directory: str = Query(...),
        forceLanguage: Union[str, None] = Query(default=None)
):
    transcribe_existing(directory, LanguageCode.from_string(forceLanguage))

# ============================================================================
# REFACTORED /ASR ENDPOINT WITH HASH-BASED DEDUPLICATION AND BLOCKING
# ============================================================================

@app.post("/asr")
async def asr(
    task: Union[str, None] = Query(default="transcribe", enum=["transcribe", "translate"]),
    language: Union[str, None] = Query(default=None),
    video_file: Union[str, None] = Query(default=None),
    initial_prompt: Union[str, None] = Query(default=None),
    audio_file: UploadFile = File(...),
    encode: bool = Query(default=True, description="Encode audio first through ffmpeg"),
    output: Union[str, None] = Query(default="srt", enum=["txt", "vtt", "srt", "tsv", "json"]),
    word_timestamps: bool = Query(default=False, description="Word-level timestamps"),
):
    """
    ASR endpoint that uses audio content hash for deduplication. 
    BLOCKS until processing is complete, then returns the result.
    
    If identical audio + task + language is already being processed,
    waits for that task to complete and returns the same result.
    """
    task_id = None
    
    try:
        logging.info(
            f"ASR {task.capitalize()} received for file '{video_file}'" 
            if video_file 
            else f"ASR {task.capitalize()} received"
        )
        
        # Read audio file content into memory
        file_content = await audio_file.read()
        
        if not file_content:
            await audio_file.close()
            return {
                "status": "error",
                "message": "Audio file is empty"
            }
        
        # Generate deterministic hash from audio (and optionally task/language)
        audio_hash = generate_audio_hash(file_content, task, language)
        
        # FIX: Use video file path if available to match TRANSCRIBE tasks
        if video_file:
            task_id = path_mapping(video_file)
            logging.debug(f"Using mapped video file path as task ID for ASR request: {task_id}")
        else:
            task_id = f"asr-{audio_hash}"
            logging.debug(f"Generated audio hash: {audio_hash} for ASR request")
        
        # Handle forced language
        final_language = language
        if force_detected_language_to: 
            final_language = force_detected_language_to.to_iso_639_1()
            logging.info(f"Forcing detected language to {force_detected_language_to}")
        
        # Create result container for this task
        with task_results_lock:
            if task_id not in task_results:
                task_results[task_id] = TaskResult()
            task_result = task_results[task_id]
        
        # Queue the ASR task
        asr_task_data = {
            'path': task_id, # DeduplicatedQueue uses this for dedup
            'type': 'asr',
            'task': task,
            'language': final_language,
            'video_file': video_file,
            'upload_filename': audio_file.filename,
            'upload_content_type': audio_file.content_type,
            'initial_prompt': initial_prompt,
            'audio_content': file_content,
            'encode': encode,
            'output': output,
            'word_timestamps': word_timestamps,
            'result_container': task_result,
        }
        
        # Try to queue (returns False if already queued/processing)
        if task_queue.put(asr_task_data):
            logging.info(f"ASR task {task_id} queued")
        else:
            logging.info(f"ASR task {task_id} already queued/processing - waiting for result")
        
        # EVENT LOOP BLOCK FIX: Use asyncio.to_thread so FastAPI can still respond to /status
        if await asyncio.to_thread(task_result.wait, asr_timeout):
            if task_result.error:
                logging.error(f"ASR task {task_id} failed: {task_result.error}")
                return {
                    "status": "error",
                    "task_id": task_id,
                    "message": f"ASR processing failed: {task_result.error}"
                }
            else: 
                logging.info(f"ASR task {task_id} completed")
                return StreamingResponse(
                    iter([task_result.result]),
                    media_type="text/plain",
                    headers={'Source': f'{task.capitalize()}d using OpenAI-compatible endpoint from Subgen'}
                )
        else:
            logging.error(f"ASR task {task_id} timed out")
            return {
                "status": "timeout",
                "task_id": task_id,
                "message": f"ASR processing timed out after {asr_timeout} seconds"
            }
            
    except Exception as e: 
        logging.error(f"Error in ASR endpoint: {e}", exc_info=True)
        return {"status": "error", "message": f"Error: {str(e)}"}
    finally:
        await audio_file.close()
        # Clean up task_results entry after task completes
        with task_results_lock:
            if task_id in task_results:
                del task_results[task_id]
                logging.debug(f"Cleaned up task_results entry for {task_id}")

# ============================================================================
# ASR WORKER FUNCTION
# ============================================================================

def get_audio_start_time(video_path: str) -> float:
    """
    Use ffprobe to detect the audio stream start_time offset from a video file.
    
    Some containers (especially Amazon WEB-DL) have audio streams that start
    later than the video stream. Bazarr compensates with adelay silence padding,
    but speech-to-text engines may ignore digital silence, causing all timestamps
    to be early by the start_time offset.
    
    Returns the audio start_time in seconds, or 0.0 if not detectable.
    """
    if not video_path or not os.path.isfile(video_path):
        return 0.0
    
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
             '-show_entries', 'stream=start_time',
             '-of', 'json', video_path],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return 0.0
        
        data = json.loads(result.stdout)
        streams = data.get('streams', [])
        if streams:
            start_time = float(streams[0].get('start_time', 0))
            if start_time > 0.1:  # only apply for significant offsets
                logging.info(f"Detected audio start_time offset: {start_time:.3f}s for {os.path.basename(video_path)}")
                return start_time
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, OSError) as e:
        logging.debug(f"Could not detect audio start_time for {video_path}: {e}")
    
    return 0.0


def asr_task_worker(task_data: dict) -> None:
    """
    Worker function that processes ASR tasks from the queue. 
    Called by transcription_worker when task type is 'asr'.
    """
    task_id = task_data.get('path', 'unknown')
    result_container = task_data.get('result_container')
    
    try:
        task = task_data['task']
        language = task_data['language']
        video_file = task_data.get('video_file')
        upload_filename = task_data.get('upload_filename')
        upload_content_type = task_data.get('upload_content_type')
        initial_prompt = task_data.get('initial_prompt')
        file_content = task_data['audio_content']
        encode = task_data['encode']
        output = task_data.get('output', 'srt')

        language_code = LanguageCode.from_string(language) if language else None
        audio_content, remote_name = prepare_upload_for_remote(
            file_content,
            encode=encode,
            upload_filename=upload_filename,
            upload_content_type=upload_content_type,
            video_file=video_file,
            language=language_code,
        )
        
        # Detect audio start_time offset from source file (if accessible)
        audio_offset = get_audio_start_time(video_file) if video_file else 0.0

        response_format = "verbose_json" if output in {"json", "tsv"} else ("text" if output == "txt" else output)
        response = remote_audio_request(
            audio_content,
            remote_name,
            task=task,
            language=language,
            prompt=initial_prompt,
            response_format=response_format,
        )

        result_text = output_from_response(response, output)
        if output == "srt":
            result_text = shift_srt_timestamps(result_text, audio_offset)
            result_text = append_srt_watermark(result_text)
        
        # Set result for blocking endpoint
        if result_container:
            result_container.set_result(result_text)

    except Exception as e:
        logging.error(f"Error processing ASR (ID: {task_id}): {e}", exc_info=True)
        if result_container: 
            result_container.set_error(str(e))

# ============================================================================
# REFACTORED /DETECT-LANGUAGE ENDPOINT WITH HASH-BASED DEDUPLICATION AND BLOCKING
# ============================================================================

@app.post("/detect-language")
async def detect_language(
    audio_file: UploadFile = File(...),
    encode: bool = Query(default=True),
    video_file: Union[str, None] = Query(default=None),
    detect_lang_length: int = Query(default=detect_language_length),
    detect_lang_offset: int = Query(default=detect_language_offset)
):
    if force_detected_language_to: 
        await audio_file.close()
        return {"detected_language": force_detected_language_to.to_name(), "language_code": force_detected_language_to.to_iso_639_1()}
    
    try:
        file_content = await audio_file.read()
        if not file_content:
            return {"detected_language": "Unknown", "language_code": "und", "status": "error"}
            
        logging.info("Language detection via transcription endpoint" + (f" for {video_file}" if video_file else ""))

        audio_content, remote_name = await asyncio.to_thread(
            prepare_upload_for_remote,
            file_content,
            encode,
            audio_file.filename,
            audio_file.content_type,
            video_file,
            None,
            detect_lang_offset,
            detect_lang_length,
        )

        result = await asyncio.to_thread(
            remote_audio_request,
            audio_content,
            remote_name,
            "transcribe",
            None,
            None,
            "verbose_json",
        )
        
        detected = response_language(result)
        
        logging.info(f"Detect Language Result: {detected.to_name()} ({detected.to_iso_639_1()})")
        
        return {
            "detected_language": detected.to_name() or "Unknown",
            "language_code": detected.to_iso_639_1() or "und"
        }

    except Exception as e: 
        logging.error(f"Error in API detect-language: {e}", exc_info=True)
        return {"detected_language": "Unknown", "language_code": "und", "status": "error"}
    finally: 
        await audio_file.close()

# ============================================================================
# DETECT LANGUAGE WORKER FOR UPLOADED AUDIO
# ============================================================================

def detect_language_from_upload(task_data: dict) -> None:
    """
    Worker function that processes detect-language tasks from uploaded audio. 
    Sets the result in the result_container when complete.
    """
    detected_language = LanguageCode.NONE
    task_id = task_data.get('path', 'unknown')
    result_container = task_data.get('result_container')
    
    try:
        video_file = task_data.get('video_file')
        upload_filename = task_data.get('upload_filename')
        upload_content_type = task_data.get('upload_content_type')
        file_content = task_data['audio_content']
        encode = task_data['encode']
        detect_lang_length = task_data['detect_lang_length']
        detect_lang_offset = task_data['detect_lang_offset']
        
        logging.info(
            f"Detecting language for '{video_file}' ({detect_lang_length}s starting at {detect_lang_offset}s) - ID: {task_id}"
            if video_file
            else f"Detecting language ({detect_lang_length}s starting at {detect_lang_offset}s) - ID: {task_id}"
        )

        audio_content, remote_name = prepare_upload_for_remote(
            file_content,
            encode=encode,
            upload_filename=upload_filename,
            upload_content_type=upload_content_type,
            video_file=video_file,
            start_time=detect_lang_offset,
            duration=detect_lang_length,
        )

        result = remote_audio_request(
            audio_content,
            remote_name,
            task="transcribe",
            response_format="verbose_json",
        )
        detected_language = response_language(result)
        language_code = detected_language.to_iso_639_1() or "und"
        
        logging.info(f"Detected language: {detected_language.to_name()} ({language_code}) - ID: {task_id}")
        
        # Set the result for the blocking endpoint
        if result_container:
            result_container.set_result({
                "detected_language": detected_language.to_name() or "Unknown",
                "language_code": language_code
            })

    except Exception as e:
        logging.error(
            f"Error detecting language (ID: {task_id}) for '{task_data.get('video_file')}': {e}"
            if task_data.get('video_file')
            else f"Error detecting language (ID: {task_id}): {e}",
            exc_info=True
        )
        if result_container: 
            result_container.set_error(str(e))
    
def detect_language_task(path, original_task_data=None):
    """
    Worker function that detects language for a local file.
    Returns the task data to be queued for transcription.
    """
    detected_language = LanguageCode.NONE
    
    try:
        logging.info(
            f"Detecting language of file: {path} "
            f"({detect_language_length}s starting at {detect_language_offset}s)"
        )

        audio_content, remote_name = extract_audio_for_remote(
            path,
            start_time=detect_language_offset,
            duration=int(detect_language_length),
        )
        if audio_content is None:
            raise ValueError("Could not extract audio for language detection")

        result = remote_audio_request(
            audio_content,
            remote_name,
            task="transcribe",
            response_format="verbose_json",
        )
        detected_language = response_language(result)
        
        logging.info(f"Detected language: {detected_language.to_name()}")

    except Exception as e:
        logging.error(f"Error detecting language for file: {e}", exc_info=True)
        
    # Create transcription task with detected language
    task_data = {
        'path': path,
        'type': 'transcribe',
        'transcribe_or_translate': transcribe_or_translate,
        'force_language': detected_language
    }
    
    # Carry over metadata (Plex IDs, etc.) from the original task
    if original_task_data:
        for key, value in original_task_data.items():
            if key not in task_data:
                task_data[key] = value
                
    return task_data

def is_audio_file_extension(file_extension):
    return file_extension.casefold() in AUDIO_EXTENSIONS

def write_lrc(lrc_text, file_path):
    with open(file_path, "w") as file:
        file.write(lrc_text)

def send_completion_webhook(source_file_path: str, subtitle_file_path: str, language: LanguageCode, task_type: str):
    """Sends a JSON POST request to a configured webhook URL upon task completion."""
    if not webhook_url_completed:
        return
        
    # Dynamically make it past-tense (transcribe -> transcribed, translate -> translated)
    event_status = f"{task_type}d" if task_type in["transcribe", "translate"] else task_type
        
    payload = {
        "event": event_status,
        "file": os.path.abspath(source_file_path),
        "subtitle": os.path.abspath(subtitle_file_path),
        "language": language.to_iso_639_1()
    }
    
    try:
        logging.info(f"Sending completion webhook ({event_status}) to {webhook_url_completed}")
        response = requests.post(webhook_url_completed, json=payload, timeout=10)
        response.raise_for_status()
        logging.debug(f"Webhook successfully delivered. Status code: {response.status_code}")
    except Exception as e:
        logging.error(f"Failed to send completion webhook: {e}")

def gen_subtitles(file_path: str, transcription_type: str, force_language: LanguageCode = LanguageCode.NONE, audio_tracks=None) -> None:
    """Generates subtitles for a video file.

    Args:
        file_path: str - The path to the video file.
        transcription_type: str - The type of transcription or translation to perform.
        force_language: LanguageCode - The language to force for transcription or translation.
        audio_tracks: Pre-fetched audio track list; fetched from file if not provided.
    """

    try:
        # Check if the file is an audio file before trying to extract audio
        file_name, file_extension = os.path.splitext(file_path)
        is_audio_file = is_audio_file_extension(file_extension)

        audio_content, remote_name = extract_audio_for_remote(
            file_path,
            force_language,
            audio_tracks=audio_tracks,
        )
        if audio_content is None:
            raise ValueError("Could not extract audio for transcription")

        response = remote_audio_request(
            audio_content,
            remote_name,
            task=transcription_type,
            language=force_language.to_iso_639_1() if force_language else None,
            response_format="verbose_json",
        )

        output_language = response_language(response, fallback=force_language, task=transcription_type)
        subtitle_file_path = ""

        # If it is an audio file, write the LRC file
        if is_audio_file and lrc_for_audio_files:
            subtitle_file_path = file_name + '.lrc'
            write_lrc(response_to_lrc(response), subtitle_file_path)
        else:
            subtitle_file_path = name_subtitle(file_path, output_language)
            srt_text = append_srt_watermark(response_to_srt(response))
            with open(subtitle_file_path, "w") as file:
                file.write(srt_text)
            
        # Trigger the downstream webhook
        send_completion_webhook(file_path, subtitle_file_path, output_language, transcription_type)

        # FIX: Provide the generated subtitle result to any waiting ASR endpoint requests
        with task_results_lock:
            if file_path in task_results:
                task_results[file_path].set_result(response_to_srt(response))

    except Exception as e:
        logging.info(f"Error processing or transcribing {file_path} in {force_language}: {e}")
        # FIX: Inform waiting ASR endpoint requests of the error so they don't hang
        with task_results_lock:
            if file_path in task_results:
                task_results[file_path].set_error(str(e))
        
def define_subtitle_language_naming(language: LanguageCode, type):
    """
    Determines the naming format for a subtitle language based on the given type. 

    Args:
        language (LanguageCode): The language code object containing methods to get different formats of the language name.
        type (str): The type of naming format desired, such as 'ISO_639_1', 'ISO_639_2_T', 'ISO_639_2_B', 'NAME', or 'NATIVE'.

    Returns:
        str: The language name in the specified format. If an invalid type is provided, it defaults to the language's name.
    """
    if subtitle_language_name:
        return subtitle_language_name
    # If we are translating, then we ALWAYS output an english file.
    switch_dict = {
        "ISO_639_1": language.to_iso_639_1,
        "ISO_639_2_T": language.to_iso_639_2_t,
        "ISO_639_2_B": language.to_iso_639_2_b,
        "NAME": language.to_name,
        "NATIVE": lambda: language.to_name(in_english=False)
    }
    if transcribe_or_translate == 'translate':
        language = LanguageCode.ENGLISH
    return switch_dict.get(type, language.to_name)()

def name_subtitle(file_path: str, language: LanguageCode) -> str:
    """
    Name the subtitle file to be written, based on the source file and the language of the subtitle. 
    
    Args: 
        file_path: The path to the source file.
        language: The language of the subtitle.
    
    Returns:
        The name of the subtitle file to be written.
    """
    subgen_part = ".subgen" if show_in_subname_subgen else ""
    model_part = f".{transcription_model}" if show_in_subname_model else ""
    lang_part = define_subtitle_language_naming(language, subtitle_language_naming_type)
    
    return f"{os.path.splitext(file_path)[0]}{subgen_part}{model_part}.{lang_part}.srt"
    
def get_audio_track_by_language(audio_tracks, language):
    """
    Returns the first audio track with the given language. 
    
    Args:
        audio_tracks (list): A list of dictionaries containing information about each audio track.
        language (str): The language of the audio track to search for. 
    
    Returns:
        dict: The first audio track with the given language, or None if no match is found.
    """
    for track in audio_tracks: 
        if track['language'] == language:
            return track
    return None

def choose_transcribe_language(file_path, forced_language, audio_tracks=None):
    """
    Determines the language to be used for transcription based on the provided
    file path and language preferences.

    Args:
        file_path: The path to the file for which the audio tracks are analyzed.
        forced_language: The language to force for transcription if specified.
        audio_tracks: Pre-fetched audio track list; if None, fetched from file.

    Returns:
        The language code to be used for transcription. It prioritizes the
        `forced_language`, then the environment variable `force_detected_language_to`,
        then the preferred audio language if available, and finally the default
        language of the audio tracks. Returns LanguageCode.NONE if undetermined.
    """
    if forced_language:
        logging.debug(f"ENV FORCE_LANGUAGE is set: Forcing language to {forced_language}")
        return forced_language

    if force_detected_language_to:
        logging.debug(f"ENV FORCE_DETECTED_LANGUAGE_TO is set: Forcing detected language to {force_detected_language_to}")
        return force_detected_language_to

    if audio_tracks is None:
        audio_tracks = get_audio_tracks(file_path)

    preferred_track_language = find_language_audio_track(audio_tracks, preferred_audio_languages)

    if preferred_track_language:
        return preferred_track_language

    default_language = find_default_audio_track_language(audio_tracks)
    if default_language:
        logging.debug(f"Default language found: {default_language}")
        return default_language

    return LanguageCode.NONE
    
def get_audio_tracks(video_file):
    """
    Extracts information about the audio tracks in a file.

    Returns:
        List of dictionaries with information about each audio track.
        Each dictionary has the following keys:
            index (int): The stream index of the audio track.
            codec (str): The name of the audio codec.
            channels (int): The number of audio channels.
            language (LanguageCode): The language of the audio track.
            title (str): The title of the audio track.
            default (bool): Whether the audio track is the default for the file.
            forced (bool): Whether the audio track is forced.
            original (bool): Whether the audio track is the original.
            commentary (bool): Whether the audio track is a commentary.
    """
    try:
        # Probe the file to get audio stream metadata
        probe = ffmpeg.probe(video_file, select_streams='a')
        audio_streams = probe.get('streams',[])
        
        # Extract information for each audio track
        audio_tracks =[]
        for stream in audio_streams:
            audio_track = {
                "index": int(stream.get("index", 0)),
                "codec": stream.get("codec_name", "Unknown"),
                "channels": int(stream.get("channels", 0)),
                "language": LanguageCode.from_iso_639_2(stream.get("tags", {}).get("language", "Unknown")),
                "title": stream.get("tags", {}).get("title", "None"),
                "start_time": stream.get("start_time"),
                "duration": stream.get("duration"),
                "default": stream.get("disposition", {}).get("default", 0) == 1,
                "forced": stream.get("disposition", {}).get("forced", 0) == 1,
                "original": stream.get("disposition", {}).get("original", 0) == 1,
                "commentary": "commentary" in stream.get("tags", {}).get("title", "").lower()
            }
            audio_tracks.append(audio_track) 

        log_audio_debug(
            "AUDIO DEBUG probed audio tracks for "
            f"{video_file}: "
            + ("; ".join(format_audio_track(track) for track in audio_tracks) if audio_tracks else "none")
        )
        return audio_tracks

    except ffmpeg.Error as e:
        logging.error(f"FFmpeg error: {e.stderr}")
        return[]
    except Exception as e:
        logging.error(f"An error occurred while reading audio track information: {str(e)}")
        return[]

def find_language_audio_track(audio_tracks, find_languages):
    """
    Checks if an audio track with any of the given languages is present in the list of audio tracks.
    Returns the first language from `find_languages` that matches.
    
    Args:
        audio_tracks (list): A list of dictionaries containing information about each audio track.
        find_languages (list): A list language codes to search for.
    
    Returns:
        str or None: The first language found from `find_languages`, or None if no match is found.
    """
    for language in find_languages:
        for track in audio_tracks:
            if track['language'] == language:
                return language
    return None

def find_default_audio_track_language(audio_tracks): 
    """
    Finds the language of the default audio track in the given list of audio tracks.

    Args:
        audio_tracks (list): A list of dictionaries containing information about each audio track.
            Must contain the key "default" which is a boolean indicating if the track is the default track.

    Returns:
        str: The ISO 639-2 code of the language of the default audio track, or None if no default track was found.
    """
    for track in audio_tracks:
        if track['default'] is True:
            return track['language']
    return None
    
def gen_subtitles_queue(file_path: str, transcription_type: str, force_language: LanguageCode = LanguageCode.NONE, **task_kwargs) -> None:
    global task_queue

    # Check if this file is already in the queue or being processed
    if task_queue.is_active(file_path):
        logging.debug(f"Ignored: {os.path.basename(file_path)} is already queued or processing.")
        return

    if not has_audio(file_path):
        logging.debug(f"{file_path} doesn't have any audio to transcribe!")
        return

    # Probe audio tracks once and pass to both helpers to avoid triple ffprobe
    audio_tracks = get_audio_tracks(file_path)
    audio_langs = [track['language'] for track in audio_tracks]

    force_language = choose_transcribe_language(file_path, force_language, audio_tracks=audio_tracks)

    if should_skip_file(file_path, force_language, audio_langs=audio_langs):
        return

    # Detect audio language via the transcription endpoint if no language is known and detection is enabled
    if not force_language and should_whisper_detect_audio_language:
        detect_task = {'path': file_path, 'type': "detect_language"}
        detect_task.update(task_kwargs)
        task_queue.put(detect_task)
        return

    task = {
        'path': file_path,
        'transcribe_or_translate': transcription_type,
        'force_language': force_language,
        'audio_tracks': audio_tracks,  # cached — avoids re-probing in gen_subtitles
    }
    task.update(task_kwargs)

    task_queue.put(task)

def should_skip_file(file_path: str, target_language: LanguageCode, audio_langs=None) -> bool:
    """
    Determines if subtitle generation should be skipped for a file.

    Args:
        file_path: Path to the media file.
        target_language: The desired language for transcription.
        audio_langs: Pre-fetched list of audio LanguageCodes; fetched if not provided.

    Returns:
        True if the file should be skipped, False otherwise.
    """
    base_name = os.path.basename(file_path)
    file_name, file_ext = os.path.splitext(base_name)
    if transcribe_or_translate == 'translate':
        target_language = LanguageCode.ENGLISH  # Force our target language as english if we are translating

    # 1. Skip if it's an audio file and an LRC file already exists.
    if is_audio_file_extension(file_ext) and lrc_for_audio_files:
        lrc_path = os.path.join(os.path.dirname(file_path), f"{file_name}.lrc")
        if os.path.exists(lrc_path):
            logging.info(f"Skipping {base_name}: LRC file already exists.")
            return True

    # 2. Audio language is unknown — two independent skip conditions for this case.
    if target_language == LanguageCode.NONE:
        if skip_unknown_language:
            logging.info(f"Skipping {base_name}: Audio language unknown and SKIP_UNKNOWN_LANGUAGE is enabled.")
            return True
        if skip_if_no_audio_language_but_subtitles_exist and get_subtitle_languages(file_path):
            logging.info(f"Skipping {base_name}: Audio language unknown but internal subtitles already exist.")
            return True

    # 3. Audio track checks (cheap — audio_langs pre-fetched by caller).
    if audio_langs is None:
        audio_langs = get_audio_languages(file_path)

    # 3a. Skip if audio language is not in the preferred list.
    if limit_to_preferred_audio_languages:
        if not any(lang in preferred_audio_languages for lang in audio_langs):
            preferred_names = [lang.to_name() for lang in preferred_audio_languages]
            logging.info(f"Skipping {base_name}: No preferred audio tracks found (looking for {', '.join(preferred_names)})")
            return True

    # 3b. Skip if audio language is in the explicit skip list.
    if any(lang in skip_audio_languages for lang in audio_langs):
        logging.info(f"Skipping {base_name}: Contains a skipped audio language.")
        return True

    # 4. Skip if a subtitle already exists in the target language.
    if skip_if_target_subtitle_exists:
        if subtitle_exists_in_language(file_path, target_language):
            if target_language == LanguageCode.NONE:
                logging.info(f"Skipping {base_name}: Subtitles already exist and audio language could not be detected from file metadata.")
            else:
                lang_name = target_language.to_name()
                logging.info(f"Skipping {base_name}: Subtitles already exist in {lang_name}.")
            return True

        # Since SUBTITLE_LANGUAGE_NAME overrides the output filename, check if it exists in the folder.
        if subtitle_language_name and LanguageCode.is_valid_language(subtitle_language_name):
            external_lang = LanguageCode.from_string(subtitle_language_name)
            if has_external_subtitle_in_language(file_path, external_lang, recursion=True, only_match_subgen_subtitles=only_match_subgen_subtitles):
                logging.info(f"Skipping {base_name}: Subtitles already exist in custom name '{subtitle_language_name}'.")
                return True

        # Check: Does the exact file Subgen intends to create already exist?
        expected_output = name_subtitle(file_path, target_language)
        if os.path.exists(expected_output):
            logging.info(f"Skipping {base_name}: Generated subtitle '{os.path.basename(expected_output)}' already exists.")
            return True

    # 5. Internal subtitle checks (grouped — both examine streams inside the container).

    # 5a. Skip if an internal subtitle exists in the specifically configured language.
    if skip_if_internal_sub_language and has_internal_subtitle_in_language(file_path, skip_if_internal_sub_language):
        lang_name = skip_if_internal_sub_language.to_name()
        logging.info(f"Skipping {base_name}: Internal subtitles in {lang_name} already exist.")
        return True

    # 5b. Skip if any embedded subtitle language is in the skip list.
    if skip_subtitle_languages and any(lang in skip_subtitle_languages for lang in get_subtitle_languages(file_path)):
        logging.info(f"Skipping {base_name}: Contains a skipped subtitle language.")
        return True

    # 6. Skip if an external subtitle exists matching the custom subtitle_language_name.
    #    Note: this overlaps with check 4b when skip_if_target_subtitle_exists is also True;
    #    it only adds distinct behaviour when skip_if_target_subtitle_exists is False.
    if skip_if_external_sub_exists and subtitle_language_name and LanguageCode.is_valid_language(subtitle_language_name):
        external_lang = LanguageCode.from_string(subtitle_language_name)
        if has_external_subtitle_in_language(file_path, external_lang, recursion=True, only_match_subgen_subtitles=only_match_subgen_subtitles):
            lang_name = external_lang.to_name()
            logging.info(f"Skipping {base_name}: External subtitles in {lang_name} already exist.")
            return True

    return False
    
def get_subtitle_languages(video_path):
    """
    Extract language codes from each subtitle stream in the video file using pyav.
    :param video_path: Path to the video file
    :return: List of language codes for each subtitle stream
    """
    languages = []

    try:
        with av.open(video_path) as container:
            for stream in container.streams.subtitles:
                lang_code = stream.metadata.get('language')
                if lang_code:
                    languages.append(LanguageCode.from_iso_639_2(lang_code))
                else:
                    languages.append(LanguageCode.NONE)
    except Exception as e:
        logging.warning(f"Could not read subtitle streams from {video_path}: {e}")

    return languages

def get_audio_languages(video_path):
    """
    Extract language codes from each audio stream in the video file.

    :param video_path: Path to the video file
    :return: List of language codes for each audio stream
    """
    audio_tracks = get_audio_tracks(video_path)
    return [track['language'] for track in audio_tracks] 

def subtitle_exists_in_language(video_file, target_language: LanguageCode):
    """
    Determines if a subtitle file with the target language is available for a specified video file.

    This function checks both within the video file and in its associated folder for subtitles
    matching the specified language.

    Args:
        video_file: The path to the video file.
        target_language: The language of the subtitle file to search for.

    Returns:
        bool: True if a subtitle file with the target language is found, False otherwise.
    """
    return has_internal_subtitle_in_language(video_file, target_language) or has_external_subtitle_in_language(video_file, target_language, recursion=True, only_match_subgen_subtitles=only_match_subgen_subtitles)

def has_internal_subtitle_in_language(video_file: str, target_language: LanguageCode) -> bool:
    """
    Checks whether a video container has an embedded subtitle track in the given language.

    Args:
        video_file: Path to the video file.
        target_language: The language to search for.

    Returns:
        True if a matching embedded subtitle stream is found, False otherwise.
    """
    try:
        with av.open(video_file) as container:
            for stream in container.streams:
                if stream.type == 'subtitle' and 'language' in stream.metadata:
                    stream_language = LanguageCode.from_string(stream.metadata.get('language', '').lower())
                    if stream_language == target_language:
                        return True
            return False

    except Exception as e:
        logging.error(f"An error occurred while checking the file with pyav: {type(e).__name__}: {e}")
        return False

def has_external_subtitle_in_language(video_file: str, target_language: LanguageCode, recursion: bool = True, only_match_subgen_subtitles: bool = False) -> bool:
    """Checks if the given folder has a subtitle file with the given language.
    Args:
        video_file (str): The path of the video file.
        target_language (LanguageCode): The language of the subtitle file to search for.
        recursion (bool): If True, search subfolders. If False, only the current folder.
        only_match_subgen_subtitles (bool): If True, only skip if subtitles are auto-generated ("subgen").
    Returns:
        bool: True if a matching subtitle file is found, False otherwise.
    """
    subtitle_extensions = {'.srt', '.vtt', '.sub', '.ass', '.ssa', '.idx', '.sbv', '.pgs', '.ttml', '.lrc'}

    video_folder = os.path.dirname(video_file)
    video_name = os.path.splitext(os.path.basename(video_file))[0]

    try:
        dir_entries = os.listdir(video_folder)
    except OSError as e:
        logging.warning(f"Could not list directory {video_folder}: {e}")
        return False
    for file_name in dir_entries:
        file_path = os.path.join(video_folder, file_name)

        # If it's a file and has a subtitle extension
        if os.path.isfile(file_path) and file_path.endswith(tuple(subtitle_extensions)):
            subtitle_name, ext = os.path.splitext(file_name)

            # Ensure the subtitle name starts with the video name
            if not subtitle_name.startswith(video_name):
                continue

            # Extract parts after video filename
            subtitle_parts = subtitle_name[len(video_name):].lstrip(".").split(".")

            # Check for "subgen"
            has_subgen = "subgen" in subtitle_parts

            # When audio language is unknown, decide based on whether this subtitle counts.
            if target_language == LanguageCode.NONE:
                if only_match_subgen_subtitles:
                    if has_subgen:
                        return True   # Subgen subtitle exists → counts as covered → skip
                    continue          # Non-subgen subtitle → ignore, keep looking
                return True           # Any subtitle found → skip

            # Check if the subtitle file matches the target language
            if is_valid_subtitle_language(subtitle_parts, target_language):
                if only_match_subgen_subtitles and not has_subgen:
                    continue  # Ignore non-subgen subtitles if flag is set
                logging.debug(f"Found matching subtitle: {file_name} for language {target_language.name} (subgen={has_subgen})")
                return True

        # Recursively search subfolders
        elif os.path.isdir(file_path) and recursion:
            if has_external_subtitle_in_language(os.path.join(file_path, os.path.basename(video_file)), target_language, False, only_match_subgen_subtitles):
                return True

    return False

def is_valid_subtitle_language(subtitle_parts: List[str], target_language: LanguageCode) -> bool:
    """Checks if any part of the subtitle name matches the target language."""
    return any(LanguageCode.from_string(part) == target_language for part in subtitle_parts)

def get_next_plex_episode(current_episode_rating_key, stay_in_season: bool = False):
    """
    Get the next episode's ratingKey based on the current episode in Plex.
    Args:
        current_episode_rating_key (str): The ratingKey of the current episode.
        stay_in_season (bool): If True, only find the next episode within the current season.
                              If False, find the next episode in the series.
    Returns:
        str: The ratingKey of the next episode, or None if it's the last episode.
    """
    try:
        # Get current episode's metadata to fetch parent (season) ratingKey
        url = f"{plexserver}/library/metadata/{current_episode_rating_key}"
        headers = {"X-Plex-Token": plextoken}
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        # Parse XML response
        root = ET.fromstring(response.content)

        # Find the show ID
        grandparent_rating_key = root.find(".//Video").get("grandparentRatingKey")
        if grandparent_rating_key is None:
            logging.debug(f"Show not found for episode {current_episode_rating_key}")
            return None

        # Find the parent season ratingKey
        parent_rating_key = root.find(".//Video").get("parentRatingKey")
        if parent_rating_key is None:
            logging.debug(f"Parent season not found for episode {current_episode_rating_key}")
            return None

        # Get the list of seasons
        url = f"{plexserver}/library/metadata/{grandparent_rating_key}/children"
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        seasons = ET.fromstring(response.content).findall(".//Directory[@type='season']")

        # Get the list of episodes in the parent season
        url = f"{plexserver}/library/metadata/{parent_rating_key}/children"
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        #print(response.content)

        # Parse XML response for the list of episodes
        episodes = ET.fromstring(response.content).findall(".//Video")
        episodes_in_season = len(episodes) #episodes.get('size') # changed from episodes.get("size") because size is not available

        # Find the current episode index and get the next one
        current_episode_number = None
        current_season_number = None
        next_season_number = None
        for episode in episodes:
            if episode.get("ratingKey") == current_episode_rating_key:
                ep_index = episode.get("index")
                if ep_index is None:
                    logging.warning(f"Episode ratingKey {current_episode_rating_key} has no index attribute")
                    return None
                current_episode_number = int(ep_index)
                current_season_number = episode.get("parentIndex")
                break
            #if rating_key_element is None:
            #    logging.warning(f"ratingKey not found for episode at index")
            #    continue

        # Logic to find the next episode
        if stay_in_season:
          if current_episode_number == episodes_in_season:
              return None # End of season
          for episode in episodes:
            ep_index = episode.get("index")
            if ep_index is not None and int(ep_index) == int(current_episode_number)+1:
                return episode.get("ratingKey")
        else: # Not staying in season, find the next overall episode
          # Find next season if it exists
          for season in seasons:
              s_index = season.get("index")
              if s_index is not None and int(s_index) == int(current_season_number)+1:
                  #print(f"next season is: {episode.get('ratingKey')}")
                  #print(season.get("title"))
                  next_season_number = season.get("ratingKey")
                  break

          if current_episode_number == episodes_in_season: # changed to episodes_in_season from int(episodes_in_season)
              if next_season_number is not None:
                logging.debug("At end of season, try to find next season and first episode.")
                url = f"{plexserver}/library/metadata/{next_season_number}/children"
                response = requests.get(url, headers=headers)
                response.raise_for_status()
                episodes = ET.fromstring(response.content).findall(".//Video")
                current_episode_number = 0
              else:
                return None
          for episode in episodes:
            ep_index = episode.get("index")
            if ep_index is not None and int(ep_index) == int(current_episode_number)+1:
                return episode.get("ratingKey")

        logging.debug(f"No next episode found for {get_plex_file_name(current_episode_rating_key, plexserver, plextoken)}, possibly end of season or series")
        return None

    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching data from Plex: {e}")
        return None
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        return None

def get_plex_file_name(itemid: str, server_ip: str, plex_token: str) -> str:
    """Gets the full path to a file from the Plex server.
    Args:
        itemid: The ID of the item in the Plex library.
        server_ip: The IP address of the Plex server.
        plex_token: The Plex token.
    Returns:
        The full path to the file.
    """

    url = f"{server_ip}/library/metadata/{itemid}"

    headers = {
        "X-Plex-Token": plex_token,
    }

    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        root = ET.fromstring(response.content)
        part = root.find(".//Part")
        if part is None:
            raise Exception("No Part element found in Plex XML response")
        fullpath = part.attrib['file']
        return fullpath
    else:
        raise Exception(f"Error: {response.status_code}")

def refresh_plex_metadata(itemid: str, server_ip: str, plex_token: str) -> None:
    """
    Refreshes the metadata of a Plex library item.
    
    Args:
        itemid: The ID of the item in the Plex library whose metadata needs to be refreshed.
        server_ip: The IP address of the Plex server.
        plex_token: The Plex token used for authentication.
        
    Raises:
        Exception: If the server does not respond with a successful status code.
    """

    # Plex API endpoint to refresh metadata for a specific item
    url = f"{server_ip}/library/metadata/{itemid}/refresh"

    # Headers to include the Plex token for authentication
    headers = {
        "X-Plex-Token": plex_token,
    }

    # Sending the PUT request to refresh metadata
    response = requests.put(url, headers=headers)

    # Check if the request was successful
    if response.status_code == 200:
        logging.info("Metadata refresh initiated successfully.")
    else:
        raise Exception(f"Error refreshing metadata: {response.status_code}")

def refresh_jellyfin_metadata(itemid: str, server_ip: str, jellyfin_token: str) -> None:
    """
    Refreshes the metadata of a Jellyfin library item.
    
    Args:
        itemid: The ID of the item in the Jellyfin library whose metadata needs to be refreshed.
        server_ip: The IP address of the Jellyfin server.
        jellyfin_token: The Jellyfin token used for authentication.
        
    Raises:
        Exception: If the server does not respond with a successful status code.
    """

    # Jellyfin API endpoint to refresh metadata for a specific item
    url = f"{server_ip}/Items/{itemid}/Refresh?MetadataRefreshMode=FullRefresh"

    # Headers to include the Jellyfin token for authentication
    headers = {
        "Authorization": f"MediaBrowser Token={jellyfin_token}",
    }

    response = requests.post(url, headers=headers)

    # Check if the request was successful
    if response.status_code == 204:
        logging.info("Metadata refresh queued successfully.")
    else:
        raise Exception(f"Error refreshing metadata: {response.status_code}")


def get_jellyfin_file_name(item_id: str, jellyfin_url: str, jellyfin_token: str) -> str:
    """Gets the full path to a file from the Jellyfin server.
    Args:
        jellyfin_url: The URL of the Jellyfin server.
        jellyfin_token: The Jellyfin token.
        item_id: The ID of the item in the Jellyfin library.
    Returns:
        The full path to the file.
    """

    headers = {
        "Authorization": f"MediaBrowser Token={jellyfin_token}",
    }

    # Cheap way to get the admin user id, and save it for later use.
    users = json.loads(requests.get(f"{jellyfin_url}/Users", headers=headers).content)
    jellyfin_admin = get_jellyfin_admin(users)

    response = requests.get(f"{jellyfin_url}/Users/{jellyfin_admin}/Items/{item_id}", headers=headers)

    if response.status_code == 200:
        file_name = json.loads(response.content)['Path']
        return file_name
    else:
        raise Exception(f"Error: {response.status_code}")

def get_jellyfin_admin(users):
    for user in users:
        if user["Policy"]["IsAdministrator"]:
            return user["Id"]

    raise Exception("Unable to find administrator user in Jellyfin")

def has_audio(file_path):
    try:
        if not is_valid_path(file_path):
            return False

        if not (has_video_extension(file_path) or has_audio_extension(file_path)):
            return False

        with av.open(file_path) as container:
            # Check for an audio stream and ensure it has a valid codec
            for stream in container.streams:
                if stream.type == 'audio':
                    # Check if the stream has a codec and if it is valid
                    if stream.codec_context and stream.codec_context.name != 'none':
                        return True
                    else:
                        logging.debug(f"Unsupported or missing codec for audio stream in {file_path}")
            return False

    except (av.FFmpegError, UnicodeDecodeError):
        logging.debug(f"Error processing file {file_path}")
        return False

def is_valid_path(file_path):
    # Check if the path is a file
    if not os.path.isfile(file_path):
        # If it's not a file, check if it's a directory
        if not os.path.isdir(file_path):
            logging.warning(f"{file_path} is neither a file nor a directory. Are your volumes correct?")
            return False
        else:
            logging.debug(f"{file_path} is a directory, skipping processing as a file.")
            return False
    else:
        return True    

def has_video_extension(file_name):
    file_extension = os.path.splitext(file_name)[1].lower() # Get the file extension
    return file_extension in VIDEO_EXTENSIONS

def has_audio_extension(file_name):
    file_extension = os.path.splitext(file_name)[1].lower() # Get the file extension
    return file_extension in AUDIO_EXTENSIONS


def path_mapping(fullpath):
    if use_path_mapping:
        logging.debug("Updated path: " + fullpath.replace(path_mapping_from, path_mapping_to))
        return fullpath.replace(path_mapping_from, path_mapping_to)
    return fullpath

def is_file_stable(file_path, wait_time=2, check_intervals=3):
    """Returns True if the file size is stable for a given number of checks."""
    if not os.path.exists(file_path):
        return False

    previous_size = -1
    for _ in range(check_intervals):
        try:
            current_size = os.path.getsize(file_path)
        except OSError:
            return False  # File might still be inaccessible

        if current_size == previous_size:
            return True  # File is stable
        previous_size = current_size
        time.sleep(wait_time)

    return False  # File is still changing

class NewFileHandler(FileSystemEventHandler):
    """Watchdog handler that queues newly created or modified media files."""

    def create_subtitle(self, event):
        if not event.is_directory:
            file_path = event.src_path
            if has_audio(file_path):
                logging.info(f"File: {path_mapping(file_path)} was added")
                gen_subtitles_queue(path_mapping(file_path), transcribe_or_translate)

    def handle_event(self, event):
        """Wait for file stability before processing."""
        if is_file_stable(event.src_path):
            self.create_subtitle(event)

    def on_created(self, event):
        time.sleep(5)  # Extra buffer time for new files
        self.handle_event(event)

    def on_modified(self, event):
        self.handle_event(event)


def transcribe_existing(transcribe_folders, forceLanguage: LanguageCode = LanguageCode.NONE):
    transcribe_folders = transcribe_folders.split("|")
    logging.info("Starting to search folders to see if we need to create subtitles.")
    logging.debug("The folders are:")
    for path in transcribe_folders:
        logging.debug(path)
        for root, dirs, files in os.walk(path):
            for file in files:
                file_path = os.path.join(root, file)
                gen_subtitles_queue(path_mapping(file_path), transcribe_or_translate, forceLanguage)
        # if the path specified was actually a single file and not a folder, process it
        if os.path.isfile(path):
            if has_audio(path):
                gen_subtitles_queue(path_mapping(path), transcribe_or_translate, forceLanguage)
    # Set up the observer to watch for new files
    if monitor:
        observer = Observer()
        for path in transcribe_folders:
            if os.path.isdir(path):
                handler = NewFileHandler()
                observer.schedule(handler, path, recursive=True)
        observer.start()
        logging.info("Finished searching and queueing files for transcription. Now watching for new files.")


if __name__ == "__main__":
    import uvicorn
    logging.info(f"Subgen v{subgen_version}")
    logging.info(f"Concurrent transcriptions: {str(concurrent_transcriptions)}")
    logging.info(f"Transcription endpoint: {openai_audio_url('transcribe')}, Model: {transcription_model}")
    uvicorn.run("__main__:app", host="0.0.0.0", port=int(webhookport), reload=reload_script_on_change, use_colors=True)
