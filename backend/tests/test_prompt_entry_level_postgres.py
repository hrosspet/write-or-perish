"""The rules of test_prompt_entry_level.py on PostgreSQL: the listings'
own query for the top of a thread and its card, the cascade and the page
cache's queries run there as on sqlite.

Runs only against a throwaway Postgres database named by
LOORE_TEST_POSTGRES_URL (it creates and drops every table in it);
skipped otherwise, like test_payments_postgres.py.
"""
import os

import pytest

PG_URL = os.environ.get("LOORE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="needs LOORE_TEST_POSTGRES_URL (a throwaway database)")

from backend.tests import test_prompt_entry_level as _rules  # noqa: E402
from backend.tests.test_prompt_entry_level import (  # noqa: E402,F401
    fake_redis,
    test_a_typed_new_thread_has_a_public_prompt_entry_only_under_a_public_default,
    test_a_recorded_new_thread_has_a_public_prompt_entry_only_under_a_public_default,
    test_raising_a_threads_level_applies_to_its_prompt_entries,
    test_lowering_a_threads_level_applies_to_its_prompt_entries,
    test_lowering_a_threads_level_lowers_a_public_prompt_entry_inside_it,
    test_the_middle_level_applies_to_every_prompt_entry_below,
    test_the_owner_sets_the_prompt_entry_public_and_private_again,
    test_a_public_thread_with_a_private_prompt_root_is_listed_once_from_its_message,
    test_what_counts_as_the_top_of_a_thread,
    test_a_thread_started_with_the_default_public_is_listed_by_its_message,
    test_a_public_prompt_root_is_listed_only_with_a_public_entry_of_its_owner,
    test_the_card_is_the_owners_first_living_public_entry_under_the_root,
    test_only_bare_prompt_roots_leave_the_listings,
    test_a_private_thread_is_listed_nowhere,
    test_a_deleted_message_and_a_paused_account_leave_the_listings,
    test_a_rename_keeps_the_old_handle_for_such_a_thread,
    test_two_threads_with_the_same_title_get_their_own_permalinks,
    test_a_listing_page_makes_as_many_queries_for_every_kind_of_top,
    test_an_edit_of_the_message_drops_the_threads_cached_pages,
    test_a_reply_and_a_takedown_drop_the_threads_cached_pages,
    test_an_edit_of_a_card_drops_the_listings_of_its_public_root,
    test_the_many_node_and_account_drops_reach_the_messages_pages,
    test_the_owner_sees_her_thread_as_before,
    test_an_uploaded_public_thread_gets_its_address_with_its_transcript,
    test_an_uploaded_thread_under_the_default_public_is_listed_with_its_transcript,
    test_a_failed_transcription_under_the_default_public_lists_nothing,
    test_a_failed_transcription_leaves_no_address_and_a_retry_gives_it,
    test_an_empty_transcript_leaves_no_address,
    test_an_upload_set_private_or_deleted_meanwhile_gets_no_address,
    test_only_the_first_entry_under_a_text_root_gets_the_address,
    test_a_visitor_reads_the_message_inside_its_thread,
    test_another_member_reads_the_message_inside_its_thread,
    test_the_message_shows_inside_another_persons_public_thread,
    test_only_a_prompt_entry_is_passed_through_and_only_to_its_owners_nodes,
    test_the_thread_page_makes_as_many_queries_with_a_prompt_entry_in_it,
    test_setting_a_stored_entry_public_lists_nothing,
    test_an_older_client_that_records_a_public_thread_gets_the_permalink,
    test_a_public_prompt_root_no_listing_shows_is_in_its_owners_log,
    test_the_log_keeps_threads_with_a_private_prompt_root,
    test_a_set_public_message_under_a_bare_public_root_lists_it_by_id,
    test_a_private_message_under_a_public_default_keeps_the_thread_private,
)


@pytest.fixture
def app(monkeypatch):
    yield from _rules.build_app(monkeypatch, PG_URL)


def test_two_uploads_whose_transcripts_arrive_together_get_different_addresses(
        app, monkeypatch):
    """Two uploaded public threads of one account, the same first line,
    their transcripts stored at the same moment in two connections: the
    account's lock makes the second choose after the first has
    committed."""
    import threading
    import time
    from backend.extensions import db
    from backend.models import Node
    from backend.utils import slugs

    first = _rules._upload(app)
    second = _rules._upload(app)
    ids = [first.id, second.id]
    db.session.remove()
    real = slugs.generate_unique_public_slug
    barrier = threading.Barrier(2, timeout=10)

    def slow(owner_id, content):
        slug = real(owner_id, content)
        time.sleep(0.5)   # the other one would choose the same now
        return slug
    monkeypatch.setattr(slugs, "generate_unique_public_slug", slow)

    def store(node_id):
        with app.app_context():
            node = db.session.get(Node, node_id)
            barrier.wait()
            slugs.slug_transcribed_thread(node, "The same first line")
            db.session.commit()
            db.session.remove()
    threads = [threading.Thread(target=store, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    got = sorted(_rules._get(i).public_slug for i in ids)
    assert got == ["the-same-first-line", "the-same-first-line-2"]
