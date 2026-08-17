"""Test doubles shared across the suite.

Nothing here talks to a network. ``fake_google`` duck-types the synchronous
google-api-python-client surface; ``asc`` builds throwaway App Store Connect
credentials and ``httpx.MockTransport`` handlers; ``gateways`` fakes the two
cross-store adapter seams.
"""
