from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .indexer import KnowledgebaseIndex
from .settings import KBSettings


def _make_settings(root: Path) -> KBSettings:
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
    )


class KnowledgebaseIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = _make_settings(self.root)
        self.index = KnowledgebaseIndex(self.settings)
        self.index.initialize()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sync_search_and_delete_documents(self) -> None:
        path = self.settings.docs_root / "acme" / "welcome.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "title: Welcome\n"
            "tags:\n"
            "  - support\n"
            "---\n\n"
            "# Intro\n"
            "Hello Acme.\n",
            encoding="utf-8",
        )

        created = self.index.sync_from_disk()
        self.assertEqual(created["created"], 1)
        self.assertEqual(created["updated"], 0)
        self.assertEqual(created["deleted"], 0)

        collections = self.index.list_collections()
        self.assertEqual(collections[0]["collection_id"], "acme")
        self.assertEqual(collections[0]["document_count"], 1)

        document = self.index.get_document("acme", "welcome")
        self.assertIsNotNone(document)
        self.assertEqual(document["title"], "Welcome")
        self.assertEqual(document["tags"], ["support"])
        self.assertEqual(document["kb_role"], "")
        self.assertEqual(document["relative_path"], "welcome")
        self.assertEqual(document["source_path"], "welcome")
        self.assertTrue(document["content"].startswith("---"))

        search_results = self.index.search(collection_id="acme", query="Hello", limit=5)
        self.assertEqual(search_results[0]["doc_id"], "acme/welcome")
        self.assertEqual(search_results[0]["source_path"], "welcome")
        self.assertTrue(search_results[0]["snippet"])

        unchanged = self.index.sync_from_disk()
        self.assertEqual(unchanged["unchanged"], 1)
        self.assertEqual(self.index.get_document("acme", "welcome")["title"], "Welcome")

        path.write_text(
            "---\n"
            "title: Welcome Updated\n"
            "tags:\n"
            "  - support\n"
            "---\n\n"
            "# Intro\n"
            "Hello again.\n",
            encoding="utf-8",
        )
        updated = self.index.sync_from_disk()
        self.assertEqual(updated["updated"], 1)
        self.assertEqual(self.index.get_document("acme", "welcome")["title"], "Welcome Updated")

        path.unlink()
        deleted = self.index.sync_from_disk()
        self.assertEqual(deleted["deleted"], 1)
        self.assertIsNone(self.index.get_document("acme", "welcome"))

    def test_list_documents_paginates_and_filters(self) -> None:
        for slug, title in [("alpha", "Alpha"), ("beta", "Beta"), ("gamma", "Gamma")]:
            path = self.settings.docs_root / "acme" / f"{slug}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"---\n"
                f"title: {title}\n"
                f"---\n\n"
                f"# {title}\n"
                f"{title} content.\n",
                encoding="utf-8",
            )

        self.index.sync_from_disk()

        self.assertEqual(self.index.count_documents(collection_id="acme"), 3)

        first_page = self.index.list_documents(collection_id="acme", limit=1, offset=0)
        second_page = self.index.list_documents(collection_id="acme", limit=1, offset=1)
        self.assertEqual(len(first_page), 1)
        self.assertEqual(len(second_page), 1)
        self.assertNotEqual(first_page[0]["doc_id"], second_page[0]["doc_id"])

        filtered = self.index.list_documents(collection_id="acme", limit=10, query="Beta")
        self.assertEqual([item["doc_id"] for item in filtered], ["acme/beta"])
        self.assertEqual(self.index.count_documents(collection_id="acme", query="Beta"), 1)

    def test_nested_documents_search_and_guides(self) -> None:
        onboarding = self.settings.docs_root / "acme" / "guides" / "onboarding.md"
        onboarding.parent.mkdir(parents=True, exist_ok=True)
        onboarding.write_text(
            "---\n"
            "kb_role: guide\n"
            "kb_summary: Acme onboarding guide\n"
            "kb_keywords:\n"
            "  - onboarding\n"
            "  - setup\n"
            "kb_use_cases:\n"
            "  - customer onboarding\n"
            "  - implementation\n"
            "kb_priority: 10\n"
            "---\n\n"
            "# Onboarding\n"
            "Use this guide for onboarding.\n",
            encoding="utf-8",
        )

        billing = self.settings.docs_root / "beta" / "faq" / "billing.md"
        billing.parent.mkdir(parents=True, exist_ok=True)
        billing.write_text(
            "# Billing\n"
            "Beta billing help.\n",
            encoding="utf-8",
        )
        password = self.settings.docs_root / "beta" / "faq" / "password.md"
        password.parent.mkdir(parents=True, exist_ok=True)
        password.write_text(
            "---\n"
            "kb_role: faq\n"
            "kb_summary: Password reset help\n"
            "kb_keywords:\n"
            "  - password\n"
            "  - reset\n"
            "---\n\n"
            "# Password Reset\n"
            "To reset your password, open settings.\n",
            encoding="utf-8",
        )
        troubleshooting = self.settings.docs_root / "beta" / "faq" / "troubleshooting.md"
        troubleshooting.parent.mkdir(parents=True, exist_ok=True)
        troubleshooting.write_text(
            "# Troubleshooting\n"
            "If you need to reset your password, follow the password reset guide.\n",
            encoding="utf-8",
        )

        created = self.index.sync_from_disk()
        self.assertEqual(created["created"], 4)

        document = self.index.get_document("acme", "guides/onboarding")
        self.assertIsNotNone(document)
        self.assertEqual(document["doc_id"], "acme/guides/onboarding")
        self.assertEqual(document["relative_path"], "guides/onboarding")
        self.assertEqual(document["kb_role"], "guide")

        all_results = self.index.search(collection_id=None, query="billing", limit=5)
        self.assertTrue(any(item["doc_id"] == "beta/faq/billing" for item in all_results))

        conversational_results = self.index.search(collection_id=None, query="How do I reset my password?", limit=5)
        self.assertGreaterEqual(len(conversational_results), 1)
        self.assertEqual(conversational_results[0]["doc_id"], "beta/faq/password")
        self.assertEqual(conversational_results[0]["source_path"], "faq/password")

        guide = self.index.get_collection_guide("acme")
        self.assertEqual(guide["collection_id"], "acme")
        self.assertEqual(guide["canonical_documents"][0]["doc_id"], "acme/guides/onboarding")
        self.assertIn("onboarding", guide["recommended_queries"])

    def test_answer_question_reranks_definition_docs_and_suggests_direct_answer(self) -> None:
        overview = self.settings.docs_root / "acme" / "company-overview.md"
        overview.parent.mkdir(parents=True, exist_ok=True)
        overview.write_text(
            "---\n"
            "title: Company Overview\n"
            "kb_canonical: true\n"
            "kb_priority: 10\n"
            "---\n\n"
            "# Company Overview\n\n"
            "Acme Cloud is a fictional infrastructure company for developer teams.\n\n"
            "Acme Cloud specializes in build automation and deployment observability.\n",
            encoding="utf-8",
        )
        checklist = self.settings.docs_root / "acme" / "launch-checklist.md"
        checklist.write_text(
            "# Launch Checklist\n\n"
            "This checklist is used by Acme Cloud staff before customer launches.\n\n"
            "- Confirm support rotation.\n"
            "- Confirm release notes.\n",
            encoding="utf-8",
        )
        runbook = self.settings.docs_root / "acme" / "incident-response-runbook.md"
        runbook.write_text(
            "# Incident Response Runbook\n\n"
            "This runbook explains how Acme Cloud handles live incidents.\n",
            encoding="utf-8",
        )

        self.index.sync_from_disk()

        result = self.index.answer_question(question="What is Acme Cloud?", max_documents=3)
        self.assertEqual(result["result"], "ok")
        self.assertEqual(result["answer_mode"], "definition")
        self.assertEqual(result["answer_support"], "supported")
        self.assertEqual(result["evidence"][0]["doc_id"], "acme/company-overview")
        self.assertIn("Acme Cloud is a fictional infrastructure company", result["suggested_answer"])
        self.assertFalse(result["suggested_answer"].startswith("#"))
        self.assertNotIn("Company Overview", result["suggested_answer"])
        self.assertNotIn("appears", result["suggested_answer"].lower())
        self.assertNotIn("seems", result["suggested_answer"].lower())
        self.assertFalse(result["evidence"][0]["content"].startswith("---"))
        self.assertNotIn("kb_canonical", result["evidence"][0]["content"])

    def test_answer_question_does_not_use_demo_question_as_definition_answer(self) -> None:
        overview = self.settings.docs_root / "acme" / "company-overview.md"
        overview.parent.mkdir(parents=True, exist_ok=True)
        overview.write_text(
            "# Company Overview\n\n"
            "Acme Cloud is a fictional infrastructure company for developer teams.\n",
            encoding="utf-8",
        )
        readme = self.settings.docs_root / "acme" / "readme.md"
        readme.write_text(
            "# Demo Questions\n\n"
            "- What is Acme Cloud?\n",
            encoding="utf-8",
        )

        self.index.sync_from_disk()

        result = self.index.answer_question(question="What is Acme?", max_documents=3)
        self.assertEqual(result["answer_support"], "supported")
        self.assertIn("Acme Cloud is a fictional infrastructure company", result["suggested_answer"])
        self.assertNotIn("What is Acme", result["suggested_answer"])

    def test_answer_question_falls_back_from_empty_collection(self) -> None:
        overview = self.settings.docs_root / "acme" / "company-overview.md"
        overview.parent.mkdir(parents=True, exist_ok=True)
        overview.write_text(
            "# Company Overview\n\n"
            "Acme Cloud is a fictional infrastructure company for developer teams.\n",
            encoding="utf-8",
        )

        self.index.sync_from_disk()
        self.index.update_collection_metadata("empty-docs", {"kind": "wiki"})

        result = self.index.answer_question(
            question="What is Acme Cloud?",
            collection_id="empty-docs",
            max_documents=3,
        )
        self.assertEqual(result["result"], "ok")
        self.assertEqual(result["requested_collection_id"], "empty-docs")
        self.assertEqual(result["collection_fallback_from"], "empty-docs")
        self.assertIsNone(result["collection_id"])
        self.assertEqual(result["evidence"][0]["doc_id"], "acme/company-overview")
        self.assertIn("Acme Cloud is a fictional infrastructure company", result["suggested_answer"])

    def test_search_snippets_use_body_content_and_strip_frontmatter(self) -> None:
        policy = self.settings.docs_root / "acme" / "refund-policy.md"
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(
            "---\n"
            "title: Refund Policy\n"
            "kb_role: policy\n"
            "---\n\n"
            "# Refund Policy\n\n"
            "Refunds over 250 euros require Sofia approval.\n",
            encoding="utf-8",
        )
        readme = self.settings.docs_root / "acme" / "readme.md"
        readme.write_text(
            "# Demo Questions\n\n"
            "The knowledgebase does not define a crypto prize payout policy.\n",
            encoding="utf-8",
        )

        self.index.sync_from_disk()

        results = self.index.search(collection_id="acme", query="refunds over 250 euros", limit=3)
        self.assertEqual(results[0]["doc_id"], "acme/refund-policy")
        self.assertIn("Refunds", results[0]["snippet"])
        self.assertNotEqual(results[0]["snippet"], "Refund Policy")
        self.assertFalse(results[0]["excerpt"].startswith("---"))
        self.assertNotIn("kb_role", results[0]["excerpt"])

        answer = self.index.answer_question(
            question="What is the refund policy for over 250 euros?",
            collection_id="acme",
        )
        self.assertEqual(answer["answer_mode"], "policy")
        self.assertIn("Sofia approval", answer["suggested_answer"])
        self.assertNotIn("crypto", answer["suggested_answer"].lower())

    def test_answer_question_ignores_demo_questions_for_undefined_policy(self) -> None:
        overview = self.settings.docs_root / "acme" / "company-overview.md"
        overview.parent.mkdir(parents=True, exist_ok=True)
        overview.write_text(
            "# Company Overview\n\n"
            "The knowledgebase does not define policy for crypto prize payouts, token rewards, or blockchain-based giveaways. "
            "If asked about those topics, the correct response is that no policy is available in the current knowledgebase.\n",
            encoding="utf-8",
        )
        readme = self.settings.docs_root / "acme" / "readme.md"
        readme.write_text(
            "# Demo Questions\n\n"
            "- What is the policy for crypto prize payouts?\n",
            encoding="utf-8",
        )

        self.index.sync_from_disk()

        answer = self.index.answer_question(
            question="What is the policy for crypto prize payouts?",
            collection_id="acme",
        )
        self.assertEqual(answer["answer_support"], "supported")
        self.assertIn("does not define policy", answer["suggested_answer"])
        self.assertNotIn("What is the policy", answer["suggested_answer"])

    def test_answer_question_prioritizes_bot_limits_for_action_requests(self) -> None:
        rules = self.settings.docs_root / "acme" / "discord-community-rules.md"
        rules.parent.mkdir(parents=True, exist_ok=True)
        rules.write_text(
            "# Discord Community Rules\n\n"
            "Moderators may delete spam, move conversations, warn users, or timeout users for up to 24 hours.\n\n"
            "Only Mira Kovac can approve permanent bans during the event.\n\n"
            "The bot may remind users of the rules, but it must not ban, timeout, delete, or moderate users directly unless a future tool explicitly grants that capability.\n",
            encoding="utf-8",
        )

        self.index.sync_from_disk()

        answer = self.index.answer_question(
            question="Can you ban a Discord user who is spamming?",
            collection_id="acme",
        )
        self.assertEqual(answer["answer_support"], "supported")
        self.assertIn("must not ban", answer["suggested_answer"])
        self.assertIn("Moderators may delete spam", answer["suggested_answer"])
        self.assertNotIn("right-click", answer["suggested_answer"].lower())
        self.assertNotIn("select ban", answer["suggested_answer"].lower())

    def test_collection_isolation(self) -> None:
        docs = {
            ("acme", "alpha.md"): "Acme only",
            ("beta", "alpha.md"): "Beta only",
        }
        for (collection_id, filename), content in docs.items():
            path = self.settings.docs_root / collection_id / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        self.index.sync_from_disk()

        acme_results = self.index.search(collection_id="acme", query="Acme", limit=10)
        beta_results = self.index.search(collection_id="beta", query="Beta", limit=10)
        self.assertEqual([item["collection_id"] for item in acme_results], ["acme"])
        self.assertEqual([item["collection_id"] for item in beta_results], ["beta"])

    def test_collection_metadata_backlinks_and_writes(self) -> None:
        raw_manifest = self.settings.docs_root / "raw" / ".kb_collection.yaml"
        raw_manifest.parent.mkdir(parents=True, exist_ok=True)
        raw_manifest.write_text(
            "kind: raw\n"
            "mutable: false\n"
            "summary: Raw source evidence\n"
            "source_collections: []\n",
            encoding="utf-8",
        )

        source = self.settings.docs_root / "wiki" / "source.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "# Source\n"
            "Primary evidence.\n",
            encoding="utf-8",
        )

        derived = self.settings.docs_root / "wiki" / "notes" / "summary.md"
        derived.parent.mkdir(parents=True, exist_ok=True)
        derived.write_text(
            "---\n"
            "kb_origin: compiled\n"
            "kb_source_docs:\n"
            "  - wiki/source\n"
            "---\n\n"
            "# Summary\n"
            "Derived note.\n",
            encoding="utf-8",
        )

        self.index.sync_from_disk()

        collections = {item["collection_id"]: item for item in self.index.list_collections()}
        self.assertIn("raw", collections)
        self.assertEqual(collections["raw"]["kind"], "raw")
        self.assertFalse(collections["raw"]["mutable"])
        self.assertEqual(collections["raw"]["summary"], "Raw source evidence")
        self.assertEqual(collections["raw"]["document_count"], 0)

        derived_doc = self.index.get_document("wiki", "notes/summary")
        self.assertIsNotNone(derived_doc)
        self.assertEqual(derived_doc["kb_origin"], "compiled")
        self.assertEqual(derived_doc["kb_source_docs"], ["wiki/source"])

        source_doc = self.index.get_document("wiki", "source")
        self.assertIsNotNone(source_doc)
        self.assertIn("wiki/notes/summary", source_doc["kb_backlinks"])

        created = self.index.create_document(
            collection_id="wiki",
            doc_path="drafts/alpha",
            raw_text="# Alpha\nHello from the wiki.\n",
            title="Alpha",
        )
        self.assertEqual(created["document"]["doc_id"], "wiki/drafts/alpha")

        appended = self.index.append_section(
            collection_id="wiki",
            doc_path="drafts/alpha",
            heading="Next Steps",
            content="Keep expanding the note.",
        )
        self.assertIn("Next Steps", appended["document"]["content"])

        updated = self.index.update_document_metadata(
            collection_id="wiki",
            doc_path="drafts/alpha",
            tags=["alpha", "wiki"],
            frontmatter_patch={"kb_summary": "Updated summary"},
        )
        self.assertEqual(updated["document"]["tags"], ["alpha", "wiki"])
        self.assertEqual(updated["document"]["frontmatter"]["kb_summary"], "Updated summary")

        guide = self.index.get_collection_guide("wiki")
        self.assertEqual(guide["kind"], "wiki")
        self.assertTrue(guide["mutable"])
        self.assertGreaterEqual(guide["doc_count"], 3)


if __name__ == "__main__":
    unittest.main()
