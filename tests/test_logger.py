"""Logger helper smoke test."""

import logging

from app.utils.logger import get_logger


def test_logger_returns_instance():
    log = get_logger("tests")
    assert isinstance(log, logging.Logger)
    log.info("hello tests")
