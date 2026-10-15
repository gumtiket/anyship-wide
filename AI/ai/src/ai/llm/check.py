import argparse
import os
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from ai.llm.base import LLMError
from ai.llm.bedrock import BedrockCallError, BedrockClient


def error_hint(code: str) -> str:
    hints = {
        "AccessDeniedException": "IAM 권한·모델 접근·교차 리전 대상 권한을 확인하세요.",
        "ValidationException": "모델 ID·리전·Converse 지원·추론 프로필 필요 여부를 확인하세요.",
        "ResourceNotFoundException": "해당 리전의 모델/추론 프로필 ID를 확인하세요.",
        "NoCredentialsError": "AWS_PROFILE과 aws login 또는 SSO 로그인 상태를 확인하세요.",
        "NoRegionError": (
            "BEDROCK_REGION과 AWS_DEFAULT_REGION 또는 AWS 프로필의 기본 region을 확인하세요."
        ),
        "MissingDependencyException": (
            "브라우저 로그인 지원은 pip install -e 'ai[login]'으로 설치하세요."
        ),
        "UnauthorizedSSOTokenError": "AWS_PROFILE에 맞는 aws sso login을 실행하세요.",
        "ThrottlingException": "호출 할당량을 확인하고 잠시 뒤 다시 시도하세요.",
        "EndpointConnectionError": "리전과 네트워크 연결을 확인하세요.",
        "ReadTimeoutError": "결과와 비용이 불확실하므로 자동 재호출하지 않았습니다.",
    }
    return hints.get(code, "AWS 프로필·리전·모델 접근·추론 프로필을 확인하세요.")


def list_models(control: Any) -> None:
    next_token = None
    while True:
        kwargs = {"nextToken": next_token} if next_token else {}
        page = control.list_inference_profiles(**kwargs)
        for profile in page.get("inferenceProfileSummaries", []):
            print("추론 프로필:", profile["inferenceProfileId"])
        next_token = page.get("nextToken")
        if not next_token:
            break
    response = control.list_foundation_models(byProvider="Anthropic")
    for model in response.get("modelSummaries", []):
        print("기반 모델:", model["modelId"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bedrock 목록 조회/짧은 호출 확인 (수동 실행)")
    parser.add_argument("--list-only", action="store_true", help="유료 Converse 호출 없이 목록만")
    args = parser.parse_args(argv)
    region = os.environ.get("BEDROCK_REGION", "").strip()
    if not region:
        print("필수 환경변수 누락: BEDROCK_REGION")
        return 2
    try:
        # 전체 확인에서는 AWS 동작 전에 두 모델 ID를 먼저 검증한다.
        llm = None if args.list_only else BedrockClient(max_tokens=32)
        session = boto3.Session(region_name=region)
        control = session.client(
            "bedrock",
            region_name=region,
            config=Config(retries={"total_max_attempts": 1}, connect_timeout=10, read_timeout=30),
        )
        list_models(control)
        if llm is not None:
            for tier in ("strong", "fast"):
                result = llm.complete(
                    "한 단어로만 응답하세요.",
                    "안녕",
                    tier=tier,
                    schema=None,
                    stage="bedrock_check",
                )
                print(
                    f"{tier}: 성공, 입력={result.input_tokens}, 출력={result.output_tokens}, "
                    f"지연={result.latency_s:.3f}s, 비용={result.cost_usd}"
                )
        return 0
    except ValueError:
        print("STRONG/FAST 모델 ID 등 필수 설정을 확인하세요. 임의 기본값은 사용하지 않습니다.")
        return 2
    except (LLMError, ClientError, BotoCoreError) as error:
        if isinstance(error, BedrockCallError):
            code = error.code
        elif isinstance(error, ClientError):
            code = error.response.get("Error", {}).get("Code", "Unknown")
        else:
            code = type(error).__name__
        print(f"Bedrock 확인 실패 ({code}): {error_hint(code)}")
        return 1
