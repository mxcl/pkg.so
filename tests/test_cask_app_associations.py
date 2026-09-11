import importlib.util
import unittest
from pathlib import Path

from scripts.bootstrap.lib.casks import cask_app_evidence_hash, cask_app_identity_evidence


def load_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "resolve-cask-app-associations.py"
    spec = importlib.util.spec_from_file_location("resolve_cask_app_associations", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CaskAppAssociationAgentTests(unittest.TestCase):
    def test_applies_validated_result_with_local_evidence_hash(self):
        module = load_module()
        cask = {
            "token": "google-chrome",
            "name": ["Google Chrome"],
            "artifacts": [
                {"app": ["Google Chrome.app"]},
                {"zap": [{"trash": ["~/Library/Caches/com.google.Chrome"]}]},
            ],
        }
        evidence = cask_app_identity_evidence(cask)
        assert evidence is not None
        candidate = {**evidence, "evidence_hash": cask_app_evidence_hash(evidence)}
        result = {
            "token": "google-chrome",
            "bundle_identifier": "com.google.Chrome",
            "confidence": "high",
            "sources": ["https://www.google.com/chrome/"],
            "reason": "Official Chrome identity.",
        }
        associations = {}

        validated = module.validated_results({"results": [result]}, {"google-chrome"})
        module.apply_results(associations, [candidate], validated)

        self.assertEqual(associations["google-chrome"]["bundle_identifier"], "com.google.Chrome")
        self.assertEqual(associations["google-chrome"]["evidence_hash"], candidate["evidence_hash"])

    def test_rejects_missing_and_duplicate_results(self):
        module = load_module()
        result = {
            "token": "google-chrome",
            "bundle_identifier": "com.google.Chrome",
            "confidence": "high",
            "sources": ["https://www.google.com/chrome/"],
            "reason": "Official Chrome identity.",
        }

        with self.assertRaises(ValueError):
            module.validated_results({"results": [result, result]}, {"google-chrome", "firefox"})


if __name__ == "__main__":
    unittest.main()
