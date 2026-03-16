import logging

from jj_review.bootstrap import configure_logging


def test_configure_logging_uses_warning_by_default() -> None:
    configure_logging(debug=False, configured_level="WARNING")

    assert logging.getLogger().getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("jj_review").getEffectiveLevel() == logging.WARNING


def test_configure_logging_uses_debug_when_requested() -> None:
    configure_logging(debug=True, configured_level="WARNING")

    assert logging.getLogger().getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("jj_review").getEffectiveLevel() == logging.DEBUG
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("asyncio").getEffectiveLevel() == logging.WARNING
