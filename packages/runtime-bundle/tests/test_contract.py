import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tandem_runtime_bundle import build_security_bundle, validate_keyring
from tandem_runtime_bundle.prepare import prepare_security
from fixtures import DEPLOYMENT, ORGANIZATION, inputs, keyring, provisioned_paths

REPO = Path(__file__).resolve().parents[3]


class ContractTests(unittest.TestCase):
    def test_current_profile_has_all_runtime_prerequisites(self):
        bundle = build_security_bundle(inputs())
        env = bundle["engine_environment"]
        self.assertEqual(env["TANDEM_RUNTIME_AUTH_MODE"], "hosted_single_tenant")
        self.assertEqual(env["TANDEM_CONTEXT_ASSERTION_REPLAY_MODE"], "bound")
        self.assertTrue(env["TANDEM_CONTEXT_ASSERTION_REPLAY_STORE_FILE"].endswith(".sqlite3"))
        self.assertNotIn("TANDEM_CONTEXT_ASSERTION_PUBLIC_KEYS", env)
        self.assertEqual(len(bundle["engine_mounts"]), 3)
        self.assertTrue(bundle["engine_mounts"][0]["read_only"])
        self.assertEqual(bundle["container_security"]["cap_drop"], ["ALL"])
        self.assertTrue(bundle["container_security"]["read_only"])
        self.assertEqual(bundle["panel_hosted"]["deployment_id"], DEPLOYMENT)
        self.assertEqual(bundle["panel_hosted"]["auth"]["panel_exchange_url"],
                         f"https://identity.example.com/api/v1/hosted/deployments/{DEPLOYMENT}/panel/exchange")

    def test_unsupported_and_incomplete_profiles_fail_closed(self):
        cases = {
            "HOSTED_RUNTIME_SECURITY_VERSION": "2", "HOSTED_RUNTIME_AUTH_MODE": "local_single_tenant",
            "HOSTED_STORAGE_PROFILE": "shared", "HOSTED_ENABLE_OUTBOX": "true",
            "HOSTED_PLATFORM": "linux/arm64", "HOSTED_TANDEM_ENGINE_RELEASE_VERSION": "0.7.1",
            "HOSTED_TANDEM_CONTROL_PANEL_RELEASE_VERSION": "latest", "HOSTED_ENGINE_IMAGE": "engine:latest",
            "HOSTED_DEPLOYMENT_ID": "", "HOSTED_ORGANIZATION_ID": "",
            "HOSTED_CONTROL_PLANE_URL": "http://identity.example.com",
            "HOSTED_CONTROL_PANEL_PUBLIC_URL": "https://user@panel.example.com",
            "HOSTED_AUDIT_ANCHOR_ROOT": "/srv/tandem/test/anchors", "HOSTED_HOST_UID": "0",
            "HOSTED_REPLAY_ROOT": "/srv/tandem/test/tandem-engine-state/data",
            "HOSTED_INSTALL_ROOT": "/srv/tandem/../test",
            "HOSTED_REPOS_ROOT": "/var/lib/tandem-audit/test",
            "HOSTED_PANEL_AUTH_ROOT": "/srv/tandem/test/tandem-data/auth",
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                values = inputs()
                values[name] = value
                with self.assertRaises(ValueError):
                    build_security_bundle(values)

    def test_auth_schema_and_supported_loopback_addresses(self):
        values = inputs()
        values["HOSTED_LOGIN_BASE_URL"] = "https://login.example.com"
        hosted = build_security_bundle(values)["panel_hosted"]
        self.assertEqual(hosted["auth"]["mode"], "hosted")
        self.assertEqual(hosted["auth"]["panel_login_url"], "https://login.example.com/hosted/panel/authorize")
        self.assertEqual(hosted["auth"]["host_agent_token_file"], "/run/tandem-panel-auth/host-agent-token")
        for origin in ("http://localhost", "http://127.0.0.2"):
            values["HOSTED_CONTROL_PLANE_URL"] = origin
            with self.assertRaises(ValueError):
                build_security_bundle(values)
        for origin in ("http://127.0.0.1", "http://[::1]"):
            values["HOSTED_CONTROL_PLANE_URL"] = origin
            self.assertEqual(build_security_bundle(values)["panel_hosted"]["auth"]["control_plane_url"], origin)

    def test_keyring_requires_explicit_deployment_scope(self):
        validate_keyring(keyring(), DEPLOYMENT, ORGANIZATION)
        for name in ("deployment_id", "organization_id", "allowed_audiences", "purpose", "status", "public_key"):
            with self.subTest(name=name):
                document = keyring()
                document["test-key"].pop(name)
                with self.assertRaises(ValueError):
                    validate_keyring(document, DEPLOYMENT, ORGANIZATION)
        with self.assertRaises(ValueError):
            validate_keyring(keyring(), ORGANIZATION, DEPLOYMENT)

    @unittest.skipUnless(os.environ.get("TANDEM_TEST_PANEL"), "released panel package required")
    def test_released_panel_consumes_auth_configuration(self):
        values = inputs()
        values["HOSTED_LOGIN_BASE_URL"] = "https://login.example.com"
        config = {"hosted": build_security_bundle(values)["panel_hosted"]}
        subprocess.run([os.environ.get("TANDEM_TEST_NODE", "node"), str(Path(__file__).with_name("panel_consumer.mjs"))],
                       input=json.dumps(config), text=True, check=True)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Linux non-root provisioning required")
    def test_provision_preserves_keys_and_replay_and_rejects_missing_audit_key(self):
        with tempfile.TemporaryDirectory() as temp:
            values, bundle, source = provisioned_paths(temp)
            prepare_security(bundle, keyring(), source)
            security = Path(bundle["host_paths"]["security"])
            audit = security / "audit-hmac-key"
            initial = audit.read_bytes()
            self.assertEqual(audit.stat().st_mode & 0o777, 0o600)
            self.assertEqual(security.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(Path(bundle["host_paths"]["replay"]).iterdir()), [])
            panel_auth = Path(bundle["host_paths"]["panel_auth"])
            self.assertEqual((panel_auth / "host-agent-token").read_bytes(), source.read_bytes())
            config = json.loads((panel_auth / "control-panel-config.json").read_text())
            self.assertEqual(config["hosted"], bundle["panel_hosted"])
            prepare_security(bundle, keyring(), source)
            self.assertEqual(audit.read_bytes(), initial)
            audit.unlink()
            with self.assertRaisesRegex(ValueError, "authorized recovery"):
                prepare_security(bundle, keyring(), source)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Linux non-root provisioning required")
    def test_provision_rejects_symlinks_and_insecure_existing_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            values, bundle, source = provisioned_paths(temp)
            prepare_security(bundle, keyring(), source)
            anchor = Path(bundle["host_paths"]["anchor"])
            anchor.chmod(0o755)
            with self.assertRaises(ValueError):
                prepare_security(bundle, keyring(), source)
            anchor.chmod(0o700)
            linked = Path(temp) / "linked"
            linked.symlink_to(anchor)
            altered = copy.deepcopy(bundle)
            altered["host_paths"]["anchor"] = str(linked)
            with self.assertRaises(ValueError):
                prepare_security(altered, keyring(), source)
            altered = copy.deepcopy(bundle)
            altered["ordinary_paths"]["REPOS"] = str(linked)
            with self.assertRaises(ValueError):
                prepare_security(altered, keyring(), source)

    @unittest.skipUnless(os.name == "posix", "packaged Bash renderers require Linux")
    def test_shell_renderers_consume_the_shared_contract(self):
        import yaml
        values = inputs()
        env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **values}
        script = REPO / "scripts" / "hosted"
        compose_result = subprocess.run(["bash", str(script / "render-compose.sh")], env=env,
                                        capture_output=True, text=True, check=True)
        compose = yaml.safe_load(compose_result.stdout)
        panel = json.loads(subprocess.check_output(["bash", str(script / "render-control-panel-config.sh")], env=env))
        bundle = build_security_bundle(values)
        engine = compose["services"]["tandem-engine"]
        for name, value in bundle["engine_environment"].items():
            self.assertEqual(engine["environment"][name], value)
        for mount in bundle["engine_mounts"]:
            self.assertIn(mount, engine["volumes"])
        for name, value in bundle["panel_hosted"].items():
            self.assertEqual(panel["hosted"][name], value)
        for name, value in bundle["container_security"].items():
            self.assertEqual(engine[name], value)
        panel_service = compose["services"]["tandem-control-panel"]
        self.assertEqual(panel_service["environment"]["TANDEM_CONTROL_PANEL_CONFIG_FILE"],
                         "/run/tandem-panel-auth/control-panel-config.json")
        for mount in bundle["panel_mounts"]:
            self.assertIn(mount, panel_service["volumes"])
            self.assertNotIn(mount, engine["volumes"])
        for name, value in {"HOSTED_RUNTIME_SECURITY_VERSION": "2", "HOSTED_PLATFORM": "linux/arm64"}.items():
            for renderer in ("render-compose.sh", "render-runtime-env.sh", "render-control-panel-config.sh"):
                result = subprocess.run(["bash", str(script / renderer)], env={**env, name: value},
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, (name, renderer))

    @unittest.skipUnless(os.name == "posix", "packaged Bash renderers require Linux")
    def test_packaged_bundle_keeps_shared_module_and_valid_compose(self):
        import yaml
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            values = inputs(root / "install", root / "anchors")
            values["HOSTED_BUNDLE_DIR"] = str(root / "bundle")
            env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **values}
            subprocess.run(["bash", str(REPO / "scripts/hosted/package-bundle.sh")],
                           env=env, capture_output=True, text=True, check=True)
            manifest = json.loads((root / "bundle/runtime-security.json").read_text())
            self.assertEqual(manifest, build_security_bundle(values))
            self.assertTrue((root / "bundle/tandem_runtime_bundle/prepare.py").is_file())
            self.assertFalse((root / "bundle/audit-hmac-key").exists())
            self.assertFalse((root / "bundle/host_agent_token").exists())
            compose = root / "bundle/docker-compose.hosted.yml"
            self.assertEqual(yaml.safe_load(compose.read_text())["services"]["tandem-engine"]["user"],
                             manifest["container_security"]["user"])
            subprocess.run(["docker", "compose", "-f", str(compose), "config", "--quiet"],
                           env=env, capture_output=True, text=True, check=True)
            # The copy shipped in the archive imports its own module without a pip install.
            subprocess.run(["python3", str(root / "bundle/runtime-security.py"), "render"],
                           env=env, cwd=root, capture_output=True, text=True, check=True)


if __name__ == "__main__":
    unittest.main()
