import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.app import create_app
from app.config import Settings


@pytest.mark.parametrize("production", [False, True])
def test_old_aws_page_redirects_to_integrated_frontend(production):
    settings = Settings(
        production=production,
        app_origin="https://service.example",
        token_key=Fernet.generate_key().decode(),
        github_client_id="test",
        github_client_secret="test",
        github_app_slug="test",
    )
    # 예전 주소도 같은 화면을 쓰며 API 인증을 우회하지 않는다.
    with TestClient(create_app(settings), base_url=settings.app_origin) as client:
        response = client.get("/dev/aws", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/#aws"
        environment = "a5361535-3ae5-40c6-ae8e-8e879154531d"
        response = client.get(
            "/dev/aws", params={"environment": environment}, follow_redirects=False
        )
        assert response.headers["location"] == f"/#aws/{environment}"
        assert (
            client.get("/dev/aws", params={"environment": "https://attacker.invalid"}).status_code
            == 422
        )
        for path in ("/dev/aws/app.js", "/dev/aws/style.css"):
            assert client.get(path).status_code == 404
        assert client.get("/api/aws/environments").status_code == 401
