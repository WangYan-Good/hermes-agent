"""Codex discovery must not silently lose live models to response decoding."""

import unittest
from unittest.mock import patch

import httpx

from hermes_cli import codex_models


class TestCodexCatalogEncoding(unittest.TestCase):
    def test_catalog_requests_uncompressed_response(self):
        """Exercise real HTTPX header merging and decoding via a mock transport."""
        for account_id in (None, "test-account"):
            with self.subTest(account_id=account_id):
                requests = []

                def serve(request):
                    requests.append(request)
                    if request.headers.get("accept-encoding") != "identity":
                        # Reproduce decoder failure on negotiated compressed content.
                        return httpx.Response(
                            200, headers={"Content-Encoding": "gzip"},
                            content=b"invalid gzip",
                        )
                    return httpx.Response(200, json={"models": [
                        {"slug": "new-live-model", "priority": 1},
                        {"slug": "hidden-model", "visibility": "hidden"},
                    ]})

                with (
                    patch.object(codex_models, "_extract_chatgpt_account_id", return_value=account_id),
                    patch("hermes_cli.auth.oauth_httpx_proxy_kwargs", return_value={}),
                    httpx.Client(transport=httpx.MockTransport(serve)) as client,
                    patch.object(httpx, "get", client.get),
                ):
                    models = codex_models._fetch_models_from_api("test-token")

                self.assertEqual(models, ["new-live-model"])
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0].headers["authorization"], "Bearer test-token")
                self.assertEqual(requests[0].headers.get("chatgpt-account-id"), account_id)

    def test_catalog_http_failure_still_allows_fallback(self):
        for status in (401, 429, 500):
            with (
                self.subTest(status=status),
                patch("hermes_cli.auth.oauth_httpx_proxy_kwargs", return_value={}),
                httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(status)
                )) as client,
                patch.object(httpx, "get", client.get),
            ):
                self.assertEqual(codex_models._fetch_models_from_api("test-token"), [])
