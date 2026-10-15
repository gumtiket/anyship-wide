from pathlib import Path

from dotenv import load_dotenv

from .app import create_app
from .config import Settings

load_dotenv(Path(__file__).resolve().parents[1] / ".env.github.local")
settings = Settings.from_env()
aws_adapter = None
if settings.aws_verify == "sts":
    from .aws_sts_adapter import STSAdapter  # boto3는 이 설정을 켠 경우에만 불러온다

    aws_adapter = STSAdapter()
app = create_app(settings, aws_adapter=aws_adapter)
