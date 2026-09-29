"""Compatibility CLI for the safe in-process retention service."""

import logging

from app.retention_service import run_retention


logger = logging.getLogger(__name__)


def main():
    result = run_retention(trigger="standalone")
    logger.info(
        "Retention finished with outcome=%s errors=%s",
        result.outcome,
        result.error_count,
    )
    return 0 if result.outcome == "success" else 1


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    raise SystemExit(main())
