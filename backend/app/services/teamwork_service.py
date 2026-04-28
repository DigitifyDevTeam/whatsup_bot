import os
from base64 import b64encode
from datetime import datetime
import re

import httpx

from app.models.task import TaskData
from app.utils.logger import get_logger
from app.utils.retry import async_retry

logger = get_logger(__name__)

PRIORITY_MAP = {
    "P0": "high",
    "P1": "medium",
    "P2": "low",
}

TRANSLATION_WRAPPER_PATTERNS: tuple[str, ...] = (
    r"^\s*voici la traduction\s*:?\s*",
    r"^\s*traduction\s*:?\s*",
    r"^\s*voici le texte traduit\s*:?\s*",
    r"^\s*bien\s*s[ûu]r,\s*je peux traduire votre texte en fran[cç]ais(?: naturel)?\s*:?\s*",
    r"^\s*bien\s*s[ûu]r,\s*je peux essayer de traduire votre message en fran[cç]ais\s*:?\s*",
    r"^\s*je peux traduire votre texte en fran[cç]ais(?: naturel)?\s*:?\s*",
    r"^\s*je peux essayer de traduire votre message en fran[cç]ais\s*:?\s*",
)


def _strip_translation_wrapper(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return cleaned

    for pattern in TRANSLATION_WRAPPER_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)

    # Remove surrounding quotes after wrapper removal.
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
        cleaned = cleaned[1:-1].strip()

    return " ".join(cleaned.split())


def _sanitize_task_for_teamwork(task: TaskData) -> TaskData:
    return task.model_copy(
        update={
            "title": _strip_translation_wrapper(task.title),
            "description": _strip_translation_wrapper(task.description),
            "client_request": _strip_translation_wrapper(task.client_request),
            "subtasks": [
                cleaned
                for cleaned in (
                    _strip_translation_wrapper(subtask) for subtask in (task.subtasks or [])
                )
                if cleaned
            ],
        }
    )


def _assert_create_only_mode() -> None:
    """
    Safety guard: this integration is create-only.
    Any configuration that attempts to allow update/delete is rejected.
    """
    allowed_actions = os.getenv("TEAMWORK_ALLOWED_ACTIONS", "create").strip().lower()
    normalized_actions = {action.strip() for action in allowed_actions.split(",") if action.strip()}
    if normalized_actions != {"create"}:
        raise ValueError(
            "Unsafe Teamwork action configuration. Only create is allowed "
            "(set TEAMWORK_ALLOWED_ACTIONS=create)."
        )


def _get_auth_header() -> str:
    api_key = os.getenv("TEAMWORK_API_KEY", "")
    token = b64encode(f"{api_key}:".encode()).decode()
    return f"Basic {token}"


def _build_headers() -> dict[str, str]:
    return {
        "Authorization": _get_auth_header(),
        "Content-Type": "application/json",
    }


def _build_monthly_tasklist_name(now: datetime | None = None) -> str:
    current = now or datetime.now()
    month_names_fr = {
        1: "Janvier",
        2: "Fevrier",
        3: "Mars",
        4: "Avril",
        5: "Mai",
        6: "Juin",
        7: "Juillet",
        8: "Aout",
        9: "Septembre",
        10: "Octobre",
        11: "Novembre",
        12: "Décembre",
    }
    month_label = month_names_fr[current.month]
    return f"Développement sur mesure pour le mois de {month_label} {current.year}"


def _safe_int(value: int | str | None) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


async def _create_tasklist(
    client: httpx.AsyncClient, list_url: str, project_id: str, tasklist_name: str
) -> int:
    create_payload = {
        "tasklist": {
            "name": tasklist_name,
            "projectId": int(project_id),
        }
    }
    create_response = await client.post(list_url, json=create_payload, headers=_build_headers())
    if create_response.status_code >= 400:
        detail = _extract_teamwork_error_detail(create_response)
        raise httpx.HTTPStatusError(
            f"Teamwork tasklist creation error {create_response.status_code}: {detail}",
            request=create_response.request,
            response=create_response,
        )

    created_tasklist = create_response.json().get("tasklist", {})
    created_tasklist_id = _safe_int(created_tasklist.get("id"))
    if created_tasklist_id is None:
        raise ValueError("Teamwork created a tasklist but no tasklist id was returned.")

    logger.info(f"Created monthly Teamwork tasklist '{tasklist_name}' (id={created_tasklist_id})")
    return created_tasklist_id


async def _resolve_tasklist_id(client: httpx.AsyncClient, domain: str, project_id: str) -> int:
    configured_tasklist_id = os.getenv("TEAMWORK_TASKLIST_ID", "").strip()
    if configured_tasklist_id:
        return int(configured_tasklist_id)

    configured_tasklist_name = os.getenv("TEAMWORK_TASKLIST_NAME", "").strip().lower()
    list_url = f"https://{domain}/projects/api/v3/tasklists.json"
    response = await client.get(
        list_url,
        params={"projectIds": project_id},
        headers=_build_headers(),
    )
    response.raise_for_status()
    payload = response.json()
    tasklists = payload.get("tasklists", [])
    if configured_tasklist_name:
        for tasklist in tasklists:
            tasklist_name = str(tasklist.get("name", "")).strip().lower()
            if tasklist_name == configured_tasklist_name:
                return int(tasklist["id"])
        raise ValueError(
            f"Configured task list name '{configured_tasklist_name}' was not found "
            f"in Teamwork project {project_id}."
        )

    monthly_tasklist_name = _build_monthly_tasklist_name().strip()
    for tasklist in tasklists:
        tasklist_name = str(tasklist.get("name", "")).strip()
        if tasklist_name.lower() == monthly_tasklist_name.lower():
            return int(tasklist["id"])

    return await _create_tasklist(client, list_url, project_id, monthly_tasklist_name)


def _extract_teamwork_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]

    if isinstance(payload, dict):
        if payload.get("MESSAGE"):
            return str(payload["MESSAGE"])
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first_error = errors[0]
            if isinstance(first_error, dict):
                detail = first_error.get("detail") or first_error.get("title")
                if detail:
                    return str(detail)

    return response.text[:500]


@async_retry(max_retries=3, base_delay=2.0, exceptions=(httpx.HTTPError,))
async def create_task(task: TaskData) -> dict:
    """Create a task in Teamwork Projects."""
    _assert_create_only_mode()
    task = _sanitize_task_for_teamwork(task)

    domain = os.getenv("TEAMWORK_DOMAIN", "")
    project_id = os.getenv("TEAMWORK_PROJECT_ID", "")

    if not domain or not project_id:
        raise ValueError("TEAMWORK_DOMAIN and TEAMWORK_PROJECT_ID must be set")

    async with httpx.AsyncClient(timeout=30.0) as client:
        tasklist_id = await _resolve_tasklist_id(client, domain, project_id)
        url = f"https://{domain}/projects/api/v3/tasklists/{tasklist_id}/tasks.json"

        task_payload: dict = {
            "task": {
                "name": f"[{task.tag}] {task.title}",
                "description": (
                    f"[Tag: {task.tag}] [Priority: {task.priority}]\n\n{task.description}"
                ),
                "priority": PRIORITY_MAP.get(task.priority, "medium"),
            }
        }

        if task.deadline:
            task_payload["task"]["dueDate"] = task.deadline

        logger.info(f"Creating Teamwork task: {task.title} in tasklist {tasklist_id}")
        response = await client.post(url, json=task_payload, headers=_build_headers())
        if response.status_code >= 400:
            detail = _extract_teamwork_error_detail(response)
            raise httpx.HTTPStatusError(
                f"Teamwork error {response.status_code}: {detail}",
                request=response.request,
                response=response,
            )
        result = response.json()
        logger.info(f"Teamwork task created: {result}")

        parent_task_id = _extract_task_id(result)
        created_subtasks: list[dict] = []
        if parent_task_id and task.subtasks:
            for subtask in task.subtasks:
                subtask_payload = {
                    "task": {
                        "name": subtask,
                        "priority": PRIORITY_MAP.get(task.priority, "medium"),
                    }
                }
                # Teamwork subtask creation must target the parent task subtask route.
                subtask_url = (
                    f"https://{domain}/projects/api/v3/tasks/{parent_task_id}/subtasks.json"
                )
                subtask_response = await client.post(
                    subtask_url, json=subtask_payload, headers=_build_headers()
                )
                if subtask_response.status_code >= 400:
                    detail = _extract_teamwork_error_detail(subtask_response)
                    raise httpx.HTTPStatusError(
                        f"Teamwork subtask error {subtask_response.status_code}: {detail}",
                        request=subtask_response.request,
                        response=subtask_response,
                    )
                created_subtasks.append(subtask_response.json())

        return {
            "parent_task": result,
            "parent_task_id": parent_task_id,
            "subtasks_created_count": len(created_subtasks),
            "subtasks": created_subtasks,
        }


async def get_teamwork_health() -> dict:
    """
    Read-only Teamwork diagnostics:
    - validates config
    - checks project access
    - checks tasklist availability
    - inspects per-user add-tasks permissions from project people endpoint
    """
    domain = os.getenv("TEAMWORK_DOMAIN", "").strip()
    project_id = os.getenv("TEAMWORK_PROJECT_ID", "").strip()
    api_key = os.getenv("TEAMWORK_API_KEY", "").strip()

    config_ok = bool(domain and project_id and api_key)
    if not config_ok:
        return {
            "status": "error",
            "config_ok": False,
            "domain_set": bool(domain),
            "project_id_set": bool(project_id),
            "api_key_set": bool(api_key),
            "error": "Missing Teamwork configuration values",
        }

    project_url = f"https://{domain}/projects/{project_id}.json"
    tasklists_url = f"https://{domain}/projects/api/v3/tasklists.json"
    people_url = f"https://{domain}/projects/{project_id}/people.json"

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1) Project reachability
        project_resp = await client.get(project_url, headers=_build_headers())
        project_ok = project_resp.status_code == 200
        project_name = None
        if project_ok:
            project_name = project_resp.json().get("project", {}).get("name")

        # 2) Tasklists availability
        tasklists_resp = await client.get(
            tasklists_url,
            params={"projectIds": project_id},
            headers=_build_headers(),
        )
        tasklists_ok = tasklists_resp.status_code == 200
        tasklists: list[dict] = []
        if tasklists_ok:
            tasklists = tasklists_resp.json().get("tasklists", [])

        # 3) People permissions
        people_resp = await client.get(people_url, headers=_build_headers())
        people_ok = people_resp.status_code == 200
        member_permissions: list[dict] = []
        if people_ok:
            people = people_resp.json().get("people", [])
            for person in people:
                perms = person.get("permissions", {})
                member_permissions.append(
                    {
                        "id": person.get("id"),
                        "full_name": person.get("full-name"),
                        "email": person.get("email-address"),
                        "can_add_tasks": perms.get("add-tasks") == "1",
                    }
                )

        can_any_member_add_tasks = any(
            item.get("can_add_tasks", False) for item in member_permissions
        )
        readable_tasklists = [
            {"id": tl.get("id"), "name": tl.get("name")} for tl in tasklists[:10]
        ]

        status = "ok" if project_ok and tasklists_ok and people_ok else "error"
        return {
            "status": status,
            "config_ok": True,
            "domain": domain,
            "project_id": project_id,
            "project_access_ok": project_ok,
            "project_name": project_name,
            "tasklists_access_ok": tasklists_ok,
            "tasklists_count": len(tasklists),
            "tasklists_preview": readable_tasklists,
            "people_access_ok": people_ok,
            "member_permissions_preview": member_permissions[:20],
            "can_any_member_add_tasks": can_any_member_add_tasks,
            "create_only_mode": os.getenv("TEAMWORK_ALLOWED_ACTIONS", "create"),
            "recommended_next_step": (
                "Request Teamwork admin to grant your user 'add tasks' permission on this project/tasklist."
                if not can_any_member_add_tasks
                else "Permissions exist for at least one member. If your own user still fails, ask admin to grant your specific account add-tasks permission."
            ),
        }


def _extract_task_id(payload: dict) -> int | None:
    """
    Extract Teamwork task id from common API response structures.
    """
    if not isinstance(payload, dict):
        return None

    direct_id = payload.get("id")
    if isinstance(direct_id, int):
        return direct_id
    if isinstance(direct_id, str) and direct_id.isdigit():
        return int(direct_id)

    task_obj = payload.get("task")
    if isinstance(task_obj, dict):
        task_id = task_obj.get("id")
        if isinstance(task_id, int):
            return task_id
        if isinstance(task_id, str) and task_id.isdigit():
            return int(task_id)

    tasks = payload.get("tasks")
    if isinstance(tasks, list) and tasks:
        first = tasks[0]
        if isinstance(first, dict):
            first_id = first.get("id")
            if isinstance(first_id, int):
                return first_id
            if isinstance(first_id, str) and first_id.isdigit():
                return int(first_id)

    return None
