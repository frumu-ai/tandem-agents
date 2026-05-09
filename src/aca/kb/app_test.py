from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from .app import create_app
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
        answer_default_documents=3,
        answer_max_documents=5,
        answer_max_chars_per_doc=8000,
    )


class KnowledgebaseAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = _make_settings(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_health_admin_and_mcp_routes(self) -> None:
        raw_manifest = self.settings.docs_root / "raw" / ".kb_collection.yaml"
        raw_manifest.parent.mkdir(parents=True, exist_ok=True)
        raw_manifest.write_text(
            "kind: raw\n"
            "mutable: false\n"
            "summary: Raw source evidence\n"
            "source_collections: []\n",
            encoding="utf-8",
        )

        app = create_app(self.settings)

        with TestClient(app) as client:
            self.assertEqual(client.get("/health").json()["status"], "ok")

            unauthorized = client.get("/admin/collections")
            self.assertEqual(unauthorized.status_code, 401)

            headers = {"Authorization": "Bearer secret"}
            upload = client.post(
                "/admin/documents",
                data={
                    "collection_id": "acme",
                    "slug": "guides/welcome",
                    "title": "Welcome",
                    "tags": "support, onboarding",
                },
                files={
                    "file": (
                        "welcome.md",
                        b"---\ntitle: Old Title\ntags:\n  - legacy\n---\n\n# Welcome\nHello from Acme.",
                        "text/markdown",
                    )
                },
                headers=headers,
            )
            self.assertEqual(upload.status_code, 200)
            uploaded_document = upload.json()["document"]
            self.assertEqual(uploaded_document["doc_id"], "acme/guides/welcome")
            self.assertEqual(uploaded_document["title"], "Welcome")
            self.assertEqual(uploaded_document["tags"], ["support", "onboarding"])
            self.assertEqual(uploaded_document["relative_path"], "guides/welcome")
            self.assertEqual(uploaded_document["source_path"], "guides/welcome")

            acme_upload = client.post(
                "/admin/documents",
                data={
                    "collection_id": "acme",
                    "slug": "guides/getting-started",
                    "title": "Getting Started",
                },
                files={
                    "file": (
                        "getting-started.md",
                        b"# Getting Started\nStart here.\n",
                        "text/markdown",
                    )
                },
                headers=headers,
            )
            self.assertEqual(acme_upload.status_code, 200)
            self.assertEqual(acme_upload.json()["document"]["doc_id"], "acme/guides/getting-started")

            beta_upload = client.post(
                "/admin/documents",
                data={"collection_id": "beta", "slug": "faq/billing"},
                files={
                    "file": (
                        "billing.md",
                        b"---\nkb_role: faq\nkb_summary: Beta billing help\n---\n\n# Billing\nBeta billing help.",
                        "text/markdown",
                    )
                },
                headers=headers,
            )
            self.assertEqual(beta_upload.status_code, 200)
            self.assertEqual(beta_upload.json()["document"]["doc_id"], "beta/faq/billing")
            self.assertEqual(beta_upload.json()["document"]["kb_role"], "faq")

            collections = client.get("/admin/collections", headers=headers)
            self.assertEqual(collections.status_code, 200)
            self.assertEqual(collections.json()["collections"][0]["collection_id"], "acme")
            self.assertIn("summary", collections.json()["collections"][0])
            self.assertIn("key_topics", collections.json()["collections"][0])

            first_page = client.get(
                "/admin/documents",
                params={"collection_id": "acme", "limit": 1, "offset": 0},
                headers=headers,
            )
            self.assertEqual(first_page.status_code, 200)
            self.assertEqual(first_page.json()["collection_id"], "acme")
            self.assertEqual(first_page.json()["total"], 2)
            self.assertEqual(first_page.json()["limit"], 1)
            self.assertEqual(first_page.json()["offset"], 0)
            self.assertTrue(first_page.json()["has_more"])
            self.assertEqual(len(first_page.json()["documents"]), 1)

            second_page = client.get(
                "/admin/documents",
                params={"collection_id": "acme", "limit": 1, "offset": 1},
                headers=headers,
            )
            self.assertEqual(second_page.status_code, 200)
            self.assertEqual(second_page.json()["total"], 2)
            self.assertEqual(len(second_page.json()["documents"]), 1)
            self.assertNotEqual(
                first_page.json()["documents"][0]["doc_id"],
                second_page.json()["documents"][0]["doc_id"],
            )

            list_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 200,
                    "method": "tools/call",
                    "params": {
                        "name": "list_documents",
                        "arguments": {"collection_id": "acme", "limit": 1, "offset": 1},
                    },
                },
            )
            self.assertEqual(list_call.status_code, 200)
            list_result = list_call.json()["result"]
            self.assertEqual(list_result["collection_id"], "acme")
            self.assertEqual(list_result["total"], 2)
            self.assertEqual(list_result["limit"], 1)
            self.assertEqual(list_result["offset"], 1)
            self.assertEqual(len(list_result["documents"]), 1)

            mcp_list = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            self.assertEqual(mcp_list.status_code, 200)
            tool_names = [tool["name"] for tool in mcp_list.json()["result"]["tools"]]
            self.assertIn("search_docs", tool_names)
            self.assertIn("answer_question", tool_names)
            self.assertIn("get_kb_guide", tool_names)
            self.assertIn("get_collection_guide", tool_names)
            self.assertIn("create_document", tool_names)
            self.assertIn("append_section", tool_names)
            self.assertIn("update_collection_metadata", tool_names)
            self.assertIn("propose_document_change", tool_names)
            self.assertIn("apply_proposed_change", tool_names)
            self.assertIn("lint_collection", tool_names)

            mcp_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "search_docs",
                        "arguments": {"query": "billing", "limit": 5},
                    },
                },
            )
            self.assertEqual(mcp_call.status_code, 200)
            search_result = mcp_call.json()["result"]
            self.assertIsNone(search_result["collection_id"])
            self.assertTrue(search_result["results"])
            hit_doc_id = search_result["results"][0]["doc_id"]
            self.assertEqual(hit_doc_id, "beta/faq/billing")
            self.assertEqual(search_result["results"][0]["kb_role"], "faq")
            self.assertEqual(search_result["results"][0]["source_path"], "faq/billing")

            answer_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 250,
                    "method": "tools/call",
                    "params": {
                        "name": "answer_question",
                        "arguments": {"question": "How does Beta billing work?"},
                    },
                },
            )
            self.assertEqual(answer_call.status_code, 200)
            answer_result = answer_call.json()["result"]
            self.assertEqual(answer_result["result"], "ok")
            self.assertEqual(answer_result["question"], "How does Beta billing work?")
            self.assertEqual(answer_result["answer_mode"], "procedure")
            self.assertIn(answer_result["answer_support"], {"supported", "partial"})
            self.assertIn("suggested_answer", answer_result)
            self.assertTrue(answer_result["evidence"])
            top = answer_result["evidence"][0]
            self.assertEqual(top["doc_id"], "beta/faq/billing")
            self.assertEqual(top["source_label"], "Billing")
            self.assertIn("Beta billing help", top["content"])
            self.assertFalse(top["content"].startswith("---"))
            self.assertFalse(top["content_truncated"])
            self.assertIn("answer_guidance", answer_result)

            empty_answer_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 251,
                    "method": "tools/call",
                    "params": {
                        "name": "answer_question",
                        "arguments": {"question": "completely-unrelated-xyzzy-term"},
                    },
                },
            )
            self.assertEqual(empty_answer_call.status_code, 200)
            empty_answer = empty_answer_call.json()["result"]
            self.assertEqual(empty_answer["result"], "no_matches")
            self.assertEqual(empty_answer["answer_support"], "unsupported")
            self.assertEqual(empty_answer["suggested_answer"], "")
            self.assertEqual(empty_answer["evidence"], [])

            missing_question_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 252,
                    "method": "tools/call",
                    "params": {"name": "answer_question", "arguments": {}},
                },
            )
            self.assertEqual(missing_question_call.status_code, 400)

            document_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "get_document", "arguments": {"doc_id": hit_doc_id}},
                },
            )
            self.assertEqual(document_call.status_code, 200)
            self.assertEqual(document_call.json()["result"]["document"]["doc_id"], hit_doc_id)
            self.assertEqual(document_call.json()["result"]["document"]["source_path"], "faq/billing")
            self.assertFalse(document_call.json()["result"]["document"]["content"].startswith("---"))

            collection_guide_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "get_collection_guide", "arguments": {"collection_id": "acme"}},
                },
            )
            self.assertEqual(collection_guide_call.status_code, 200)
            acme_guide = collection_guide_call.json()["result"]["guide"]
            self.assertEqual(acme_guide["collection_id"], "acme")
            self.assertEqual(acme_guide["canonical_documents"][0]["doc_id"], "acme/guides/welcome")

            guide_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "get_kb_guide", "arguments": {}},
                },
            )
            self.assertEqual(guide_call.status_code, 200)
            guide = guide_call.json()["result"]["guide"]
            self.assertEqual(guide["server_name"], "ac.tandem/kb-mcp")
            self.assertIn("current_collections", guide)
            self.assertIn("collection_guides", guide)
            self.assertIn("compiled_wiki_model", guide)
            self.assertIn("raw", [item["collection_id"] for item in guide["current_collections"]])

            create_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "create_document",
                        "arguments": {
                            "collection_id": "wiki",
                            "doc_path": "drafts/alpha",
                            "raw_text": "# Alpha\nHello from the wiki.\n",
                            "title": "Alpha",
                            "tags": ["alpha", "wiki"],
                        },
                    },
                },
            )
            self.assertEqual(create_call.status_code, 200)
            create_result = create_call.json()["result"]["document"]
            self.assertEqual(create_result["doc_id"], "wiki/drafts/alpha")
            self.assertEqual(create_result["kb_origin"], "admin")

            append_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "append_section",
                        "arguments": {
                            "collection_id": "wiki",
                            "doc_path": "drafts/alpha",
                            "heading": "Next Steps",
                            "content": "Keep expanding the note.",
                        },
                    },
                },
            )
            self.assertEqual(append_call.status_code, 200)
            self.assertIn("Next Steps", append_call.json()["result"]["document"]["content"])

            update_metadata_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {
                        "name": "update_document_metadata",
                        "arguments": {
                            "collection_id": "wiki",
                            "doc_path": "drafts/alpha",
                            "tags": ["alpha", "updated"],
                            "frontmatter_patch": {"kb_summary": "Updated summary"},
                        },
                    },
                },
            )
            self.assertEqual(update_metadata_call.status_code, 200)
            updated_doc = update_metadata_call.json()["result"]["document"]
            self.assertEqual(updated_doc["tags"], ["alpha", "updated"])
            self.assertEqual(updated_doc["frontmatter"]["kb_summary"], "Updated summary")

            propose_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {
                        "name": "propose_document_change",
                        "arguments": {
                            "collection_id": "wiki",
                            "operation": "create",
                            "doc_path": "drafts/proposed",
                            "raw_text": "# Proposed\nThis came from a staged change.\n",
                            "title": "Proposed",
                        },
                    },
                },
            )
            self.assertEqual(propose_call.status_code, 200)
            proposal = propose_call.json()["result"]["proposal"]
            self.assertEqual(proposal["status"], "pending")
            change_id = proposal["change_id"]

            list_proposals_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "list_proposed_changes",
                        "arguments": {"collection_id": "wiki"},
                    },
                },
            )
            self.assertEqual(list_proposals_call.status_code, 200)
            self.assertTrue(list_proposals_call.json()["result"]["changes"])

            apply_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {"name": "apply_proposed_change", "arguments": {"change_id": change_id}},
                },
            )
            self.assertEqual(apply_call.status_code, 200)
            self.assertEqual(apply_call.json()["result"]["status"], "applied")

            get_proposed_doc = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 12,
                    "method": "tools/call",
                    "params": {"name": "get_document", "arguments": {"doc_id": "wiki/drafts/proposed"}},
                },
            )
            self.assertEqual(get_proposed_doc.status_code, 200)
            self.assertEqual(get_proposed_doc.json()["result"]["document"]["doc_id"], "wiki/drafts/proposed")

            lint_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 13,
                    "method": "tools/call",
                    "params": {"name": "lint_collection", "arguments": {"collection_id": "wiki"}},
                },
            )
            self.assertEqual(lint_call.status_code, 200)
            self.assertEqual(lint_call.json()["result"]["lint"]["collection_id"], "wiki")

            reindex_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 14,
                    "method": "tools/call",
                    "params": {"name": "reindex_collection", "arguments": {"collection_id": "wiki"}},
                },
            )
            self.assertEqual(reindex_call.status_code, 200)
            self.assertEqual(reindex_call.json()["result"]["collection_id"], "wiki")

            raw_create_call = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 15,
                    "method": "tools/call",
                    "params": {
                        "name": "create_document",
                        "arguments": {
                            "collection_id": "raw",
                            "doc_path": "evidence/source",
                            "raw_text": "Raw docs stay immutable.",
                        },
                    },
                },
            )
            self.assertEqual(raw_create_call.status_code, 400)
            self.assertIn("error", raw_create_call.json())
            self.assertIn("not writable", raw_create_call.json()["error"]["message"].lower())


if __name__ == "__main__":
    unittest.main()
