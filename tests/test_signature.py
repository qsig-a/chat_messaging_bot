"""Twilio-scheme request signing, as used by SignalWire's LaML API.

This is the only authentication on the webhook endpoints, so it gets a known
external vector rather than a self-computed one.
"""

from sms_bridge.signalwire import valid_signature

TWILIO_URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
TWILIO_PARAMS = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+14158675309",
    "Digits": "1234",
    "From": "+14158675309",
    "To": "+18005551212",
}
TWILIO_TOKEN = "12345"
TWILIO_SIGNATURE = "RSOYDt4T1cUTdK1PDd93/VVr8B8="


def test_accepts_documented_twilio_vector():
    assert valid_signature(
        TWILIO_URL, TWILIO_PARAMS, TWILIO_SIGNATURE, TWILIO_TOKEN
    )


def test_rejects_wrong_token():
    assert not valid_signature(
        TWILIO_URL, TWILIO_PARAMS, TWILIO_SIGNATURE, "not-the-token"
    )


def test_rejects_tampered_url():
    assert not valid_signature(
        TWILIO_URL + "&extra=1", TWILIO_PARAMS, TWILIO_SIGNATURE, TWILIO_TOKEN
    )


def test_rejects_tampered_param():
    tampered = dict(TWILIO_PARAMS, Digits="9999")
    assert not valid_signature(
        TWILIO_URL, tampered, TWILIO_SIGNATURE, TWILIO_TOKEN
    )


def test_rejects_added_param():
    extra = dict(TWILIO_PARAMS, Body="surprise")
    assert not valid_signature(
        TWILIO_URL, extra, TWILIO_SIGNATURE, TWILIO_TOKEN
    )


def test_rejects_empty_signature():
    assert not valid_signature(TWILIO_URL, TWILIO_PARAMS, "", TWILIO_TOKEN)


def test_param_order_does_not_matter():
    """Params are sorted before hashing, so dict order must be irrelevant."""
    reordered = dict(reversed(list(TWILIO_PARAMS.items())))
    assert valid_signature(
        TWILIO_URL, reordered, TWILIO_SIGNATURE, TWILIO_TOKEN
    )


def test_empty_params_signs_url_alone():
    """A status callback with no form body still has to validate."""
    import base64
    import hashlib
    import hmac

    url = "https://sms.example.com/sms/status"
    expected = base64.b64encode(
        hmac.new(b"key", url.encode(), hashlib.sha1).digest()
    ).decode()
    assert valid_signature(url, {}, expected, "key")
