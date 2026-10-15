import pytest


def pytest_collection_modifyitems(config, items):
    # -m 식을 명시해야 실행된다. 마커를 선언했다는 것만으로는 제외되지 않는다.
    if config.option.markexpr:
        return
    skip = pytest.mark.skip(reason="외부 호출 테스트는 -m bedrock 또는 -m docker로 명시 실행")
    for item in items:
        if item.get_closest_marker("bedrock") or item.get_closest_marker("docker"):
            item.add_marker(skip)
