"""확인 필요(C 확정 전): temporary names, limits and defaults live only in this file.

Change Field aliases here when C finalizes names; callers use semantic Python attributes.
These limits are the demo contract, not the actual cloud platform limits.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, model_validator

from ai.spec.models import DeploySpec

PENDING = "확인 필요(C 확정 전)"
MVP_REQUEST_LIMIT_SECONDS = 30


class CommonVars(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)
    image_reference: str | None = Field(
        default=None, alias="image_tag", max_length=1024, description=PENDING
    )
    http_port: int = Field(default=8080, alias="port", ge=1, le=65535, description=PENDING)

    @model_validator(mode="after")
    def coherent_inputs(self, info: ValidationInfo):
        context = info.context or {}
        if self.http_port != context.get("port", self.http_port):
            raise ValueError("port_must_match_spec")
        if self.image_reference and (
            any(c.isspace() for c in self.image_reference)
            or "@" in self.image_reference
            and "@sha256:" not in self.image_reference
            or "://" in self.image_reference
        ):
            raise ValueError("image_reference_invalid")
        if "image_reference" in context and self.image_reference != context["image_reference"]:
            raise ValueError("image_reference_must_match_input")
        return self


class ServerlessVars(CommonVars):
    memory_limit_mb: int = Field(
        default=256, alias="memory_mb", ge=128, le=3008, description=PENDING
    )
    request_timeout_seconds: int = Field(
        default=MVP_REQUEST_LIMIT_SECONDS,
        alias="timeout_s",
        ge=1,
        le=MVP_REQUEST_LIMIT_SECONDS,
        description=PENDING,
    )


class ContainerVars(CommonVars):
    memory_limit_mb: int = Field(
        default=512, alias="memory_mb", ge=128, le=16384, description=PENDING
    )
    web_replicas: int = Field(default=1, alias="instances", ge=1, le=8, description=PENDING)

    @model_validator(mode="after")
    def fixed_web_instances(self, info: ValidationInfo):
        if self.web_replicas != (info.context or {}).get("instances", self.web_replicas):
            raise ValueError("instances_must_match_spec")
        return self


def make_tfvars(
    selected: Literal["aws-serverless", "aws-always-on", "onprem"],
    spec: DeploySpec,
    *,
    image_reference: str | None = None,
    overrides: dict | None = None,
) -> tuple[ServerlessVars | ContainerVars, bool]:
    model = ServerlessVars if selected == "aws-serverless" else ContainerVars
    values = model(image_reference=image_reference, http_port=spec.port).model_dump(by_alias=True)
    context = {
        "port": spec.port,
        "instances": spec.processes.web.instances,
        "image_reference": image_reference,
    }
    if selected == "aws-serverless":
        # 이 분기는 짧은 요청용 세트만 받는다. 사용자 값을 추측해서 잘라 내지 않는다.
        values[model.model_fields["request_timeout_seconds"].alias] = max(
            MVP_REQUEST_LIMIT_SECONDS, spec.workload.max_request_seconds
        )
    defaults = model.model_validate(values, context=context)
    try:
        return model.model_validate({**values, **(overrides or {})}, context=context), False
    except ValidationError:
        return defaults, True
