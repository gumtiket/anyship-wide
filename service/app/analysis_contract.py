"""Versioned web boundary; worker paths and credentials never enter this payload."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .analysis_source import excluded, safe_path


class ProposalFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    content: str = Field(max_length=524288)
    mode: Literal["100644", "100755"] = "100644"
    before_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")

    @field_validator("path")
    @classmethod
    def valid_path(cls, value):
        if not safe_path(value) or excluded(value):
            raise ValueError("unsafe_proposal_path")
        return value


class WorkerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    report: dict
    files: list[ProposalFile] = Field(max_length=100)


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    target_env: Literal["aws", "onprem"] = "aws"

    @field_validator("request_id")
    @classmethod
    def valid_request_id(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("request_id must be a canonical UUID")
        return value


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approve_risky: bool = False
