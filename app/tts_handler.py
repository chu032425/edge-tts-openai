# tts_handler.py
#
# Patched version (2026-11-25):
#   - edge-tts pinned to 7.3.1 (Sec-MS-GEC v2 token, fixes the 2026-10 403 storm)
#   - retry with exponential backoff for transient failures (NoAudioReceived / 403 / timeouts)
#   - synthesis serialised through a gevent semaphore to avoid Microsoft-side rate limiting
#   - speed range aligned with the OpenAI spec (0.25 - 4.0)
#   - clear error when a non-mp3 format is requested but ffmpeg is missing
#   - temp files are always cleaned up (including the failure path)

import asyncio
import os
import random
import subprocess
import tempfile
import time
from pathlib import Path

import aiohttp
import edge_tts
from gevent.lock import Semaphore

from utils import DETAILED_ERROR_LOGGING, filter_voices_by_tag
from config import DEFAULT_CONFIGS
from ratelimit import (
    TokenBucketLimiter,
    CircuitBreaker,
    RateLimitExceeded,
    UpstreamUnavailable,
)

# Language default (environment variable)
DEFAULT_LANGUAGE = os.getenv('DEFAULT_LANGUAGE', DEFAULT_CONFIGS["DEFAULT_LANGUAGE"])

# Retry policy for transient Microsoft-side failures.
RETRY_ATTEMPTS = int(os.getenv('TTS_RETRY_ATTEMPTS', '3'))
RETRY_BASE_DELAY = float(os.getenv('TTS_RETRY_BASE_DELAY', '1.0'))

# Serialise synthesis. Microsoft rate-limits per IP/voice; parallel requests
# are the main cause of the "1 in 4 requests succeeds" reports.
_SYNTH_SEMAPHORE = Semaphore(1)

# Token bucket + bounded queue + circuit breaker (see ratelimit.py)
LIMITER = TokenBucketLimiter()
BREAKER = CircuitBreaker()

# Exceptions worth retrying. Everything else (bad voice, empty text, ...) is
# surfaced to the caller immediately.
_RETRYABLE = (
    edge_tts.exceptions.NoAudioReceived,
    edge_tts.exceptions.WebSocketError,
    edge_tts.exceptions.UnexpectedResponse,
    edge_tts.exceptions.UnknownResponse,
    aiohttp.ClientError,
    aiohttp.WSServerHandshakeError,
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
)

# OpenAI voice names mapped to edge-tts equivalents
voice_mapping = {
    'alloy': 'en-US-JennyNeural',
    'ash': 'en-US-AndrewNeural',
    'ballad': 'en-GB-ThomasNeural',
    'coral': 'en-AU-NatashaNeural',
    'echo': 'en-US-GuyNeural',
    'fable': 'en-GB-SoniaNeural',
    'nova': 'en-US-AriaNeural',
    'onyx': 'en-US-EricNeural',
    'sage': 'en-US-JennyNeural',
    'shimmer': 'en-US-EmmaNeural',
    'verse': 'en-US-BrianNeural',
}

# 对外暴露的模型 ID 列表：逗号分隔。这些只是别名，实际都走同一个 edge-tts 合成引擎。
TTS_MODELS = os.getenv('TTS_MODELS', 'tts-1')
model_data = [
    {"id": model_id, "name": "Text-to-speech"}
    for model_id in (x.strip() for x in TTS_MODELS.split(',') if x.strip())
]


class FfmpegNotInstalledError(RuntimeError):
    """Raised when a format conversion is requested but ffmpeg is unavailable."""


class SynthesisError(RuntimeError):
    """Raised when all retry attempts against the upstream service failed."""


def is_ffmpeg_installed():
    """Check if FFmpeg is installed and accessible."""
    try:
        subprocess.run(['ffmpeg', '-version'], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _log(msg):
    print(msg, flush=True)


async def _communicate(text, voice, speed, pitch="+0Hz"):
    """One synthesis attempt. Returns the mp3 bytes."""
    edge_tts_voice = voice_mapping.get(voice, voice)
    rate = speed_to_rate(speed)

    chunks = bytearray()
    communicator = edge_tts.Communicate(text=text, voice=edge_tts_voice, rate=rate, pitch=pitch)
    async for chunk in communicator.stream():
        if chunk["type"] == "audio":
            chunks.extend(chunk["data"])

    if not chunks:
        raise edge_tts.exceptions.NoAudioReceived(
            "No audio was received. Please verify that your parameters are correct."
        )
    return bytes(chunks)


async def _synthesize_mp3_with_retry(text, voice, speed, pitch="+0Hz"):
    """Synthesis with retry/backoff. Returns mp3 bytes."""
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with _SYNTH_SEMAPHORE:
                return await _communicate(text, voice, speed, pitch)
        except _RETRYABLE as exc:
            last_error = exc
            if attempt >= RETRY_ATTEMPTS:
                break
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            _log(f"[tts] attempt {attempt}/{RETRY_ATTEMPTS} failed ({type(exc).__name__}: {exc}); "
                 f"retrying in {delay:.1f}s")
            await asyncio.sleep(delay)
    raise SynthesisError(
        f"TTS synthesis failed after {RETRY_ATTEMPTS} attempts: {type(last_error).__name__}: {last_error}"
    ) from last_error


async def _generate_audio_stream(text, voice, speed, pitch="+0Hz"):
    """Generate streaming TTS audio using edge-tts (async generator)."""
    edge_tts_voice = voice_mapping.get(voice, voice)
    rate = speed_to_rate(speed)

    communicator = edge_tts.Communicate(text=text, voice=edge_tts_voice, rate=rate, pitch=pitch)
    async for chunk in communicator.stream():
        if chunk["type"] == "audio":
            yield chunk["data"]


def _stream_sync(text, voice, speed, pitch="+0Hz"):
    """Synchronous wrapper around the async generator, serialised by the semaphore."""
    with _SYNTH_SEMAPHORE:
        loop = asyncio.new_event_loop()
        try:
            agen = _generate_audio_stream(text, voice, speed, pitch)
            while True:
                try:
                    chunk = loop.run_until_complete(agen.__anext__())
                except StopAsyncIteration:
                    break
                yield chunk
        finally:
            try:
                loop.run_until_complete(agen.aclose())
            except Exception:
                pass
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()


def generate_speech_stream(text, voice, speed=1.0, pitch="+0Hz"):
    """Generate streaming speech audio (synchronous wrapper)."""
    BREAKER.check()
    LIMITER.acquire()

    def _guarded():
        try:
            yield from _stream_sync(text, voice, speed)
        except RateLimitExceeded:
            raise
        except Exception:
            BREAKER.record_failure()
            raise
        else:
            BREAKER.record_success()
        finally:
            LIMITER.release()

    return _guarded()


async def _generate_audio(text, voice, response_format, speed, pitch="+0Hz"):
    """Generate TTS audio and optionally convert to a different format."""
    mp3_bytes = await _synthesize_mp3_with_retry(text, voice, speed, pitch)

    temp_mp3_file_obj = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3", prefix=TEMP_PREFIX)
    temp_mp3_path = temp_mp3_file_obj.name
    temp_mp3_file_obj.write(mp3_bytes)
    temp_mp3_file_obj.close()

    # If the requested format is mp3, return the generated file directly
    if response_format == "mp3":
        return temp_mp3_path

    # Check if FFmpeg is installed
    if not is_ffmpeg_installed():
        Path(temp_mp3_path).unlink(missing_ok=True)
        raise FfmpegNotInstalledError(
            f"response_format='{response_format}' requires ffmpeg, which is not installed in this "
            "container. Use the 'latest-ffmpeg' image (or build with INSTALL_FFMPEG=true), "
            "or request response_format='mp3'."
        )

    # Create a new temporary file for the converted output
    converted_file_obj = tempfile.NamedTemporaryFile(delete=False, suffix=f".{response_format}", prefix=TEMP_PREFIX)
    converted_path = converted_file_obj.name
    converted_file_obj.close()  # Close file object, ffmpeg will write to the path

    # Build the FFmpeg command
    ffmpeg_command = [
        "ffmpeg",
        "-i", temp_mp3_path,  # Input file path
        "-c:a", {
            "aac": "aac",
            "mp3": "libmp3lame",
            "wav": "pcm_s16le",
            "opus": "libopus",
            "flac": "flac",
            "pcm": "pcm_s16le"
        }.get(response_format, "aac"),  # Default to AAC if unknown
    ]

    # Lossless/raw formats do not take a bitrate; only lossy encoders do.
    if response_format in ("aac", "mp3", "opus"):
        ffmpeg_command.extend(["-b:a", "192k"])

    ffmpeg_command.extend([
        "-f", {
            "aac": "mp4",  # AAC in MP4 container
            "mp3": "mp3",
            "wav": "wav",
            "opus": "ogg",
            "flac": "flac",
            "pcm": "s16le"  # raw 16-bit little-endian PCM
        }.get(response_format, response_format),  # Default to matching format
        "-y",  # Overwrite without prompt
        converted_path  # Output file path
    ])

    try:
        # Run FFmpeg command and ensure no errors occur
        subprocess.run(ffmpeg_command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        # Clean up potentially created (but incomplete) converted file
        Path(converted_path).unlink(missing_ok=True)
        # Clean up the original mp3 file as well, since conversion failed
        Path(temp_mp3_path).unlink(missing_ok=True)

        if DETAILED_ERROR_LOGGING:
            error_message = f"FFmpeg error during audio conversion. Command: '{' '.join(e.cmd)}'. Stderr: {e.stderr.decode('utf-8', 'ignore')}"
            _log(error_message)  # Log for server-side diagnosis
        else:
            error_message = f"FFmpeg error during audio conversion: {e}"
            _log(error_message)  # Log a simpler message
        raise RuntimeError(f"FFmpeg error during audio conversion: {e}")  # The raised error will still have details via e

    # Clean up the original temporary file (original mp3) as it's now converted
    Path(temp_mp3_path).unlink(missing_ok=True)

    return converted_path


TEMP_PREFIX = "edge-tts-"
TEMP_MAX_AGE = float(os.getenv('TTS_TEMP_MAX_AGE', '3600'))


def cleanup_stale_temp_files(max_age_seconds=None):
    """Delete leftover temp audio files (crash / SIGKILL mid-request).

    Files are written to the system temp dir (TMPDIR, default /tmp) and are
    normally deleted as soon as the response body has been read.  Only files
    matching TEMP_PREFIX older than max_age_seconds are removed, so in-flight
    requests are never touched.
    """
    max_age = TEMP_MAX_AGE if max_age_seconds is None else max_age_seconds
    temp_dir = tempfile.gettempdir()
    now = time.time()
    removed = 0
    try:
        entries = os.listdir(temp_dir)
    except OSError:
        return 0
    for name in entries:
        if not name.startswith(TEMP_PREFIX):
            continue
        path = os.path.join(temp_dir, name)
        try:
            if now - os.path.getmtime(path) < max_age:
                continue
            os.unlink(path)
            removed += 1
        except OSError:
            pass
    if removed:
        _log(f"[tts] cleaned up {removed} stale temp file(s) from {temp_dir}")
    return removed


def generate_speech(text, voice, response_format, speed=1.0, pitch="+0Hz"):
    """Synthesise with rate limiting + circuit breaker.

    Raises RateLimitExceeded (429) when the queue is full / too slow,
    UpstreamUnavailable (503) when the breaker is open.
    """
    BREAKER.check()
    LIMITER.acquire()
    try:
        path = asyncio.run(_generate_audio(text, voice, response_format, speed, pitch))
    except RateLimitExceeded:
        raise
    except UpstreamUnavailable:
        raise
    except SynthesisError:
        # retries exhausted -> count as an upstream failure
        BREAKER.record_failure()
        raise
    else:
        BREAKER.record_success()
        return path
    finally:
        LIMITER.release()


def get_models():
    return model_data


def get_models_formatted():
    return [{"id": x["id"]} for x in model_data]


def get_voices_formatted():
    """OpenAI alias -> Edge voice map, enriched with gender/language/status/tags."""
    by_name = {v["name"]: v for v in asyncio.run(_get_voices("all"))}
    result = []
    for alias, edge_name in voice_mapping.items():
        item = {"id": alias, "name": edge_name}
        info = by_name.get(edge_name)
        if info:
            item.update({key: info[key] for key in ("gender", "language", "status", "tags")})
        result.append(item)
    return result


# Simple TTL cache for the (large) voice list, so /v1/voices does not hammer Microsoft.
_VOICES_CACHE = {"ts": 0.0, "data": None}
_VOICES_CACHE_TTL = float(os.getenv('TTS_VOICES_CACHE_TTL', '300'))


async def _get_voices(language=None, tag=None, match='any'):
    # List all voices, filter by language if specified
    now = time.monotonic()
    if now - _VOICES_CACHE["ts"] > _VOICES_CACHE_TTL or _VOICES_CACHE["data"] is None:
        _VOICES_CACHE["data"] = await edge_tts.list_voices()
        _VOICES_CACHE["ts"] = now
    all_voices = _VOICES_CACHE["data"]

    language = language or DEFAULT_LANGUAGE  # Use default if no language specified
    filtered_voices = [
        {
            "name": v['ShortName'],
            "gender": v['Gender'],
            "language": v['Locale'],
            "status": v.get('Status'),
            "tags": {
                "content_categories": [c.strip() for c in v.get('VoiceTag', {}).get('ContentCategories', [])],
                "personalities": [c.strip() for c in v.get('VoiceTag', {}).get('VoicePersonalities', [])],
            },
        }
        for v in all_voices if language == 'all' or language is None or v['Locale'] == language
    ]
    return filter_voices_by_tag(filtered_voices, tag, match)


def get_voices(language=None, tag=None, match='any'):
    return asyncio.run(_get_voices(language, tag, match))


def speed_to_rate(speed: float) -> str:
    """
    Converts a multiplicative speed value to the edge-tts "rate" format.

    Args:
        speed (float): The multiplicative speed value (e.g., 1.5 for +50%, 0.5 for -50%).

    Returns:
        str: The formatted "rate" string (e.g., "+50%" or "-50%").
    """
    if speed < 0.25 or speed > 4.0:
        raise ValueError("Speed must be between 0.25 and 4.0 (inclusive).")

    # Convert speed to percentage change
    percentage_change = (speed - 1) * 100

    # Format with a leading "+" or "-" as required
    return f"{percentage_change:+.0f}%"
