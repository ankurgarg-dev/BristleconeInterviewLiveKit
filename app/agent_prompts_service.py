from __future__ import annotations

import json
import threading
from pathlib import Path

from shared.prompts import AGENT_PROMPT_ORDER, get_default_agent_prompts

PROMPTS_PATH = Path(__file__).resolve().parents[1] / "data" / "agent_prompts.json"
_prompts_lock = threading.Lock()


def _normalize_agent(agent: str) -> str:
    return str(agent or "").strip().lower()


def _validate_agent(agent: str) -> str:
    normalized = _normalize_agent(agent)
    defaults = get_default_agent_prompts()
    if normalized not in defaults:
        raise ValueError(f"unsupported agent: {agent}")
    return normalized


def _load_overrides() -> dict[str, str]:
    if not PROMPTS_PATH.exists():
        return {}

    try:
        raw = json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

    if not isinstance(raw, dict):
        return {}

    defaults = get_default_agent_prompts()
    overrides: dict[str, str] = {}
    for key, value in raw.items():
        normalized = _normalize_agent(key)
        if normalized in defaults and isinstance(value, str) and value.strip():
            overrides[normalized] = value.strip()
    return overrides


def _save_overrides(overrides: dict[str, str]) -> None:
    PROMPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    serialized = {agent: overrides[agent] for agent in AGENT_PROMPT_ORDER if agent in overrides}
    PROMPTS_PATH.write_text(json.dumps(serialized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_effective_prompt(agent: str) -> str:
    normalized = _validate_agent(agent)
    defaults = get_default_agent_prompts()
    with _prompts_lock:
        overrides = _load_overrides()
        return overrides.get(normalized) or defaults[normalized]


def list_prompt_records() -> list[dict[str, str | bool]]:
    defaults = get_default_agent_prompts()
    with _prompts_lock:
        overrides = _load_overrides()

    records: list[dict[str, str | bool]] = []
    for agent in AGENT_PROMPT_ORDER:
        default_prompt = defaults[agent]
        prompt = overrides.get(agent) or default_prompt
        records.append(
            {
                "agent": agent,
                "prompt": prompt,
                "default_prompt": default_prompt,
                "is_default": agent not in overrides,
            }
        )
    return records


def set_prompt(agent: str, prompt: str) -> dict[str, str | bool]:
    normalized = _validate_agent(agent)
    next_prompt = str(prompt or "").strip()
    if not next_prompt:
        raise ValueError("prompt cannot be empty")

    defaults = get_default_agent_prompts()
    with _prompts_lock:
        overrides = _load_overrides()
        if next_prompt == defaults[normalized]:
            overrides.pop(normalized, None)
            is_default = True
            stored_prompt = defaults[normalized]
        else:
            overrides[normalized] = next_prompt
            is_default = False
            stored_prompt = next_prompt
        _save_overrides(overrides)

    return {
        "agent": normalized,
        "prompt": stored_prompt,
        "default_prompt": defaults[normalized],
        "is_default": is_default,
    }


def reset_prompt(agent: str) -> dict[str, str | bool]:
    normalized = _validate_agent(agent)
    defaults = get_default_agent_prompts()

    with _prompts_lock:
        overrides = _load_overrides()
        overrides.pop(normalized, None)
        _save_overrides(overrides)

    return {
        "agent": normalized,
        "prompt": defaults[normalized],
        "default_prompt": defaults[normalized],
        "is_default": True,
    }
