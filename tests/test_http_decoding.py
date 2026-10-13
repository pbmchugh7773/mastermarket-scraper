"""
Regression test for Brotli response decoding (2026-10-13).

simple_local_to_prod.py sends `Accept-Encoding: gzip, deflate, br` from the
Lidl scraper and the Tesco/Dunnes requests fallbacks. urllib3 only decodes
`br` when a Brotli package is installed; without one, `response.text` is the
compressed body read as text. When Lidl's CDN started answering in Brotli,
every Lidl page came back as ~39 KB of binary — no <title>, no JSON-LD, no
price — and the store silently went from 13 prices per scrape to 0
(last Lidl price: 2026-09-06).

Ensures the environment built from requirements.txt can decode what the
scrapers ask for.
"""

from __future__ import annotations

import unittest

import urllib3.util.request


class BrotliDecodingTests(unittest.TestCase):
    def test_urllib3_can_decode_brotli(self) -> None:
        self.assertIn(
            'br',
            urllib3.util.request.ACCEPT_ENCODING,
            "No Brotli decoder installed: responses sent with "
            "'Content-Encoding: br' will not be decompressed. "
            "Install the `brotli` package (requirements.txt).",
        )

    def test_brotli_body_round_trips_through_urllib3(self) -> None:
        import brotli
        from urllib3.response import HTTPResponse
        import io

        html = b'<html><title>ITALIAMO Tiramisu | Lidl</title>\xe2\x82\xac2.99</html>'
        resp = HTTPResponse(
            body=io.BytesIO(brotli.compress(html)),
            headers={'content-encoding': 'br'},
            preload_content=False,
        )
        self.assertEqual(resp.read(decode_content=True), html)


if __name__ == '__main__':
    unittest.main()
