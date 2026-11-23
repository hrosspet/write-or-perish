"""The rules of test_prompt_entries_private.py on PostgreSQL: the
listings' own query for the top of a thread, the cascade, the page
cache's queries and the script's locked batches run there as on sqlite.

Runs only against a throwaway Postgres database named by
LOORE_TEST_POSTGRES_URL (it creates and drops every table in it);
skipped otherwise, like test_payments_postgres.py.
"""
import os

import pytest

PG_URL = os.environ.get("LOORE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="needs LOORE_TEST_POSTGRES_URL (a throwaway database)")

from backend.tests import test_prompt_entries_private as _rules  # noqa: E402
from backend.tests.test_prompt_entries_private import (  # noqa: E402,F401
    fake_redis,
    test_a_typed_new_thread_has_a_private_prompt_entry,
    test_a_recorded_new_thread_has_a_private_prompt_entry,
    test_raising_a_threads_level_leaves_its_prompt_entries_private,
    test_lowering_a_threads_level_applies_to_its_prompt_entries,
    test_the_owner_sets_the_prompt_entry_public_and_private_again,
    test_a_public_thread_with_a_private_prompt_root_is_listed_once_from_its_message,
    test_what_counts_as_the_top_of_a_thread,
    test_a_private_thread_is_listed_nowhere,
    test_a_deleted_message_and_a_paused_account_leave_the_listings,
    test_a_rename_keeps_the_old_handle_for_such_a_thread,
    test_two_threads_with_the_same_title_get_their_own_permalinks,
    test_a_listing_page_makes_as_many_queries_for_either_kind_of_top,
    test_an_edit_of_the_message_drops_the_threads_cached_pages,
    test_a_reply_and_a_takedown_drop_the_threads_cached_pages,
    test_the_many_node_and_account_drops_reach_the_messages_pages,
    test_the_owner_sees_her_thread_as_before,
    test_lowering_a_threads_level_lowers_a_public_prompt_entry_inside_it,
    test_a_visitor_reads_the_message_inside_its_thread,
    test_another_member_reads_the_message_inside_its_thread,
    test_the_message_shows_inside_another_persons_public_thread,
    test_only_a_prompt_entry_is_passed_through_and_only_to_its_owners_nodes,
    test_the_thread_page_makes_as_many_queries_with_a_prompt_entry_in_it,
    test_the_script_reports_without_writing,
    test_the_script_sets_them_private_and_a_second_run_finds_nothing,
    test_the_script_moves_a_roots_permalink_to_the_message,
    test_the_script_leaves_entries_changed_after_the_given_time,
)


@pytest.fixture
def app(monkeypatch):
    yield from _rules.build_app(monkeypatch, PG_URL)
