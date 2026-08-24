from pre_pr_verify import __version__


def test_package_imports() -> None:
    assert __version__ == "0.1.2"
