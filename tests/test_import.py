import specula_client


def test_package_is_importable_and_versioned() -> None:
    assert isinstance(specula_client.__version__, str)


def test_specula_extra_prefix_is_exported_at_package_level() -> None:
    # TF-937: Konsumenten (z.B. ratums PiiScrubbingLogFilter) sollen die Konvention aus
    # `specula_client` importieren koennen, statt sie selbst hartzukodieren.
    assert specula_client.SPECULA_EXTRA_PREFIX == "specula_"
