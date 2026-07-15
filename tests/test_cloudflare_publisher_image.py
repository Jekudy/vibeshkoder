"""Static build contract for the bundled, non-root Wrangler runtime."""

from __future__ import annotations

import json
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_wrangler_dependency_is_exact_and_locked() -> None:
    package = json.loads((PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((PROJECT_ROOT / "package-lock.json").read_text(encoding="utf-8"))

    wrangler_version = package["dependencies"]["wrangler"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", wrangler_version)
    assert lock["packages"][""]["dependencies"]["wrangler"] == wrangler_version
    assert lock["packages"]["node_modules/wrangler"]["version"] == wrangler_version
    assert lock["packages"]["node_modules/wrangler"]["integrity"].startswith("sha512-")


def test_bot_image_bundles_pinned_wrangler_and_remains_non_root() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile.bot").read_text(encoding="utf-8")

    assert re.search(
        r"^FROM node:\d+\.\d+\.\d+-bookworm-slim AS wrangler-runtime$", dockerfile, re.M
    )
    assert "RUN npm ci --omit=dev" in dockerfile
    assert "COPY --from=wrangler-runtime /usr/local/bin/node /usr/local/bin/node" in dockerfile
    assert (
        "COPY --from=wrangler-runtime /wrangler/node_modules /opt/wrangler/node_modules"
        in dockerfile
    )
    assert "npx " not in dockerfile
    assert "npm install" not in dockerfile

    user_position = dockerfile.rindex("USER appuser")
    assert user_position > dockerfile.index("COPY --from=wrangler-runtime")
    assert "USER root" not in dockerfile[user_position:]


def test_bot_image_execs_python_after_migration_for_pid1_signal_delivery() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile.bot").read_text(encoding="utf-8")

    assert 'CMD ["sh", "-c", "alembic upgrade head && exec python -m bot"]' in dockerfile
