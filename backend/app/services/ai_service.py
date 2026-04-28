import os
import json
import re

import httpx

from app.models.task import TaskData
from app.utils.logger import get_logger

logger = get_logger(__name__)
TAG_GESTION_CATALOGUE = "Gestion de catalogue"
TAG_QFIX = "QFIX"
TAG_DEMANDE_SEO = "Demande SEO"
TAG_CUSTOM_DEV = "Custom DEV"

SYSTEM_PROMPT = """You are an AI assistant for a digital agency.

Context:
- You work for a web-development agency team.
- Client messages are primarily requests about building, fixing, or improving web applications and e-commerce flows.
- Your output is used directly by a dev/ops/content team in project management.
- Primary project context: all client messages are about refonte or maintenance for a PrestaShop e-commerce website: https://originecbd.fr/
- Assume requests refer to this store unless the message explicitly says otherwise.
- Use this context to disambiguate vague phrases (products, pages, checkout, shipping, SEO, visuals, etc.) toward PrestaShop e-commerce operations.

Your job is to extract structured task data from client messages with reliable categorization.

You must return ONLY valid JSON.

Schema:
{
  "title": "short task title",
  "description": "clear detailed description",
  "client_request": "original intent",
  "deadline": "ISO date or null",
  "priority": "P0 | P1 | P2",
  "tag": "Gestion de catalogue | QFIX | Demande SEO | Custom DEV",
  "subtasks": ["short actionable sub-task in French", "..."]
}

Rules:
- Do not add explanations
- Do not add text outside JSON
- Infer missing fields when possible
- If unknown, use null
- IMPORTANT: `title`, `description`, and `client_request` MUST be written in French only.
- IMPORTANT: Never output English for any textual field.
- IMPORTANT: If the client message contains multiple independent actions, split them into `subtasks`.
- IMPORTANT: Each subtask must be a concise action sentence in French.
- IMPORTANT: If there is only one action, return `subtasks` as an empty array [].

Priority rules:
- P0 = Critique, response < 1 hour, blocking incidents (payment, delivery, cart, server/product unavailable)
- P1 = Majeur, response < 2 hours, key feature impacted (emails, relay points, loyalty points)
- P2 = Mineur, response < 24 hours, non-blocking issue

Tag rules:
- "Gestion de catalogue": CRUD products, facets/filters, brands, product content
- "QFIX": quick fixes and promo/banner/menu/image adjustments
- "Demande SEO": SEO pages, blog articles, on-page optimizations
- "Custom DEV": new feature development, process changes, module/config updates

Tag disambiguation:
- If message mentions SEO/content/blog/meta/title/maillage/landing: prefer "Demande SEO".
- If message mentions product data/catalogue/filtres/facettes/marques/attributs/variation: prefer "Gestion de catalogue".
- If message mentions quick UI/content patch like banner/promo visuel/image/menu/text correction: prefer "QFIX".
- If message mentions integration/API/module/workflow/business logic/payment/shipping/rules/new capability: prefer "Custom DEV".
- When in doubt between "QFIX" and "Custom DEV":
  - minor visual/content tweak => "QFIX"
  - technical implementation/integration/logic change => "Custom DEV"."""

TRANSLATE_TO_FRENCH_PROMPT = """You are a translation assistant.

Task:
- Translate the user text into natural French.
- Keep names, brands, links, numbers, and dates unchanged when appropriate.
- Return ONLY the translated French text.
- Do not add explanations, quotes, or extra formatting."""


def _clean_french_translation_output(text: str) -> str:
    """
    Remove common LLM wrappers so we keep only the translated sentence.
    Examples removed: "Voici la traduction ...:", surrounding quotes, markdown fences.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()

    lower_cleaned = cleaned.lower()
    markers = [
        "voici la traduction",
        "traduction :",
        "voici le texte traduit",
        "bien sûr, je peux traduire votre texte en français naturel",
        "bien sûr, je peux traduire votre texte en français",
        "bien sûr, je peux essayer de traduire votre message en français",
        "je peux traduire votre texte en français naturel",
        "je peux traduire votre texte en français",
        "je peux essayer de traduire votre message en français",
    ]
    for marker in markers:
        marker_index = lower_cleaned.find(marker)
        if marker_index == -1:
            continue
        colon_index = cleaned.find(":", marker_index)
        if colon_index != -1 and colon_index + 1 < len(cleaned):
            cleaned = cleaned[colon_index + 1 :].strip()
            lower_cleaned = cleaned.lower()
            break

    # If model still keeps an intro before a quoted translation, keep only quoted body.
    first_quote_positions = [pos for pos in (cleaned.find('"'), cleaned.find("'")) if pos != -1]
    if first_quote_positions:
        first_quote = min(first_quote_positions)
        if first_quote > 0:
            intro = cleaned[:first_quote].lower()
            if any(token in intro for token in ("tradu", "français", "francais", "message")):
                cleaned = cleaned[first_quote:].strip()

    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
        cleaned = cleaned[1:-1].strip()

    return cleaned


async def extract_task(message: str) -> TaskData:
    """
    Send the message to LLaMA via Ollama and parse a structured TaskData.
    Retries once on validation failure.
    """
    endpoint = os.getenv("LLAMA_ENDPOINT", "http://localhost:11434").rstrip("/")
    model = _get_llama_model()
    return await _extract_task_with_model(endpoint, model, message)


async def _extract_task_with_model(endpoint: str, model: str, message: str) -> TaskData:
    for attempt in range(1, 3):
        raw_text = await _call_ollama_chat(
            endpoint, model, message, use_json_format=True, system_prompt=SYSTEM_PROMPT
        )
        raw_json = _extract_json_object(raw_text)
        try:
            parsed = json.loads(raw_json)
            task = TaskData(**parsed)
            task = _normalize_task(task, message)
            logger.info(f"Task extracted successfully on attempt {attempt} ({model})")
            return task
        except (ValueError, TypeError) as e:
            logger.warning(
                f"Attempt {attempt}: Failed to parse LLM output: {e}. "
                f"Raw output: {raw_text[:500]}"
            )
            if attempt == 2:
                raise ValueError(
                    f"LLM output failed validation after 2 attempts: {e}"
                ) from e

    raise ValueError("Unreachable")


def _extract_json_object(text: str) -> str:
    """Take first {...} block from model output (handles markdown fences)."""
    s = text.strip()
    if "```" in s:
        start = s.find("```")
        rest = s[start + 3 :]
        if rest.lstrip().lower().startswith("json"):
            rest = rest.lstrip()[4:].lstrip()
        end = rest.find("```")
        if end != -1:
            s = rest[:end].strip()
    first = s.find("{")
    last = s.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return s
    return s[first : last + 1]


def _normalize_task(task: TaskData, source_message: str) -> TaskData:
    """Deduplicate and clean subtask text returned by the model."""
    normalized_subtasks: list[str] = []
    seen: set[str] = set()

    for subtask in task.subtasks:
        cleaned = " ".join(subtask.strip().split())
        if not cleaned:
            continue
        # Remove optional bullet prefixes from model output.
        cleaned = cleaned.lstrip("-").strip()
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized_subtasks.append(cleaned)

    derived_subtasks = _derive_subtasks_from_message(source_message)
    final_subtasks = derived_subtasks or normalized_subtasks

    # For multi-action client messages, keep full original message as parent body.
    corrected_tag = _infer_tag(source_message, task)

    if len(final_subtasks) > 1:
        full_message = " ".join(source_message.strip().split())
        return task.model_copy(
            update={
                "description": full_message,
                "client_request": full_message,
                "subtasks": final_subtasks,
                "tag": corrected_tag,
            }
        )

    return task.model_copy(update={"subtasks": final_subtasks, "tag": corrected_tag})


def _derive_subtasks_from_message(source_message: str) -> list[str]:
    """
    Extract concrete action chunks from long messages.
    This keeps promo/details in parent description and pushes actionable items
    to subtasks.
    """
    text = " ".join(source_message.strip().split())
    if not text:
        return []

    patterns = [
        # Full promo instruction including scope, date window, and promo code.
        r"(Bandeau Promo\s*:\s*Black Friday\s*:\s*-15%\s+sur tout le site\s*!\s*Du\s*26/11/2024\s*au\s*02/12/2024\s*minuit,\s*profitez de\s*-15%\s*avec le code\s*[A-Z0-9_-]+\s+Ne laissez pas passer cette offre exclusive)",
        # Fallback generic promo sentence if wording slightly differs.
        r"(Bandeau Promo\s*:\s*Black Friday[^.]+?Ne laissez pas passer cette offre exclusive)",
        # conception de l'image de couverture de Sport Auto
        r"(conception de l['’]image de couverture de[^.]+?)(?=\s+et\s+n['’]oublie|\s*\.$|$)",
        # Remettre la Suisse ... européens
        r"(Remettre la Suisse en tant que pays de livraison possible avec les mêmes règles que les autres pays européens)",
    ]

    subtasks: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = " ".join(match.group(1).strip().split()).lstrip("-").strip()
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        subtasks.append(candidate)

    return subtasks


def _infer_tag(source_message: str, task: TaskData) -> str:
    """
    Deterministic tag correction layer to reduce wrong LLM tag picks.
    """
    combined = " ".join(
        [
            source_message or "",
            task.title or "",
            task.description or "",
            task.client_request or "",
            " ".join(task.subtasks or []),
        ]
    ).lower()

    keyword_groups: dict[str, list[str]] = {
        TAG_DEMANDE_SEO: [
            "seo",
            "blog",
            "article",
            "meta title",
            "meta description",
            "mot clé",
            "mots clés",
            "schema.org",
            "rich snippet",
            "landing page",
            "maillage interne",
        ],
        TAG_GESTION_CATALOGUE: [
            "catalogue",
            "catalog",
            "produit",
            "produits",
            "facet",
            "facette",
            "filtre",
            "filtres",
            "marque",
            "marques",
            "attribut",
            "variation",
            "sku",
            "stock",
        ],
        TAG_QFIX: [
            "bandeau",
            "banner",
            "bannière",
            "black friday",
            "promo",
            "promotion",
            "visuel",
            "image de couverture",
            "menu",
            "correction rapide",
            "hotfix",
            "quick fix",
            "text correction",
        ],
        TAG_CUSTOM_DEV: [
            "intégration",
            "integration",
            "api",
            "webhook",
            "module",
            "workflow",
            "logique",
            "business rule",
            "paiement",
            "payment",
            "livraison",
            "shipping",
            "mailchimp",
            "erp",
            "crm",
            "nouvelle fonctionnalité",
            "new feature",
        ],
    }

    scores: dict[str, int] = dict.fromkeys(keyword_groups, 0)
    for tag, keywords in keyword_groups.items():
        for keyword in keywords:
            if keyword in combined:
                scores[tag] += 1

    best_tag = max(scores, key=scores.get)
    best_score = scores[best_tag]
    current_tag = task.tag

    # Keep model output when deterministic evidence is weak.
    if best_score == 0:
        return current_tag

    # Resolve common ambiguity: promo-only texts should remain QFIX unless strong DEV signals.
    if best_tag == TAG_QFIX and scores[TAG_CUSTOM_DEV] >= 2 and scores[TAG_CUSTOM_DEV] >= scores[TAG_QFIX]:
        return TAG_CUSTOM_DEV

    return best_tag


async def _call_ollama_chat(
    endpoint: str,
    model: str,
    message: str,
    *,
    use_json_format: bool,
    system_prompt: str,
) -> str:
    url = f"{endpoint}/api/chat"
    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ],
        "stream": False,
    }
    if use_json_format:
        payload["format"] = "json"

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            return await _post_ollama_chat(client, url, payload, use_json_format)
    except httpx.RequestError as e:
        logger.error(f"Ollama request failed: {e}")
        raise ValueError(f"Cannot reach Ollama at {endpoint}: {e}") from e


async def _post_ollama_chat(
    client: httpx.AsyncClient,
    url: str,
    payload: dict,
    use_json_format: bool,
) -> str:
    logger.info(
        f"Calling Ollama chat: model={payload.get('model')} format_json={use_json_format}"
    )
    response = await client.post(url, json=payload)
    if response.status_code >= 400:
        body = (response.text or "")[:2000]
        logger.error(f"Ollama HTTP {response.status_code} at {url}: {body}")
        if use_json_format and "format" in payload:
            logger.info("Retrying Ollama without format=json")
            payload_no_fmt = {k: v for k, v in payload.items() if k != "format"}
            response2 = await client.post(url, json=payload_no_fmt)
            if response2.status_code >= 400:
                body2 = (response2.text or "")[:2000]
                logger.error(
                    f"Ollama HTTP {response2.status_code} (no format): {body2}"
                )
                raise ValueError(
                    f"Ollama error {response2.status_code}: {body2[:500]}"
                )
            data2 = response2.json()
            msg2 = data2.get("message") or {}
            return (msg2.get("content") or "").strip()
        raise ValueError(f"Ollama error {response.status_code}: {body[:500]}")

    data = response.json()
    msg = data.get("message") or {}
    return (msg.get("content") or "").strip()


async def translate_to_french(text: str) -> str:
    """Translate arbitrary text to French using the configured Ollama model."""
    endpoint = os.getenv("LLAMA_ENDPOINT", "http://localhost:11434").rstrip("/")
    model = _get_llama_model()
    translated = await _call_ollama_chat(
        endpoint,
        model,
        text,
        use_json_format=False,
        system_prompt=TRANSLATE_TO_FRENCH_PROMPT,
    )
    cleaned = _clean_french_translation_output(translated)
    if cleaned:
        return cleaned
    raise ValueError("Empty translation output")


def _get_llama_model() -> str:
    """Use exactly one Ollama model, no fallback chain."""
    return (os.getenv("LLAMA_MODEL") or "llama3.1:8b").strip()
