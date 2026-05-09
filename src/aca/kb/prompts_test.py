from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from . import prompts as prompt_defaults
from .app import create_app
from .indexer import KnowledgebaseIndex
from .settings import KBSettings


def _make_settings(root: Path, *, prompts_seed_file: Path | None = None) -> KBSettings:
    docs_root = root / "docs"
    index_root = root / "index"
    key_file = root / "secrets" / "kb_admin_api_key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text("secret\n", encoding="utf-8")
    return KBSettings(
        server_name="ac.tandem/kb-mcp",
        server_title="Tandem Knowledgebase MCP",
        server_description="Test KB server",
        server_version="0.1.0",
        public_base_url="http://127.0.0.1:39736/mcp",
        port=39736,
        docs_root=docs_root,
        index_root=index_root,
        admin_api_key_file=key_file,
        admin_api_key="",
        reconcile_interval_seconds=0.1,
        default_search_limit=5,
        max_search_limit=20,
        max_list_limit=200,
        max_upload_bytes=5 * 1024 * 1024,
        answer_default_documents=3,
        answer_max_documents=5,
        answer_max_chars_per_doc=8000,
        prompts_seed_file=prompts_seed_file,
    )


class PromptDefaultsTest(unittest.TestCase):
    def test_known_keys_match_registry(self) -> None:
        keys = {p.key for p in prompt_defaults.PROMPT_KEYS}
        self.assertIn("mcp_initialize_instructions", keys)
        self.assertIn("answer_question_tool_description", keys)
        self.assertIn("match_guidance", keys)
        self.assertIn("no_match_guidance", keys)
        self.assertIn("no_query_guidance", keys)

    def test_collection_override_flag(self) -> None:
        self.assertFalse(prompt_defaults.supports_collection_override("mcp_initialize_instructions"))
        self.assertFalse(prompt_defaults.supports_collection_override("answer_question_tool_description"))
        self.assertTrue(prompt_defaults.supports_collection_override("match_guidance"))
        self.assertTrue(prompt_defaults.supports_collection_override("no_match_guidance"))
        self.assertTrue(prompt_defaults.supports_collection_override("no_query_guidance"))

    def test_unknown_key_raises(self) -> None:
        with self.assertRaises(KeyError):
            prompt_defaults.get_default("not-a-real-key")
        self.assertFalse(prompt_defaults.is_known_key("not-a-real-key"))


class PromptIndexerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = _make_settings(self.root)
        self.index = KnowledgebaseIndex(self.settings)
        self.index.initialize()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_get_prompt_returns_default_when_unset(self) -> None:
        value = self.index.get_prompt("match_guidance")
        self.assertEqual(value, prompt_defaults.get_default("match_guidance"))

    def test_set_global_override(self) -> None:
        self.index.set_prompt("match_guidance", "GLOBAL custom guidance")
        self.assertEqual(self.index.get_prompt("match_guidance"), "GLOBAL custom guidance")
        self.assertEqual(
            self.index.get_prompt("match_guidance", "any-collection"),
            "GLOBAL custom guidance",
        )

    def test_set_collection_override_takes_precedence(self) -> None:
        self.index.set_prompt("match_guidance", "GLOBAL value")
        self.index.set_prompt("match_guidance", "SUPPORT value", collection_id="support")
        self.assertEqual(self.index.get_prompt("match_guidance", "support"), "SUPPORT value")
        self.assertEqual(self.index.get_prompt("match_guidance", "engineering"), "GLOBAL value")
        self.assertEqual(self.index.get_prompt("match_guidance"), "GLOBAL value")

    def test_collection_override_rejected_for_global_only_key(self) -> None:
        with self.assertRaises(ValueError):
            self.index.set_prompt(
                "mcp_initialize_instructions",
                "per-collection",
                collection_id="support",
            )

    def test_set_prompt_rejects_empty_value(self) -> None:
        with self.assertRaises(ValueError):
            self.index.set_prompt("match_guidance", "")
        with self.assertRaises(ValueError):
            self.index.set_prompt("match_guidance", "   ")

    def test_set_prompt_rejects_unknown_key(self) -> None:
        with self.assertRaises(ValueError):
            self.index.set_prompt("not-a-real-key", "x")

    def test_delete_prompt_reverts_to_default(self) -> None:
        self.index.set_prompt("match_guidance", "GLOBAL")
        self.index.set_prompt("match_guidance", "SUPPORT", collection_id="support")
        self.index.delete_prompt("match_guidance", collection_id="support")
        self.assertEqual(self.index.get_prompt("match_guidance", "support"), "GLOBAL")
        self.index.delete_prompt("match_guidance")
        self.assertEqual(
            self.index.get_prompt("match_guidance"),
            prompt_defaults.get_default("match_guidance"),
        )

    def test_list_prompts_marks_scope_correctly(self) -> None:
        self.index.set_prompt("match_guidance", "GLOBAL")
        self.index.set_prompt("match_guidance", "SUPPORT", collection_id="support")
        listing = {p["key"]: p for p in self.index.list_prompts(collection_id="support")}
        self.assertEqual(listing["match_guidance"]["scope"], "collection")
        self.assertEqual(listing["match_guidance"]["current"], "SUPPORT")
        self.assertEqual(listing["match_guidance"]["global_override"], "GLOBAL")
        self.assertEqual(
            [o["collection_id"] for o in listing["match_guidance"]["collection_overrides"]],
            ["support"],
        )
        # Without a collection: scope falls back to global
        no_collection = {p["key"]: p for p in self.index.list_prompts()}
        self.assertEqual(no_collection["match_guidance"]["scope"], "global")
        self.assertEqual(no_collection["match_guidance"]["current"], "GLOBAL")
        # Unset key still reports default
        self.assertEqual(
            no_collection["mcp_initialize_instructions"]["scope"],
            "default",
        )

    def test_seed_yaml_bootstraps_when_table_empty(self) -> None:
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("pyyaml not installed")
        seed_root = Path(self._tmp.name) / "seed.yaml"
        seed_root.write_text(
            "match_guidance:\n"
            "  value: SEED_GLOBAL\n"
            "  collections:\n"
            "    support: SEED_SUPPORT\n"
            "no_match_guidance: SEED_NO_MATCH_GLOBAL\n",
            encoding="utf-8",
        )
        # Fresh root so seeding actually runs
        fresh_root = Path(tempfile.mkdtemp())
        try:
            settings = _make_settings(fresh_root, prompts_seed_file=seed_root)
            index = KnowledgebaseIndex(settings)
            index.initialize()
            self.assertEqual(index.get_prompt("match_guidance"), "SEED_GLOBAL")
            self.assertEqual(index.get_prompt("match_guidance", "support"), "SEED_SUPPORT")
            self.assertEqual(index.get_prompt("no_match_guidance"), "SEED_NO_MATCH_GLOBAL")
        finally:
            import shutil

            shutil.rmtree(fresh_root, ignore_errors=True)

    def test_seed_does_not_rerun_after_owner_clears_overrides(self) -> None:
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("pyyaml not installed")
        seed_path = Path(self._tmp.name) / "seed.yaml"
        seed_path.write_text("match_guidance: SEED_VALUE\n", encoding="utf-8")
        fresh_root = Path(tempfile.mkdtemp())
        try:
            settings = _make_settings(fresh_root, prompts_seed_file=seed_path)
            index = KnowledgebaseIndex(settings)
            index.initialize()
            self.assertEqual(index.get_prompt("match_guidance"), "SEED_VALUE")

            # Owner clears the override via the UI / admin endpoint
            index.delete_prompt("match_guidance")
            self.assertEqual(
                index.get_prompt("match_guidance"),
                prompt_defaults.get_default("match_guidance"),
            )

            # Restart simulation: a fresh index pointing at the same DB and the
            # same seed file. Should NOT re-seed because the sentinel is set.
            restarted = KnowledgebaseIndex(settings)
            restarted.initialize()
            self.assertEqual(
                restarted.get_prompt("match_guidance"),
                prompt_defaults.get_default("match_guidance"),
            )
        finally:
            import shutil

            shutil.rmtree(fresh_root, ignore_errors=True)

    def test_sentinel_does_not_appear_in_list_prompts(self) -> None:
        # Even after seeding, list_prompts should only return registered keys.
        self.index.set_prompt("match_guidance", "GLOBAL")
        listing = self.index.list_prompts()
        keys = {entry["key"] for entry in listing}
        self.assertNotIn("__seeded__", keys)
        self.assertEqual(keys, {p.key for p in prompt_defaults.PROMPT_KEYS})


class PromptAdminApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = _make_settings(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_admin_prompt_crud_roundtrip(self) -> None:
        app = create_app(self.settings)
        headers = {"Authorization": "Bearer secret"}
        with TestClient(app) as client:
            # Listing requires admin auth
            self.assertEqual(client.get("/admin/prompts").status_code, 401)

            listing = client.get("/admin/prompts", headers=headers)
            self.assertEqual(listing.status_code, 200)
            payload = listing.json()
            self.assertIsNone(payload["requested_collection_id"])
            keys = {p["key"] for p in payload["prompts"]}
            self.assertIn("match_guidance", keys)

            # Set global override
            put = client.put(
                "/admin/prompts/match_guidance",
                headers=headers,
                json={"value": "GLOBAL_OVERRIDE"},
            )
            self.assertEqual(put.status_code, 200, put.text)
            self.assertEqual(put.json()["value"], "GLOBAL_OVERRIDE")

            # Set per-collection override
            put_coll = client.put(
                "/admin/prompts/match_guidance",
                headers=headers,
                json={"value": "SUPPORT_OVERRIDE", "collection_id": "support"},
            )
            self.assertEqual(put_coll.status_code, 200, put_coll.text)

            # Listing for that collection shows scope=collection
            listing_support = client.get(
                "/admin/prompts",
                params={"collection_id": "support"},
                headers=headers,
            )
            entries = {p["key"]: p for p in listing_support.json()["prompts"]}
            self.assertEqual(entries["match_guidance"]["scope"], "collection")
            self.assertEqual(entries["match_guidance"]["current"], "SUPPORT_OVERRIDE")

            # Reject per-collection on global-only key
            bad = client.put(
                "/admin/prompts/mcp_initialize_instructions",
                headers=headers,
                json={"value": "x", "collection_id": "support"},
            )
            self.assertEqual(bad.status_code, 400)

            # Unknown key returns 404
            unknown = client.put(
                "/admin/prompts/not-a-real-key",
                headers=headers,
                json={"value": "x"},
            )
            self.assertEqual(unknown.status_code, 404)

            # Delete collection override
            deleted = client.delete(
                "/admin/prompts/match_guidance",
                params={"collection_id": "support"},
                headers=headers,
            )
            self.assertEqual(deleted.status_code, 200)
            self.assertEqual(deleted.json()["removed"], 1)

            # Confirm collection override is gone (falls back to global)
            after = client.get(
                "/admin/prompts",
                params={"collection_id": "support"},
                headers=headers,
            )
            entries_after = {p["key"]: p for p in after.json()["prompts"]}
            self.assertEqual(entries_after["match_guidance"]["scope"], "global")
            self.assertEqual(entries_after["match_guidance"]["current"], "GLOBAL_OVERRIDE")

    def test_initialize_instructions_use_overrides(self) -> None:
        app = create_app(self.settings)
        headers = {"Authorization": "Bearer secret"}
        with TestClient(app) as client:
            client.put(
                "/admin/prompts/mcp_initialize_instructions",
                headers=headers,
                json={"value": "CUSTOM_INIT"},
            )
            response = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["result"]["instructions"],
                "CUSTOM_INIT",
            )

    def test_answer_question_tool_description_uses_override(self) -> None:
        app = create_app(self.settings)
        headers = {"Authorization": "Bearer secret"}
        with TestClient(app) as client:
            client.put(
                "/admin/prompts/answer_question_tool_description",
                headers=headers,
                json={"value": "CUSTOM_TOOL_DESC"},
            )
            response = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
            tools = {t["name"]: t for t in response.json()["result"]["tools"]}
            self.assertEqual(tools["answer_question"]["description"], "CUSTOM_TOOL_DESC")


if __name__ == "__main__":
    unittest.main()
