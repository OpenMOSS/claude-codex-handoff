#!/usr/bin/env python3
"""Write one handoff JSONL message with protocol-safe defaults.

This helper intentionally uses only the Python standard library so it can be
called from PowerShell, Git Bash, Claude, or Codex without extra setup.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


VALID_TYPES = {"task", "handoff", "done", "status", "question", "error", "cancel"}
VALID_SIDES = {"codex", "claude"}
VALID_STATES = {"claimed", "progress", "blocked", "awaiting-input", "shutdown"}
TERMINAL_TYPES = {"done", "error", "cancel"}
ID_RE = re.compile(r"^(codex|claude)-\d{6,}$")
DURATION_RE = re.compile(
    r"^P((\d+D)(T(\d+H(\d+M)?(\d+S)?|\d+M(\d+S)?|\d+S))?|"
    r"T(\d+H(\d+M)?(\d+S)?|\d+M(\d+S)?|\d+S))$"
)
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
SEND_LOCK_TIMEOUT_SECONDS = 30.0
SEND_LOCK_STALE_SECONDS = 600.0


class HandoffError(Exception):
    pass


class SendLock:
    def __init__(self, runtime: Path, side: str) -> None:
        self.path = runtime / "locks" / f"{side}-send.lock"
        self.side = side
        self.acquired = False

    def __enter__(self) -> "SendLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + SEND_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                if hasattr(os, "O_BINARY"):
                    flags |= os.O_BINARY
                fd = os.open(self.path, flags, 0o666)
                try:
                    payload = {
                        "side": self.side,
                        "pid": os.getpid(),
                        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                    }
                    os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self.acquired = True
                return self
            except FileExistsError:
                if self._try_remove_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise HandoffError(f"timed out waiting for send lock: {self.path}")
                time.sleep(0.1)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _try_remove_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return True
        if age < SEND_LOCK_STALE_SECONDS:
            return False
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False


def utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def find_project_root(start: Path) -> Path:
    current = start.resolve()
    for path in [current, *current.parents]:
        if (path / ".handoff").is_dir():
            return path
    raise HandoffError("could not find project root containing .handoff")


def runtime_dir(root: Path) -> Path:
    runtime = root / ".handoff-runtime"
    if not runtime.is_dir():
        raise HandoffError("could not find .handoff-runtime; run .handoff/setup.ps1")
    return runtime


def side_paths(handoff: Path, side: str) -> tuple[Path, Path, Path]:
    if side == "codex":
        return (
            handoff / ".codex-seq",
            handoff / "codex-to-claude.jsonl",
            handoff / "claude-to-codex.jsonl",
        )
    if side == "claude":
        return (
            handoff / ".claude-seq",
            handoff / "claude-to-codex.jsonl",
            handoff / "codex-to-claude.jsonl",
        )
    raise HandoffError(f"invalid side: {side}")


def read_seq(path: Path) -> int:
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8").strip()
    return int(text or "0")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(path, flags, 0o666)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    messages: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                messages.append(value)
    return messages


def max_seq(path: Path) -> int:
    max_seen = 0
    for msg in iter_jsonl(path):
        msg_id = str(msg.get("id", ""))
        if "-" not in msg_id:
            continue
        try:
            seq = int(msg_id.rsplit("-", 1)[1])
        except ValueError:
            continue
        max_seen = max(max_seen, seq)
    return max_seen


def infer_thread(reply_to: str | None, explicit: str | None, streams: list[Path], fallback: str) -> str:
    if explicit:
        return explicit
    if reply_to:
        for stream in streams:
            for msg in iter_jsonl(stream):
                if msg.get("id") == reply_to:
                    thread = msg.get("thread")
                    if isinstance(thread, str) and thread:
                        return thread
        return reply_to
    return fallback


def find_message(message_id: str | None, streams: list[Path]) -> dict[str, Any] | None:
    if not message_id:
        return None
    for stream in streams:
        for msg in iter_jsonl(stream):
            if msg.get("id") == message_id:
                return msg
    return None


def validate_session_id(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        return f"{field} must be a non-empty string"
    if not SESSION_RE.match(value):
        return f"{field} must be 1-64 ASCII chars: letters, digits, _, -, ., :"
    return None


def resolve_session_id(args: argparse.Namespace, runtime: Path) -> str:
    candidates: list[str | None] = [
        args.session,
        os.environ.get("HANDOFF_SESSION_ID"),
        os.environ.get(f"{args.side.upper()}_SESSION_ID"),
    ]
    session_file = runtime / f".{args.side}-session"
    if session_file.exists():
        candidates.append(session_file.read_text(encoding="utf-8").strip())
    candidates.append(f"{args.side}-default")
    for candidate in candidates:
        if not candidate:
            continue
        error = validate_session_id(candidate, "session")
        if error:
            raise HandoffError(error)
        return candidate
    raise HandoffError("could not resolve session id")


def infer_to_session(
    reply_to: str | None,
    explicit: str | None,
    broadcast: bool,
    streams: list[Path],
) -> str | None:
    if broadcast:
        if explicit is not None:
            raise HandoffError("--broadcast cannot be combined with --to-session")
        return None
    if explicit is not None:
        error = validate_session_id(explicit, "to_session")
        if error:
            raise HandoffError(error)
        return explicit
    original = find_message(reply_to, streams)
    if original:
        value = original.get("from_session")
        if isinstance(value, str) and value:
            error = validate_session_id(value, "to_session")
            if error:
                raise HandoffError(error)
            return value
    return None


def read_context(args: argparse.Namespace) -> str | None:
    parts: list[str] = []
    if args.context:
        parts.append(args.context)
    if args.context_file:
        parts.append(Path(args.context_file).read_text(encoding="utf-8"))
    if args.context_stdin:
        parts.append(sys.stdin.read())
    if not parts:
        return None
    return "\n\n".join(parts)


def parse_skipped(values: list[str] | None) -> list[dict[str, str]] | None:
    if not values:
        return None
    parsed: list[dict[str, str]] = []
    for value in values:
        text = value.strip()
        if text.startswith("{"):
            try:
                item = json.loads(text)
            except json.JSONDecodeError as exc:
                raise HandoffError(f"--skipped JSON is invalid: {exc}") from exc
            if not isinstance(item, dict):
                raise HandoffError("--skipped JSON value must be an object")
            skipped_id = item.get("id")
            reason = item.get("reason")
        else:
            if "=" in text:
                skipped_id, reason = text.split("=", 1)
            elif ":" in text:
                skipped_id, reason = text.split(":", 1)
            else:
                raise HandoffError("--skipped must be JSON or ID=REASON")
        if skipped_id is None or reason is None:
            raise HandoffError("--skipped requires id and reason")
        skipped_id = str(skipped_id).strip()
        reason = str(reason).strip()
        if not skipped_id or not reason:
            raise HandoffError("--skipped requires non-empty id and reason")
        parsed.append({"id": skipped_id, "reason": reason})
    return parsed


def truncate_summary(summary: str, note_rel: str) -> str:
    suffix = f"... see {note_rel}"
    room = 180 - len(suffix)
    if room < 20:
        room = 20
    return summary[:room].rstrip() + suffix


def fsync_note(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(path, flags, 0o666)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_notes_file(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        return "refs.notes_file must be a non-empty string or null"
    if "\\" in value:
        return "refs.notes_file must use POSIX '/' separators"
    if ":" in value:
        return "refs.notes_file must be relative and cannot contain a drive or scheme"
    path = PurePosixPath(value)
    if path.is_absolute():
        return "refs.notes_file must be relative to .handoff-runtime"
    parts = path.parts
    if not parts or parts[0] != "notes":
        return "refs.notes_file must be under notes/"
    if any(part in {"", ".", ".."} for part in parts):
        return "refs.notes_file cannot contain empty, '.', or '..' path segments"
    return None


def resolve_note_path(runtime: Path, value: Any) -> Path:
    error = validate_notes_file(value)
    if error:
        raise HandoffError(error)
    candidate = (runtime / str(value)).resolve()
    notes_root = (runtime / "notes").resolve()
    try:
        candidate.relative_to(notes_root)
    except ValueError as exc:
        raise HandoffError("refs.notes_file escapes .handoff-runtime/notes") from exc
    return candidate


def validate_message(msg: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    required = ["v", "id", "ts", "from", "type", "thread", "summary", "blocking", "refs"]
    for key in required:
        if key not in msg:
            errors.append(f"missing {key}")
    if msg.get("v") != "1.0":
        errors.append("v must be 1.0")
    if not isinstance(msg.get("id"), str) or not ID_RE.match(str(msg.get("id"))):
        errors.append("id is invalid")
    if msg.get("from") not in VALID_SIDES:
        errors.append("from is invalid")
    for field in ["from_session", "to_session"]:
        error = validate_session_id(msg.get(field), field)
        if error:
            errors.append(error)
    if msg.get("type") not in VALID_TYPES:
        errors.append("type is invalid")
    if not isinstance(msg.get("thread"), str) or not ID_RE.match(str(msg.get("thread"))):
        errors.append("thread is invalid")
    summary = msg.get("summary")
    if not isinstance(summary, str):
        errors.append("summary must be a string")
    else:
        if "\n" in summary or "\r" in summary:
            errors.append("summary must be single-line")
        if len(summary) > 200:
            errors.append("summary exceeds 200 characters")
        if len(summary) > 180:
            warnings.append("summary is longer than 180 characters")
    if not isinstance(msg.get("blocking"), bool):
        errors.append("blocking must be boolean")
    refs_obj = msg.get("refs")
    refs: dict[str, Any] = refs_obj if isinstance(refs_obj, dict) else {}
    if not isinstance(refs_obj, dict):
        errors.append("refs must be an object")
    else:
        for key in ["reply_to", "notes_file", "commit"]:
            if key not in refs:
                errors.append(f"refs.{key} missing")
        note_error = validate_notes_file(refs.get("notes_file"))
        if note_error:
            errors.append(note_error)
    msg_type = msg.get("type")
    if msg_type in {"done", "cancel"} and not refs.get("reply_to"):
        errors.append(f"{msg_type} requires refs.reply_to")
    if msg_type == "task" and not (msg.get("goal") or msg.get("next_action")):
        errors.append("task requires goal or next_action")
    if msg_type == "handoff" and not msg.get("next_action"):
        errors.append("handoff requires next_action")
    if "state" in msg and msg_type not in {"status", "done"}:
        errors.append("state is only valid on status or done")
    if "state" in msg and msg.get("state") not in VALID_STATES:
        errors.append("state is invalid")
    for field in ["applied", "skipped", "total_proposed"]:
        if field in msg and msg_type != "done":
            errors.append(f"{field} is only valid on done")
    for field in ["applied", "total_proposed"]:
        value = msg.get(field)
        if value is not None and (not isinstance(value, int) or value < 0):
            errors.append(f"{field} must be a non-negative integer")
    skipped = msg.get("skipped")
    if skipped is not None:
        if not isinstance(skipped, list):
            errors.append("skipped must be an array")
        else:
            for index, item in enumerate(skipped):
                if not isinstance(item, dict):
                    errors.append(f"skipped[{index}] must be an object")
                    continue
                skipped_id = item.get("id")
                reason = item.get("reason")
                if not isinstance(skipped_id, str) or not skipped_id:
                    errors.append(f"skipped[{index}].id must be a non-empty string")
                if not isinstance(reason, str) or not reason:
                    errors.append(f"skipped[{index}].reason must be a non-empty string")
                elif "\n" in reason or "\r" in reason or len(reason) > 200:
                    errors.append(f"skipped[{index}].reason must be single-line and <=200 chars")
    for field in ["expected_within", "eta"]:
        value = msg.get(field)
        if value is not None and not DURATION_RE.match(str(value)):
            errors.append(f"{field} must be ISO 8601 duration")
    return errors, warnings


def duplicate_warnings(outbox: Path, msg: dict[str, Any]) -> list[str]:
    reply_to = msg.get("refs", {}).get("reply_to")
    if not reply_to or msg.get("type") not in TERMINAL_TYPES:
        return []
    hits: list[str] = []
    for old in iter_jsonl(outbox)[-50:]:
        if old.get("type") == msg.get("type") and old.get("refs", {}).get("reply_to") == reply_to:
            hits.append(str(old.get("id")))
    if not hits:
        return []
    return [f"recent duplicate {msg.get('type')} for {reply_to}: {', '.join(hits)}"]


def next_local_seq(seq_path: Path, outbox: Path) -> int:
    """Recover from a stale seq file by also consulting the real outbox."""
    return max(read_seq(seq_path), max_seq(outbox)) + 1


def build_message(args: argparse.Namespace, runtime: Path) -> tuple[dict[str, Any], Path, Path, str | None]:
    seq_path, outbox, peer_stream = side_paths(runtime, args.side)
    next_seq = next_local_seq(seq_path, outbox)
    msg_id = f"{args.side}-{next_seq:06d}"
    streams = [outbox, peer_stream]
    context = read_context(args)
    refs = {
        "reply_to": args.reply_to,
        "notes_file": args.notes_file,
        "commit": args.commit,
    }
    summary = args.summary
    note_text: str | None = None
    if len(summary) > 180:
        note_rel = refs["notes_file"] or f"notes/{msg_id}.md"
        note_text = "# Original summary\n\n" + summary + "\n"
        if context:
            note_text += "\n# Context\n\n" + context.rstrip() + "\n"
            context = None
        refs["notes_file"] = note_rel
        summary = truncate_summary(summary, note_rel)
    thread = infer_thread(args.reply_to, args.thread, streams, msg_id)
    from_session = resolve_session_id(args, runtime)
    to_session = infer_to_session(args.reply_to, args.to_session, args.broadcast, streams)
    msg: dict[str, Any] = {
        "v": "1.0",
        "id": msg_id,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "from": args.side,
        "from_session": from_session,
        "type": args.type,
        "thread": thread,
        "summary": summary,
        "blocking": args.blocking,
        "peer_cursor_observed": args.peer_cursor_observed
        if args.peer_cursor_observed is not None
        else max_seq(peer_stream),
        "refs": refs,
    }
    if to_session:
        msg["to_session"] = to_session
    optional_fields = {
        "context": context,
        "next_action": args.next_action,
        "goal": args.goal,
        "priority": args.priority,
        "expected_within": args.expected_within,
        "state": args.state,
        "eta": args.eta,
        "applied": args.applied,
        "skipped": parse_skipped(args.skipped),
        "total_proposed": args.total_proposed,
    }
    for key, value in optional_fields.items():
        if value is not None:
            msg[key] = value
    for key, values in [
        ("acceptance", args.acceptance),
        ("constraints", args.constraint),
        ("context_files", args.context_file_ref),
        ("files_changed", args.file_changed),
    ]:
        if values:
            msg[key] = values
    else:
        msg.setdefault("files_changed", [])
    return msg, seq_path, outbox, note_text


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send one .handoff JSONL message")
    parser.add_argument("--side", required=True, choices=sorted(VALID_SIDES))
    parser.add_argument("--type", required=True, choices=sorted(VALID_TYPES))
    parser.add_argument("--summary", required=True)
    parser.add_argument("--session", help="Current session id. Defaults to env or .handoff-runtime/.<side>-session.")
    parser.add_argument("--to-session", help="Direct this message to a specific peer session.")
    parser.add_argument("--broadcast", action="store_true", help="Do not infer to_session from --reply-to.")
    parser.add_argument("--thread")
    parser.add_argument("--reply-to")
    parser.add_argument("--notes-file")
    parser.add_argument("--commit")
    parser.add_argument("--context")
    parser.add_argument("--context-file")
    parser.add_argument("--context-stdin", action="store_true")
    parser.add_argument("--next-action")
    parser.add_argument("--goal")
    parser.add_argument("--acceptance", action="append")
    parser.add_argument("--constraint", action="append")
    parser.add_argument("--context-file-ref", action="append")
    parser.add_argument("--file-changed", action="append")
    parser.add_argument("--priority", choices=["urgent", "normal", "backlog"])
    parser.add_argument("--blocking", action="store_true")
    parser.add_argument("--expected-within")
    parser.add_argument("--state", choices=sorted(VALID_STATES))
    parser.add_argument("--eta")
    parser.add_argument("--applied", type=int)
    parser.add_argument("--skipped", action="append", help="Repeatable. JSON object or ID=REASON.")
    parser.add_argument("--total-proposed", type=int)
    parser.add_argument("--peer-cursor-observed", type=int)
    parser.add_argument("--dry-run", "--verify", dest="dry_run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    utf8_stdout()
    args = parse_args(argv)
    root = find_project_root(Path.cwd())
    runtime = runtime_dir(root)
    if args.dry_run:
        return write_message(args, runtime, dry_run=True)
    with SendLock(runtime, args.side):
        return write_message(args, runtime, dry_run=False)


def write_message(args: argparse.Namespace, runtime: Path, dry_run: bool) -> int:
    msg, seq_path, outbox, note_text = build_message(args, runtime)
    errors, warnings = validate_message(msg)
    warnings.extend(duplicate_warnings(outbox, msg))
    if errors:
        for error in errors:
            print(f"FATAL: {error}", file=sys.stderr)
        print(json.dumps(msg, ensure_ascii=False, indent=2))
        return 2
    for warning in warnings:
        print(f"WARN: {warning}", file=sys.stderr)
    print(json.dumps(msg, ensure_ascii=False, indent=2))
    if dry_run:
        print(f"DRY-RUN: would write {msg['id']} to {outbox}")
        if note_text:
            print(f"DRY-RUN: would write {msg['refs']['notes_file']}")
        return 0
    if note_text:
        note_path = resolve_note_path(runtime, msg["refs"]["notes_file"])
        fsync_note(note_path, note_text)
    append_jsonl(outbox, msg)
    atomic_write_text(seq_path, str(int(msg["id"].rsplit("-", 1)[1])))
    print(f"WROTE {msg['id']} to {outbox}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except HandoffError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise SystemExit(2)
