import mrm


def test_package_importable() -> None:
    assert mrm is not None


def test_arithmetic() -> None:
    assert 1 + 1 == 2
