from anyship_adapters import (
    MASK,
    AdapterError,
    DeployResult,
    LogEvent,
    make_safe_log,
    redact_event,
    redact_model,
    redact_text,
)

# 비밀 스캐너가 토큰 모양의 문자열을 저장소에서 보지 않도록 조각으로 조립해서 만든다.
FAKE_GITHUB_TOKEN = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
FAKE_AWS_KEY_ID = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
FAKE_PEM = (
    "-----BEGIN "
    + "OPENSSH PRIVATE KEY"
    + "-----\nb3BlbnNzaC1rZXk=\n-----END "
    + "OPENSSH PRIVATE KEY"
    + "-----"
)


# --- 비밀 값 자체 ---------------------------------------------------------------
def test_known_values_are_replaced_everywhere():
    secrets = {"API_KEY": "s3cr3t-value-123", "DB_PASSWORD": "hunter2hunter2"}
    text = "key=s3cr3t-value-123 and again s3cr3t-value-123, pw hunter2hunter2"
    out = redact_text(text, secrets)
    assert "s3cr3t-value-123" not in out and "hunter2hunter2" not in out
    assert out.count(MASK) == 3


def test_longest_secret_goes_first_so_no_tail_is_left():
    out = redact_text("token abcdefgh!", {"SHORT": "abcd", "LONG": "abcdefgh"})
    assert "efgh" not in out and out == f"token {MASK}!"


def test_tiny_and_empty_secrets_are_ignored_instead_of_wrecking_the_text():
    assert redact_text("a b c 1 2 3", {"X": "a", "Y": "", "Z": "12"}) == "a b c 1 2 3"


def test_text_without_secrets_is_unchanged():
    assert redact_text("이미지 전달 중") == "이미지 전달 중"


# --- 알려진 비밀의 모양 --------------------------------------------------------------------
def test_well_known_secret_shapes_are_replaced_even_when_unknown_to_the_caller():
    assert FAKE_GITHUB_TOKEN not in redact_text(f"token {FAKE_GITHUB_TOKEN}")
    assert FAKE_AWS_KEY_ID not in redact_text(f"key {FAKE_AWS_KEY_ID}")
    assert "b3BlbnNzaC1rZXk=" not in redact_text(f"before\n{FAKE_PEM}\nafter")
    assert (
        redact_text("postgresql://app:p4ssw0rd@db.internal/app")
        == f"postgresql://{MASK}@db.internal/app"
    )


def test_a_url_without_credentials_is_untouched():
    url = "https://todo-a1b2.demo.onprem.anyship.cloud/healthz"
    assert redact_text(url) == url


# --- 이벤트와 결과 ---------------------------------------------------------------------
def test_events_are_redacted_in_message_name_and_nested_data():
    event = LogEvent(
        step=2,
        total=5,
        name="전송 s3cr3t-value-123",
        message="값: s3cr3t-value-123",
        data={"a": "s3cr3t-value-123", "nested": {"list": ["x", "s3cr3t-value-123"]}, "n": 3},
    )
    out = redact_event(event, {"K": "s3cr3t-value-123"})
    assert "s3cr3t-value-123" not in out.model_dump_json()
    assert out.step == 2 and out.data["n"] == 3  # 글자가 아닌 데이터는 그대로 유지된다


def test_redaction_returns_a_copy_and_leaves_the_original_alone():
    event = LogEvent(message="s3cr3t-value-123")
    redact_event(event, {"K": "s3cr3t-value-123"})
    assert event.message == "s3cr3t-value-123"


def test_results_are_redacted_including_error_text_and_url():
    result = DeployResult(
        ok=False,
        error=AdapterError(
            code="boom", message="failed with s3cr3t-value-123", hint="try s3cr3t-value-123"
        ),
        details={"x": "s3cr3t-value-123"},
    )
    out = redact_model(result, {"K": "s3cr3t-value-123"})
    assert "s3cr3t-value-123" not in out.model_dump_json()
    assert out.ok is False and out.error.code == "boom"


def test_safe_log_hands_only_redacted_events_to_the_sink():
    seen = []
    log = make_safe_log(seen.append, {"K": "s3cr3t-value-123"})
    log(LogEvent(message="leak s3cr3t-value-123"))
    log(LogEvent(message="fine"))
    assert [e.message for e in seen] == [f"leak {MASK}", "fine"]
