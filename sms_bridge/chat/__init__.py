"""Chat platform adapters.

An adapter reports capability and executes instructions. It never decides
policy - in particular it never decides what happens when the secure channel
is unreachable. That decision lives in sms_bridge.delivery so it exists once.
"""
