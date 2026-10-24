"""Input checks shared by the API and the email worker (standard library only)."""
import re

# Deliberately strict: letters, digits and _ % + - in the name part (dots only between pieces),
# a real domain, and an alphabetic ending. No commas, spaces, quotes or brackets, so one field can
# never mean "several recipients" or confuse a mail server. (Rust-regex compatible: no look-arounds.)
EMAIL_PATTERN = (r"^[A-Za-z0-9_%+\-]+(?:\.[A-Za-z0-9_%+\-]+)*@"
                 r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}$")
_EMAIL_RE = re.compile(EMAIL_PATTERN)


def is_valid_email(value) -> bool:
    return isinstance(value, str) and len(value) <= 254 and _EMAIL_RE.fullmatch(value) is not None
