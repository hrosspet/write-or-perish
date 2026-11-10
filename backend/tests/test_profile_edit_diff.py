"""#421, option (c) (Peter, voice review 2026-11-10): what a rebuild
merges for an older round of the user's profile edits
(utils/profile_edit_diff.older_edit). Pure text; every profile text here
is invented."""
from backend.utils.profile_edit_diff import older_edit

BASE = ("### SURFACE MAP\n"
        "Likes long walks.\n"
        "WRONG GUESS\n"
        "\n"
        "### SHADOW MAP\n"
        "Avoids crowds.")
CELLO = "Plays the cello on weekends."


def _lines(*lines):
    return "\n".join(lines)


# The first round corrects WRONG GUESS and adds a sentence; the update
# after it rewords the first line and the correction and keeps the rest.
FIRST = _lines("### SURFACE MAP", "Likes long walks.", "RIGHT GUESS", "",
               "### SHADOW MAP", "Avoids crowds.", CELLO)
UPDATE = _lines("### SURFACE MAP", "Likes long walks in the hills.",
                "Right guess, in the update's words.", "",
                "### SHADOW MAP", "Avoids crowds.", CELLO)


def test_a_sentence_added_then_deleted_stays_deleted():
    """The second round deletes the sentence the first added: the first
    round is merged without it, and its correction is kept."""
    second = UPDATE.replace(f"\n{CELLO}", "")
    result = older_edit(BASE, FIRST, [(UPDATE, second)])
    assert result.text == _lines("### SURFACE MAP", "Likes long walks.",
                                 "RIGHT GUESS", "", "### SHADOW MAP",
                                 "Avoids crowds.")
    assert CELLO not in result.text


def test_a_round_whose_only_change_was_deleted_again_is_not_merged():
    first = f"{BASE}\n{CELLO}"
    update = first
    second = BASE
    assert older_edit(BASE, first, [(update, second)]).text is None


def test_an_older_correction_on_untouched_lines_survives():
    """#421's case: the update softened the first correction and the
    second round changed another line. The first round is merged with its
    correction; the line the second round changed is left out."""
    second = UPDATE.replace("Avoids crowds.", "Avoids crowds, but not concerts.")
    result = older_edit(BASE, FIRST, [(UPDATE, second)])
    assert "RIGHT GUESS" in result.text and CELLO in result.text
    assert "Avoids crowds." not in result.text
    assert result.kept == result.changes


def test_a_line_next_to_a_newer_change_is_kept():
    """A newer round that adds a line right after the correction does not
    touch it."""
    second = UPDATE.replace("Right guess, in the update's words.",
                            "Right guess, in the update's words.\nA NEW LINE")
    result = older_edit(BASE, FIRST, [(UPDATE, second)])
    assert result.text == FIRST


def test_three_rounds():
    """Round 1 corrects a line and adds a sentence, round 2 deletes the
    sentence, round 3 rewrites a third line. Round 1 is merged with its
    correction only, round 2 (a deletion alone) without the line round 3
    rewrote."""
    second = UPDATE.replace(f"\n{CELLO}", "")
    update2 = second.replace("in the hills", "in the hills at dawn")
    third = update2.replace("Avoids crowds.", "Seeks out small gatherings.")
    first_merged = older_edit(BASE, FIRST, [(UPDATE, second),
                                            (update2, third)])
    assert first_merged.text == _lines("### SURFACE MAP", "Likes long walks.",
                                       "RIGHT GUESS", "", "### SHADOW MAP")
    second_merged = older_edit(UPDATE, second, [(update2, third)])
    assert second_merged.text is not None
    assert CELLO not in second_merged.text
    assert "Avoids crowds." not in second_merged.text
    assert "Right guess, in the update's words." in second_merged.text


def test_a_round_that_only_deletes_is_merged_while_its_deletion_holds():
    first = BASE.replace("\nWRONG GUESS", "")
    update = first.replace("Likes long walks.", "Likes long walks a lot.")
    second = update.replace("Avoids crowds.", "Avoids large crowds.")
    result = older_edit(BASE, first, [(update, second)])
    assert result.text is not None and "WRONG GUESS" not in result.text
    assert "Avoids crowds." not in result.text


def test_a_deletion_the_user_undid_later_is_not_merged():
    first = BASE.replace("\nWRONG GUESS", "")
    second = first.replace("Likes long walks.",
                           "Likes long walks.\nWRONG GUESS")
    assert older_edit(BASE, first, [(first, second)]).text is None


def test_a_whitespace_only_round_is_not_merged():
    first = BASE.replace("Likes long walks.", "Likes  long walks.  ") + "\n\n"
    second = BASE.replace("Avoids crowds.", "Avoids large crowds.")
    result = older_edit(BASE, first, [(BASE, second)])
    assert result.text is None and result.changes == 0


def test_a_whitespace_only_newer_round_takes_nothing_away():
    second = UPDATE.replace("Avoids crowds.", "Avoids   crowds. ") + "\n\n\n"
    result = older_edit(BASE, FIRST, [(UPDATE, second)])
    assert result.text == FIRST and result.removed == 0


def test_a_sentence_the_update_moved_and_reworded_is_found():
    """The update moved the added sentence to another section and changed
    a word; the second round deleted it there. Its words are close enough
    (SIMILAR_LINE_RATIO) to count as the same line."""
    first = f"{BASE}\n{CELLO}"
    update = _lines("### SURFACE MAP", "Likes long walks.",
                    "Plays the cello at weekends.", "WRONG GUESS", "",
                    "### SHADOW MAP", "Avoids crowds.")
    second = update.replace("Plays the cello at weekends.\n", "")
    assert older_edit(BASE, first, [(update, second)]).text is None


def test_a_newer_rewrite_of_everything_leaves_nothing():
    second = "### SURFACE MAP\nA profile written again from scratch."
    assert older_edit(BASE, FIRST, [(UPDATE, second)]).text is None


def test_a_hand_written_profile_is_all_change():
    """A profile written by hand has no version it edited: every line is
    its own change, kept where no newer round touched it."""
    second = UPDATE.replace(f"\n{CELLO}", "")
    result = older_edit("", FIRST, [(UPDATE, second)])
    assert result.text is not None and CELLO not in result.text
    assert "RIGHT GUESS" in result.text


def test_a_model_line_a_newer_round_deleted_is_left_out():
    """The first round left a line of the model's as it was, and the
    second deleted it: the first round's merge does not bring it back."""
    second = UPDATE.replace("Avoids crowds.\n", "")
    result = older_edit(BASE, FIRST, [(UPDATE, second)])
    assert "Avoids crowds." not in result.text
    assert "RIGHT GUESS" in result.text
