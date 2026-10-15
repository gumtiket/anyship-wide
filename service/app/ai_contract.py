"""Contract only. No model call, fabricated result or code execution is provided."""

from typing import Literal, Protocol

from pydantic import BaseModel, Field


class AnalysisInput(BaseModel):
    project_id: str
    repository: str
    base_sha: str


class ChangeItem(BaseModel):
    id: str
    title: str
    evidence: str
    proposed_change: str
    paths: list[str]
    risk: Literal["low", "medium", "high"]
    depends_on: list[str] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    base_sha: str
    supported: bool
    reason: str
    items: list[ChangeItem]


class ModificationInput(BaseModel):
    project_id: str
    base_sha: str
    items: list[ChangeItem]


class ModificationResult(BaseModel):
    base_sha: str
    patch: str
    summary: str


class AIProvider(Protocol):
    def analyze(self, request: AnalysisInput) -> AnalysisResult: ...
    def modify(self, request: ModificationInput) -> ModificationResult: ...
