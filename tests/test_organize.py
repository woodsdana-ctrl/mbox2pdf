import hashlib

import pytest

from pst2pdf.model import Options
from pst2pdf.organize import (
    Organized,
    deduplicate,
    normalize_subject,
    organize,
    thread_index_prefix,
)

from conftest import make_model


def opts(**kw) -> Options:
    return Options(request_id="2026-0001", **kw)


def sources(result: list[Organized]) -> list[str]:
    return [str(o.model.source) for o in result]


def thread_ids(result: list[Organized]) -> list[str]:
    return [o.thread_id for o in result]


def expected_thread_id(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def test_dedup_by_message_id_collects_folders():
    models = [
        make_model("Sent Items/1.eml", message_id="m1@x"),
        make_model("Inbox/1.eml", message_id="m1@x"),
        make_model("Inbox/2.eml", message_id="m2@x"),
    ]
    result = organize(models, opts())

    assert len(result) == 2
    kept = next(o for o in result if o.model.message_id == "m1@x")
    # First in sorted-source order wins; both folders are recorded.
    assert str(kept.model.source) == "Inbox/1.eml"
    assert kept.folders == ["Inbox", "Sent Items"]


def test_dedup_by_content_hash_when_no_message_id():
    models = [
        make_model("Inbox/1.eml", content_hash="same"),
        make_model("Archive/1.eml", content_hash="same"),
        make_model("Inbox/2.eml", content_hash="other"),
    ]
    result = organize(models, opts())
    assert len(result) == 2
    kept = next(o for o in result if o.model.content_hash == "same")
    assert str(kept.model.source) == "Archive/1.eml"
    assert kept.folders == ["Archive", "Inbox"]


def test_dedup_does_not_merge_different_ids_with_same_hash():
    models = [
        make_model("Inbox/1.eml", message_id="a@x", content_hash="same"),
        make_model("Inbox/2.eml", message_id="b@x", content_hash="same"),
    ]
    assert len(organize(models, opts())) == 2


def test_dedup_disabled_keeps_every_copy():
    models = [
        make_model("Sent Items/1.eml", message_id="m1@x"),
        make_model("Inbox/1.eml", message_id="m1@x"),
    ]
    result = organize(models, opts(dedup_messages=False))
    assert len(result) == 2
    assert [o.folders for o in result] == [["Inbox"], ["Sent Items"]]


def test_same_folder_duplicate_recorded_once():
    entries = deduplicate(
        [make_model("Inbox/1.eml", message_id="m@x"), make_model("Inbox/2.eml", message_id="m@x")]
    )
    assert len(entries) == 1
    assert entries[0].folders == ["Inbox"]


PURVIEW_A = (
    '<p>Please review <a>the report [https://contoso-my.sharepoint.com/:x:/r/personal/a/report.xlsx]</a> '
    "by Friday.</p><p>Thanks,<br>Alice</p>"
)
PURVIEW_B = (
    '<p>Please review <a>the report [https://nam12.safelinks.protection.outlook.com/?url=https%3A%2F%2F'
    "contoso-my.sharepoint.com%2F%3Ax%3A%2Fr%2Fpersonal%2Fa%2Freport.xlsx&amp;data=05%7C02%7Cbob%40x.org]</a>\n"
    "   by Friday.</p>\n<p>Thanks,<br>Alice</p>"
)


def test_dedup_collapses_purview_copies_that_differ_only_in_urls(caplog):
    models = [
        make_model("Export/1.eml", message_id="m1@x", body_html=PURVIEW_A),
        make_model("Export/2.eml", message_id="m1@x", body_html=PURVIEW_B),
    ]
    with caplog.at_level("WARNING", logger="pst2pdf.organize"):
        result = deduplicate(models)
    assert len(result) == 1 and result[0].copies == 2
    assert caplog.records == []


def test_dedup_keeps_different_messages_that_share_an_id(caplog):
    models = [
        make_model("Inbox/1.eml", message_id="shared@x", body_html="<p>Meeting moved to 3pm.</p>"),
        make_model("Inbox/2.eml", message_id="shared@x", body_html="<p>Jane Doe's IEP is attached.</p>"),
        make_model("Inbox/3.eml", message_id="shared@x", body_html="<p>Meeting  moved to 3pm.</p>"),
    ]
    with caplog.at_level("WARNING", logger="pst2pdf.organize"):
        result = deduplicate(models)
    assert sources(result) == ["Inbox/1.eml", "Inbox/2.eml"]
    assert [o.copies for o in result] == [2, 1]  # 3.eml is a whitespace-only copy of 1.eml
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "shared@x" in message and "Inbox/1.eml" in message and "Inbox/2.eml" in message


def test_body_fingerprint_ignores_tags_urls_and_whitespace():
    from pst2pdf.organize import body_fingerprint

    base = body_fingerprint("<p>Hello <b>world</b> see https://a.example/x?y=1 now</p>")
    assert base == body_fingerprint("Hello world see http://b.example/other/path now")
    assert base == body_fingerprint("  Hello\n\tworld   see    now ")
    assert base == body_fingerprint('<div style="color:red"><span>Hello</span> world see now</div>')
    assert base != body_fingerprint("Hello world see now!")
    assert base != body_fingerprint("Hello world now")


# ---------------------------------------------------------------------------
# Threading
# ---------------------------------------------------------------------------


def test_thread_via_in_reply_to_and_references():
    models = [
        make_model("Inbox/1.eml", message_id="m1@x", date="2024-01-01T10:00:00+00:00"),
        make_model("Inbox/2.eml", message_id="m2@x", in_reply_to="m1@x", date="2024-01-01T11:00:00+00:00"),
        make_model("Inbox/3.eml", message_id="m3@x", references=["m1@x", "m2@x"], date="2024-01-01T12:00:00+00:00"),
        make_model("Inbox/4.eml", message_id="m4@x", subject="Unrelated", participants=["zed@example.com"]),
    ]
    result = organize(models, opts())
    ids = dict(zip(sources(result), thread_ids(result)))

    assert ids["Inbox/1.eml"] == ids["Inbox/2.eml"] == ids["Inbox/3.eml"]
    assert ids["Inbox/4.eml"] != ids["Inbox/1.eml"]
    assert ids["Inbox/1.eml"] == expected_thread_id("m1@x")


def test_thread_via_shared_reference_to_missing_root():
    # Neither message is the root and the root is not in the archive, but
    # they both reference it, so they belong together.
    models = [
        make_model("Inbox/1.eml", message_id="m2@x", references=["root@x"], subject="A", participants=["a@x"]),
        make_model("Inbox/2.eml", message_id="m3@x", references=["root@x"], subject="B", participants=["b@x"]),
    ]
    result = organize(models, opts())
    assert len(set(thread_ids(result))) == 1


def test_thread_via_thread_index_only():
    base = "AQHZ8s5qNfJk0xGp8UeZg3+4kJ"  # 26 chars: root prefix plus change
    models = [
        make_model("Inbox/1.eml", message_id="m1@x", subject="First", participants=["a@x"], thread_index=base + "AAAAAA=="),
        make_model("Inbox/2.eml", message_id="m2@x", subject="Second", participants=["b@x"], thread_index=base + "BBBBBBCCCCCC=="),
        make_model("Inbox/3.eml", message_id="m3@x", subject="Third", participants=["c@x"], thread_index="ZZZZZZZZZZZZZZZZZZZZZZZZZZZZ=="),
    ]
    result = organize(models, opts())
    ids = dict(zip(sources(result), thread_ids(result)))
    assert ids["Inbox/1.eml"] == ids["Inbox/2.eml"]
    assert ids["Inbox/3.eml"] != ids["Inbox/1.eml"]


def test_thread_index_prefix_helper():
    assert thread_index_prefix(None) is None
    assert thread_index_prefix("short") is None
    assert thread_index_prefix(" AQHZ 8s5qNfJk0xGp8UeZg3+4kJ2rTw== ") == "AQHZ8s5qNfJk0xGp8UeZg3"


def test_subject_merge_requires_shared_participant():
    models = [
        make_model("Inbox/1.eml", message_id="m1@x", subject="Budget", participants=["a@x", "b@x"]),
        make_model("Inbox/2.eml", message_id="m2@x", subject="RE: Budget", participants=["b@x", "c@x"]),
        make_model("Inbox/3.eml", message_id="m3@x", subject="Fwd: budget", participants=["z@x"]),
    ]
    result = organize(models, opts())
    ids = dict(zip(sources(result), thread_ids(result)))

    assert ids["Inbox/1.eml"] == ids["Inbox/2.eml"]
    # Same subject, no shared participant: stays separate.
    assert ids["Inbox/3.eml"] != ids["Inbox/1.eml"]


def test_subject_merge_is_transitive_through_shared_participants():
    models = [
        make_model("Inbox/1.eml", message_id="m1@x", subject="Budget", participants=["a@x"]),
        make_model("Inbox/2.eml", message_id="m2@x", subject="Re: Budget", participants=["a@x", "b@x"]),
        make_model("Inbox/3.eml", message_id="m3@x", subject="RE: RE: Budget", participants=["b@x"]),
    ]
    assert len(set(thread_ids(organize(models, opts())))) == 1


def test_empty_subject_never_merges():
    models = [
        make_model("Inbox/1.eml", message_id="m1@x", subject="", participants=["a@x"]),
        make_model("Inbox/2.eml", message_id="m2@x", subject="Re:", participants=["a@x"]),
    ]
    assert len(set(thread_ids(organize(models, opts())))) == 2


def test_normalize_subject():
    assert normalize_subject("RE: Fwd:  FW: aw:  Budget   2027 ") == "budget 2027"
    assert normalize_subject("Re[2]: Budget") == "budget"
    assert normalize_subject("Budget") == "budget"
    assert normalize_subject("") == ""
    assert normalize_subject("Re:") == ""
    # "Rewards" is not a prefix.
    assert normalize_subject("Rewards: program") == "rewards: program"


@pytest.mark.parametrize(
    "subject",
    [
        "Budget",
        "[EXTERNAL] Budget",
        "[EXTERNAL] Re: Budget",
        "Re: [EXTERNAL] Budget",
        "Re: [EXTERNAL] Re: Budget",
        "RE: [EXTERNAL] FW: [EXTERNAL] Budget",
        "Fwd: RE: [EXTERNAL]Budget",
        "[External]  [Encrypted]  Re: Budget",
        "  [EXTERNAL] re:  budget ",
    ],
)
def test_normalize_subject_consumes_bracketed_tags(subject):
    assert normalize_subject(subject) == "budget"


def test_normalize_subject_leaves_long_brackets_and_inner_text():
    # Over 20 characters is not a tag, and brackets after the subject text stay.
    long = "[This is not a short tag at all] Budget"
    assert normalize_subject(long) == long.lower()
    assert normalize_subject("Budget [EXTERNAL]") == "budget [external]"
    assert normalize_subject("Re: Budget [2027]") == "budget [2027]"
    assert normalize_subject("[EXTERNAL]") == ""


def test_external_banner_does_not_split_a_thread():
    models = [
        make_model("Inbox/1.eml", message_id="m1@x", subject="Budget", participants=["a@x", "b@x"]),
        make_model("Inbox/2.eml", message_id="m2@x", subject="[EXTERNAL] Re: Budget", participants=["b@x", "c@x"]),
        make_model("Inbox/3.eml", message_id="m3@x", subject="Re: [EXTERNAL] Re: Budget", participants=["c@x"]),
        make_model("Inbox/4.eml", message_id="m4@x", subject="RE: [EXTERNAL] Budget", participants=["a@x"]),
    ]
    assert len(set(thread_ids(organize(models, opts())))) == 1


def test_thread_id_uses_earliest_message_key():
    models = [
        make_model("Inbox/1.eml", message_id="later@x", in_reply_to="earlier@x", date="2024-02-01T00:00:00+00:00"),
        make_model("Inbox/2.eml", message_id="earlier@x", date="2024-01-01T00:00:00+00:00"),
        make_model("Inbox/3.eml", message_id=None, content_hash="h3", in_reply_to="earlier@x", date=None),
    ]
    result = organize(models, opts())
    assert set(thread_ids(result)) == {expected_thread_id("earlier@x")}


def test_thread_id_falls_back_to_content_hash():
    models = [
        make_model("Inbox/1.eml", message_id=None, content_hash="hash-one", subject="Solo"),
    ]
    assert thread_ids(organize(models, opts())) == [expected_thread_id("hash-one")]


def test_singletons_get_distinct_thread_ids():
    models = [
        make_model("Inbox/1.eml", message_id="m1@x", subject="A", participants=["a@x"]),
        make_model("Inbox/2.eml", message_id="m2@x", subject="B", participants=["b@x"]),
    ]
    assert len(set(thread_ids(organize(models, opts())))) == 2


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def _ordering_fixture():
    # Thread T1 starts Jan 2, thread T2 starts Jan 1 but its second message is
    # the latest of all; a lone undated message; different folders throughout.
    return [
        make_model("Sent Items/1.eml", message_id="t1a@x", date="2024-01-02T00:00:00+00:00", subject="T1", participants=["a@x"]),
        make_model("Inbox/2.eml", message_id="t1b@x", in_reply_to="t1a@x", date="2024-01-03T00:00:00+00:00", subject="T1", participants=["a@x"]),
        make_model("Inbox/3.eml", message_id="t2a@x", date="2024-01-01T00:00:00+00:00", subject="T2", participants=["b@x"]),
        make_model("Archive/4.eml", message_id="t2b@x", in_reply_to="t2a@x", date="2024-01-04T00:00:00+00:00", subject="T2", participants=["b@x"]),
        make_model("Inbox/5.eml", message_id="lone@x", date=None, subject="Lone", participants=["c@x"]),
    ]


def test_thread_order():
    result = organize(_ordering_fixture(), opts(order="thread"))
    assert sources(result) == [
        "Inbox/3.eml",  # T2 (earliest thread start)
        "Archive/4.eml",
        "Sent Items/1.eml",  # T1
        "Inbox/2.eml",
        "Inbox/5.eml",  # undated last
    ]


def test_thread_order_is_default():
    assert sources(organize(_ordering_fixture(), opts())) == sources(
        organize(_ordering_fixture(), opts(order="thread"))
    )


def test_chrono_order():
    result = organize(_ordering_fixture(), opts(order="chrono"))
    assert sources(result) == [
        "Inbox/3.eml",
        "Sent Items/1.eml",
        "Inbox/2.eml",
        "Archive/4.eml",
        "Inbox/5.eml",
    ]


def test_folder_order():
    result = organize(_ordering_fixture(), opts(order="folder"))
    assert sources(result) == [
        "Archive/4.eml",
        "Inbox/3.eml",
        "Inbox/2.eml",
        "Inbox/5.eml",  # undated last within its folder
        "Sent Items/1.eml",
    ]


def test_undated_message_inside_thread_sorts_last_in_thread():
    models = [
        make_model("Inbox/1.eml", message_id="a@x", date=None, subject="T", participants=["a@x"]),
        make_model("Inbox/2.eml", message_id="b@x", in_reply_to="a@x", date="2024-01-01T00:00:00+00:00", subject="T", participants=["a@x"]),
        make_model("Inbox/3.eml", message_id="c@x", subject="Other", date="2024-02-01T00:00:00+00:00", participants=["z@x"]),
    ]
    result = organize(models, opts(order="thread"))
    assert sources(result) == ["Inbox/2.eml", "Inbox/1.eml", "Inbox/3.eml"]


def test_fully_undated_thread_sorts_after_dated_threads():
    models = [
        make_model("Inbox/1.eml", message_id="a@x", date=None, subject="A", participants=["a@x"]),
        make_model("Inbox/2.eml", message_id="b@x", date="2024-01-01T00:00:00+00:00", subject="B", participants=["b@x"]),
    ]
    assert sources(organize(models, opts(order="thread"))) == ["Inbox/2.eml", "Inbox/1.eml"]


def test_date_ties_break_on_source_path():
    models = [
        make_model("Inbox/b.eml", message_id="b@x", subject="B", participants=["b@x"]),
        make_model("Inbox/a.eml", message_id="a@x", subject="A", participants=["a@x"]),
    ]
    assert sources(organize(models, opts(order="chrono"))) == ["Inbox/a.eml", "Inbox/b.eml"]


def test_mixed_naive_and_aware_dates_compare():
    from datetime import datetime

    models = [
        make_model("Inbox/1.eml", message_id="a@x", date=datetime(2024, 1, 2, 12, 0), subject="A", participants=["a@x"]),
        make_model("Inbox/2.eml", message_id="b@x", date="2024-01-02T06:00:00-08:00", subject="B", participants=["b@x"]),
    ]
    # 06:00-08:00 is 14:00 UTC, after the naive (treated as UTC) 12:00.
    assert sources(organize(models, opts(order="chrono"))) == ["Inbox/1.eml", "Inbox/2.eml"]


def test_unknown_order_rejected():
    with pytest.raises(ValueError):
        organize([make_model()], opts(order="random"))


def test_empty_input():
    assert organize([], opts()) == []


def test_organize_is_deterministic_regardless_of_input_order():
    models = _ordering_fixture()
    forward = organize(models, opts())
    backward = organize(list(reversed(models)), opts())
    assert sources(forward) == sources(backward)
    assert thread_ids(forward) == thread_ids(backward)
