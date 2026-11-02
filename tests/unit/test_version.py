from taiwan_stock.shared.version import get_version


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "9.9.9")

    assert get_version() == "9.9.9"


def test_falls_back_to_installed_package_version(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)

    assert get_version() != ""
