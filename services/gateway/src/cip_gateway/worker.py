"""Worker entrypoint.

Deliberately a stub that refuses rather than a process that starts and silently consumes
nothing. A worker that boots without a broker, registers no handlers, and reports itself
healthy is the worst possible failure mode: the queue fills, nothing is processed, and every
signal says the system is fine.
"""

from __future__ import annotations

__all__ = ["main"]


def main() -> None:  # pragma: no cover - process entrypoint
    raise SystemExit(
        "The worker entrypoint requires a configured broker and registered job handlers; "
        "wire them in the composition root before running this image with the 'worker' role."
    )


if __name__ == "__main__":  # pragma: no cover
    main()
