#!/usr/bin/env python3
"""Read-only diagnostics for a claude-codex-handoff runtime.

Run from a project root that contains `.handoff-runtime/`, or from the
`.handoff/` directory inside such a project:

    python .handoff/tools/doctor.py

The doctor validates stream JSON, local seq files, per-session cursors, legacy
cursor anchors, claims, note references, and unexpected top-level runtime files.
It never modifies project or runtime state.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


STREAMS = {
    "c2x": ("claude-to-codex.jsonl", "claude", "codex"),
    "x2c": ("codex-to-claude.jsonl", "codex", "claude"),
}

ID_RE = re.compile(r"^(codex|claude)-(\d+)$")
SESSION_CURSOR_RE = re.compile(r"^(codex|claude)-[A-Za-z0-9_.:-]{1,64}$")

KNOWN_DIRS = {"archive", "claims", "cursors", "locks", "notes"}
KNOWN_FILES = {
    "claude-to-codex.jsonl",
    "codex-to-claude.jsonl",
    ".claude-cursor",
    ".claude-lastseen",
    ".claude-seq",
    ".claude-session",
    ".codex-cursor",
    ".codex-lastseen",
    ".codex-seq",
    ".codex-session",
    "codex-heartbeat-state.json",
}


@dataclass
class Issue:
    level: str
    message: str


class Report:
    def __init__(self) -> None:
        self.issues: list[Issue] = []

    def info(self, message: str) -> None:
        self.issues.append(Issue("INFO", message))

    def warn(self, message: str) -> None:
        self.issues.append(Issue("WARN", message))

    def error(self, message: str) -> None:
        self.issues.append(Issue("ERROR", message))

    def count(self, level: str) -> int:
        return sum(1 for issue in self.issues if issue.level == level)

    def print(self) -> None:
        for issue in self.issues:
            print(f"{issue.level}: {issue.message}")
        print(
            f"SUMMARY: {self.count('ERROR')} error(s), "
            f"{self.count('WARN')} warning(s), {self.count('INFO')} info"
        )


def find_project_root(cwd: Path) -> Path:
    candidates = [cwd, *cwd.parents]
    for path in candidates:
        if (path / ".handoff-runtime").is_dir():
            return path
        if path.name == ".handoff" and (path.parent / ".handoff-runtime").is_dir():
            return path.parent
        if (path / ".handoff").is_dir() and (path / ".handoff" / "PROTOCOL.md").is_file():
            return path
    return cwd.parent if cwd.name == ".handoff" else cwd


def read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8-sig").strip() or "0")
    except (OSError, ValueError):
        return None


def parse_id(value: Any) -> tuple[str, int] | None:
    if not isinstance(value, str):
        return None
    match = ID_RE.match(value)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_notes_file(runtime: Path, msg_id: str, value: Any, report: Report) -> None:
    if value in (None, ""):
        return
    if not isinstance(value, str):
        report.warn(f"{msg_id}: refs.notes_file is not a string")
        return
    if "\\" in value or ":" in value:
        report.warn(f"{msg_id}: refs.notes_file must be POSIX relative path: {value}")
        return
    rel = PurePosixPath(value)
    if rel.is_absolute() or not rel.parts or rel.parts[0] != "notes":
        report.warn(f"{msg_id}: refs.notes_file must be under notes/: {value}")
        return
    if any(part in {"", ".", ".."} for part in rel.parts):
        report.warn(f"{msg_id}: refs.notes_file has unsafe path segment: {value}")
        return
    if not (runtime / value).is_file():
        report.warn(f"{msg_id}: refs.notes_file is missing: {value}")


def read_stream(runtime: Path, key: str, report: Report) -> tuple[int, dict[str, dict[str, Any]]]:
    filename, writer_side, _reader_side = STREAMS[key]
    path = runtime / filename
    if not path.is_file():
        report.error(f"{filename}: stream missing")
        return 0, {}

    max_seq = 0
    last_seq = 0
    seen_ids: set[str] = set()
    messages: dict[str, dict[str, Any]] = {}

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        report.error(f"{filename}: cannot read stream: {exc}")
        return 0, {}

    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            report.error(f"{filename}:{line_no}: invalid JSON: {exc.msg}")
            continue
        if not isinstance(msg, dict):
            report.error(f"{filename}:{line_no}: message is not an object")
            continue

        msg_id = msg.get("id")
        parsed = parse_id(msg_id)
        if parsed is None:
            report.error(f"{filename}:{line_no}: invalid id {msg_id!r}")
            continue
        side, seq = parsed
        if side != writer_side:
            report.error(f"{filename}:{line_no}: id side {side!r} does not match writer {writer_side!r}")
        if msg.get("from") != writer_side:
            report.error(f"{filename}:{line_no}: from={msg.get('from')!r} does not match writer {writer_side!r}")
        if seq < last_seq:
            report.warn(f"{filename}:{line_no}: seq decreased from {last_seq} to {seq}")
        last_seq = seq
        max_seq = max(max_seq, seq)
        if msg_id in seen_ids:
            report.error(f"{filename}:{line_no}: duplicate id {msg_id}")
        seen_ids.add(str(msg_id))
        messages[str(msg_id)] = msg

        refs = msg.get("refs")
        if not isinstance(refs, dict):
            report.error(f"{filename}:{line_no}: refs missing or not an object")
        else:
            validate_notes_file(runtime, str(msg_id), refs.get("notes_file"), report)

    report.info(f"{filename}: {len(seen_ids)} message(s), max seq {max_seq}")
    return max_seq, messages


def check_seq_files(runtime: Path, max_by_side: dict[str, int], report: Report) -> None:
    for side in ("claude", "codex"):
        path = runtime / f".{side}-seq"
        value = read_int(path)
        if value is None:
            report.error(f".{side}-seq: missing or invalid integer")
            continue
        expected = max_by_side.get(side, 0)
        if value < expected:
            report.warn(f".{side}-seq={value} is behind stream max seq {expected}; send.py can recover")
        elif value > expected:
            report.info(f".{side}-seq={value} is ahead of stream max seq {expected}")


def check_cursors(runtime: Path, max_by_reader: dict[str, int], report: Report) -> None:
    cursors_dir = runtime / "cursors"
    if not cursors_dir.is_dir():
        report.warn("cursors/: missing; run setup for v1.9 runtime layout")
        return

    seen: dict[str, int] = {"claude": 0, "codex": 0}
    for path in sorted(cursors_dir.iterdir()):
        if not path.is_file():
            continue
        if not SESSION_CURSOR_RE.match(path.name):
            report.warn(f"cursors/{path.name}: unexpected cursor filename")
            continue
        side = path.name.split("-", 1)[0]
        seen[side] += 1
        value = read_int(path)
        if value is None:
            report.error(f"cursors/{path.name}: invalid integer")
            continue
        peer_max = max_by_reader.get(side, 0)
        if value > peer_max:
            report.warn(f"cursors/{path.name}={value} is ahead of peer stream max seq {peer_max}")
        elif value < peer_max:
            report.info(f"cursors/{path.name}: {peer_max - value} unread peer message(s)")

    for side, count in seen.items():
        if count == 0:
            report.info(f"cursors/: no {side} session cursor yet")

    for side in ("claude", "codex"):
        path = runtime / f".{side}-cursor"
        value = read_int(path)
        if value is None:
            report.warn(f".{side}-cursor: missing or invalid legacy cursor")
            continue
        peer_max = max_by_reader.get(side, 0)
        if value > peer_max:
            report.warn(f".{side}-cursor={value} is ahead of peer stream max seq {peer_max}")


def check_claims(runtime: Path, all_messages: dict[str, dict[str, Any]], report: Report) -> None:
    claims_dir = runtime / "claims"
    if not claims_dir.is_dir():
        report.warn("claims/: missing")
        return

    now = datetime.now(timezone.utc)
    count = 0
    for path in sorted(claims_dir.glob("*.json")):
        count += 1
        try:
            claim = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report.error(f"claims/{path.name}: cannot parse JSON: {exc}")
            continue
        if not isinstance(claim, dict):
            report.error(f"claims/{path.name}: claim is not an object")
            continue
        message_id = claim.get("message_id")
        if not isinstance(message_id, str):
            report.error(f"claims/{path.name}: message_id missing or invalid")
        elif message_id not in all_messages:
            report.warn(f"claims/{path.name}: message_id {message_id} not found in live streams")
        expires_at = parse_time(claim.get("expires_at"))
        if expires_at is None:
            report.warn(f"claims/{path.name}: expires_at missing or invalid")
        elif expires_at < now:
            report.warn(f"claims/{path.name}: expired at {expires_at.isoformat()}")
    report.info(f"claims/: {count} claim file(s)")


def check_runtime_top_level(runtime: Path, report: Report) -> None:
    for path in sorted(runtime.iterdir()):
        if path.is_dir():
            if path.name not in KNOWN_DIRS:
                report.warn(f"{path.name}/: unexpected top-level runtime directory")
            continue
        if path.name in KNOWN_FILES:
            continue
        report.warn(f"{path.name}: unexpected top-level runtime file; likely stale temp/scratch")


def run(root: Path, strict: bool) -> int:
    runtime = root / ".handoff-runtime"
    report = Report()
    report.info(f"project root: {root}")
    report.info(f"runtime: {runtime}")

    if not runtime.is_dir():
        report.error(".handoff-runtime/ is missing; run setup before using doctor")
        report.print()
        return 2

    check_runtime_top_level(runtime, report)

    max_c2x, messages_c2x = read_stream(runtime, "c2x", report)
    max_x2c, messages_x2c = read_stream(runtime, "x2c", report)
    max_by_side = {"claude": max_c2x, "codex": max_x2c}
    max_by_reader = {"codex": max_c2x, "claude": max_x2c}
    all_messages = {**messages_c2x, **messages_x2c}

    check_seq_files(runtime, max_by_side, report)
    check_cursors(runtime, max_by_reader, report)
    check_claims(runtime, all_messages, report)

    report.print()
    if report.count("ERROR"):
        return 2
    if strict and report.count("WARN"):
        return 1
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Read-only diagnostics for .handoff-runtime.")
    parser.add_argument("--root", type=Path, help="Project root; defaults to auto-detected current project.")
    parser.add_argument("--strict", action="store_true", help="Return non-zero when warnings are found.")
    args = parser.parse_args(argv)

    root = args.root.resolve() if args.root else find_project_root(Path.cwd().resolve())
    return run(root, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
