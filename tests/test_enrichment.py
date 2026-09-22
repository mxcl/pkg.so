import tempfile
import unittest
import json
import importlib.util
import argparse
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from scripts.bootstrap.lib.render import agent_record_from_json, merge_agent_layer, parse_simple_yaml, validate_curated_fields
from scripts.bootstrap.lib.yaml_writer import yaml_text
from scripts.enrichment import (
    agent_record_from_result,
    apply_results,
    curation_facts,
    hash_curation_facts,
    hash_source_facts,
    load_projects,
    needs_new_curation,
    normalize_docs,
    normalize_history,
    normalize_category_path,
    normalize_path_locations,
    normalize_repo,
    normalize_tags,
    parse_project_yaml,
    prompt_text,
    select_projects,
    update_observed_state,
    validation_error_summary,
    validation_rejection_count,
    validate_codex_payload,
    validate_codex_payload_partial,
)


class CategoryTests(unittest.TestCase):
    def test_ai_category_paths_are_valid(self):
        self.assertEqual(normalize_category_path(["ai", "agents"]), ["ai", "agents"])
        self.assertEqual(normalize_category_path("ai/coding-agents"), ["ai", "coding-agents"])


def load_enrich_projects_module():
    path = Path("scripts/enrich-projects.py")
    spec = importlib.util.spec_from_file_location("enrich_projects", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample_record():
    return {
        "id": "brew:bat",
        "display-name": "bat",
        "homepage": "https://github.com/sharkdp/bat/",
        "repo": "https://github.com/sharkdp/bat",
        "package-manager": {"brew": "bat"},
        "package-manager-url": "https://formulae.brew.sh/formula/bat",
        "version": "0.26.1",
        "license": "Apache-2.0 OR MIT",
        "tags": ["cli"],
        "description": "Clone of cat(1) with syntax highlighting and Git integration",
        "source-archive": "https://github.com/sharkdp/bat/archive/refs/tags/v0.26.1.tar.gz",
        "executables": ["bat"],
        "provenance": {"provider": "brew", "source": "https://formulae.brew.sh/api/formula.json", "formula": "bat"},
    }


def sample_result(**overrides):
    result = {
        "id": "brew:bat",
        "repo": None,
        "repo-confidence": "high",
        "display-name": "bat",
        "display-name-confidence": "high",
        "category_path": ["developer-tools"],
        "category-confidence": "high",
        "docs": ["https://github.com/sharkdp/bat?utm_source=x#readme"],
        "docs-confidence": "high",
        "config-file-location": {
            "unix": ["~/.config/bat/config"],
            "windows": ["%APPDATA%\\bat\\config"],
        },
        "credentials-file-location": {
            "unix": ["~/.config/bat/credentials"],
        },
        "history": {
            "summary": ["bat is a cat-style source viewer for terminals."],
            "project-history": ["bat was created as a modern clone of cat with syntax highlighting and Git integration."],
            "adoption-history": ["It became part of the wave of Rust command-line tools packaged by Homebrew and other managers."],
            "usage": ["Developers use bat as an interactive replacement for cat and as a pager companion."],
            "package-nerd-significance": ["It is often packaged alongside ripgrep, fd, and exa as an example of modern Unix-style CLI replacements."],
            "timeline": ["2018: Homebrew package metadata describes bat as a cat clone with syntax highlighting."],
            "related-projects": ["ripgrep", "fd"],
        },
        "history-confidence": "medium",
        "tags": ["cli-tool", "git", "k8s"],
        "tags-confidence": "high",
        "repo_sources": [],
        "docs_sources": ["README"],
        "category_sources": ["README"],
        "tags_sources": ["README"],
        "display_name_sources": ["GitHub About"],
        "history_sources": ["Official README"],
    }
    result.update(overrides)
    return result


def sample_enrich_project_args(**overrides):
    values = {
        "backend": "external",
        "batch_size": 1,
        "commit_after_batch": False,
        "confidence_threshold": "medium",
        "dry_run": False,
        "force": False,
        "include_missing_curated_fields": False,
        "only_missing_curated_fields": False,
        "limit": 0,
        "mode": "new",
        "phase": "prepare",
        "provider": "brew",
        "run_id": "unit-run",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class EnrichmentTests(unittest.TestCase):
    def test_source_hash_excludes_curated_fields(self):
        before = sample_record()
        after = sample_record()
        after["docs"] = ["https://github.com/sharkdp/bat#readme"]
        after["category"] = "developer-tools"
        after["tags"] = ["cli", "git"]
        after["display-name"] = "bat"
        after["repo"] = "https://example.com/curated/repo"
        after["config-file-location"] = {"unix": ["~/.config/bat/config"]}
        after["credentials-file-location"] = None
        self.assertEqual(hash_source_facts(before), hash_source_facts(after))
        self.assertNotEqual(hash_curation_facts(before), hash_curation_facts(after))

    def test_normalization_before_hashing(self):
        first = sample_record()
        second = sample_record()
        second["homepage"] = "https://github.com/sharkdp/bat"
        self.assertEqual(hash_source_facts(first), hash_source_facts(second))

    def test_source_hash_excludes_volatile_package_metadata(self):
        before = sample_record()
        after = sample_record()
        after["version"] = "0.27.0"
        after["source-archive"] = "https://example.com/bat-0.27.0.tar.gz"

        self.assertEqual(hash_source_facts(before), hash_source_facts(after))

    def test_new_mode_treats_slug_display_name_as_missing(self):
        record = sample_record()
        self.assertTrue(needs_new_curation(record))
        entry = {"last_verified": date.today().isoformat(), "field_confidence": {"display-name": "high"}}
        record["docs"] = ["https://github.com/sharkdp/bat#readme"]
        record["category"] = "developer-tools"
        record["tags"] = ["cli", "git"]
        record["config-file-location"] = {"unix": ["~/.config/bat/config"]}
        record["credentials-file-location"] = None
        self.assertFalse(needs_new_curation(record, entry))
        record["display-name"] = "Bat"
        self.assertFalse(needs_new_curation(record))

    def test_new_mode_does_not_repeat_verified_missing_repo(self):
        record = sample_record()
        record["repo"] = None
        record["display-name"] = "Bat"
        record["docs"] = ["https://github.com/sharkdp/bat#readme"]
        record["category"] = "developer-tools"
        record["tags"] = ["cli", "git"]
        record["config-file-location"] = {"unix": ["~/.config/bat/config"]}
        record["credentials-file-location"] = None
        entry = {"last_verified": date.today().isoformat(), "field_confidence": {"repo": "high"}}
        self.assertFalse(needs_new_curation(record, entry))

    def test_new_mode_does_not_repeat_verified_missing_path_locations(self):
        record = sample_record()
        record["display-name"] = "Bat"
        record["docs"] = ["https://github.com/sharkdp/bat#readme"]
        record["category"] = "developer-tools"
        record["tags"] = ["cli", "git"]
        entry = {"last_verified": date.today().isoformat(), "field_confidence": {"repo": "high"}}
        self.assertFalse(needs_new_curation(record, entry))

    def test_missing_curated_fields_flag_selects_previously_verified_records(self):
        record = sample_record()
        record["display-name"] = "Bat"
        record["docs"] = ["https://github.com/sharkdp/bat#readme"]
        record["category"] = "developer-tools"
        record["tags"] = ["cli", "git"]
        state = {"brew:bat": {"last_verified": date.today().isoformat(), "field_confidence": {"repo": "high"}}}
        selected = select_projects(
            [record],
            state,
            mode="new",
            today=date.today().isoformat(),
            include_missing_curated_fields=True,
        )
        self.assertEqual([item["id"] for item in selected], ["brew:bat"])

    def test_daily_candidates_are_prioritized_before_older_backlog(self):
        module = load_enrich_projects_module()
        today = date.today().isoformat()
        args = sample_enrich_project_args(limit=1, include_missing_curated_fields=True)
        old = sample_record()
        old["id"] = "brew:aaa-old"
        new = sample_record()
        new["id"] = "brew:zzz-new"
        state = {
            "brew:aaa-old": {
                "first_observed": "2026-01-01",
                "last_source_change": "2026-01-01",
                "source_fact_hash": hash_source_facts(old),
            }
        }

        with mock.patch.object(module, "load_projects", return_value=[old, new]):
            _, selected = module.selected_projects_for_args(args, state, today)

        self.assertEqual([record["id"] for record in selected], ["brew:zzz-new"])

    def test_missing_curated_fields_flag_treats_explicit_null_as_reviewed(self):
        record = sample_record()
        record["display-name"] = "Bat"
        record["docs"] = ["https://github.com/sharkdp/bat#readme"]
        record["category"] = "developer-tools"
        record["tags"] = ["cli", "git"]
        record["config-file-location"] = None
        record["credentials-file-location"] = None
        record["history"] = None
        state = {"brew:bat": {"last_verified": date.today().isoformat(), "field_confidence": {"repo": "high"}}}
        selected = select_projects(
            [record],
            state,
            mode="new",
            today=date.today().isoformat(),
            include_missing_curated_fields=True,
        )
        self.assertEqual(selected, [])

    def test_load_projects_preserves_combined_history_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            combined = Path(tmp) / "combined"
            projects.mkdir()
            combined.mkdir()
            (projects / "aamath.yml").write_text(
                "id: brew:aamath\n"
                "display-name: aamath\n"
                "package-manager:\n"
                "  brew: aamath\n"
                "executables:\n"
                "  - aamath\n",
                encoding="utf-8",
            )
            (combined / "aamath.yml").write_text(
                "id: brew:aamath\n"
                "history: null\n",
                encoding="utf-8",
            )
            with mock.patch("scripts.enrichment.COMBINED_DIR", combined):
                record = load_projects(projects_dir=projects)[0]
        self.assertIn("history", record)
        self.assertIsNone(record["history"])

    def test_review_stale_updated_selection(self):
        record = sample_record()
        today = date.today()
        state = {
            "brew:bat": {
                "last_source_change": today.isoformat(),
                "last_verified": (today - timedelta(days=91)).isoformat(),
                "field_confidence": {"docs": "high"},
            }
        }
        selected = select_projects([record], state, mode="review-stale-updated", today=today.isoformat())
        self.assertEqual([item["id"] for item in selected], ["brew:bat"])

    def test_history_missing_mode_is_accepted_for_legacy_controller_runs(self):
        module = load_enrich_projects_module()
        args = argparse.Namespace(
            provider="brew",
            mode="history-missing",
            include_missing_curated_fields=False,
            only_missing_curated_fields=False,
            limit=0,
        )
        record = sample_record()
        state = {}

        with mock.patch.object(module, "load_projects", return_value=[record]):
            projects, selected = module.selected_projects_for_args(args, state, date.today().isoformat())

        self.assertEqual(projects, [record])
        self.assertEqual([item["id"] for item in selected], ["brew:bat"])

    def test_low_confidence_skips_non_empty_field_but_caches_review(self):
        record = sample_record()
        record["category"] = "developer-tools"
        record["__path"] = Path("unused.yml")
        state = {"brew:bat": {"field_ownership": {"category": "managed"}, "managed_values": {"category": "developer-tools"}}}
        summary = apply_results(
            {"brew:bat": record},
            state,
            [sample_result(**{"category_path": ["security"], "category-confidence": "low"})],
            confidence_threshold="medium",
            today=date.today().isoformat(),
            dry_run=True,
        )
        self.assertEqual(record["category"], "developer-tools")
        self.assertEqual(summary["skipped_low_confidence"], 1)
        self.assertEqual(state["brew:bat"]["field_confidence"]["category"], "low")

    def test_manual_tags_are_preserved_and_codex_tags_are_normalized(self):
        record = sample_record()
        record["tags"] = ["cli", "manual"]
        state = {"brew:bat": {"field_ownership": {"tags": "manual"}, "managed_values": {"tags": ["cli"]}}}
        apply_results(
            {"brew:bat": record},
            state,
            [sample_result()],
            confidence_threshold="medium",
            today=date.today().isoformat(),
            dry_run=True,
        )
        self.assertEqual(record["tags"], ["cli", "git", "kubernetes", "manual"])

    def test_missing_field_apply_preserves_existing_curation(self):
        record = sample_record()
        record["display-name"] = "Bat"
        record["docs"] = ["https://github.com/sharkdp/bat#readme"]
        record["category"] = "developer-tools"
        record["tags"] = ["cli", "syntax-highlighting"]
        state = {"brew:bat": {"field_ownership": {}, "managed_values": {}}}
        summary = apply_results(
            {"brew:bat": record},
            state,
            [
                sample_result(
                    **{
                        "display-name": "wrong",
                        "docs": ["https://example.com/wrong"],
                        "category_path": ["security"],
                        "tags": ["cli", "wrong"],
                    }
                )
            ],
            confidence_threshold="medium",
            today=date.today().isoformat(),
            dry_run=True,
            preserve_existing_fields=True,
        )
        self.assertEqual(summary["changed"], 1)
        self.assertEqual(record["display-name"], "Bat")
        self.assertEqual(record["docs"], ["https://github.com/sharkdp/bat#readme"])
        self.assertEqual(record["category"], "developer-tools")
        self.assertEqual(record["tags"], ["cli", "syntax-highlighting"])
        self.assertEqual(record["config-file-location"], {"unix": ["~/.config/bat/config"], "windows": ["%APPDATA%\\bat\\config"]})
        self.assertEqual(record["credentials-file-location"], {"unix": ["~/.config/bat/credentials"]})
        self.assertEqual(record["history"]["summary"], ["bat is a cat-style source viewer for terminals."])

    def test_repo_is_applied_only_when_missing(self):
        missing = sample_record()
        missing["repo"] = None
        existing = sample_record()
        state = {"brew:bat": {"field_ownership": {}, "managed_values": {}}}
        apply_results(
            {"brew:bat": missing},
            state,
            [sample_result(repo="https://github.com/sharkdp/bat")],
            confidence_threshold="medium",
            today=date.today().isoformat(),
            dry_run=True,
        )
        self.assertEqual(missing["repo"], "https://github.com/sharkdp/bat")
        apply_results(
            {"brew:bat": existing},
            state,
            [sample_result(repo="https://example.com/not-bat")],
            confidence_threshold="medium",
            today=date.today().isoformat(),
            dry_run=True,
        )
        self.assertEqual(existing["repo"], "https://github.com/sharkdp/bat")

    def test_agent_record_keeps_confidence_and_sources(self):
        record = agent_record_from_result(sample_result(repo="https://github.com/sharkdp/bat", repo_sources=["GitHub"]))
        self.assertEqual(record["repo-confidence"], "high")
        self.assertEqual(record["category-path"], ["developer-tools"])
        self.assertEqual(record["config-file-location"]["unix"], ["~/.config/bat/config"])
        self.assertEqual(record["credentials-file-location"]["unix"], ["~/.config/bat/credentials"])
        self.assertEqual(record["history-confidence"], "medium")
        self.assertEqual(record["history"]["sources"], ["Official README"])
        self.assertEqual(record["provenance"]["repo-sources"], ["GitHub"])
        self.assertEqual(record["provenance"]["history-sources"], ["Official README"])

    def test_agent_record_preserves_history_source_urls(self):
        record = agent_record_from_result(
            sample_result(
                history={
                    **sample_result()["history"],
                    "sources": ["https://github.com/sharkdp/bat#readme"],
                },
                history_sources=["Official README"],
            )
        )
        self.assertEqual(record["history"]["sources"], ["https://github.com/sharkdp/bat#readme"])
        self.assertEqual(record["provenance"]["history-sources"], ["https://github.com/sharkdp/bat#readme"])

    def test_agent_yaml_emits_top_level_location_nulls(self):
        text = yaml_text(
            agent_record_from_result(
                sample_result(
                    **{
                        "config-file-location": None,
                        "credentials-file-location": None,
                        "history": None,
                    }
                )
            )
        )
        self.assertIn("config-file-location: null\n", text)
        self.assertIn("credentials-file-location: null\n", text)
        self.assertIn("history: null\n", text)
        parsed = parse_simple_yaml(text)
        self.assertIsNone(parsed["config-file-location"])
        self.assertIsNone(parsed["credentials-file-location"])
        self.assertIsNone(parsed["history"])

    def test_agent_yaml_preserves_windows_backslashes_readably(self):
        text = yaml_text(agent_record_from_result(sample_result()))
        self.assertIn("  windows:\n    - '%APPDATA%\\bat\\config'\n", text)
        parsed = parse_simple_yaml(text)
        self.assertEqual(parsed["config-file-location"]["windows"], ["%APPDATA%\\bat\\config"])

    def test_agent_yaml_is_derived_from_json_payload(self):
        record = agent_record_from_json(sample_result(repo="https://github.com/sharkdp/bat", repo_sources=["GitHub"]))
        self.assertEqual(record, agent_record_from_result(sample_result(repo="https://github.com/sharkdp/bat", repo_sources=["GitHub"])))

    def test_combined_agent_layer_excludes_confidence_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bat.yml"
            path.write_text(
                "id: brew:bat\n"
                "repo: https://github.com/sharkdp/bat\n"
                "repo-confidence: high\n"
                "display-name: Bat\n"
                "display-name-confidence: high\n"
                "config-file-location:\n"
                "  unix:\n"
                "    - $HOME/.config/bat/config\n"
                "credentials-file-location:\n"
                "  unix:\n"
                "    - $HOME/.config/bat/credentials\n"
                "    - $XDG_CONFIG_HOME/bat/credentials\n"
                "history:\n"
                "  summary:\n"
                "    - Modern cat clone for terminals.\n"
                "category-path:\n"
                "  - developer-tools\n"
                "tags:\n"
                "  - cli\n"
                "  - git\n",
                encoding="utf-8",
            )
            record = {"id": "brew:bat", "repo": None, "display-name": "bat", "tags": ["cli"]}
            merge_agent_layer(record, path)
        self.assertEqual(record["repo"], "https://github.com/sharkdp/bat")
        self.assertEqual(record["display-name"], "Bat")
        self.assertEqual(record["config-file-location"], {"unix": "~/.config/bat/config"})
        self.assertEqual(
            record["credentials-file-location"],
            {"unix": ["~/.config/bat/credentials", "$XDG_CONFIG_HOME/bat/credentials"]},
        )
        self.assertEqual(record["history"], {"summary": ["Modern cat clone for terminals."]})
        self.assertEqual(record["category"], "developer-tools")
        self.assertNotIn("repo-confidence", record)

    def test_combined_agent_layer_publishes_location_nulls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bat.yml"
            path.write_text(
                "id: brew:bat\n"
                "config-file-location: null\n"
                "credentials-file-location: null\n",
                encoding="utf-8",
            )
            record = {"id": "brew:bat"}
            merge_agent_layer(record, path)
        self.assertIsNone(record["config-file-location"])
        self.assertIsNone(record["credentials-file-location"])

    def test_docs_ranking_rejects_package_manager_and_tracking(self):
        docs = normalize_docs(
            [
                "https://formulae.brew.sh/formula/bat",
                "https://github.com/sharkdp/bat/wiki/",
                "https://docs.rs/bat?utm_campaign=x",
                "https://example.com/blog/bat-tutorial",
            ]
        )
        self.assertEqual(docs, ["https://docs.rs/bat", "https://github.com/sharkdp/bat/wiki"])

    def test_repo_normalization_rejects_non_repo_surfaces(self):
        self.assertEqual(normalize_repo("https://formulae.brew.sh/formula/bat"), "")
        self.assertEqual(normalize_repo("https://github.com/sharkdp/bat.git"), "https://github.com/sharkdp/bat")
        self.assertEqual(normalize_repo("git://github.com/sharkdp/bat.git"), "https://github.com/sharkdp/bat")
        self.assertEqual(normalize_repo("ssh://git@gitlab.com/example/tool.git"), "https://gitlab.com/example/tool")
        self.assertEqual(normalize_repo("git://aften.git.sourceforge.net/gitroot/aften/aften"), "https://aften.git.sourceforge.net/gitroot/aften/aften")

    def test_tag_canonicalization(self):
        self.assertEqual(normalize_tags(["cli-tool", "k8s", "awscli", "utility"]), ["aws", "cli", "kubernetes"])

    def test_history_normalization_keeps_supported_lists(self):
        self.assertEqual(
            normalize_history(
                {
                    "summary": "A compact package history.",
                    "timeline": ["2018: First package", "2018: First package"],
                    "ignored": ["nope"],
                }
            ),
            {"summary": ["A compact package history."], "timeline": ["2018: First package"]},
        )

    def test_parse_project_yaml_round_trip_subset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bat.yml"
            path.write_text(
                "id: brew:bat\n"
                "display-name: Bat\n"
                "docs:\n"
                "  - https://github.com/sharkdp/bat#readme\n"
                "config-file-location:\n"
                "  unix: ~/.config/bat/config\n"
                "credentials-file-location: null\n"
                "category: developer-tools\n"
                "tags:\n"
                "  - cli\n"
                "  - git\n"
                "package-manager:\n"
                "  brew: bat\n",
                encoding="utf-8",
            )
            record = parse_project_yaml(path)
        self.assertEqual(record["docs"], ["https://github.com/sharkdp/bat#readme"])
        self.assertEqual(record["config-file-location"], {"unix": "~/.config/bat/config"})
        self.assertEqual(curation_facts(record)["config-file-location"], {"unix": ["~/.config/bat/config"]})
        self.assertIsNone(record["credentials-file-location"])
        self.assertEqual(record["package-manager"], {"brew": "bat"})
        self.assertEqual(curation_facts(record)["category"], "developer-tools")

    def test_update_observed_state_marks_manual_field(self):
        record = sample_record()
        record["tags"] = ["cli", "manual"]
        state = {"brew:bat": {"managed_values": {"tags": ["cli"]}, "field_ownership": {"tags": "managed"}}}
        update_observed_state(state, [record], date.today().isoformat())
        self.assertEqual(state["brew:bat"]["field_ownership"]["tags"], "manual")

    def test_validation_allows_curated_docs_and_category(self):
        failures = validate_curated_fields(
            Path("bat.yml"),
            "id: brew:bat\n"
            "docs:\n"
            "  - https://github.com/sharkdp/bat#readme\n"
            "category: developer-tools\n"
            "tags:\n"
            "  - cli\n"
            "  - git\n",
        )
        self.assertEqual(failures, [])

    def test_parse_simple_yaml_reads_folded_list_url(self):
        record = parse_simple_yaml(
            "docs:\n"
            "  - >\n"
            "    https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/using-sam-cli.html\n"
            "source-archive: >\n"
            "  https://files.pythonhosted.org/packages/aws_sam_cli-1.161.0.tar.gz\n"
        )
        self.assertEqual(
            record["docs"],
            ["https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/using-sam-cli.html"],
        )
        self.assertEqual(record["source-archive"], "https://files.pythonhosted.org/packages/aws_sam_cli-1.161.0.tar.gz")

    def test_parse_project_yaml_uses_folded_scalar_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "aws-sam-cli.yml"
            path.write_text(
                "id: brew:aws-sam-cli\n"
                "docs:\n"
                "  - >\n"
                "    https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/using-sam-cli.html\n"
                "package-manager:\n"
                "  brew: aws-sam-cli\n",
                encoding="utf-8",
            )
            record = parse_project_yaml(path)
        self.assertEqual(
            record["docs"],
            ["https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/using-sam-cli.html"],
        )
        self.assertEqual(record["package-manager"], {"brew": "aws-sam-cli"})

    def test_prompt_names_input_shape_and_safe_jq(self):
        prompt = prompt_text(Path("/tmp/input.json"), 10)
        large_prompt = prompt_text(Path("/tmp/input.json"), 11)
        self.assertIn("/goal Reliably enrich every project", prompt)
        self.assertIn("Review the full input in one pass", prompt)
        self.assertIn("break the input into smaller internal batches", large_prompt)
        self.assertIn("Do not switch to fallback rows because the full input is large", large_prompt)
        self.assertIn("top-level keys `schema` and `projects`", prompt)
        self.assertIn("The file contains 10 project records.", prompt)
        self.assertIn("exactly 10 results", prompt)
        self.assertIn("jq '.projects | length' /tmp/input.json", prompt)
        self.assertIn("jq -r '.projects[].id' /tmp/input.json", prompt)
        self.assertIn("jq -c '.projects[] | {id, source_facts, current_curation}' /tmp/input.json", prompt)
        self.assertNotIn("[0:10]", prompt)
        self.assertIn("Do not probe the input as a top-level array", prompt)
        self.assertIn("Do not emit placeholder fallback rows", prompt)
        self.assertNotIn("Empty source arrays are invalid", prompt)
        self.assertNotIn("non-empty array of file locations", prompt)
        self.assertNotIn("Do not combine alternatives into one string", prompt)
        self.assertIn("Prefer `unix` when official docs describe one shared Unix-like path", prompt)
        self.assertIn("top-level `null` for `credentials-file-location`", prompt)
        self.assertIn("Never return `git://`, `ssh://`, or `git@host:` clone URLs", prompt)
        self.assertIn("neutral Wikipedia-style package history", prompt)
        self.assertIn("Use more paragraphs for significant packages", prompt)

    def test_path_location_normalization_splits_or_alternatives(self):
        self.assertEqual(
            normalize_path_locations({"unix": ["$XDG_CONFIG_HOME/gh/config.yml or $HOME/.config/gh/config.yml"]}),
            {"unix": ["$XDG_CONFIG_HOME/gh/config.yml", "~/.config/gh/config.yml"]},
        )

    def test_validate_codex_payload_rejects_missing_ids(self):
        normalized, errors = validate_codex_payload(
            {"results": [sample_result(id="brew:bat")]},
            {"brew:bat", "brew:fd"},
        )
        self.assertEqual([item["id"] for item in normalized], ["brew:bat"])
        self.assertTrue(any("missing results for 1 project ids" in error for error in errors))

    def test_validate_codex_payload_partial_keeps_valid_results(self):
        valid = sample_result(id="brew:bat")
        invalid = sample_result(id="brew:fd", **{"category_path": ["not-a-category"]})
        normalized, errors, invalid_results = validate_codex_payload_partial(
            {"results": [valid, invalid]},
            {"brew:bat", "brew:fd"},
        )
        self.assertEqual([item["id"] for item in normalized], ["brew:bat"])
        self.assertTrue(any("category_path must start" in error for error in errors))
        self.assertEqual(invalid_results[0]["id"], "brew:fd")

    def test_validate_codex_payload_rejects_placeholder_sources(self):
        result = sample_result(docs_sources=["not completed"])
        normalized, errors, invalid_results = validate_codex_payload_partial(
            {"results": [result]},
            {"brew:bat"},
        )
        self.assertEqual(normalized, [])
        self.assertTrue(any("placeholder provenance" in error for error in errors))
        self.assertEqual(invalid_results[0]["id"], "brew:bat")

    def test_validate_codex_payload_rejects_missing_sources_for_claimed_values(self):
        result = sample_result(category_sources=[], tags_sources=[], display_name_sources=[])
        normalized, errors, invalid_results = validate_codex_payload_partial(
            {"results": [result]},
            {"brew:bat"},
        )
        self.assertEqual(normalized, [])
        self.assertTrue(any("category_sources must cite at least one source" in error for error in errors))
        self.assertTrue(any("tags_sources must cite at least one source" in error for error in errors))
        self.assertTrue(any("display_name_sources must cite at least one source" in error for error in errors))
        self.assertEqual(invalid_results[0]["id"], "brew:bat")

    def test_validate_codex_payload_rejects_missing_history_sources_when_history_present(self):
        result = sample_result(history_sources=[])
        normalized, errors, invalid_results = validate_codex_payload_partial(
            {"results": [result]},
            {"brew:bat"},
        )
        self.assertEqual(normalized, [])
        self.assertTrue(any("history_sources must cite at least one source" in error for error in errors))
        self.assertEqual(invalid_results[0]["id"], "brew:bat")

    def test_validate_codex_payload_rejects_invalid_path_locations(self):
        result = sample_result(
            **{
                "config-file-location": {"plan9": "/lib/bat/config", "unix": ""},
                "credentials-file-location": {"unix": "~/.config/bat/credentials", "windows": []},
            }
        )
        normalized, errors, invalid_results = validate_codex_payload_partial(
            {"results": [result]},
            {"brew:bat"},
        )
        self.assertEqual(normalized, [])
        self.assertTrue(any("config-file-location contains unsupported platform" in error for error in errors))
        self.assertTrue(any("config-file-location.unix must be a non-empty array of strings" in error for error in errors))
        self.assertTrue(any("credentials-file-location.unix must be a non-empty array of strings" in error for error in errors))
        self.assertTrue(any("credentials-file-location.windows must be a non-empty array of strings" in error for error in errors))
        self.assertEqual(invalid_results[0]["id"], "brew:bat")

    def test_validation_error_summary_groups_by_message(self):
        summary = validation_error_summary(
            [
                "brew:bat: category_sources must cite at least one source",
                "brew:fd: category_sources must cite at least one source",
                "missing results for 1 project ids: brew:ripgrep",
            ]
        )
        self.assertEqual(summary[0], {"error": "category_sources must cite at least one source", "count": 2})
        self.assertEqual(summary[1], {"error": "missing results for 1 project ids: brew:ripgrep", "count": 1})

    def test_output_schema_rejects_empty_source_bailout_shape(self):
        schema = json.loads(Path("schemas/codex-project-enrichment-output.schema.json").read_text(encoding="utf-8"))
        item_schema = schema["properties"]["results"]["items"]["properties"]
        self.assertEqual(item_schema["category_sources"]["minItems"], 1)
        self.assertEqual(item_schema["display_name_sources"]["minItems"], 1)
        self.assertEqual(item_schema["tags_sources"]["minItems"], 1)
        self.assertEqual(item_schema["tags"]["minItems"], 1)
        self.assertEqual(item_schema["display-name"]["minLength"], 1)
        self.assertIn("config-file-location", schema["properties"]["results"]["items"]["required"])
        self.assertIn("credentials-file-location", schema["properties"]["results"]["items"]["required"])
        self.assertIn("history", schema["properties"]["results"]["items"]["required"])
        self.assertIn("history-confidence", schema["properties"]["results"]["items"]["required"])
        self.assertEqual(item_schema["config-file-location"]["anyOf"][0]["type"], "null")
        self.assertEqual(item_schema["history"]["anyOf"][0]["type"], "null")
        history_object = item_schema["history"]["anyOf"][1]
        self.assertIn("usage", history_object["properties"])
        self.assertNotIn("minProperties", history_object)
        self.assertEqual(
            sorted(history_object["required"]),
            sorted(history_object["properties"]),
        )
        credentials_object = item_schema["credentials-file-location"]["anyOf"][1]
        self.assertFalse(credentials_object["additionalProperties"])
        self.assertEqual(credentials_object["properties"]["windows"]["type"], "array")
        self.assertEqual(credentials_object["properties"]["windows"]["items"]["type"], "string")

    def test_batch_output_schema_uses_strict_platform_map_variants(self):
        module = load_enrich_projects_module()
        config_schema = module.strict_platform_map_schema()
        credentials_schema = module.strict_platform_map_schema()
        self.assertEqual(len(config_schema["anyOf"]), 17)
        self.assertEqual(config_schema["anyOf"][0], {"type": "null"})
        empty_variant = config_schema["anyOf"][1]
        self.assertEqual(empty_variant["properties"], {})
        self.assertEqual(empty_variant["required"], [])
        unix_variant = next(
            variant for variant in config_schema["anyOf"]
            if set(variant.get("properties", {})) == {"unix"}
        )
        self.assertEqual(unix_variant["required"], ["unix"])
        self.assertEqual(unix_variant["properties"]["unix"]["type"], "array")
        self.assertEqual(unix_variant["properties"]["unix"]["items"]["type"], "string")
        credentials_unix_variant = next(
            variant for variant in credentials_schema["anyOf"]
            if set(variant.get("properties", {})) == {"unix"}
        )
        self.assertEqual(credentials_unix_variant["properties"]["unix"]["type"], "array")
        self.assertEqual(credentials_unix_variant["properties"]["unix"]["items"]["type"], "string")

    def test_prepare_phase_writes_batch_artifacts_without_invoking_codex(self):
        module = load_enrich_projects_module()
        args = sample_enrich_project_args()
        record = sample_record()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            run_dir = tmp_root / "cache" / "enrichment" / "runs" / "unit-run"
            with mock.patch.object(module, "ROOT", tmp_root):
                with mock.patch.object(module, "invoke_codex") as invoke_codex:
                    manifest = module.prepare_run(args, [record], run_dir)

            self.assertEqual(manifest["selected_count"], 1)
            self.assertEqual(manifest["batches"][0]["status"], "pending")
            self.assertFalse(manifest["commit_after_batch"])
            self.assertFalse(manifest["include_missing_curated_fields"])
            self.assertTrue((run_dir / "input.json").is_file())
            self.assertTrue((run_dir / "batches" / "0001" / "input.json").is_file())
            self.assertTrue((run_dir / "batches" / "0001" / "prompt.md").is_file())
            self.assertTrue((run_dir / "batches" / "0001" / "output-schema.json").is_file())
            self.assertTrue((run_dir / "controller-manifest.json").is_file())
            invoke_codex.assert_not_called()

    def test_prepare_phase_records_include_missing_curated_fields(self):
        module = load_enrich_projects_module()
        args = sample_enrich_project_args(include_missing_curated_fields=True)
        record = sample_record()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            run_dir = tmp_root / "cache" / "enrichment" / "runs" / "unit-run"
            with mock.patch.object(module, "ROOT", tmp_root):
                manifest = module.prepare_run(args, [record], run_dir)

        self.assertTrue(manifest["include_missing_curated_fields"])

    def test_prepare_phase_records_commit_behavior(self):
        module = load_enrich_projects_module()
        args = sample_enrich_project_args(commit_after_batch=True)

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            with mock.patch.object(module, "ROOT", Path(tmp)):
                manifest = module.prepare_run(args, [sample_record()], run_dir)

        self.assertTrue(manifest["commit_after_batch"])

    def test_only_missing_curated_fields_selects_only_missing_records(self):
        module = load_enrich_projects_module()
        args = sample_enrich_project_args(only_missing_curated_fields=True)
        missing = sample_record()
        missing["id"] = "brew:missing-history"
        missing["display-name"] = "Missing History"
        missing["repo"] = "https://example.com/missing-history"
        missing["docs"] = ["https://example.com/missing-history/docs"]
        missing["category"] = "developer-tools"
        missing["tags"] = ["cli", "test"]
        missing["config-file-location"] = None
        missing["credentials-file-location"] = None
        reviewed = dict(missing)
        reviewed["id"] = "brew:reviewed-history"
        reviewed["history"] = None
        state = {}
        with mock.patch.object(module, "load_projects", return_value=[missing, reviewed]):
            _projects, selected = module.selected_projects_for_args(args, state, date.today().isoformat())
        self.assertEqual([record["id"] for record in selected], ["brew:missing-history"])

    def test_apply_phase_consumes_codex_output_and_commits_after_valid_batch(self):
        module = load_enrich_projects_module()
        args = sample_enrich_project_args(commit_after_batch=True, phase="apply")
        record = sample_record()
        result = sample_result()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            run_dir = tmp_root / "cache" / "enrichment" / "runs" / "unit-run"
            state_path = tmp_root / "cache" / "enrichment" / "state.json"
            state_path.parent.mkdir(parents=True)
            state: dict[str, object] = {}
            with mock.patch.object(module, "ROOT", tmp_root):
                manifest = module.prepare_run(args, [record], run_dir)
                codex_output_path = tmp_root / manifest["batches"][0]["codex_output_path"]
                codex_output_path.write_text(json.dumps({"results": [result]}) + "\n", encoding="utf-8")
                with (
                    mock.patch.object(module, "apply_results", return_value={"reviewed": 1, "changed": 1, "rejected": 0, "skipped_low_confidence": 0, "no_op": 0}) as apply_results_mock,
                    mock.patch.object(module, "render_combined_tree") as render_mock,
                    mock.patch.object(module, "git_commit_if_changed", return_value="abc123") as commit_mock,
                ):
                    exit_code = module.apply_prepared_batches(
                        args,
                        manifest,
                        [record],
                        state,
                        state_path,
                        run_dir,
                        date.today().isoformat(),
                    )

            self.assertEqual(exit_code, 0)
            self.assertTrue((run_dir / "batches" / "0001" / "normalized-output.json").is_file())
            self.assertTrue((run_dir / "batches" / "0001" / "apply-summary.json").is_file())
            apply_results_mock.assert_called_once()
            render_mock.assert_called_once()
            commit_mock.assert_called_once()

    def test_valid_checkpoint_skips_backend_and_reuses_normalized_output(self):
        module = load_enrich_projects_module()
        args = sample_enrich_project_args()
        record = sample_record()
        normalized = sample_result()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            run_dir = tmp_root / "cache" / "enrichment" / "runs" / "unit-run"
            batch_dir = run_dir / "batches" / "0001"
            batch_dir.mkdir(parents=True)
            (batch_dir / "normalized-output.json").write_text(
                json.dumps({"results": [normalized], "errors": [], "error_summary": [], "invalid": []}) + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(module, "ROOT", tmp_root):
                with mock.patch.object(module, "invoke_codex") as invoke_codex:
                    manifest = module.prepare_run(args, [record], run_dir)
                    module.invoke_codex_cli_for_pending_batches(args, manifest)

            self.assertEqual(manifest["batches"][0]["status"], "checkpointed")
            invoke_codex.assert_not_called()

    def test_saved_raw_research_is_reused_after_interruption(self):
        module = load_enrich_projects_module()
        args = sample_enrich_project_args()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            run_dir = tmp_root / "cache/enrichment/runs/unit-run"
            with mock.patch.object(module, "ROOT", tmp_root):
                manifest = module.prepare_run(args, [sample_record()], run_dir)
                raw = tmp_root / manifest["batches"][0]["codex_output_path"]
                raw.write_text(json.dumps({"results": [sample_result()]}))
                with mock.patch.object(module, "invoke_codex") as invoke:
                    module.invoke_codex_cli_for_pending_batches(args, manifest)
                invoke.assert_not_called()
                normalized = tmp_root / manifest["batches"][0]["normalized_output_path"]
                self.assertIsNotNone(module.load_valid_checkpoint(normalized, {sample_record()["id"]}))

    def test_resume_keeps_original_manifest_selection(self):
        module = load_enrich_projects_module()
        args = sample_enrich_project_args()
        args.run_id = "unit-run"
        args.phase = "run"
        args.backend = "codex-cli"
        args.dry_run = False
        manifest = {"batches": [], "selected_count": 0}
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            path = cache / "enrichment/runs/unit-run/controller-manifest.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(manifest))
            with (
                mock.patch.object(module, "CACHE_DIR", cache),
                mock.patch.object(module, "parse_args", return_value=args),
                mock.patch.object(module, "ensure_root"),
                mock.patch.object(module, "selected_projects_for_args", return_value=([], [])),
                mock.patch.object(module, "prepare_run") as prepare,
                mock.patch.object(module, "invoke_codex_cli_for_pending_batches") as invoke,
                mock.patch.object(module, "apply_prepared_batches", return_value=0),
            ):
                self.assertEqual(module.main(), 0)
            prepare.assert_not_called()
            self.assertEqual(invoke.call_args.args[1], manifest)

    def test_codex_timeout_isolated_to_one_batch(self):
        module = load_enrich_projects_module()
        args = sample_enrich_project_args()
        args.batch_size = 1
        first = {**sample_record(), "id": "brew:first"}
        second = {**sample_record(), "id": "brew:second"}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            run_dir = tmp_root / "cache" / "enrichment" / "runs" / "unit-run"
            with mock.patch.object(module, "ROOT", tmp_root):
                manifest = module.prepare_run(args, [first, second], run_dir)
                with mock.patch.object(module, "invoke_codex", side_effect=[False, True]) as invoke_codex:
                    module.invoke_codex_cli_for_pending_batches(args, manifest)

            self.assertEqual(invoke_codex.call_count, 2)
            first_status = json.loads((run_dir / "batches" / "0001" / "status.json").read_text())
            second_status = json.loads((run_dir / "batches" / "0002" / "status.json").read_text())
            self.assertEqual(first_status["status"], "timed-out")
            self.assertEqual(second_status["status"], "codex-cli-completed")

    def test_timed_out_batch_does_not_fail_apply_phase(self):
        module = load_enrich_projects_module()
        args = sample_enrich_project_args()
        record = sample_record()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            run_dir = tmp_root / "cache" / "enrichment" / "runs" / "unit-run"
            state_path = tmp_root / "cache" / "enrichment" / "state.json"
            state_path.parent.mkdir(parents=True)
            with mock.patch.object(module, "ROOT", tmp_root):
                manifest = module.prepare_run(args, [record], run_dir)
                batch_dir = run_dir / "batches" / "0001"
                module.write_batch_status(batch_dir, {"batch": "0001", "status": "timed-out"})
                exit_code = module.apply_prepared_batches(
                    args, manifest, [record], {}, state_path, run_dir, date.today().isoformat()
                )

            self.assertEqual(exit_code, 0)
            status = json.loads((batch_dir / "status.json").read_text())
            self.assertEqual(status["status"], "timed-out")

    def test_validation_aggregates_duplicate_errors_and_counts_rejected_ids(self):
        normalized, errors, invalid_results = validate_codex_payload_partial(
            {
                "results": [
                    sample_result(id="brew:bat"),
                    sample_result(id="brew:fd"),
                    sample_result(id="brew:fd"),
                    sample_result(id="brew:fd"),
                ]
            },
            {"brew:bat", "brew:fd", "brew:ripgrep"},
        )
        self.assertEqual([item["id"] for item in normalized], ["brew:bat", "brew:fd"])
        self.assertEqual(invalid_results, [])
        self.assertTrue(any("brew:fd: duplicate result repeated 2 times" == error for error in errors))
        self.assertEqual(validation_rejection_count({"brew:bat", "brew:fd", "brew:ripgrep"}, normalized), 1)

    def test_validation_accepts_later_valid_duplicate(self):
        invalid = sample_result(id="brew:fd", **{"category_path": ["not-a-category"]})
        valid = sample_result(id="brew:fd", **{"category_path": ["developer-tools"]})
        normalized, errors, invalid_results = validate_codex_payload_partial(
            {"results": [invalid, valid]},
            {"brew:fd"},
        )
        self.assertEqual([item["id"] for item in normalized], ["brew:fd"])
        self.assertEqual(invalid_results[0]["id"], "brew:fd")
        self.assertTrue(any("brew:fd: duplicate result repeated 1 times" == error for error in errors))
        self.assertEqual(validation_rejection_count({"brew:fd"}, normalized), 0)


if __name__ == "__main__":
    unittest.main()
