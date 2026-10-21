"""A failed reply stores a plain message, never the exception's text
(Peter, 2026-10-20: no raw exception text anywhere). The reply's owner
sees llm_task_error on the web and the iPhone. (LLMCompletionTask.on_failure
stores reply_error_text(exc); the celery glue is stubbed in the harness, so
the class itself is not reachable here.)"""
from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod,
)
from backend.utils.error_privacy import (
    REPLY_FAILED_TEXT, UserMessageError,
)

SECRET = "-".join(["typed", "by", "the", "user"])


def test_unknown_exception_gets_the_fixed_message(app):
    exc = RuntimeError(f"DETAIL: Key (body)=({SECRET}) is bad")
    assert _llm_task_mod.reply_error_text(exc) == REPLY_FAILED_TEXT


def test_messages_written_for_the_user_are_kept(app):
    from backend.llm_providers import (
        ModelUnavailableError, ProviderAccountError, ProviderUnavailableError,
    )
    from backend.utils.llm_nodes import AIUsageRefused
    for exc in (ProviderUnavailableError("Anthropic"),
                ProviderAccountError(), ModelUnavailableError(),
                _llm_task_mod.EmptyTruncatedReplyError(),
                UserMessageError("pick another model"), AIUsageRefused()):
        assert _llm_task_mod.reply_error_text(exc) == str(exc)
        assert str(exc)
