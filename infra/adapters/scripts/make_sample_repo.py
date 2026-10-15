"""B의 todo 변환본을 '배포 가능한 저장소 모양'으로 만든다(서비스 연동 시험용).

AI가 서비스에 붙기 전까지, 병합된 소스의 루트에 `Dockerfile`과 `deploy-spec.yaml`이 있다고 가정하고 시험하려는 것이다.
원본 샘플 + changes.diff + Dockerfile + deploy-spec.yaml을 한 폴더에 모은다. 이 폴더를 테스트용 GitHub 저장소로 올리면 된다.

    python infra/adapters/scripts/make_sample_repo.py <출력 폴더>
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / "AI" / "samples" / "todo"
OUT = ROOT / "AI" / "ai" / "demo-cache" / "todo" / "out"
EXCLUDE = ("VIOLATIONS.json",)  # 분석용 메모라 배포 저장소에는 필요 없다


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    target = Path(sys.argv[1])
    if target.exists():
        print(f"{target}이(가) 이미 있습니다. 새 폴더 이름을 지정하세요.")
        return 1
    shutil.copytree(SAMPLE, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".venv"))
    subprocess.run(
        ["git", "apply", "--whitespace=nowarn", str(OUT / "changes.diff")], cwd=target, check=True
    )
    shutil.copy(OUT / "Dockerfile", target / "Dockerfile")
    shutil.copy(OUT / "deploy-spec.yaml", target / "deploy-spec.yaml")
    for name in EXCLUDE:
        (target / name).unlink(missing_ok=True)
    print(f"만들었습니다: {target}")
    print("  " + ", ".join(sorted(p.name for p in target.iterdir())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
