"""§10.2 step 1: the crop's embedding, and the name that joins it back.

The value function in `REFERENCE_SCORING_REVIEW.md` §10.1 asks one question of
every reference photo: does it make a REAL confirmed crop of that button rank
#1?  Answering it needs the crop's CLIP vector, which the service computes at
match time and throws away — and needs to know WHICH crop each vector belongs
to, because the label (the confirmed slogan and year) lives in confirm_log,
keyed on job_id + crop_num.

So the two halves tested here are one mechanism: the sidecar carries the
vectors, the staged name carries the key, and a mismatch between them is the
whole measurement lost.  The nastiest failure available is the quiet one —
appending the crop number to a name whose lot id is parsed to the extension
would make every crop of one photo its own "lot" and switch off the same-lot
collapse without a word.

Run: python tests/run_crop_vectors_tests.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from ebayscout import crop_vectors as cv


# --- the kill switch ------------------------------------------------------------

def test_sidecars_are_written_by_default_and_the_switch_turns_them_off():
    """Default ON: a lot that is not recorded is a lot the library can never be
    measured on, and the cost is 40KB beside a 120KB label JPEG."""
    for raw, want in (("", False), ("0", False), ("false", False),
                      ("1", True), ("yes", True)):
        os.environ["BUTTONMATCHER_CROP_VECTORS"] = raw
        assert cv.vectors_enabled() is want, raw
    del os.environ["BUTTONMATCHER_CROP_VECTORS"]
    assert cv.vectors_enabled() is True


# --- the lot key ----------------------------------------------------------------

def test_a_lot_key_can_never_break_out_of_a_blob_name():
    """A key is pasted into both a blob path and a staged crop name, so a slash
    or a dot in it would move the object or eat the extension."""
    k = cv.safe_key("C0123/ABC.1757 964.123456")
    assert "/" not in k and "." not in k and " " not in k
    assert k == "C0123-ABC-1757-964-123456"


def test_a_lot_key_cannot_produce_the_field_separator():
    """Fields are separated by ``__``; an id that could contain one would make
    the name ambiguous.  Underscores fold to dashes for exactly that reason."""
    assert "__" not in cv.safe_key("job_id_with_underscores")


def test_the_slash_lane_key_is_the_id_the_staged_name_already_used():
    """channel-thread has identified a slash lot since the review queue learned
    to collapse one photo's crops; reusing it means the sidecar and the staged
    blob agree on the key with no second convention to keep in step."""
    assert cv.lot_key_from_thread("C123", "1757964.123456") == "C123-1757964-123456"


def test_the_sidecar_blob_sits_beside_the_label_sidecar():
    assert cv.vectors_blob_name("job-7") == "pipeline/embeddings/job-7.npz"
    assert cv.vectors_blob_name("a/b") == "pipeline/embeddings/a-b.npz"


# --- the staged name: build ------------------------------------------------------

def test_a_staged_name_carries_the_lot_and_the_crop_number():
    n = cv.staged_crop_name(1757964000123, lot="C1-T1", crop_num=7)
    assert n == "1757964000123__lot-C1-T1__crop-7.jpg"


def test_the_timestamp_stays_first_so_newest_first_sorting_is_unchanged():
    """The queue sorts staged crops by name to get newest-first.  Moving the
    timestamp would silently reverse which crops a session even looks at."""
    names = [cv.staged_crop_name(t, lot="L", crop_num=1)
             for t in (1757964000001, 1757964000003, 1757964000002)]
    assert sorted(names, reverse=True)[0].startswith("1757964000003")


def test_an_absent_field_is_omitted_rather_than_written_empty():
    """A name must not claim provenance it does not have: an empty lot id would
    read as a real lot and lump unrelated crops together."""
    assert cv.staged_crop_name(5, lot=None, crop_num=None) == "5.jpg"
    assert cv.staged_crop_name(5, lot="L") == "5__lot-L.jpg"
    assert cv.staged_crop_name(5, crop_num=2) == "5__crop-2.jpg"


def test_crop_zero_is_written_not_dropped():
    """``if crop_num:`` would silently drop crop 0.  The pipeline numbers from 1,
    but a join key that depends on that is a trap for the next caller."""
    assert cv.staged_crop_name(5, crop_num=0) == "5__crop-0.jpg"


# --- the staged name: parse -----------------------------------------------------

def test_the_lot_id_ends_at_the_next_field_not_at_the_extension():
    """THE regression this file exists for.  Parsing the lot to the extension
    would return "C1-T1__crop-7" — a lot id unique per crop, which makes
    collapse_same_lot a no-op and shows the reviewer six crops of one photo.
    Nothing would error; the queue would just quietly get worse."""
    n = "reference/_staging/42/1757964000123__lot-C1-T1__crop-7.jpg"
    assert cv.source_lot(n) == "C1-T1"
    assert cv.source_crop(n) == 7


def test_a_name_from_before_rs04_still_parses_its_lot():
    """Every crop staged before this change is ``<ms>__lot-<id>.jpg``; those are
    the crops in the queue right now."""
    assert cv.source_lot("1757964000123__lot-C1-T1.jpg") == "C1-T1"
    assert cv.source_crop("1757964000123__lot-C1-T1.jpg") is None


def test_an_unkeyed_crop_reads_as_unknown_not_as_a_shared_lot():
    """ebayscout named every staged crop ``<ms>.jpg`` until this change, so the
    queue holds thousands of them.  Reading them as one lot would collapse
    unrelated slogans' crops into one representative."""
    assert cv.source_lot("1757964000123.jpg") is None
    assert cv.source_crop("1757964000123.jpg") is None


def test_a_junk_crop_number_loses_the_join_and_not_the_crop():
    assert cv.source_crop("1__lot-L__crop-abc.jpg") is None
    assert cv.source_lot("1__lot-L__crop-abc.jpg") == "L"


def test_parsing_is_order_independent_within_the_name():
    """Nothing should depend on which marker came first."""
    assert cv.source_lot("1__crop-3__lot-L.jpg") == "L"
    assert cv.source_crop("1__crop-3__lot-L.jpg") == 3


def test_a_round_trip_recovers_both_fields():
    for lot, num in (("C1-T1", 7), ("job-abc", 1), ("x", 128)):
        n = cv.staged_crop_name(1757964000123, lot=lot, crop_num=num)
        assert (cv.source_lot(n), cv.source_crop(n)) == (lot, num)


def test_nothing_raises_on_the_names_a_bucket_actually_holds():
    for n in (None, "", "reference/_staging/42/", "__lot-.jpg", "junk"):
        cv.source_lot(n)
        cv.source_crop(n)


# --- the meta record ------------------------------------------------------------

def test_meta_is_json_serializable_and_names_its_schema():
    m = cv.build_meta(lot_key="job-7", service="ebayscout", command="/pipeline",
                      job_id="job-7", count=13, dim=512)
    assert m["schema"] == cv.SCHEMA
    assert json.loads(json.dumps(m))["count"] == 13


def test_meta_says_whether_the_vectors_came_from_the_match_or_a_backfill():
    """A backfilled crop is re-cut from the ≤800px detection image, not the
    ≤2200px working image the live match used, so the two are not bit-identical.
    The value function is entitled to know which it is reading."""
    assert cv.build_meta(lot_key="j", service="s", command="c")["source"] == "match"
    assert cv.build_meta(lot_key="j", service="s", command="c",
                         source="backfill")["source"] == "backfill"


def test_meta_does_not_duplicate_the_outcome():
    """label_harvest's rule: one durable source of truth per fact.  A confirmed
    slogan copied in here would rot the moment a correction lands."""
    m = cv.build_meta(lot_key="j", service="s", command="c")
    assert m["confirm_join"] == "confirm_log.job_id+crop_num"
    for k in ("slogan", "year", "entry_id", "confirmed"):
        assert k not in m
