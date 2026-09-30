from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Item = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]


class Summary(BaseModel):
    """The structure required by prompts/summary-prompt.md. Also validates what the LLM returns: the limits are
    the "bounded output length" rule, so a model that rambles is rejected instead of stored."""

    # A chatty model may add extra fields; we keep only the five we asked for.
    model_config = ConfigDict(extra="ignore")

    overview: str = Field(min_length=1, max_length=3000)
    key_points: list[Item] = Field(max_length=20)
    action_items: list[Item] = Field(max_length=20)
    decisions: list[Item] = Field(max_length=20)
    uncertainties: list[Item] = Field(max_length=20)
