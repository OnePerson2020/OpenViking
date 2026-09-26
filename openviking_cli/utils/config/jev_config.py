# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Configuration for the reusable Jev System One decision client."""

from typing import Optional

from pydantic import BaseModel, Field, model_validator


class JevConfig(BaseModel):
    """Connection settings for a Jev-compatible System One endpoint."""

    api_url: str = Field(description="Full Jev /v1/systemone endpoint URL")
    api_key: str = Field(description="Bearer token for the Jev endpoint")
    model: Optional[str] = Field(default=None, description="Optional Jev model override")
    timeout: float = Field(default=30.0, gt=0, le=300)
    verify_ssl: bool = True
    max_retries: int = Field(default=3, ge=0, le=5)
    retry_backoff_seconds: float = Field(default=0.5, ge=0, le=10)
    max_input_tokens: int = Field(
        default=28_000,
        ge=1024,
        le=1_000_000,
        description="Conservative client-side input budget used to split Jev question batches",
    )

    @model_validator(mode="after")
    def validate_endpoint(self) -> "JevConfig":
        self.api_url = self.api_url.strip()
        self.api_key = self.api_key.strip()
        if not self.api_url.startswith(("http://", "https://")):
            raise ValueError("Jev api_url must use http or https")
        if not self.api_key:
            raise ValueError("Jev api_key must not be blank")
        return self
