"""Spoken warning text.

Devanagari rather than Roman script: Timbre with ``language="hi-IN"`` pronounces
it correctly, whereas transliterated Hinglish is read as English phonemes.
"""

from __future__ import annotations

WARNING_HI = (
    "सावधान। यह स्कैम हो सकता है। कोई ओटीपी या पैसे न भेजें। "
    "फोन काट दीजिए और अपने परिवार को बताइए।"
)

WARNING_EN = (
    "Warning. This may be a scam. Do not share any OTP or send money. "
    "Hang up and tell your family."
)

# Shorter variant for a second alert on the same call, so the demo does not
# repeat the same sentence verbatim.
WARNING_HI_REPEAT = "यह स्कैम है। फोन काट दीजिए और किसी को पैसे न भेजें।"
