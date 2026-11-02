"""Installed-package version lookup, shared by the app and web pages."""

import os
from importlib.metadata import PackageNotFoundError, version


def get_version() -> str:
    if app_version := os.environ.get("APP_VERSION"):
        return app_version
    try:
        return version("taiwan-stock")
    except PackageNotFoundError:
        return "0.0.0"
