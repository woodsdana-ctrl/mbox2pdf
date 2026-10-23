from pathlib import Path

from pst2pdf import wordlist
from pst2pdf.model import MessageModel, Participant


def _model(participants) -> MessageModel:
    return MessageModel(
        source=Path("/work/x.eml"),
        folder="Inbox",
        subject="s",
        from_addr="",
        to_addrs="",
        cc_addrs="",
        date=None,
        body_html="",
        participants=participants,
    )


def test_collect_orders_and_dedupes():
    models = [
        _model([Participant("Zoë Ångström", "zoe@example.com"), Participant("alice example", "ALICE@example.com")]),
        _model([Participant("Alice Example", "alice@example.com"), Participant("", "bob@example.com")]),
        _model([Participant("Bob", ""), Participant("Al", "al@example.com"), Participant("", "")]),
    ]
    names, addresses = wordlist.collect(models)
    # Case-insensitive sort and case-insensitive distinctness (first spelling wins).
    assert names == ["Bob", "Zoë Ångström", "alice example"] or names == ["alice example", "Bob", "Zoë Ångström"]
    assert [n.casefold() for n in names] == sorted(n.casefold() for n in names)
    assert "Al" not in names  # under three characters
    assert addresses == ["al@example.com", "alice@example.com", "bob@example.com", "zoe@example.com"]


def test_name_that_is_an_address_lands_with_addresses():
    models = [_model([Participant("carol@example.com", "carol@example.com"), Participant("Dan", "dan@example.com")])]
    names, addresses = wordlist.collect(models)
    assert names == ["Dan"]
    assert addresses == ["carol@example.com", "dan@example.com"]


def test_quotes_and_whitespace_stripped():
    models = [_model([Participant('"Example, Heidi"', "heidi@example.com "), Participant("  Ivan\r\n Example ", "")])]
    names, _ = wordlist.collect(models)
    assert names == ["Example, Heidi", "Heidi Example", "Ivan Example"]


def test_name_forms():
    assert wordlist.name_forms("Jane Doe") == ["Jane Doe"]
    assert wordlist.name_forms("Jane Doe (Finance)") == ["Jane Doe (Finance)", "Jane Doe"]
    assert wordlist.name_forms("Doe, Jane") == ["Doe, Jane", "Jane Doe"]
    assert wordlist.name_forms("Doe, Jane (Finance)") == ["Doe, Jane (Finance)", "Doe, Jane", "Jane Doe"]
    # Not a person written last-name-first: left alone.
    assert wordlist.name_forms("Undisclosed recipients:") == ["Undisclosed recipients:"]
    assert wordlist.name_forms("A, B, C") == ["A, B, C"]


def test_derived_forms_are_deduplicated_and_sorted():
    models = [
        _model([Participant("Doe, Jane (Finance)", "jane@example.com"), Participant("Jane Doe", "jane@example.com")]),
        _model([Participant("jane doe", "")]),
    ]
    names, _ = wordlist.collect(models)
    assert names == ["Doe, Jane", "Doe, Jane (Finance)", "Jane Doe"]


def test_write_is_utf8_crlf_names_then_addresses(tmp_path):
    models = [
        _model([Participant("Zoë Ångström", "zoe@example.com"), Participant("Alice Example", "alice@example.com")]),
    ]
    out = wordlist.write(models, tmp_path)
    assert out == tmp_path / "Redaction word list.txt"
    raw = out.read_bytes()
    assert raw == "Alice Example\r\nZoë Ångström\r\nalice@example.com\r\nzoe@example.com\r\n".encode("utf-8")
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert not raw.startswith(b"\xef\xbb\xbf")


def test_write_empty(tmp_path):
    out = wordlist.write([], tmp_path)
    assert out.read_bytes() == b""
