import specula_client


def test_package_is_importable_and_versioned() -> None:
    assert isinstance(specula_client.__version__, str)
