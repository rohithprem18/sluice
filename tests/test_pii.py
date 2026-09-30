from sluice.pii import Redactor, StreamRestorer, luhn_ok, restore, verhoeff_ok

R = Redactor()


def test_detects_common_pii():
    text = (
        "Mail a.b@corp.io, card 4111 1111 1111 1111, SSN 123-45-6789, PAN ABCPE1234F, "
        "phone +1 415 555 0134, key sk-abcdefghijklmnopqrstuvwxyz123456, ip 10.0.0.12"
    )
    masked, red = R.redact(text)
    for kind in ("EMAIL", "CARD", "SSN", "PAN", "PHONE", "SECRET", "IP"):
        assert red.counts.get(kind) == 1, kind
    for original in ("a.b@corp.io", "4111 1111 1111 1111", "123-45-6789", "ABCPE1234F", "sk-abcdefghij"):
        assert original not in masked


def test_checksums_prevent_false_positives():
    assert luhn_ok("4111111111111111") and not luhn_ok("4111111111111112")
    assert verhoeff_ok("234123412346") and not verhoeff_ok("234123412345")
    masked, red = R.redact("Order 4111 1111 1111 1112 shipped")  # fails Luhn: not a card
    assert "CARD" not in red.counts


def test_aadhaar_uses_verhoeff():
    _, red = R.redact("Aadhaar 2341 2341 2346")
    assert red.counts.get("AADHAAR") == 1


def test_same_value_same_placeholder_and_round_trip():
    msgs = [{"role": "user", "content": "Email x@y.com twice: x@y.com"}, {"role": "user", "content": "and x@y.com"}]
    out, red = R.redact_messages(msgs)
    assert out[0]["content"] == "Email <EMAIL_1> twice: <EMAIL_1>"
    assert out[1]["content"] == "and <EMAIL_1>"
    assert restore("Reply sent to <EMAIL_1>.", red) == "Reply sent to x@y.com."


def test_ordinary_numbers_untouched():
    masked, red = R.redact("We shipped 1200 units in 2024 at 3.5% margin.")
    assert red.total == 0 and masked == "We shipped 1200 units in 2024 at 3.5% margin."


def test_stream_restorer_handles_split_placeholders():
    _, red = R.redact("contact x@y.com")
    sr = StreamRestorer(red)
    out = "".join(sr.feed(p) for p in ["Mail <EM", "AIL_", "1> now"]) + sr.flush()
    assert out == "Mail x@y.com now"
