"""Honeypot fixture for test_no_llm_provider_urls_outside_gateway.

This file intentionally contains an LLM provider URL in a comment so the
url-guard test can verify that the detection mechanism works correctly.

When the scan path is extended to include tests/fixtures/, this file must
appear in the detected-violations list.

Honeypot marker: # api.openai.com
"""

# This module contains NO runnable code — it is a static fixture only.
