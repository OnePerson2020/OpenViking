# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Agent Evolution runtime configuration."""

from typing import Literal, Optional

from pydantic import BaseModel, model_validator

from .runtime_field import RuntimeField


class DagDeciderConfig(BaseModel):
    """Decision policy for Experience DAG runtime nodes."""

    provider: Literal["vlm", "jev"] = RuntimeField(default="vlm")
    noul_true_threshold: float = RuntimeField(default=0.7, ge=0, le=1)
    noul_false_threshold: float = RuntimeField(default=0.3, ge=0, le=1)
    choice_confidence_threshold: float = RuntimeField(default=0.5, ge=0, le=1)
    max_state_chars: int = RuntimeField(default=32768, ge=1024, le=262144)

    @model_validator(mode="after")
    def validate_thresholds(self) -> "DagDeciderConfig":
        if self.noul_false_threshold >= self.noul_true_threshold:
            raise ValueError("noul_false_threshold must be lower than noul_true_threshold")
        return self


class AgentEvolutionConfig(BaseModel):
    """Agent Evolution switch shared by cluster and account configuration."""

    enabled: bool = RuntimeField(default=True)
    dag_decider: Optional[DagDeciderConfig] = RuntimeField(default=None)
