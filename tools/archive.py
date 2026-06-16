#!/usr/bin/env python3
"""Archive the fully-consumed prefix of a handoff stream (PROTOCOL.md §13).

Append-only streams grow without bound. This tool moves the prefix that *every*
reading-side session has already consumed into `.handoff-runtime/archive/`, then
atomically rewrites the live stream with only the still-unconsumed tail. It never
drops a line any reader hasn't passed, and never drops the latest line.

Stdlib only, so it runs from PowerShell, bash, Claude, or Codex without setup.

Usage:
    python .handoff/tools/archive.py            # archive both streams
    python .handoff/tools/archive.py --dry-run  # report only, write nothing
    python .handoff/tools/archive.py --stream c2x
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

# stream key -> (filename, writer side, reader side)
STREAMS = {
    "c2x": ("claude-to-codex.jsonl", "claude", "codex"),
    "x2c": ("codex-to-claude.jsonl", "codex", "claude"),
}

LOCK_TIMEOUT_SECONDS = 30.0
LOCK_STALE_SECONDS = 600.0


class ArchiveError(Exception):
    pass


def find_runtime(start: Path) -> Path:
    current = start.resolve()
    for candidate in [current, *current.parents]:
        runtime = candidate / ".handoff-runtime"
        if runtime.is_dir():
            return runtime
    raise ArchiveError("could not find .handoff-runtime; run .handoff/setup.ps1")


def seq_of(line: str) -> int | None:
    line = line.strip()
    if not line:
        return None
    try:
        import json

        msg = json.loads(line)
        mid = str(msg["id"])
        return int(mid.rsplit("-", 1)[1])
    except Exception:
        return None


def read_cursor(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def reader_archive_point(runtime: Path, reader_side: str) -> int | None:
    """Min consumed seq across all reader-side cursors. None if no reader cursor."""
    values: list[int] = []
    cursors_dir = runtime / "cursors"
    if cursors_dir.is_dir():
        for f in cursors_dir.iterdir():
            if f.is_file() and f.name.startswith(reader_side + "-"):
                v = read_cursor(f)
                if v is not None:
                    values.append(v)
    if values:
        return min(values)
    # No per-session cursor yet (e.g. pre-v1.9 runtime): fall back to the legacy
    # shared cursor. Once per-session cursors exist they are the source of truth.
    return read_cursor(runtime / f".{reader_side}-cursor")


def atomic_write(path: Path, data: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class SendLock:
    """Same lock file send.py uses, so archiving serializes against sends."""

    def __init__(self, runtime: Path, side: str) -> None:
        self.path = runtime / "locks" / f"{side}-send.lock"

    def __enter__(self) -> "SendLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > LOCK_STALE_SECONDS:
                        os.unlink(self.path)
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise ArchiveError(f"could not acquire {self.path} within {LOCK_TIMEOUT_SECONDS}s")
                time.sleep(0.1)

    def __exit__(self, *exc) -> None:
        try:
            os.unlink(self.path)
        except OSError:
            pass


def archive_stream(runtime: Path, key: str, dry_run: bool) -> str:
    filename, writer_side, reader_side = STREAMS[key]
    stream = runtime / filename
    if not stream.is_file():
        return f"{key}: stream missing, skip"

    point = reader_archive_point(runtime, reader_side)
    if point is None:
        return f"{key}: no {reader_side} cursor yet, skip (nothing safe to archive)"
    if point <= 0:
        return f"{key}: reader at seq {point}, nothing consumed, skip"

    with SendLock(runtime, writer_side):
        lines = stream.read_text(encoding="utf-8").splitlines()
        parsed = [(seq_of(l), l) for l in lines if l.strip()]
        if not parsed:
            return f"{key}: empty, skip"
        max_seq = max(s for s, _ in parsed if s is not None)
        to_archive = [l for s, l in parsed if s is not None and s <= point and s != max_seq]
        retained = [l for s, l in parsed if not (s is not None and s <= point and s != max_seq)]
        if not to_archive:
            return f"{key}: archive_point={point}, nothing below it (keeping latest seq {max_seq}), skip"
        if dry_run:
            return f"{key}: would archive {len(to_archive)} line(s) (seq<= {point}), keep {len(retained)} (DRY-RUN)"

        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        archive_path = runtime / "archive" / f"{filename}.{ts}.jsonl"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(archive_path, "\n".join(to_archive) + "\n")
        atomic_write(stream, ("\n".join(retained) + "\n") if retained else "")
        return f"{key}: archived {len(to_archive)} line(s) -> archive/{archive_path.name}, kept {len(retained)}"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Archive consumed handoff stream prefix (PROTOCOL.md §13).")
    parser.add_argument("--stream", choices=sorted(STREAMS), help="Only this stream; default both.")
    parser.add_argument("--dry-run", "--verify", dest="dry_run", action="store_true")
    args = parser.parse_args(argv)

    try:
        runtime = find_runtime(Path.cwd())
        keys = [args.stream] if args.stream else list(STREAMS)
        for key in keys:
            print(archive_stream(runtime, key, args.dry_run))
    except ArchiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
