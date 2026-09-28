from agentmemory.secrets_scan import scan_for_secrets


def test_empty_and_none_text():
    assert scan_for_secrets("") == []
    assert scan_for_secrets(None) == []


def test_ordinary_text_no_false_positive():
    text = "Fixed the parse_ts bug and rolled it out to all four agents today."
    assert scan_for_secrets(text) == []


def test_openai_key_detected_and_redacted():
    text = "set OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456"
    matches = scan_for_secrets(text)
    names = [m.pattern_name for m in matches]
    assert "openai_key" in names
    for m in matches:
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in m.redacted


def test_github_token_detected():
    text = "the deploy key is ghp_1234567890abcdef1234567890abcdef1234"
    matches = scan_for_secrets(text)
    assert any(m.pattern_name == "github_token" for m in matches)


def test_aws_access_key_detected():
    text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
    matches = scan_for_secrets(text)
    assert any(m.pattern_name == "aws_access_key" for m in matches)


def test_private_key_block_detected():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----"
    matches = scan_for_secrets(text)
    assert any(m.pattern_name == "private_key_block" for m in matches)


def test_generic_password_label_detected():
    text = "mail password: hunter2fallback in the config"
    matches = scan_for_secrets(text)
    assert any(m.pattern_name == "generic_password" for m in matches)


def test_generic_api_key_label_variants():
    for label in ("api_key", "api-key", "secret", "token", "bearer"):
        text = f"{label}: abcdef123456"
        matches = scan_for_secrets(text)
        assert matches, f"expected a match for label {label!r}"


def test_placeholder_values_do_not_trigger():
    for placeholder in ("changeme", "REDACTED", "not_set", "<redacted>", "TBD"):
        text = f"password: {placeholder}"
        assert scan_for_secrets(text) == [], f"placeholder {placeholder!r} should not match"


def test_short_generic_value_does_not_trigger():
    # Under the 6-char minimum -- avoids firing on "key: no" style text.
    assert scan_for_secrets("key: no") == []


def test_redaction_never_exposes_short_values_fully():
    matches = scan_for_secrets("token: abcdef")
    assert matches
    for m in matches:
        assert m.redacted == "*" * len("abcdef") or "…" in m.redacted


def test_multiple_matches_in_one_block():
    text = (
        "old notes: OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n"
        "also AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE somewhere in the same handoff"
    )
    matches = scan_for_secrets(text)
    names = {m.pattern_name for m in matches}
    assert "openai_key" in names
    assert "aws_access_key" in names
