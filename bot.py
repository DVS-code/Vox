"""
Legacy entrypoint that boots the Vyxen CORE runtime via the Discord adapter.
Discord is treated purely as an I/O surface; cognition runs continuously.
"""

import asyncio
import os
from pathlib import Path
import resource


def _load_env() -> None:
    loaded_paths: list[str] = []
    candidates = [Path(".env"), Path(__file__).resolve().parent / ".env"]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, val = stripped.split("=", 1)
                if key and val and (key not in os.environ or not os.environ.get(key)):
                    os.environ[key] = val.strip().strip('"').strip("'")
                loaded_paths.append(str(candidate))
        except Exception:
            continue
    print(
        f"[ENV] cwd={os.getcwd()} loaded_env={bool(loaded_paths)} token_present={bool(os.getenv('DISCORD_TOKEN'))}"
    )


_load_env()

from discord_adapter import main  # noqa: E402


def _log_mem(label: str) -> None:
    mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"[MEMDBG] {label}: {mb:.1f} MB", flush=True)


if __name__ == "__main__":
    _log_mem("pre-run")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # PM2 restarts typically send SIGINT which surfaces as KeyboardInterrupt.
        # Exit cleanly to avoid noisy tracebacks in logs.
        print("[SHUTDOWN] Received interrupt; exiting cleanly.", flush=True)
