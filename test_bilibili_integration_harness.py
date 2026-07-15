import tempfile
import unittest
from pathlib import Path

from tools.verify_bilibili_comments import load_credential_file, redact_error


class BilibiliIntegrationHarnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_rejects_credential_file_inside_repository(self) -> None:
        credential_path = self.root / "cookie.json"
        credential_path.write_text('{"sessdata":"secret"}', encoding="utf-8")
        credential_path.chmod(0o600)

        with self.assertRaisesRegex(ValueError, "outside the repository"):
            load_credential_file(credential_path, repo_root=self.root)

    def test_requires_private_file_permissions(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        credential_path = self.root / "cookie.json"
        credential_path.write_text('{"sessdata":"secret"}', encoding="utf-8")
        credential_path.chmod(0o644)

        with self.assertRaisesRegex(ValueError, "0600"):
            load_credential_file(credential_path, repo_root=repo)

    def test_redaction_removes_cookie_fields_and_html_body(self) -> None:
        text = "SESSDATA=secret bili_jct=csrf <!DOCTYPE html>blocked"

        redacted = redact_error(text)

        self.assertNotIn("secret", redacted)
        self.assertNotIn("csrf", redacted)
        self.assertNotIn("DOCTYPE", redacted)
