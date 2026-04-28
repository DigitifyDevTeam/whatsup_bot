import os
import tempfile
import shutil
import subprocess

import whisper  # type: ignore[import-untyped]

from app.utils.logger import get_logger

logger = get_logger(__name__)

_model = None
_ffmpeg_bootstrapped = False


def _ensure_ffmpeg_available() -> None:
    """
    Ensure Whisper can invoke `ffmpeg` on all platforms.

    On Windows, `openai-whisper` shells out to the `ffmpeg` binary name.
    If it's not installed system-wide, we try to bootstrap it from
    `imageio-ffmpeg` and expose it on PATH as `ffmpeg.exe`.
    """

    global _ffmpeg_bootstrapped
    if _ffmpeg_bootstrapped:
        return

    if shutil.which("ffmpeg"):
        _ffmpeg_bootstrapped = True
        return

    try:
        import imageio_ffmpeg  # type: ignore[import-untyped]
    except Exception as exc:
        raise RuntimeError(
            "ffmpeg is required for Whisper audio decoding but was not found. "
            "Install ffmpeg system-wide or add `imageio-ffmpeg` to dependencies."
        ) from exc

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    if not ffmpeg_exe or not os.path.exists(ffmpeg_exe):
        raise RuntimeError("Unable to resolve ffmpeg binary from imageio-ffmpeg.")

    ffmpeg_dir = os.path.dirname(ffmpeg_exe)
    candidate_path = ffmpeg_exe

    # Some imageio builds store ffmpeg under a versioned filename.
    # Whisper invokes plain `ffmpeg`, so expose a stable alias if needed.
    if os.path.basename(ffmpeg_exe).lower() != "ffmpeg.exe":
        alias_dir = os.path.join(tempfile.gettempdir(), "whisper_ffmpeg_bin")
        os.makedirs(alias_dir, exist_ok=True)
        alias_path = os.path.join(alias_dir, "ffmpeg.exe")
        if not os.path.exists(alias_path):
            shutil.copyfile(ffmpeg_exe, alias_path)
        candidate_path = alias_path
        ffmpeg_dir = alias_dir

    os.environ["PATH"] = f"{ffmpeg_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            f"ffmpeg bootstrap failed. Candidate binary path: {candidate_path}"
        )

    logger.info(f"ffmpeg is available at: {shutil.which('ffmpeg')}")
    _ffmpeg_bootstrapped = True


def _get_model() -> whisper.Whisper:
    global _model
    if _model is None:
        model_size = os.getenv("WHISPER_MODEL_SIZE", "base")
        logger.info(f"Loading Whisper model: {model_size}")
        _model = whisper.load_model(model_size)
        logger.info("Whisper model loaded")
    return _model


async def transcribe(file_path: str) -> str:
    """Transcribe an audio file to text using Whisper."""
    logger.info(f"Transcribing audio: {file_path}")

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Audio file not found: {file_path}")

    _ensure_ffmpeg_available()
    model = _get_model()

    processed_path = _preprocess_audio_for_whisper(file_path)
    try:
        transcribe_options = _build_transcribe_options()
        result = model.transcribe(processed_path, **transcribe_options)
        text = result["text"].strip()
    finally:
        if processed_path != file_path:
            try:
                os.remove(processed_path)
            except OSError:
                pass

    logger.info(f"Transcription complete: {len(text)} characters")
    return text


async def transcribe_upload(upload_data: bytes, filename: str) -> str:
    """Transcribe uploaded audio bytes."""
    tmp_dir = tempfile.mkdtemp(prefix="whisper_")
    tmp_path = os.path.join(tmp_dir, filename)

    try:
        with open(tmp_path, "wb") as f:
            f.write(upload_data)
        return await transcribe(tmp_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _build_transcribe_options() -> dict:
    """
    Build Whisper decoding options tuned for fast speech.
    Defaults improve robustness for rapid voice notes while remaining configurable.
    """
    language = (os.getenv("WHISPER_LANGUAGE", "fr") or "").strip()
    beam_size = _int_env("WHISPER_BEAM_SIZE", 5)
    best_of = _int_env("WHISPER_BEST_OF", 5)
    temperature = _float_env("WHISPER_TEMPERATURE", 0.0)

    return {
        "language": language or None,
        "task": "transcribe",
        "temperature": temperature,
        "beam_size": beam_size,
        "best_of": best_of,
        # Better for fast speech: don't over-anchor to previous segment text.
        "condition_on_previous_text": False,
        # Keep defaults explicit for readability/tuning.
        "compression_ratio_threshold": 2.4,
        "logprob_threshold": -1.0,
        "no_speech_threshold": 0.6,
    }


def _preprocess_audio_for_whisper(file_path: str) -> str:
    """
    Normalize audio before Whisper:
    - mono 16k PCM WAV
    - light denoise/normalize chain for clearer fast speech
    """
    preprocess_enabled = (os.getenv("WHISPER_PREPROCESS_AUDIO", "true").lower() == "true")
    if not preprocess_enabled:
        return file_path

    base_name = os.path.splitext(os.path.basename(file_path))[0]
    processed_path = os.path.join(tempfile.gettempdir(), f"{base_name}_whisper_ready.wav")

    ffmpeg_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        file_path,
        "-ac",
        "1",
        "-ar",
        "16000",
        "-af",
        "highpass=f=80,lowpass=f=7000,dynaudnorm",
        "-c:a",
        "pcm_s16le",
        processed_path,
    ]

    try:
        subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True)
        return processed_path
    except subprocess.CalledProcessError as exc:
        logger.warning(
            f"Audio preprocessing failed, using original file. stderr: {(exc.stderr or '')[:400]}"
        )
        return file_path


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name, str(default)) or "").strip()
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"Invalid {name}={raw!r}, using default {default}")
        return default


def _float_env(name: str, default: float) -> float:
    raw = (os.getenv(name, str(default)) or "").strip()
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"Invalid {name}={raw!r}, using default {default}")
        return default
