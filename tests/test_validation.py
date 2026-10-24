"""Email address validation. No database needed."""
import pytest

from app.validation import is_valid_email


@pytest.mark.parametrize("address", [
    "ayesha@example.com", "sara.ali+shop@mail.example.co.uk", "a@b.io", "Sara@Example.COM",
    "x_y%z@sub-domain.example.org", "load@example.com"])
def test_real_addresses_are_accepted(address):
    assert is_valid_email(address)


@pytest.mark.parametrize("address", [
    "oylad@8.io0_b!}", "oylad@8.io0_b!},n", "n", "a@b", "a@@b.com", "a b@c.com", ".a@b.com", "a.@b.com",
    "a..b@c.com", "a@-b.com", "a@b-.com", "a@b.c", "user@exa_mple.com", "@b.com", "a@b.com\n",
    "a@b.com,c@d.com", "a@b.com ", "ayesha@example.com>", "<a@b.com>", "", None])
def test_junk_and_multi_recipient_strings_are_rejected(address):
    assert not is_valid_email(address)
