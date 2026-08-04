"""Pipeline stages."""

from cip_copilot.agents.stages import (
    StageDeps,
    stage_aggregate,
    stage_execute,
    stage_generate,
    stage_plan,
    stage_reason,
    stage_reflect,
    stage_remember,
    stage_validate,
)

__all__ = [
    "StageDeps",
    "stage_aggregate",
    "stage_execute",
    "stage_generate",
    "stage_plan",
    "stage_reason",
    "stage_reflect",
    "stage_remember",
    "stage_validate",
]
