import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def load_cross_ecosystem():
    spec = importlib.util.spec_from_file_location(
        "pkg_cross_ecosystem_for_tests",
        ROOT / "scripts" / "generate-pkg-cross-ecosystem.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cross = load_cross_ecosystem()


def dnf_item(package_id):
    return {
        "platform": "linux",
        "manager": "dnf",
        "command": f"sudo dnf install {package_id}",
        "confidence": 0.92,
        "source": {
            "manager": "dnf",
            "package_id": package_id,
            "package_name": package_id,
            "source_label": "Fedora",
            "source_url": "https://example.test/primary.xml.zst",
        },
    }


class PackageCrossEcosystemTests(unittest.TestCase):
    def test_cargo_native_commands_use_manifest_features(self):
        commands = cross.native_commands({
            "provider": "cargo", "name": "raw-cpuid",
            "cargoRequiredFeatures": {"cpuid": ["cli"]},
        })
        self.assertEqual([item["command"] for item in commands],
                         ["cargo install raw-cpuid --bin=cpuid --features=cli"])

    def test_exact_package_id_wins_over_prefixed_external_match(self):
        facts = {"provider": "brew", "name": "openjpeg", "executables": []}
        matcher = {"openjpeg": [dnf_item("mingw32-openjpeg"), dnf_item("openjpeg")]}

        commands = cross.source_backed_manager_commands(facts, matcher)
        matches = cross.source_backed_manager_matches(facts, matcher)

        self.assertEqual(commands[0]["command"], "sudo dnf install openjpeg")
        self.assertEqual(matches[0]["packageId"], "openjpeg")

    def test_non_brew_name_collisions_are_not_promoted_to_commands(self):
        matcher = {"example": [dnf_item("example")]}

        for provider in ("npm", "cargo", "pip", "cask"):
            with self.subTest(provider=provider):
                facts = {"provider": provider, "name": "example", "executables": []}
                self.assertEqual(cross.source_backed_manager_commands(facts, matcher), [])


if __name__ == "__main__":
    unittest.main()
