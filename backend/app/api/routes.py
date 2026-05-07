from typing import Optional
import os
import json
import hashlib
import re
from datetime import datetime, timezone

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile

from app.models.task import TaskData
from app.services import ai_service, teamwork_service, whisper_service
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()
_MESSAGE_DEDUPE_CACHE: dict[str, float] = {}
_ACK_ONLY_RE = re.compile(
    r"^(?:"
    r"(?:ok|okay|okk|okkk|d['’]?accord|oui|yes|merci|thanks|thank you|c['’]?est bon|cest bon|"
    r"parfait|super|top|bien reçu|bien recu|reçu|recu|vu|noté|note|validé|valide|"
    r"je check(?:e)?(?: plus tard)?|hello|salut)"
    r"(?:[\s,;:!?.-]+)?"
    r")+?$",
    re.IGNORECASE,
)
_LOW_SIGNAL_PHRASES_RE = re.compile(
    r"^(?:"
    r"(?:ok|okay|okk|okkk|oui|yes|merci|thanks|reçu|recu|vu|noté|note|validé|valide)"
    r"(?:[\s,;:!?.-]+)?"
    r"|(?:ok\s+tu\s+me\s+dis)"
    r"|(?:tu\s+me\s+tiens\s+au\s+courant)"
    r"|(?:tiens[\s-]?moi\s+au\s+courant)"
    r"|(?:je\s+m['’]en\s+occupe(?:\s+plus\s+tard)?(?:\s+ca\s+peut\s+tarder)?)"
    r"|(?:on\s+voit\s+ça\s+plus\s+tard)"
    r"|(?:d['’]accord\s+je\s+g[ée]re)"
    r")+$",
    re.IGNORECASE,
)
_THANKS_ONLY_RE = re.compile(
    r"^(?:"
    r"(?:merci(?:\s+beaucoup)?|mrc|thx|thanks|thank you|c['’]?est gentil|nickel)"
    r"(?:[\s,;:!?.-]+)?"
    r")+?$",
    re.IGNORECASE,
)
def _normalize_text_for_dedupe(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _build_message_fingerprint(sender_id: str, raw_text: str) -> str:
    normalized = _normalize_text_for_dedupe(raw_text)
    payload = f"{sender_id}::{normalized}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_duplicate_recent_message(sender_id: str, raw_text: str) -> bool:
    """
    Prevent duplicate task creation when the same sender sends
    the same content repeatedly in a short time window.
    """
    dedupe_window_seconds = int(os.getenv("MESSAGE_DEDUPE_WINDOW_SECONDS", "900"))
    now_ts = datetime.now(timezone.utc).timestamp()
    fingerprint = _build_message_fingerprint(sender_id, raw_text)
    previous_ts = _MESSAGE_DEDUPE_CACHE.get(fingerprint)
    _MESSAGE_DEDUPE_CACHE[fingerprint] = now_ts

    if previous_ts is None:
        return False
    return (now_ts - previous_ts) <= dedupe_window_seconds


def _is_non_action_message(raw_text: str) -> bool:
    """
    Ignore short acknowledgements/validation-only messages so they never become tasks.
    """
    normalized = _normalize_text_for_dedupe(raw_text)
    if not normalized:
        return True
    if len(normalized) <= 3:
        return True

    token_count = len(normalized.split())
    if _THANKS_ONLY_RE.fullmatch(normalized):
        return True
    # Keep tiny acknowledgment-questions non-action (e.g. "ok ?", "d'accord ?", "merci ?").
    if "?" in raw_text and token_count <= 3:
        question_stripped = _normalize_text_for_dedupe(
            re.sub(r"[?!.;,:\-]+", " ", normalized)
        )
        if (
            _ACK_ONLY_RE.fullmatch(question_stripped)
            or _THANKS_ONLY_RE.fullmatch(question_stripped)
            or _LOW_SIGNAL_PHRASES_RE.fullmatch(question_stripped)
        ):
            return True
    if token_count <= 6 and _ACK_ONLY_RE.fullmatch(normalized):
        return True
    if token_count <= 12 and _LOW_SIGNAL_PHRASES_RE.fullmatch(normalized):
        return True
    # Short status updates with no concrete action are often low-signal.
    if token_count <= 8 and any(
        phrase in normalized
        for phrase in (
            "plus tard",
            "au courant",
            "tu me dis",
            "je m'en occupe",
            "je m’en occupe",
        )
    ):
        return True

    return False


def _is_low_signal_extracted_task(raw_text: str, task: TaskData) -> bool:
    """
    Safety net: avoid creating Teamwork tasks when AI over-interprets vague input.
    """
    normalized = _normalize_text_for_dedupe(raw_text)
    token_count = len(normalized.split())
    if token_count > 12:
        return False

    if _is_non_action_message(raw_text):
        return True

    # If source message is very short but LLM generated multiple subtasks, treat as hallucinated.
    if token_count <= 8 and len(task.subtasks) > 0:
        return True

    # If both title and description barely overlap with source, likely over-inference.
    source_tokens = {tok for tok in re.findall(r"\w+", normalized) if len(tok) >= 3}
    generated_tokens = {
        tok
        for tok in re.findall(
            r"\w+",
            _normalize_text_for_dedupe(f"{task.title} {task.description} {task.client_request}"),
        )
        if len(tok) >= 3
    }
    if not source_tokens:
        return True
    overlap_ratio = len(source_tokens & generated_tokens) / len(source_tokens)
    return overlap_ratio < 0.35


def _filter_low_signal_tasks(raw_text: str, tasks: list[TaskData]) -> list[TaskData]:
    return [task for task in tasks if not _is_low_signal_extracted_task(raw_text, task)]


def _store_transcript(
    sender_id: str,
    sender_participant_jid: Optional[str],
    transcription_source: str,
    raw_text: str,
) -> None:
    """Persist transcriptions as JSONL so they can be reviewed later."""
    transcript_log_path = os.getenv("TRANSCRIPT_LOG_PATH", "logs/transcripts.jsonl")
    log_dir = os.path.dirname(transcript_log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sender_id": sender_id,
        "sender_participant_jid": sender_participant_jid,
        "transcription_source": transcription_source,
        "raw_text": raw_text,
    }
    with open(transcript_log_path, "a", encoding="utf-8") as transcript_file:
        transcript_file.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _store_task_output(
    sender_id: str,
    sender_participant_jid: Optional[str],
    transcription_source: str,
    raw_text: str,
    task: TaskData,
) -> None:
    """Persist extracted structured tasks as JSONL."""
    task_log_path = os.getenv("TASK_OUTPUT_LOG_PATH", "logs/tasks_output.jsonl")
    log_dir = os.path.dirname(task_log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sender_id": sender_id,
        "sender_participant_jid": sender_participant_jid,
        "transcription_source": transcription_source,
        "raw_text": raw_text,
        "task": task.model_dump(),
    }
    with open(task_log_path, "a", encoding="utf-8") as task_file:
        task_file.write(json.dumps(payload, ensure_ascii=False) + "\n")


@router.get("/teamwork-health")
async def teamwork_health() -> dict:
    try:
        return await teamwork_service.get_teamwork_health()
    except Exception as e:
        logger.error(f"Teamwork health check failed: {e}")
        raise HTTPException(status_code=502, detail=f"Teamwork health check failed: {e}")


@router.post("/process-message")
async def process_message(
    sender_id: str = Form(...),
    sender_participant_jid: Optional[str] = Form(None),
    message: Optional[str] = Form(None),
    audio_file: Optional[UploadFile] = File(None),
) -> dict:
    logger.info(
        f"Received message from {sender_id} | "
        f"text={'yes' if message else 'no'} | "
        f"audio={'yes' if audio_file else 'no'}"
    )

    text = message
    transcription_source = "text"

    if audio_file:
        try:
            audio_data = await audio_file.read()
            text = await whisper_service.transcribe_upload(
                audio_data, audio_file.filename or "audio.ogg"
            )
            transcription_source = "audio_whisper"
            logger.info(f"Transcribed audio for {sender_id}: {text[:100]}...")

            target_language = os.getenv("AUDIO_OUTPUT_LANGUAGE", "fr").strip().lower()
            if target_language in {"fr", "french", "francais", "français"}:
                try:
                    translated_text = await ai_service.translate_to_french(text)
                    if translated_text:
                        text = translated_text
                        transcription_source = "audio_whisper_translated_fr"
                        logger.info(f"French transcript for {sender_id}: {text[:100]}...")
                except Exception as e:
                    # Translation is a best-effort enhancement; do not fail the whole request.
                    logger.warning(
                        f"Audio translation failed (continuing with raw transcript): {e}"
                    )
        except Exception as e:
            logger.error(f"Whisper transcription failed: {e}")
            raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")

    if not text:
        raise HTTPException(status_code=400, detail="No message content provided")

    # Keep a plain-text copy for clients that need raw transcription output.
    raw_text = text
    _store_transcript(sender_id, sender_participant_jid, transcription_source, raw_text)
    logger.info(f"Stored transcript for {sender_id}: {raw_text}")
    if _is_non_action_message(raw_text):
        logger.info(
            "Acknowledgment/validation-only message detected. Skipping AI extraction and Teamwork task creation."
        )
        return {
            "status": "ignored_non_action",
            "sender_id": sender_id,
            "raw_text": raw_text,
            "transcription_source": transcription_source,
            "task": None,
            "teamwork_response": None,
        }

    if _is_duplicate_recent_message(sender_id, raw_text):
        logger.info(
            "Duplicate message detected. Skipping AI extraction and Teamwork task creation."
        )
        return {
            "status": "duplicate_ignored",
            "sender_id": sender_id,
            "raw_text": raw_text,
            "transcription_source": transcription_source,
            "task": None,
            "teamwork_response": None,
        }

    whisper_only = os.getenv("WHISPER_ONLY", "false").lower() == "true"
    fallback_to_transcript = (
        os.getenv("FALLBACK_TO_TRANSCRIPT_ON_AI_ERROR", "true").lower() == "true"
    )

    if whisper_only:
        logger.info("WHISPER_ONLY=true, returning raw text without AI extraction")
        return {
            "status": "ok_transcription_only",
            "sender_id": sender_id,
            "raw_text": raw_text,
            "transcription_source": transcription_source,
            "task": None,
            "teamwork_response": None,
        }

    try:
        extracted_tasks: list[TaskData] = await ai_service.extract_tasks(text)
        logger.info(f"Extracted {len(extracted_tasks)} task(s)")
        tasks = _filter_low_signal_tasks(raw_text, extracted_tasks)
        if not tasks:
            logger.info(
                "All extracted tasks were low-signal. Skipping Teamwork task creation."
            )
            return {
                "status": "ignored_low_signal_after_ai",
                "sender_id": sender_id,
                "raw_text": raw_text,
                "transcription_source": transcription_source,
                "task": None,
                "tasks": [],
                "teamwork_response": None,
                "teamwork_responses": [],
            }
        for task in tasks:
            _store_task_output(
                sender_id, sender_participant_jid, transcription_source, raw_text, task
            )
        logger.info(f"Stored {len(tasks)} structured task output(s) for {sender_id}")
    except ValueError as e:
        logger.error(f"Task extraction failed: {e}")
        detail = str(e)
        if "memory" in detail.lower() and (
            "ollama" in detail.lower() or "model requires" in detail.lower()
        ):
            raise HTTPException(
                status_code=503,
                detail=(
                    f"{detail} "
                    "Free RAM or use a smaller model (e.g. `ollama pull llama3.2:3b` "
                    "then set LLAMA_MODEL=llama3.2:3b or LLAMA_FALLBACK_MODEL=llama3.2:3b)."
                ),
            )
        if fallback_to_transcript:
            logger.warning(
                "FALLBACK_TO_TRANSCRIPT_ON_AI_ERROR=true, returning raw text after AI failure"
            )
            return {
                "status": "ok_transcription_fallback",
                "sender_id": sender_id,
                "raw_text": raw_text,
                "transcription_source": transcription_source,
                "task": None,
                "teamwork_response": None,
                "ai_error": detail,
            }
        raise HTTPException(status_code=422, detail=f"Task extraction failed: {e}")

    has_teamwork_config = bool(os.getenv("TEAMWORK_DOMAIN")) and bool(
        os.getenv("TEAMWORK_PROJECT_ID")
    )
    skip_teamwork = os.getenv("SKIP_TEAMWORK", "false").lower() == "true" or not has_teamwork_config
    if skip_teamwork:
        logger.info("SKIP_TEAMWORK=true, returning extracted task(s) without Teamwork API call")
        return {
            "status": "ok_ai_only",
            "sender_id": sender_id,
            "raw_text": raw_text,
            "transcription_source": transcription_source,
            "task": tasks[0].model_dump(),
            "tasks": [task.model_dump() for task in tasks],
            "teamwork_response": None,
            "teamwork_responses": [],
        }

    try:
        teamwork_responses: list[dict] = []
        for task in tasks:
            teamwork_responses.append(await teamwork_service.create_task(task))
    except Exception as e:
        logger.error(f"Teamwork API failed: {e}")
        raise HTTPException(status_code=502, detail=f"Teamwork API failed: {e}")

    return {
        "status": "ok",
        "sender_id": sender_id,
        "raw_text": raw_text,
        "transcription_source": transcription_source,
        "task": tasks[0].model_dump(),
        "tasks": [task.model_dump() for task in tasks],
        "teamwork_response": teamwork_responses[0],
        "teamwork_responses": teamwork_responses,
    }


@router.get("/process-message")
async def process_message_get_hint() -> dict:
    return {
        "status": "method_not_supported",
        "detail": "Use POST /process-message with form data (sender_id, message and/or audio_file).",
    }


@router.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)
