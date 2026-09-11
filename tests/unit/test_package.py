import acpl_assistant


def test_package_exposes_version() -> None:
    assert acpl_assistant.__version__
