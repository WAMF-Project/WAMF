"""Compatibility CLI for the safe in-process retention service."""

import logging

from app.config_loader import load_runtime_config
from app.db import ensure_schema
from app.retention_service import run_retention
from wamf_paths import get_database_path


logger = logging.getLogger(__name__)


def main():
    try:
        config = load_runtime_config()
        ensure_schema(get_database_path(config))
    except Exception:
        logger.exception("Standalone retention schema initialization failed")
        return 1

    result = run_retention(trigger="standalone", config=config)
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
