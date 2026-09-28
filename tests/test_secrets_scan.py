"""Tests for secrets_scan.py.

Real-shaped test fixtures for provider-specific patterns (Discord, etc.)
are built at test-run time (see _fake_discord_token below) rather than
committed as literals -- GitHub push protection's secret scanner flags a
literal token-shaped string regardless of whether it's an actual live
credential, confirmed directly 2026-09-28 when a synthetic-but-literal
Discord token shape still got blocked on push. None of the values in this
file are real credentials.
"""
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


def _fake_discord_token(middle_len: int) -> str:
    """Build a token-shaped-but-fake string at test-run time rather than as
    a literal in the source. A literal token-shaped string here trips
    GitHub push protection's secret scanner regardless of whether it's a
    real credential (confirmed directly, 2026-09-28 -- see the note at the
    top of this test module), since it pattern-matches on shape, not on
    whether the value is actually live. Constructing it at runtime means
    the committed diff itself has no matching literal to flag."""
    first = "N" + "z" * 23
    middle = "AbCdEfG"[:middle_len]
    last = "Z" * 27
    return f"{first}.{middle}.{last}"


def test_discord_bot_token_detected_six_char_middle():
    # Caught in a backward-adversarial self-review, 2026-09-28: the first
    # version of this pattern required an exact 6-char middle segment, but
    # Discord's real tokens vary 6-7 chars there -- this pattern only
    # covers the 6-char case; see the 7-char test below.
    text = f"DISCORD_BOT_TOKEN: {_fake_discord_token(6)}"
    matches = scan_for_secrets(text)
    assert any(m.pattern_name == "discord_bot_token" for m in matches)


def test_discord_bot_token_detected_seven_char_middle():
    # Same real bug as above, seven-char middle segment -- the case that
    # was silently missed entirely (by both the provider pattern AND the
    # generic label fallback) before this fix.
    text = f"DISCORD_BOT_TOKEN: {_fake_discord_token(7)}"
    matches = scan_for_secrets(text)
    assert any(m.pattern_name == "discord_bot_token" for m in matches)


def test_generic_label_matches_screaming_snake_case_env_var_name():
    # Caught in the same self-review: a plain \b(...)\b does not match
    # "TOKEN" inside "DISCORD_BOT_TOKEN" because underscore counts as a
    # word character in regex, so there's no boundary between "_" and "T".
    # This is a very common real shape (SCREAMING_SNAKE_CASE env var names)
    # and the original pattern missed it entirely -- would have missed the
    # literal Discord bot token sitting in this machine's own primary
    # config file.
    for text in (
        "DISCORD_BOT_TOKEN=abcdef123456",
        "MAIL_PASSWORD: hunter2fallback",
        "STRIPE_API_KEY = sk_live_abcdefghij",
        "BEARER_TOKEN: abcdef123456",
    ):
        assert scan_for_secrets(text), f"expected a match for {text!r}"


def test_generic_label_mid_word_still_does_not_match():
    # The underscore-boundary fix must not regress the existing
    # mid-word-without-separator false-positive guard.
    assert scan_for_secrets("atoken: abcdef123456") == []
    assert scan_for_secrets("mytokenvalue is fine text") == []


def test_multiple_matches_in_one_block():
    text = (
        "old notes: OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n"
        "also AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE somewhere in the same handoff"
    )
    matches = scan_for_secrets(text)
    names = {m.pattern_name for m in matches}
    assert "openai_key" in names
    assert "aws_access_key" in names
