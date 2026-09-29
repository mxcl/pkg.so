import pathlib
import re
import unittest
from urllib.parse import urlparse


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy-pkg-cloudfront.sh"


class CloudFrontDeployTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = DEPLOY_SCRIPT.read_text()

    def test_pkg_owned_origin_is_default(self):
        self.assertIn(
            'origin_domain="${PKG_CF_ORIGIN_DOMAIN:-origin.pkg.so}"',
            self.script,
        )

    def test_csp_allows_adsense_resources(self):
        policy = re.search(r'ContentSecurityPolicy: "([^"]+)"', self.script)[1]
        directives = dict(
            (parts[0], parts[1:])
            for directive in policy.split(";")
            if (parts := directive.split())
        )
        for directive, url in (
            ("script-src", "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js"),
            ("script-src", "https://fundingchoicesmessages.google.com/i/pub.js"),
            ("frame-src", "https://googleads.g.doubleclick.net/pagead/ads"),
            ("frame-src", "https://tpc.googlesyndication.com/safeframe/"),
            ("connect-src", "https://pagead2.googlesyndication.com/pagead/ads"),
            ("img-src", "https://pagead2.googlesyndication.com/pagead/1p-user-list/"),
        ):
            with self.subTest(directive=directive, url=url):
                target = urlparse(url)
                self.assertTrue(any(
                    source.startswith("https://") and (
                        target.hostname == urlparse(source).hostname or
                        (urlparse(source).hostname.startswith("*.") and
                         target.hostname.endswith(urlparse(source).hostname[1:]))
                    )
                    for source in directives.get(directive, directives["default-src"])
                ), f"{directive} blocks {url}")

    def test_flattened_search_behaviors_use_query_string_cache_policy(self):
        for path in (
            "search.json",
            "de/search.json",
            "fr/search.json",
            "ja/search.json",
            "zh-hans/search.json",
        ):
            self.assertIn(f'PathPattern: "{path}"', self.script)

        for old_path in (
            "pkg/search.json",
            "de/pkg/search.json",
            "fr/pkg/search.json",
            "ja/pkg/search.json",
            "zh-hans/pkg/search.json",
        ):
            self.assertNotIn(f'PathPattern: "{old_path}"', self.script)

        self.assertIn(
            'search_behavior_json="$(behavior_json "${search_cache_policy_id}")"',
            self.script,
        )
        self.assertIn(
            'QueryStrings: {Quantity: 4, Items: ["q", "offset", "limit", "locale"]}',
            self.script,
        )


if __name__ == "__main__":
    unittest.main()
