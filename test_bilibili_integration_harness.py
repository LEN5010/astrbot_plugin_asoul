import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from asoul_comment_journal import CommentJournal
from test_comment_journal import video_resource
from tools.verify_bilibili_comments import (
    _force_full_rescan,
    _rescan_complete,
    load_credential_file,
    redact_error,
)


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

    def test_force_rescan_wakes_only_known_reply_work(self) -> None:
        db_path = self.root / "comments.sqlite3"
        journal = CommentJournal(db_path)
        lifecycle = journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], 100
        ).activated[0]
        with journal._connection:
            journal._connection.execute(
                """
                INSERT INTO comment_root_state(
                    lifecycle_id, root_rpid, known_reply_count,
                    reconciled_reply_count, last_reply_scan_at,
                    next_safety_scan_at
                ) VALUES(?, '9001', 1, 1, 101, 999999)
                """,
                (lifecycle.lifecycle_id,),
            )
            journal._connection.execute(
                """
                INSERT INTO scan_task(
                    lifecycle_id, kind, root_rpid, cursor, page_index,
                    bootstrap_pending, next_attempt_at, last_success_at,
                    scan_lane, task_state
                ) VALUES(?, 'reply', '9001', '', 1, 0, 0, 101,
                         'reply', 'dormant')
                """,
                (lifecycle.lifecycle_id,),
            )
        journal.close()

        _force_full_rescan(db_path, 200)

        self.assertFalse(_rescan_complete(db_path, 200))
        with closing(sqlite3.connect(db_path)) as connection:
            reply = connection.execute(
                """
                SELECT task_state, next_attempt_at, last_success_at
                FROM scan_task WHERE scan_lane = 'reply'
                """
            ).fetchone()
        self.assertEqual(reply, ("scheduled", 200, 0))
