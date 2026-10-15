"""미리 만들어 둔 배포 세트의 이름.

AI 쪽(AI/ai/src/ai/spec)이 세트를 고를 때 쓰는 이름과 같아서, 선택 결과를
그대로 어댑터에 넘길 수 있다."""

from typing import Literal

SetName = Literal["aws-serverless", "aws-always-on", "onprem"]

AWS_SERVERLESS: SetName = "aws-serverless"  # Lambda + Function URL
AWS_ALWAYS_ON: SetName = "aws-always-on"  # 공용 EC2 호스트 + Compose + Traefik
ONPREM: SetName = "onprem"  # 사용자 서버에 SSH로 접속해 Compose + Traefik

SET_NAMES: tuple[SetName, ...] = (AWS_SERVERLESS, AWS_ALWAYS_ON, ONPREM)
