#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import secrets
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from asoul_bilibili import (  # noqa: E402
    BILIBILI_CREDENTIAL_FIELDS,
    BilibiliCommentResource,
    BilibiliGateway,
)
from asoul_comment_capture import (  # noqa: E402
    CommentCaptureCoordinator,
    CommentCaptureError,
    CommentRetryPolicy,
    CommentWorkScheduler,
)
from asoul_comment_journal import CommentJournal  # noqa: E402

LOCAL_DELIVERY_ORIGIN = "local:comment-verification"
HTML_RESPONSE_OMITTED = "HTML response omitted"


def load_credential_file(path: Path, repo_root: Path) -> dict[str, str]:
    resolved = Path(path).expanduser().resolve()
    root = Path(repo_root).resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError("credential file must be outside the repository")
    if resolved.stat().st_mode & 0o077:
        raise ValueError("credential file permissions must be 0600")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("credential file must contain a JSON object")
    allowed = set(BILIBILI_CREDENTIAL_FIELDS)
    credential = {
        key: str(value).strip()
        for key, value in payload.items()
        if key in allowed and str(value).strip()
    }
    if not credential.get("sessdata"):
        raise ValueError("credential file must contain sessdata")
    return credential


def redact_error(text: Any) -> str:
    raw = str(text or "")
    if re.search(r"<!doctype\s+html|<html(?:\s|>)", raw, flags=re.IGNORECASE):
        return HTML_RESPONSE_OMITTED
    field_pattern = "|".join(re.escape(name) for name in BILIBILI_CREDENTIAL_FIELDS)
    redacted = re.sub(
        rf"(?i)\b({field_pattern})\b\s*[:=]\s*[^\s;,&]+",
        lambda match: f"{match.group(1)}=***",
        raw,
    )
    return re.sub(r"\s+", " ", redacted).strip()


def _classify_error(exc: Exception) -> CommentCaptureError:
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    normalized_code = str(code if code is not None else status or "")
    category = "risk_control" if normalized_code == "412" else "request"
    return CommentCaptureError(
        category=category,
        code=normalized_code,
        message=redact_error(exc),
    )


def _resource_key(kind: str, type_value: int, oid: int) -> str:
    if kind == "dynamic":
        return f"dynamic:{int(type_value)}:{int(oid)}"
    return f"{kind}:{int(oid)}"


def _capture_snapshot(
    db_path: Path,
    *,
    marker: str,
    target_uid: str,
) -> dict[str, Any]:
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT observed_comment.rpid, observed_comment.is_reply,
                   observed_comment.text
            FROM observed_comment
            WHERE observed_comment.author_uid = ?
              AND instr(observed_comment.text, ?) > 0
            ORDER BY observed_comment.created_at, observed_comment.rpid
            """,
            (str(target_uid), str(marker)),
        ).fetchall()
        event_row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM comment_event
            JOIN observed_comment
              ON observed_comment.lifecycle_id = comment_event.lifecycle_id
             AND observed_comment.rpid = comment_event.rpid
            WHERE observed_comment.author_uid = ?
              AND instr(observed_comment.text, ?) > 0
            """,
            (str(target_uid), str(marker)),
        ).fetchone()
        retry_row = connection.execute(
            """
            SELECT
              COALESCE((SELECT SUM(retry_count) FROM scan_task), 0)
              + COALESCE((SELECT SUM(attempt_count) FROM event_delivery), 0)
              AS count
            """
        ).fetchone()
    return {
        "captured_rpids": [str(row["rpid"]) for row in rows],
        "root_count": sum(1 for row in rows if not bool(row["is_reply"])),
        "reply_count": sum(1 for row in rows if bool(row["is_reply"])),
        "event_count": int(event_row["count"]),
        "retry_count": int(retry_row["count"]),
    }


def _force_full_rescan(db_path: Path, now: int) -> None:
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            """
            UPDATE scan_task
            SET next_attempt_at = ?, last_success_at = 0,
                task_state = 'scheduled', page_index = CASE
                    WHEN scan_lane = 'reply' THEN 1 ELSE page_index END
            WHERE lifecycle_id IN (
                SELECT lifecycle_id FROM resource_lifecycle
                WHERE retired_at = 0
            ) AND (
                scan_lane IN ('head', 'reconcile')
                OR (
                    scan_lane = 'reply'
                    AND EXISTS (
                        SELECT 1 FROM comment_root_state
                        WHERE comment_root_state.lifecycle_id = scan_task.lifecycle_id
                          AND comment_root_state.root_rpid = scan_task.root_rpid
                          AND comment_root_state.known_reply_count > 0
                    )
                )
            )
            """,
            (int(now),),
        )
        connection.commit()


def _rescan_complete(db_path: Path, started_at: int) -> bool:
    with closing(sqlite3.connect(db_path)) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) FROM scan_task
            JOIN resource_lifecycle USING(lifecycle_id)
            WHERE resource_lifecycle.retired_at = 0
              AND scan_task.last_success_at < ?
              AND (
                scan_task.scan_lane IN ('head', 'reconcile')
                OR (
                    scan_task.scan_lane = 'reply'
                    AND EXISTS (
                        SELECT 1 FROM comment_root_state
                        WHERE comment_root_state.lifecycle_id = scan_task.lifecycle_id
                          AND comment_root_state.root_rpid = scan_task.root_rpid
                          AND comment_root_state.known_reply_count > 0
                    )
                )
              )
            """,
            (int(started_at),),
        ).fetchone()
    return int(row[0]) == 0


async def main_async(args: argparse.Namespace) -> int:
    credential = load_credential_file(args.credential_file, REPO_ROOT)
    marker = f"ASOUL-COMMENT-{secrets.token_hex(4).upper()}"
    resource_key = _resource_key(args.resource_kind, args.type_value, args.oid)
    started_monotonic = time.monotonic()
    started_at = int(time.time())
    duration_seconds = max(1, int(args.duration_seconds))
    deadline = started_monotonic + duration_seconds
    errors: list[str] = []
    delivered_roots = 0
    delivered_replies = 0
    duplicate_count = 0
    rescan_started_at = 0
    events_before_rescan = 0
    rescan_completed = False

    print(marker)
    print("请使用目标 UID 在指定评论区发表一条包含该标记的一级评论。")
    print("再回复这条评论，并让楼中楼正文也包含同一标记。")

    with tempfile.TemporaryDirectory(prefix="asoul-comment-verify-") as temp_dir:
        db_path = Path(temp_dir) / "comments.sqlite3"
        journal = CommentJournal(db_path)
        gateway = BilibiliGateway(credential_data=credential)
        coordinator = CommentCaptureCoordinator(
            gateway=gateway,
            journal=journal,
            classify_error=_classify_error,
            retry_policy=CommentRetryPolicy(random_value=random.random),
        )
        scheduler = CommentWorkScheduler()
        resource = BilibiliCommentResource(
            key=resource_key,
            owner_uid=str(args.owner_uid),
            owner_name=str(args.owner_uid),
            resource_kind=str(args.resource_kind),
            oid=int(args.oid),
            type_value=int(args.type_value),
            title="评论抓取验证资源",
            url=str(args.resource_url),
        )
        journal.sync_resource_catalog(
            str(args.owner_uid), str(args.owner_uid), [resource], started_at
        )

        async def record_delivery(_origin: str, notification: Any) -> None:
            nonlocal delivered_roots, delivered_replies
            if notification.uid != str(args.target_uid) or marker not in notification.text:
                return
            if notification.comment_action_text == "回复了评论":
                delivered_replies += 1
            else:
                delivered_roots += 1

        try:
            while time.monotonic() < deadline:
                now = int(time.time())
                if await coordinator.deliver_one(record_delivery, now):
                    await asyncio.sleep(0)
                    continue

                journal.activate_reply_gaps(now)
                task = scheduler.next_task(
                    journal,
                    now,
                    [str(args.owner_uid)],
                )
                if task is not None:
                    await coordinator.run_scan_task(
                        task,
                        target_uids=[str(args.target_uid)],
                        target_origins=[LOCAL_DELIVERY_ORIGIN],
                        now=now,
                    )
                    continue

                snapshot = _capture_snapshot(
                    db_path, marker=marker, target_uid=str(args.target_uid)
                )
                if (
                    snapshot["root_count"] >= 1
                    and snapshot["reply_count"] >= 1
                    and delivered_roots >= 1
                    and delivered_replies >= 1
                ):
                    if not rescan_started_at:
                        events_before_rescan = int(snapshot["event_count"])
                        rescan_started_at = int(time.time())
                        _force_full_rescan(db_path, rescan_started_at)
                        continue
                    if _rescan_complete(db_path, rescan_started_at):
                        after = _capture_snapshot(
                            db_path,
                            marker=marker,
                            target_uid=str(args.target_uid),
                        )
                        duplicate_count = max(
                            0, int(after["event_count"]) - events_before_rescan
                        )
                        rescan_completed = True
                        break
                await asyncio.sleep(0.25)
        except Exception as exc:
            errors.append(redact_error(exc))
        finally:
            snapshot = _capture_snapshot(
                db_path, marker=marker, target_uid=str(args.target_uid)
            )
            journal.close()

    passed = bool(
        snapshot["root_count"] >= 1
        and snapshot["reply_count"] >= 1
        and delivered_roots >= 1
        and delivered_replies >= 1
        and rescan_completed
        and duplicate_count == 0
        and not errors
    )
    report = {
        "marker": marker,
        "resource_key": resource_key,
        "captured_rpids": snapshot["captured_rpids"],
        "root_count": snapshot["root_count"],
        "reply_count": snapshot["reply_count"],
        "duplicate_count": duplicate_count,
        "retry_count": snapshot["retry_count"],
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
        "status": "pass" if passed else "fail",
        "errors": errors,
    }
    report_path = Path(args.report).expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0 if passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读验证 B 站评论完整分页抓取和去重行为。"
    )
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--owner-uid", required=True)
    parser.add_argument("--target-uid", required=True)
    parser.add_argument("--oid", required=True, type=int)
    parser.add_argument("--type-value", required=True, type=int)
    parser.add_argument(
        "--resource-kind", required=True, choices=("dynamic", "video")
    )
    parser.add_argument("--resource-url", required=True)
    parser.add_argument("--duration-seconds", required=True, type=int)
    parser.add_argument("--report", required=True, type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(main_async(args))
    except Exception as exc:
        print(redact_error(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
