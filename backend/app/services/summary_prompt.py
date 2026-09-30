"""Loads the summary prompts from prompts/summary-prompt.md, the single source of truth for what the LLM is told.

The markdown is parsed by its `## Section` headings. Loading checks that every section and `{{PLACEHOLDER}}` the code
relies on exists, so a broken or missing prompt file stops the worker at startup instead of failing a user's job.
"""

import re
from dataclasses import dataclass
from functools import lru_cache

from app.core.config import REPO_ROOT

PROMPT_FILE = REPO_ROOT / "prompts" / "summary-prompt.md"


class PromptError(Exception):
    """The prompt file is missing, or lacks a section or placeholder the code depends on."""


@dataclass(frozen=True)
class SummaryPrompts:
    system: str  # the system prompt plus the grounding rules
    user_template: str  # contains {{TRANSCRIPT}}
    part_note_template: str  # contains {{PART}} and {{TOTAL}}; put before the user message for one part of many
    merge_system: str
    merge_user_template: str  # contains {{PARTS}}
    shorter_note: str  # appended to the user message when the previous answer was cut off

    def user_message(self, transcript: str) -> str:
        return self.user_template.replace("{{TRANSCRIPT}}", transcript)

    def part_message(self, transcript: str, part: int, total: int) -> str:
        note = self.part_note_template.replace("{{PART}}", str(part)).replace("{{TOTAL}}", str(total))
        return f"{note}\n\n{self.user_message(transcript)}"

    def merge_message(self, parts_json: str) -> str:
        return self.merge_user_template.replace("{{PARTS}}", parts_json)


def _sections(markdown: str) -> dict[str, str]:
    """{"System prompt": body, ...} split on lines that start with '## '."""
    found: dict[str, str] = {}
    for chunk in re.split(r"^## ", markdown, flags=re.MULTILINE)[1:]:
        title, _, body = chunk.partition("\n")
        found[title.strip()] = body.strip()
    return found


def parse_prompts(markdown: str) -> SummaryPrompts:
    sections = _sections(markdown)
    needed = (
        "System prompt",
        "User message template",
        "Grounding rules",
        "Part summary",
        "Merge system prompt",
        "Merge user template",
        "Shorter retry",
    )
    if missing := [name for name in needed if not sections.get(name)]:
        raise PromptError(f"prompt file is missing sections: {', '.join(missing)}")

    prompts = SummaryPrompts(
        system=f"{sections['System prompt']}\n\nRules:\n{sections['Grounding rules']}",
        user_template=sections["User message template"],
        part_note_template=sections["Part summary"],
        merge_system=sections["Merge system prompt"],
        merge_user_template=sections["Merge user template"],
        shorter_note=sections["Shorter retry"],
    )
    for text, placeholders in (
        (prompts.user_template, ["{{TRANSCRIPT}}"]),
        (prompts.part_note_template, ["{{PART}}", "{{TOTAL}}"]),
        (prompts.merge_user_template, ["{{PARTS}}"]),
    ):
        if absent := [p for p in placeholders if p not in text]:
            raise PromptError(f"prompt file is missing placeholders: {', '.join(absent)}")
    return prompts


@lru_cache
def get_prompts() -> SummaryPrompts:
    try:
        return parse_prompts(PROMPT_FILE.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PromptError(f"cannot read {PROMPT_FILE}") from exc
