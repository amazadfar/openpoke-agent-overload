"""Test-only helpers: fake Gmail backend, scripted LLM, state reset.

Nothing in production imports this package except the fake-Gmail switch in
``services/gmail/client.py``, which only does so when OPENPOKE_FAKE_GMAIL=1.
"""
