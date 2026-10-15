"""시험에 쓰는 배포 명세. AI 쪽이 실제로 만든 두 가지 모양을 그대로 옮겼다."""

import copy

# AI가 생성한 명세(AI/ai/demo-cache/todo/out/deploy-spec.yaml): PORT가 env에 들어 있다.
GENERATED = {
    "app": "todo",
    "source": {"repo": "sample://todo"},
    "build": {"dockerfile": "Dockerfile", "runtime_hint": "python3.12"},
    "port": 8080,
    "healthcheck": "/healthz",
    "ingress": "public",
    "env": [
        {"name": "LOG_LEVEL", "secret": False, "generate": False, "value": "INFO"},
        {"name": "PORT", "secret": False, "generate": False, "value": "8080"},
        {"name": "SECRET_KEY", "secret": True, "generate": True},
    ],
    "backing_services": [{"type": "postgres", "bind_as": "DATABASE_URL"}],
    "workload": {
        "type": "request-driven",
        "scale_to_zero": True,
        "max_request_seconds": 10,
        "websocket": False,
    },
    "processes": {"web": {"instances": 1}},
    "release": {"migrate": "python -m app.migrate"},
    "profile": "dev",
}

# 사람이 쓴 명세(AI/ai/deliverables/hand-specs/todo.deploy-spec.yaml): object_storage가 있다.
HAND_WRITTEN = {
    "app": "todo",
    "source": {"repo": "samples/todo", "commit": "REPLACE_WITH_DEMO_COMMIT"},
    "port": 8080,
    "healthcheck": "/healthz",
    "env": [
        {"name": "SECRET_KEY", "secret": True, "generate": True},
        {"name": "LOG_LEVEL", "value": "info"},
    ],
    "backing_services": [
        {"type": "postgres", "bind_as": "DATABASE_URL"},
        {"type": "object_storage", "bind_as": "STORAGE_URL"},
    ],
    "workload": {
        "type": "always-on",
        "scale_to_zero": False,
        "max_request_seconds": 10,
        "websocket": False,
    },
    "processes": {"web": {"instances": 1}},
    "release": {"migrate": "python -m app.migrate"},
    "profile": "dev",
}


def make(base=GENERATED, **override):
    spec = copy.deepcopy(base)
    spec.update(override)
    return spec
