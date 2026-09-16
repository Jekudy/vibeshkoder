"""Deploy SHA-pinned bot/web only; rollback images, never database data (#521)."""

import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from healthcheck import COMPOSE, local_checks

ROOT = Path("/srv/shkoder")


def image_values(text: str) -> dict[str, str]:
    values = {}
    for service in ("BOT", "WEB"):
        name = service + "_IMAGE"
        matches = re.findall(rf"^{name}=(.+)$", text, re.MULTILINE)
        pattern = rf"ghcr\.io/jekudy/vibe-gatekeeper-{service.lower()}(?::sha-[0-9a-f]{{40}}|@sha256:[0-9a-f]{{64}})"
        if len(matches) != 1 or not re.fullmatch(pattern, matches[0]):
            raise ValueError(f"{name} must have exactly one SHA-pinned image")
        values[name] = matches[0]
    return values


def write_env(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temporary:
        temporary.write(text)
        name = temporary.name
    os.replace(name, path)


def compose(*args: str) -> None:
    subprocess.run([*COMPOSE, *args], check=True, timeout=300)


def wait_healthy() -> None:
    deadline = time.monotonic() + 180
    while True:
        checks = local_checks()
        print(f"Readiness: {checks}", flush=True)
        if all(checks.values()):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("bot/web/database readiness failed")
        time.sleep(5)


def deploy(sha: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("release SHA must be 40 lowercase hexadecimal characters")
    repository = os.environ["REPOSITORY"]
    if repository != "Jekudy/vibeshkoder":
        raise ValueError("unexpected deployment repository")
    with (ROOT / ".deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = subprocess.check_output(
            ["git", "ls-remote", f"https://github.com/{repository}.git", "refs/heads/main"],
            text=True,
            timeout=30,
        ).split()[0]
        if current != sha:
            print("Skipping stale release", sha)
            return
        path = ROOT / ".env"
        previous = path.read_text()
        values = image_values(previous)
        updated = previous
        for name, old in values.items():
            image = old.split(":sha-")[0].split("@sha256:")[0] + ":sha-" + sha
            updated = re.sub(rf"^{name}=.+$", f"{name}={image}", updated, flags=re.MULTILINE)
        image_values(updated)
        write_env(path, updated)
        try:
            compose("pull", "bot", "web")
            # Single polling consumer: old bot must exit before new bot starts.
            compose("stop", "bot")
            compose("up", "-d", "--no-deps", "bot", "web")
            wait_healthy()
        except (subprocess.SubprocessError, RuntimeError, OSError) as exc:
            print(
                f"Deployment failed ({type(exc).__name__}); restoring previous application images. DB unchanged.",
                flush=True,
            )
            write_env(path, previous)
            compose("stop", "bot")
            compose("up", "-d", "--no-deps", "bot", "web")
            wait_healthy()
            raise
        print("Deployed", sha)


if __name__ == "__main__":
    deploy(sys.argv[1])
