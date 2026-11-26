# utils.py

from flask import request, jsonify
from functools import wraps
import hmac
import os
import re
from dotenv import load_dotenv

from config import DEFAULT_CONFIGS

load_dotenv()

def getenv_bool(name: str, default: bool = False) -> bool:
    # The default parameter for getenv_bool is used if the config default itself needs a fallback,
    # or if the call site specifically wants to override the global default.
    # For typical usage, the config default (passed at call site) is preferred.
    return os.getenv(name, str(default)).lower() in ("yes", "y", "true", "1", "t")

API_KEY = os.getenv('API_KEY', DEFAULT_CONFIGS["API_KEY"])
REQUIRE_API_KEY = getenv_bool('REQUIRE_API_KEY', DEFAULT_CONFIGS["REQUIRE_API_KEY"])
DETAILED_ERROR_LOGGING = getenv_bool('DETAILED_ERROR_LOGGING', DEFAULT_CONFIGS["DETAILED_ERROR_LOGGING"])

PITCH_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)(?:hz)?", re.IGNORECASE)


def normalize_pitch(pitch):
    """Normalise a pitch offset to edge-tts' '+NHz' form.

    Accepts None (0 Hz), a number of Hz, or a string like '+10Hz' / '-20Hz'.
    Raises ValueError when outside -100..+100 Hz.
    """
    if pitch is None:
        return "+0Hz"
    if isinstance(pitch, bool):
        raise ValueError("'pitch' must be a number or a string like '+10Hz'")
    if isinstance(pitch, (int, float)):
        hz = float(pitch)
    else:
        text = str(pitch).strip()
        match = PITCH_RE.fullmatch(text)
        if not match:
            raise ValueError("'pitch' must be a number or a string like '+10Hz'")
        hz = float(match.group(1))
    if not -100 <= hz <= 100:
        raise ValueError("'pitch' must be between -100 and +100 Hz")
    return f"{hz:+.0f}Hz"


def filter_voices_by_tag(voices, tags, match="any"):
    """Filter voice entries by VoiceTag names (content categories + personalities).

    ``tags`` may be a comma separated string or an iterable, matched
    case-insensitively against both ``tags.content_categories`` and
    ``tags.personalities``.  ``match`` is ``"any"`` (default) or ``"all"``.
    """
    if tags is None:
        wanted = set()
    elif isinstance(tags, str):
        wanted = {tag.strip().lower() for tag in tags.split(',') if tag.strip()}
    else:
        wanted = {str(tag).strip().lower() for tag in tags if str(tag).strip()}

    if not wanted:
        return list(voices)

    def voice_tags(voice):
        info = voice.get('tags') or {}
        return {
            str(tag).strip().lower()
            for key in ('content_categories', 'personalities')
            for tag in (info.get(key) or [])
        }

    if match == 'all':
        return [voice for voice in voices if wanted <= voice_tags(voice)]
    return [voice for voice in voices if wanted & voice_tags(voice)]


def openai_error(message, status=400, *, error_type="invalid_request_error", code=None, headers=None):
    """Return an OpenAI-compatible error envelope.

    {"error": {"message": ..., "type": ..., "param": null, "code": ...}}
    """
    return jsonify({
        "error": {
            "message": message,
            "type": error_type,
            "param": None,
            "code": code,
        }
    }), status, headers or {}


def require_api_key(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not REQUIRE_API_KEY:
            return f(*args, **kwargs)

        # OpenAI uses "Authorization: Bearer <key>"; Azure-style "api-key" is also accepted.
        token = None
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header[len('Bearer '):].strip()
        elif request.headers.get('api-key'):
            token = request.headers.get('api-key').strip()

        if not token:
            return openai_error(
                "Missing or invalid API key",
                status=401,
                error_type="authentication_error",
                code="invalid_api_key",
            )
        if not hmac.compare_digest(token, API_KEY):
            return openai_error(
                "Invalid API key",
                status=401,
                error_type="authentication_error",
                code="invalid_api_key",
            )
        return f(*args, **kwargs)
    return decorated_function

# Mapping of audio format to MIME type
AUDIO_FORMAT_MIME_TYPES = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/L16"
}
