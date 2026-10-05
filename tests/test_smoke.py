from aitobuild import __version__


def test_version_exists() -> None:
    assert isinstance(__version__, str)
    assert __version__
