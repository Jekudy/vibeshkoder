from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("nickname", "@nickname"),
        ("@Nick_Name", "@nick_name"),
        ("t.me/Nickname", "@nickname"),
        ("https://t.me/Nickname/", "@nickname"),
        ("https://t.me/Nickname/?start=questionnaire", "@nickname"),
        ("  @Nickname  ", "@nickname"),
    ],
)
def test_normalize_referral_username_accepts_supported_forms(
    raw: str,
    expected: str,
) -> None:
    from bot.services.referral_username import normalize_referral_username

    assert normalize_referral_username(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "@",
        "nick name",
        "узнал от @nickname",
        "https://example.com/nickname",
        "https://t.me/nickname/extra",
        "https://t.me/+invite",
        "name!",
        "abcd",
        "a" * 33,
    ],
)
def test_normalize_referral_username_rejects_invalid_forms(raw: str) -> None:
    from bot.services.referral_username import InvalidReferralUsername
    from bot.services.referral_username import normalize_referral_username

    with pytest.raises(InvalidReferralUsername):
        normalize_referral_username(raw)
