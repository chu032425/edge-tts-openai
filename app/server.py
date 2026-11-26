# server.py
#
# Patched version (2026-11-25):
#   - /health endpoint for docker HEALTHCHECK
#   - input/speed validation with proper 400s
#   - clear 500 when ffmpeg is missing but a converted format was requested
#   - warning at startup when the default API key is still in use

from flask import Flask, request, jsonify, Response
from gevent.pywsgi import WSGIServer
from dotenv import load_dotenv
import os
import uuid
import gevent
import traceback
import json
import base64

from config import DEFAULT_CONFIGS
from handle_text import prepare_tts_input_with_context
from tts_handler import (
    generate_speech,
    generate_speech_stream,
    get_models_formatted,
    get_voices,
    get_voices_formatted,
    is_ffmpeg_installed,
    cleanup_stale_temp_files,
    speed_to_rate,
    FfmpegNotInstalledError,
    LIMITER,
    BREAKER,
)
from ratelimit import RateLimitExceeded, UpstreamUnavailable
from utils import (
    getenv_bool,
    require_api_key,
    openai_error,
    normalize_pitch,
    filter_voices_by_tag,
    AUDIO_FORMAT_MIME_TYPES,
    DETAILED_ERROR_LOGGING,
)

import edge_tts

APP_VERSION = "2.4.0-patched+edge-tts-7.3.1"
MAX_INPUT_CHARS = 4096

app = Flask(__name__)
load_dotenv()

API_KEY = os.getenv('API_KEY', DEFAULT_CONFIGS["API_KEY"])
PORT = int(os.getenv('PORT', str(DEFAULT_CONFIGS["PORT"])))

DEFAULT_VOICE = os.getenv('DEFAULT_VOICE', DEFAULT_CONFIGS["DEFAULT_VOICE"])
DEFAULT_RESPONSE_FORMAT = os.getenv('DEFAULT_RESPONSE_FORMAT', DEFAULT_CONFIGS["DEFAULT_RESPONSE_FORMAT"])
DEFAULT_SPEED = float(os.getenv('DEFAULT_SPEED', str(DEFAULT_CONFIGS["DEFAULT_SPEED"])))

REMOVE_FILTER = getenv_bool('REMOVE_FILTER', DEFAULT_CONFIGS["REMOVE_FILTER"])
EXPAND_API = getenv_bool('EXPAND_API', DEFAULT_CONFIGS["EXPAND_API"])

# DEFAULT_MODEL = os.getenv('DEFAULT_MODEL', 'tts-1')


@app.after_request
def _add_request_id(response):
    """Expose an OpenAI-style x-request-id header on every response."""
    response.headers.setdefault('x-request-id', uuid.uuid4().hex)
    return response


_AZURE_PITCH_LEVELS = {
    "x-low": -50.0,
    "low": -25.0,
    "medium": 0.0,
    "high": 25.0,
    "x-high": 50.0,
}


def _parse_azure_rate(value):
    """'10%' / '+10%' / '1.2' -> speed multiplier (0.25..4.0)."""
    text = str(value).strip().lower()
    try:
        multiplier = 1.0 + float(text[:-1]) / 100.0 if text.endswith('%') else float(text)
    except ValueError:
        raise ValueError(f"Invalid SSML rate '{value}'")
    speed_to_rate(multiplier)  # range check
    return multiplier


def _parse_azure_pitch(value):
    """'x-high' / '+20Hz' -> edge-tts '+NHz' form."""
    text = str(value).strip().lower()
    if text in _AZURE_PITCH_LEVELS:
        return normalize_pitch(_AZURE_PITCH_LEVELS[text])
    return normalize_pitch(text)


def _parse_voice_filters(data):
    """Read ``tag``/``tags`` + ``match`` query filters for the voices endpoints."""
    data = data or {}
    tag = data.get('tag') if 'tag' in data else data.get('tags')
    match = str(data.get('match') or 'any').lower()
    if match not in ('any', 'all'):
        raise ValueError("'match' must be 'any' or 'all'")
    return tag, match


def _validate_request(data):
    """Validate and normalise an OpenAI-compatible TTS request.

    Returns (text, voice, response_format, speed, pitch).
    Raises ValueError for client errors (400).
    """
    if not data or 'input' not in data:
        raise ValueError("Missing 'input' in request body")

    text = data.get('input')
    if not isinstance(text, str) or not text.strip():
        raise ValueError("'input' must be a non-empty string")
    if len(text) > MAX_INPUT_CHARS:
        raise ValueError(f"'input' is too long: {len(text)} characters (max {MAX_INPUT_CHARS})")

    if not REMOVE_FILTER:
        text = prepare_tts_input_with_context(text)
    if not text.strip():
        raise ValueError("'input' is empty after Markdown/emoji filtering")

    voice = data.get('voice', DEFAULT_VOICE)
    response_format = data.get('response_format', DEFAULT_RESPONSE_FORMAT)
    if response_format not in AUDIO_FORMAT_MIME_TYPES:
        raise ValueError(
            f"Unsupported response_format '{response_format}'. "
            f"Supported: {', '.join(sorted(AUDIO_FORMAT_MIME_TYPES))}"
        )

    try:
        speed = float(data.get('speed', DEFAULT_SPEED))
    except (TypeError, ValueError):
        raise ValueError("'speed' must be a number")

    speed_to_rate(speed)  # raises ValueError when out of range

    # Optional extension: pitch offset in Hz (e.g. "+10Hz", "-20Hz", 10)
    pitch = normalize_pitch(data.get('pitch'))  # raises ValueError when invalid

    return text, voice, response_format, speed, pitch


def _audio_response_from_temp(path, mime_type):
    """Read a temp audio file into memory, delete it, and return a Response.

    Nothing is left on disk once the response has been built.
    """
    try:
        with open(path, 'rb') as audio_file:
            audio_data = audio_file.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return Response(
        audio_data,
        mimetype=mime_type,
        headers={
            'Content-Type': mime_type,
            'Content-Length': str(len(audio_data)),
        },
    )


def generate_sse_audio_stream(text, voice, speed, pitch="+0Hz"):
    """Generator function for SSE streaming with JSON events."""
    try:
        # Generate streaming audio chunks and convert to SSE format
        for chunk in generate_speech_stream(text, voice, speed, pitch):
            # Base64 encode the audio chunk
            encoded_audio = base64.b64encode(chunk).decode('utf-8')

            # Create SSE event for audio delta
            event_data = {
                "type": "speech.audio.delta",
                "audio": encoded_audio
            }

            # Format as SSE event
            yield f"data: {json.dumps(event_data)}\n\n"

        # Send completion event
        completion_event = {
            "type": "speech.audio.done",
            "usage": {
                "input_tokens": len(text.split()),  # Rough estimate
                "output_tokens": 0,  # Edge TTS doesn't provide this
                "total_tokens": len(text.split())
            }
        }
        yield f"data: {json.dumps(completion_event)}\n\n"

    except Exception as e:
        print(f"Error during SSE streaming: {e}")
        # Send error event
        error_event = {
            "type": "error",
            "error": str(e)
        }
        yield f"data: {json.dumps(error_event)}\n\n"


@app.route('/health', methods=['GET'])
@app.route('/healthz', methods=['GET'])
def health():
    """Liveness probe. No auth, no network calls."""
    return jsonify({
        "status": "ok",
        "version": APP_VERSION,
        "edge_tts": getattr(edge_tts, "__version__", "unknown"),
        "ffmpeg": is_ffmpeg_installed(),
        "rate_limit": LIMITER.snapshot(),
        "breaker": BREAKER.snapshot(),
    })


# OpenAI endpoint format
@app.route('/v1/audio/speech', methods=['POST'])
@app.route('/audio/speech', methods=['POST'])  # Add this line for the alias
@require_api_key
def text_to_speech():
    try:
        data = request.json
        text, voice, response_format, speed, pitch = _validate_request(data)

        # Check stream format - only "sse" triggers streaming
        stream_format = data.get('stream_format', 'audio')  # 'audio' (default) or 'sse'

        mime_type = AUDIO_FORMAT_MIME_TYPES.get(response_format, "audio/mpeg")

        if stream_format == 'sse':
            # Return SSE streaming response with JSON events
            def generate_sse():
                for event in generate_sse_audio_stream(text, voice, speed, pitch):
                    yield event

            return Response(
                generate_sse(),
                mimetype='text/event-stream',
                headers={
                    'Content-Type': 'text/event-stream',
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive',
                    'X-Accel-Buffering': 'no'  # Disable nginx buffering
                }
            )
        else:
            # Return raw audio data (like OpenAI) - can be piped to ffplay
            output_file_path = generate_speech(text, voice, response_format, speed, pitch)
            return _audio_response_from_temp(output_file_path, mime_type)

    except RateLimitExceeded as e:
        # Queue full / waited too long -> 429 with Retry-After
        return openai_error(
            str(e),
            status=429,
            error_type="rate_limit_error",
            code="rate_limit_exceeded",
            headers={"Retry-After": str(e.retry_after)},
        )
    except UpstreamUnavailable as e:
        # Circuit breaker open -> 503 with Retry-After
        return openai_error(
            str(e),
            status=503,
            error_type="server_error",
            code="upstream_unavailable",
            headers={"Retry-After": str(e.retry_after)},
        )
    except ValueError as e:
        # Client error (bad input, unsupported format, speed out of range)
        return openai_error(str(e), status=400, error_type="invalid_request_error", code="invalid_request")
    except FfmpegNotInstalledError as e:
        return openai_error(str(e), status=500, error_type="server_error", code="ffmpeg_not_installed")
    except Exception as e:
        if DETAILED_ERROR_LOGGING:
            app.logger.error(f"Error in text_to_speech: {str(e)}\n{traceback.format_exc()}")
        else:
            app.logger.error(f"Error in text_to_speech: {str(e)}")
        # Return a 500 error for unhandled exceptions, which is more standard than 400
        return openai_error(
            "An internal server error occurred",
            status=500,
            error_type="server_error",
            code="internal_error",
        )


# OpenAI endpoint format
@app.route('/v1/models', methods=['GET', 'POST'])
@app.route('/models', methods=['GET', 'POST'])
@app.route('/v1/audio/models', methods=['GET', 'POST'])
@app.route('/audio/models', methods=['GET', 'POST'])
@require_api_key
def list_models():
    # OpenAI SDK-compatible shape ("data")
    data = [
        {"id": m["id"], "object": "model", "created": 0, "owned_by": "edge-tts"}
        for m in get_models_formatted()
    ]
    return jsonify({"object": "list", "data": data})


@app.route('/v1/models/<model_id>', methods=['GET'])
@app.route('/models/<model_id>', methods=['GET'])
@require_api_key
def get_model_by_id(model_id):
    """OpenAI-compatible model retrieval (client.models.retrieve)."""
    for model in get_models_formatted():
        if model["id"] == model_id:
            return jsonify({
                "id": model["id"],
                "object": "model",
                "created": 0,
                "owned_by": "edge-tts",
            })
    return openai_error(
        f"Model '{model_id}' not found",
        status=404,
        error_type="invalid_request_error",
        code="model_not_found",
    )


# OpenAI endpoint format
@app.route('/v1/audio/voices', methods=['GET', 'POST'])
@app.route('/audio/voices', methods=['GET', 'POST'])
@require_api_key
def list_voices_formatted():
    return jsonify({"voices": get_voices_formatted()})


@app.route('/v1/voices', methods=['GET', 'POST'])
@app.route('/voices', methods=['GET', 'POST'])
@require_api_key
def list_voices():
    specific_language = None

    data = request.args if request.method == 'GET' else (request.json or {})
    if data and ('language' in data or 'locale' in data):
        specific_language = data.get('language') if 'language' in data else data.get('locale')

    try:
        tag, match = _parse_voice_filters(data)
    except ValueError as e:
        return openai_error(str(e), status=400, error_type="invalid_request_error", code="invalid_request")

    return jsonify({"voices": get_voices(specific_language, tag, match)})


@app.route('/v1/voices/all', methods=['GET', 'POST'])
@app.route('/voices/all', methods=['GET', 'POST'])
@require_api_key
def list_all_voices():
    data = request.args if request.method == 'GET' else (request.json or {})
    try:
        tag, match = _parse_voice_filters(data)
    except ValueError as e:
        return openai_error(str(e), status=400, error_type="invalid_request_error", code="invalid_request")
    return jsonify({"voices": get_voices('all', tag, match)})


"""
Support for ElevenLabs and Azure AI Speech
    (currently in beta)
"""

# http://localhost:5050/elevenlabs/v1/text-to-speech
# http://localhost:5050/elevenlabs/v1/text-to-speech/en-US-AndrewNeural
@app.route('/elevenlabs/v1/text-to-speech/<voice_id>', methods=['POST'])
@require_api_key
def elevenlabs_tts(voice_id):
    if not EXPAND_API:
        return openai_error("Endpoint not allowed", status=500, error_type="server_error", code="endpoint_disabled")

    # Parse the incoming JSON payload
    try:
        payload = request.json
        if not payload or 'text' not in payload:
            return openai_error("Missing 'text' in request body", status=400, error_type="invalid_request_error", code="invalid_request")
    except Exception as e:
        return openai_error(f"Invalid JSON payload: {str(e)}", status=400, error_type="invalid_request_error", code="invalid_request")

    text = payload['text']

    if not REMOVE_FILTER:
        text = prepare_tts_input_with_context(text)

    voice = voice_id  # ElevenLabs uses the voice_id in the URL

    # Use default format for edge-tts
    response_format = 'mp3'

    # Generate speech using edge-tts
    try:
        output_file_path = generate_speech(text, voice, response_format, speed, pitch)
    except Exception as e:
        return openai_error(f"TTS generation failed: {str(e)}", status=500, error_type="server_error", code="tts_generation_failed")

    # Return the generated audio and remove the temp file (no disk leak)
    return _audio_response_from_temp(output_file_path, "audio/mpeg")


# tts.speech.microsoft.com/cognitiveservices/v1
# https://{region}.tts.speech.microsoft.com/cognitiveservices/v1
# http://localhost:5050/azure/cognitiveservices/v1
@app.route('/azure/cognitiveservices/v1', methods=['POST'])
@require_api_key
def azure_tts():
    if not EXPAND_API:
        return openai_error("Endpoint not allowed", status=500, error_type="server_error", code="endpoint_disabled")

    # Parse the SSML payload
    try:
        ssml_data = request.data.decode('utf-8')
        if not ssml_data:
            return openai_error("Missing SSML payload", status=400, error_type="invalid_request_error", code="invalid_request")

        # Extract the text, voice and optional <prosody> from SSML
        from xml.etree import ElementTree as ET
        ns = '{http://www.w3.org/2001/10/synthesis}'
        root = ET.fromstring(ssml_data)
        voice_el = root.find(f'.//{ns}voice')
        text = ''.join(voice_el.itertext()).strip()
        voice = voice_el.get('name')

        speed = DEFAULT_SPEED
        pitch = "+0Hz"
        prosody = root.find(f'.//{ns}prosody')
        if prosody is not None:
            if prosody.get('rate'):
                speed = _parse_azure_rate(prosody.get('rate'))
            if prosody.get('pitch'):
                pitch = _parse_azure_pitch(prosody.get('pitch'))
    except Exception as e:
        return openai_error(f"Invalid SSML payload: {str(e)}", status=400, error_type="invalid_request_error", code="invalid_request")

    # Use default format for edge-tts (speed/pitch come from <prosody> when present)
    response_format = 'mp3'

    if not REMOVE_FILTER:
        text = prepare_tts_input_with_context(text)

    # Generate speech using edge-tts
    try:
        output_file_path = generate_speech(text, voice, response_format, speed, pitch)
    except Exception as e:
        return openai_error(f"TTS generation failed: {str(e)}", status=500, error_type="server_error", code="tts_generation_failed")

    # Return the generated audio and remove the temp file (no disk leak)
    return _audio_response_from_temp(output_file_path, "audio/mpeg")


def _temp_cleanup_loop(interval):
    """Periodically drop stale temp audio files left behind by crashes."""
    while True:
        gevent.sleep(interval)
        try:
            cleanup_stale_temp_files()
        except Exception as exc:  # never let the sweeper die
            app.logger.warning(f"temp cleanup failed: {exc}")


def _startup_banner():
    cleanup_stale_temp_files()
    print(" Edge TTS (Free Azure TTS) Replacement for OpenAI's TTS API", flush=True)
    print(f" version: {APP_VERSION}", flush=True)
    print(" * Serving OpenAI Edge TTS", flush=True)
    print(f" * Server running on http://localhost:{PORT}", flush=True)
    print(f" * TTS Endpoint: http://localhost:{PORT}/v1/audio/speech", flush=True)
    if not is_ffmpeg_installed():
        print(" * ffmpeg: NOT installed -> only response_format=mp3 is available", flush=True)
    else:
        print(" * ffmpeg: installed -> mp3/opus/aac/flac/wav/pcm", flush=True)
    if API_KEY == DEFAULT_CONFIGS["API_KEY"]:
        print(" * WARNING: API_KEY is still the default 'your_api_key_here' — set API_KEY!", flush=True)
    print(" ", flush=True)


if __name__ == '__main__':
    _startup_banner()
    cleanup_interval = float(os.getenv('TTS_TEMP_CLEANUP_INTERVAL', '600'))
    if cleanup_interval > 0:
        gevent.spawn(_temp_cleanup_loop, cleanup_interval)
        print(f" * temp cleanup: every {cleanup_interval:.0f}s (files older than "
              f"{os.getenv('TTS_TEMP_MAX_AGE', '3600')}s)", flush=True)
    http_server = WSGIServer(('0.0.0.0', PORT), app)
    http_server.serve_forever()
