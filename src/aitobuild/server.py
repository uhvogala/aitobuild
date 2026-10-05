"""ASGI entrypoint for local development."""

from aitobuild.app import create_app

app = create_app()
