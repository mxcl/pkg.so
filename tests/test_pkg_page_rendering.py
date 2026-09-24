import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load_renderer():
    spec = importlib.util.spec_from_file_location("pkg_pages_for_tests", ROOT / "scripts" / "generate-pkg-pages.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


pkg_pages = load_renderer()


def sshfs_page():
    page = pkg_pages.PackagePage(
        provider="brew",
        name="sshfs",
        summary="File system client based on SSH File Transfer Protocol",
        version="3.7.6",
        package_manager="Homebrew",
        package_manager_url="https://formulae.brew.sh/formula/sshfs",
        executables=[
            {"name": "mount.fuse.sshfs", "kind": "cli"},
            {"name": "mount.sshfs", "kind": "cli"},
            {"name": "sshfs", "kind": "cli"},
        ],
        geiger={
            "level": "blue",
            "confidence": "medium",
            "category": "filesystem",
            "reasons": ["Can mount remote filesystems over SSH."],
            "signals": ["remote filesystem access"],
        },
        install_commands=[
            {
                "platform": "portable",
                "manager": "Automic Vault",
                "command": "sudo av install brew:sshfs",
                "kind": "automic_vault",
                "confidence": 1.0,
                "evidence": "deterministic local package key",
            },
            {
                "platform": "macos",
                "manager": "Homebrew",
                "command": "brew install sshfs",
                "kind": "package_manager",
                "confidence": 1.0,
                "evidence": "local Homebrew formula metadata",
            },
            {
                "platform": "linux",
                "manager": "Debian apt",
                "command": "sudo apt install sshfs",
                "kind": "package_manager",
                "confidence": 0.92,
                "evidence": "Debian stable package indexes",
                "source": {"manager": "debian"},
            },
            {
                "platform": "linux",
                "manager": "Fedora dnf",
                "command": "sudo dnf install fuse-sshfs",
                "kind": "package_manager",
                "confidence": 0.92,
                "evidence": "Fedora package metadata",
                "source": {"manager": "dnf"},
            },
            {
                "platform": "linux",
                "manager": "Nix",
                "command": "nix profile install nixpkgs#sshfs",
                "kind": "package_manager",
                "confidence": 0.92,
                "evidence": "nixpkgs package indexes",
                "source": {"manager": "nix"},
            },
        ],
    )
    page.extra["pkgTaxonomy"] = {"category": "filesystem-tools", "tags": ["ssh", "sftp", "fuse", "filesystem"]}
    return page


class PackagePageRenderingTests(unittest.TestCase):
    def test_catalog_is_canonical_at_the_site_root(self):
        html = pkg_pages.render_index(
            [sshfs_page()],
            [],
            {
                "generated_at": "2026-06-12T00:00:00Z",
                "radioisotope_manifest_count": 0,
                "source_file_count": 1,
            },
        )

        self.assertIn('<link rel="canonical" href="https://pkg.so/">', html)
        self.assertIn('<a class="brand" href="../"', html)
        self.assertIn('href="../#search">Search</a>', html)
        self.assertIn('href="/feed/">Feed</a>', html)
        self.assertIn('href="https://github.com/mxcl/pkg.so">GitHub', html)
        self.assertIn('href="https://mxcl.dev">a mxcl project</a>', html)
        self.assertNotIn("Explore packages", html)
        self.assertNotIn("Data feed", html)
        self.assertIn('id="search" class="pkg-search"', html)
        self.assertNotIn('<link rel="canonical" href="https://pkg.so/pkg/">', html)

    def test_title_and_description_use_install_intent_and_managers(self):
        page = sshfs_page()

        self.assertEqual(
            pkg_pages.package_install_title(page),
            "How to Install sshfs | Homebrew, apt, dnf, Nix",
        )
        description = pkg_pages.meta_description(page)

        self.assertIn("Install sshfs with Homebrew, apt, dnf, Nix.", description)
        self.assertIn("Includes executables", description)

    def test_summary_and_support_coverage_are_metadata_backed(self):
        page = sshfs_page()

        self.assertEqual(
            pkg_pages.plain_package_summary(page),
            "sshfs mounts remote machines over SSH/SFTP and exposes them as local filesystems.",
        )
        self.assertEqual(
            pkg_pages.package_manager_coverage(page),
            [
                ("macOS", ["Homebrew"]),
                ("Debian/Ubuntu", ["apt"]),
                ("Fedora", ["dnf"]),
                ("NixOS", ["Nix"]),
            ],
        )

    def test_faq_schema_is_emitted_from_page_metadata(self):
        page = sshfs_page()
        schema = pkg_pages.schema_for_package(page, pkg_pages.meta_description(page), "2026-06-12")
        faq = next(item for item in schema["@graph"] if item["@type"] == "FAQPage")

        self.assertGreaterEqual(len(faq["mainEntity"]), 4)
        questions = [item["name"] for item in faq["mainEntity"]]
        self.assertIn("What is sshfs?", questions)
        self.assertIn("Is sshfs safe for AI agents to use?", questions)

    def test_rendered_html_contains_top_summary_support_and_agent_risk(self):
        html = pkg_pages.render_package_page(sshfs_page(), {"generated_at": "2026-06-12T00:00:00Z"})

        self.assertEqual(html.split("</head>")[0].count("adsbygoogle.js?client=ca-pub-5218328608281545"), 1)
        self.assertIn('class="summary-card"', html)
        self.assertIn('id="support-title"', html)
        self.assertIn("Agent Risk Assessment", html)
        self.assertIn('"@type": "FAQPage"', html)

    def test_npm_zero_ranks_are_rebuilt_from_download_counts(self):
        pages = pkg_pages.package_pages_from_sources(
            {
                "db": {
                    "entries": {
                        "acorn": "npm:acorn",
                        "0x": "npm:0x",
                    },
                    "npms": {
                        "acorn": {
                            "summary": "ECMAScript parser",
                            "executable": "acorn",
                            "popularity": {
                                "downloads_per_30_days": 900,
                                "rank": 0,
                            },
                        },
                        "0x": {
                            "summary": "Flamegraph profiler",
                            "executable": "0x",
                            "popularity": {
                                "downloads_per_30_days": 100,
                                "rank": 0,
                            },
                        },
                    }
                }
            }
        )

        self.assertEqual(pages["npm:acorn"].popularity["rank"], 1)
        self.assertEqual(pages["npm:0x"].popularity["rank"], 2)
        self.assertIn("rank 1", pkg_pages.label_for(pages["npm:acorn"]))

    def test_source_archive_comes_from_runtime_database(self):
        pages = pkg_pages.package_pages_from_sources(
            {
                "db": {
                    "entries": {"rg": "brew:ripgrep"},
                    "formulas": {
                        "ripgrep": {
                            "sourceArchive": "https://example.com/ripgrep-15.2.0.tar.gz",
                        }
                    },
                }
            }
        )

        self.assertEqual(
            pages["brew:ripgrep"].source_archive,
            "https://example.com/ripgrep-15.2.0.tar.gz",
        )


if __name__ == "__main__":
    unittest.main()
