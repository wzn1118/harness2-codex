from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harnesspilot.broker import Broker, validate_request
from harnesspilot.budget import Budget, BudgetExceeded, Limits
from harnesspilot.config import ModelConfig
from harnesspilot.files import SecureRoot, inventory, relative
from harnesspilot.redaction import redact_text
from harnesspilot.sandbox import Sandbox
from harnesspilot.suite import Suite


def write_codex_home(home: Path, auth: dict) -> tuple[bytes, bytes]:
    home.mkdir(parents=True, exist_ok=True)
    config = (
        'model = "fixture-model"\n'
        'model_provider = "fixture"\n'
        'model_reasoning_effort = "high"\n'
        '[model_providers.fixture]\n'
        'wire_api = "responses"\n'
        'base_url = "https://provider.invalid/v1"\n'
    ).encode()
    auth_bytes = json.dumps(auth).encode()
    (home / "config.toml").write_bytes(config)
    (home / "auth.json").write_bytes(auth_bytes)
    return config, auth_bytes


class ConfigAndRedactionTests(unittest.TestCase):
    def test_current_apikey_config_is_read_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config_before, auth_before = write_codex_home(
                home, {"auth_mode": "apikey", "OPENAI_API_KEY": "fixture-secret-value"})
            loaded = ModelConfig.load(home)
            self.assertEqual(loaded.public()["model"], "fixture-model")
            self.assertEqual(loaded.public()["wire_api"], "responses")
            self.assertEqual((home / "config.toml").read_bytes(), config_before)
            self.assertEqual((home / "auth.json").read_bytes(), auth_before)
            self.assertNotIn("fixture-secret-value", repr(loaded))

    def test_process_local_endpoint_override_does_not_mutate_codex_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config_before, auth_before = write_codex_home(
                home, {"auth_mode": "apikey", "OPENAI_API_KEY": "fixture-secret-value"})
            with patch.dict(os.environ, {"HARNESSPILOT_API_BASE_URL": ""}):
                original = ModelConfig.load(home)
            with patch.dict(os.environ, {
                "HARNESSPILOT_API_BASE_URL": "https://api.a6api.com/v1"
            }):
                overridden = ModelConfig.load(home)
            self.assertEqual(overridden.base_url, "https://api.a6api.com/v1")
            self.assertEqual(overridden.public()["endpoint_host"], "api.a6api.com")
            self.assertNotEqual(overridden.fingerprint, original.fingerprint)
            self.assertEqual((home / "config.toml").read_bytes(), config_before)
            self.assertEqual((home / "auth.json").read_bytes(), auth_before)

    def test_oauth_auth_is_rejected_without_conversion(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            _, auth_before = write_codex_home(
                home, {"auth_mode": "chatgpt", "tokens": {"access_token": "fixture-oauth"}})
            with self.assertRaisesRegex(ValueError, "不会把会话认证转换成 API key"):
                ModelConfig.load(home)
            self.assertEqual((home / "auth.json").read_bytes(), auth_before)

    def test_known_and_configured_credentials_are_redacted(self):
        text = (
            'api_key="fixture-assigned-secret" '
            'Authorization: Bearer fixture-bearer-token-12345 '
            'extra=fixture-provider-secret-value'
        )
        result = redact_text(text, ["fixture-provider-secret-value"])
        for secret in ("fixture-assigned-secret", "fixture-bearer-token-12345",
                       "fixture-provider-secret-value"):
            self.assertNotIn(secret, result)

    @unittest.skipUnless(os.name == "posix", "Linux directory-handle APIs required")
    def test_inventory_skips_credentials_and_keeps_workspace_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("provider=fixture-private-value", encoding="utf-8")
            (root / ".env").write_text("TOKEN=fixture-env-secret", encoding="utf-8")
            (root / "auth.json").write_text('{"key":"fixture-auth-secret"}', encoding="utf-8")
            original = (root / "README.md").read_bytes()
            result = inventory(root, secrets=["fixture-private-value"])
            self.assertEqual(set(result), {"README.md"})
            self.assertNotIn("fixture-private-value", result["README.md"])
            self.assertEqual((root / "README.md").read_bytes(), original)

    @unittest.skipUnless(os.name == "posix", "Linux directory-handle APIs required")
    def test_inventory_skips_large_binary_with_bounded_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "video.mp4"
            media.write_bytes(b"\0ftypmp42" + b"x" * 1_200_000)
            self.assertEqual(inventory(root, skip_binary=True), {})
            self.assertGreater(media.stat().st_size, 1_048_576)

    @unittest.skipUnless(os.name == "posix", "Linux directory-handle APIs required")
    def test_inventory_rejects_large_utf8_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "large.txt").write_text("汉" * 400_000, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "超过单文件大小限制"):
                inventory(root, skip_binary=True)


class BoundaryTests(unittest.TestCase):
    def make_config(self) -> ModelConfig:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        write_codex_home(home, {"auth_mode": "apikey", "OPENAI_API_KEY": "fixture-key-value"})
        return ModelConfig.load(home)

    def request(self) -> dict:
        return {
            "model": "fixture-model",
            "reasoning": {"effort": "high"},
            "input": "fixture prompt",
            "tools": [{"type": "function", "name": "local_tool", "parameters": {}}],
        }

    def test_broker_accepts_only_approved_local_request_shape(self):
        validate_request(self.request(), self.make_config())

    def test_broker_rejects_model_remote_files_remote_tools_and_continuations(self):
        config = self.make_config()
        cases = []
        wrong_model = self.request()
        wrong_model["model"] = "other-model"
        cases.append(wrong_model)
        remote_file = self.request()
        remote_file["input"] = [{"image_url": "https://provider.invalid/private.png"}]
        cases.append(remote_file)
        remote_tool = self.request()
        remote_tool["tools"] = [{"type": "mcp", "server_label": "remote"}]
        cases.append(remote_tool)
        continuation = self.request()
        continuation["previous_response_id"] = "remote-session"
        cases.append(continuation)
        for request in cases:
            with self.subTest(request=request), self.assertRaises(ValueError):
                validate_request(request, config)

    @unittest.skipUnless(os.name == "posix", "Unix broker socket required")
    def test_broker_socket_is_owned_and_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "broker.sock"
            with Broker(path, self.make_config(), Budget(Limits())):
                info = path.stat()
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                self.assertEqual(info.st_uid, os.getuid())

    def test_relative_path_validation_rejects_escape(self):
        for name in ("../secret", "/etc/passwd", "C:/Users/key", "a\\b"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                relative(name)

    @unittest.skipUnless(os.name == "posix", "Linux directory-handle APIs required")
    def test_secure_root_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(OSError):
                SecureRoot(link)

    def test_budget_reserves_before_any_request(self):
        budget = Budget(Limits(requests=1, tokens=1000, output_tokens=256))
        with self.assertRaises(BudgetExceeded):
            budget.begin(2000)
        self.assertEqual(budget.public()["requests"], 0)

    def test_sealed_label_does_not_expose_prompt_through_public_task_api(self):
        suite = Suite()
        sealed = suite.ids("sealed")
        self.assertTrue(sealed)
        for task_id in sealed:
            self.assertNotIn("prompt", suite.public_task(task_id))


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bwrap"),
                     "Linux bubblewrap required")
class NamespaceTests(unittest.TestCase):
    def test_workspace_is_writable_but_host_tmp_and_network_are_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            host_file = Path(temporary) / "host-only.txt"
            host_file.write_text("private", encoding="utf-8")
            code = (
                "import json,socket,sys; "
                "open('/workspace/created.txt','w').write('sandbox'); "
                "print(json.dumps({'interfaces':[n for _,n in socket.if_nameindex()],"
                "'host_file':__import__('os').path.exists(sys.argv[1])}))"
            )
            result = Sandbox().run(
                {"original.txt": "untouched"},
                ["/usr/bin/python3", "-c", code, str(host_file)], timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            probe = json.loads(result.stdout)
            self.assertEqual(probe["interfaces"], ["lo"])
            self.assertFalse(probe["host_file"])
            self.assertEqual(result.files["original.txt"], "untouched")
            self.assertEqual(result.files["created.txt"], "sandbox")
            self.assertEqual(host_file.read_text(encoding="utf-8"), "private")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_node_runtime_is_available_inside_sandbox(self):
        result = Sandbox().run({}, [shutil.which("node"), "-e", "process.stdout.write(process.version)"],
                               timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("v"), result.stdout)


if __name__ == "__main__":
    unittest.main()
