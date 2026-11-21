"""Contract: a worker's tool path, for the evaluation only.

The Lead must not see which tools a worker used: it gets a ``WorkerResult``
(worker_io.py), which carries findings and sources, never tool calls. The tool path is
kept apart from it. A worker publishes one ``ToolPath`` per task, and only a recorder
that an evaluation or a test sets up receives it (services/agent/app/workers/tool_path.py).
The Lead's code never sets one up, so in the running app the path goes nowhere.

scripts/check_tool_calls.py joins each ``ToolPath`` to its ``WorkerResult`` by task_id,
to resolve the sources a call returned, and checks the calls against the tool_calls of
a golden case's expected.json (golden.py).
"""

from typing import Any, Literal

from pydantic import Field, model_validator

from .case_input import CASE_ID, Contract

ToolCallStatus = Literal[
    "ok",            # the tool ran and returned its sources
    "repeat",        # same call (or same IRS name control) as earlier in the task: the earlier sources again
    "rejected",      # refused: a tool this worker doesn't have, or invalid arguments
    "error",         # the tool or the case service failed
    "over_budget",   # not run: the tool-call budget was used up (not counted in tool_calls_used)
]


class ToolCall(Contract):
    """One tool call the worker's model asked for. Written by code, in order."""

    turn: int = Field(ge=1)              # the model turn that asked for it; parallel calls share a turn
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    source_ids: list[str] = Field(default_factory=list)   # the sources it returned, as in WorkerResult.sources
    status: ToolCallStatus = "ok"


class ToolPath(Contract):
    """Every tool call of one worker task, in order: the first task or one follow-up."""

    task_id: str                          # the WorkerInput's, and its WorkerResult's
    case_id: str = Field(pattern=CASE_ID)
    worker: Literal["records", "evidence"]
    tool_calls: list[ToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def turns_in_order(self) -> "ToolPath":
        turns = [c.turn for c in self.tool_calls]
        if turns != sorted(turns):
            raise ValueError("tool_calls are not in turn order")
        return self

    @property
    def tool_calls_used(self) -> int:
        """Calls counted against the budget, as in WorkerResult.tool_calls_used."""
        return sum(c.status != "over_budget" for c in self.tool_calls)


__all__ = ["ToolCall", "ToolCallStatus", "ToolPath"]
