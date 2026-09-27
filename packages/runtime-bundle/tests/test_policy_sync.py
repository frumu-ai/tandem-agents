from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from fixtures import DEPLOYMENT, ORGANIZATION, inputs
from policy_tls_fixture import tls_endpoint
from tandem_runtime_bundle import build_security_bundle
from tandem_runtime_bundle.policy_contract import POLICY_ENGINE_REVISION, service_files
from tandem_runtime_bundle.policy_sync import decode_policy, fetch_policy, PolicySyncError

TOKEN = "synthetic-policy-host-token-" + "a" * 32


def document(**changes):
    return json.dumps({"schema_version": 1, "policy_version": 7,
        "organization_id": ORGANIZATION, "deployment_id": DEPLOYMENT,
        "generated_at": datetime.now(timezone.utc).isoformat(), "users": [],
        "org_units": [], "org_unit_memberships": [], "deployment_grants": [], **changes}).encode()


class PolicySyncTests(unittest.TestCase):
    def test_authenticated_tls_fetch_uses_exact_deployment_and_preserves_source_time(self):
        body = document()
        with tls_endpoint(body) as (url, context, seen):
            self.assertEqual(fetch_policy(url, ORGANIZATION, DEPLOYMENT, TOKEN, tls_context=context), body)
            self.assertEqual(seen, [(f"/api/v1/hosted/agent/deployments/{DEPLOYMENT}/policy-bundle", f"Bearer {TOKEN}")])

    def test_untrusted_tls_rejects_before_sending_credentials(self):
        with tls_endpoint(document()) as (url, _, seen):
            with self.assertRaisesRegex(PolicySyncError, "transport_failed"):
                fetch_policy(url, ORGANIZATION, DEPLOYMENT, TOKEN)
            self.assertEqual(seen, [])

    def test_redirect_and_error_never_follow_or_expose_response_details(self):
        for status in (301, 302, 307, 401, 403, 500):
            with self.subTest(status=status), tls_endpoint(TOKEN.encode(), status=status,
                    headers={"Location": "https://other.example.com/collect"}) as (url, context, seen):
                with self.assertRaisesRegex(PolicySyncError, "^policy_control_plane_http_error$"):
                    fetch_policy(url, ORGANIZATION, DEPLOYMENT, TOKEN, tls_context=context)
                self.assertEqual(len(seen), 1)

    def test_response_bounds_content_type_encoding_and_truncation(self):
        body = document()
        for headers in ({"Content-Length": str(4 * 1024 * 1024 + 1)},
                        {"Content-Length": str(len(body) + 1)},
                        {"Content-Type": "text/html"}, {"Content-Encoding": "gzip"}):
            with self.subTest(headers=headers), tls_endpoint(body, headers=headers) as (url, context, _):
                with self.assertRaises(PolicySyncError):
                    fetch_policy(url, ORGANIZATION, DEPLOYMENT, TOKEN, tls_context=context)

    def test_invalid_config_is_rejected_before_network(self):
        with patch("http.client.HTTPSConnection") as connection:
            for url in ("http://127.0.0.1", "https://user:pass@example.com", "https://example.com?next=elsewhere"):
                with self.assertRaises(PolicySyncError):
                    fetch_policy(url, ORGANIZATION, DEPLOYMENT, TOKEN)
            with self.assertRaises(PolicySyncError):
                fetch_policy("https://example.com", ORGANIZATION, DEPLOYMENT, TOKEN + "\r\nX-Evil: yes")
            connection.assert_not_called()

    def test_policy_scope_revision_freshness_and_duplicate_fields(self):
        now = datetime.now(timezone.utc)
        cases = [document(organization_id=DEPLOYMENT), document(deployment_id=ORGANIZATION),
                 document(policy_version=True), document(policy_version=0), document(schema_version=True),
                 document(generated_at=(now - timedelta(seconds=121)).isoformat()),
                 document(generated_at=(now + timedelta(seconds=6)).isoformat()),
                 document(generated_at=now.replace(tzinfo=None).isoformat()), document(users=None),
                 b'{"schema_version":1,"schema_version":1}', b'{"truncated":']
        for body in cases:
            with self.subTest(body=body), self.assertRaises(PolicySyncError):
                decode_policy(body, ORGANIZATION, DEPLOYMENT, now=now)

    def test_v2_requires_policy_capable_engine_and_isolates_policy_mount(self):
        values = {**inputs(), "HOSTED_RUNTIME_SECURITY_VERSION": "2"}
        with self.assertRaisesRegex(ValueError, "engine source revision"):
            build_security_bundle(values)
        values["HOSTED_TANDEM_ENGINE_SOURCE_REVISION"] = POLICY_ENGINE_REVISION
        bundle = build_security_bundle(values)
        self.assertEqual(bundle["profile"], "hosted-single-node-v2")
        self.assertEqual(bundle["engine_environment"]["TANDEM_HOSTED_ORGANIZATION_ID"], ORGANIZATION)
        self.assertEqual(bundle["engine_environment"]["TANDEM_HOSTED_POLICY_FILE"], "/run/tandem-hosted-policy/current.json")
        self.assertTrue(bundle["engine_mounts"][-1]["read_only"])
        self.assertNotIn("policy", str(bundle["panel_mounts"]))
        for root in (bundle["host_paths"]["security"], bundle["ordinary_paths"]["DATA"]):
            with self.assertRaises(ValueError):
                build_security_bundle({**values, "HOSTED_POLICY_ROOT": root})
        files = service_files(bundle, "/srv/tandem/test/hosted-agent", "/srv/tandem/test/hosted-agent/agent-token")
        self.assertIn("TimeoutStartSec=15", files["service"])
        self.assertIn("OnUnitInactiveSec=30s", files["timer"])
        self.assertNotIn(TOKEN, json.dumps(files))
        self.assertNotIn("update-agent", files["service"])


if __name__ == "__main__":
    unittest.main()
