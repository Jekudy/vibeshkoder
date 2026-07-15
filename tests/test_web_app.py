from __future__ import annotations

from tests.conftest import import_module


def test_create_app_registers_expected_routes(app_env) -> None:
    web_app = import_module("web.app")
    app = web_app.create_app()

    # Included routers may be lazy route groups without a public ``path``
    # attribute. OpenAPI is the stable, flattened route view.
    paths = set(app.openapi()["paths"])
    assert "/" in paths
    assert "/login" in paths
    assert "/dashboard" in paths
    assert "/members" in paths
