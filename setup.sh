#!/usr/bin/env bash
set -euo pipefail

FRESH=0
FORCE_PROJECT_FILES=0
for arg in "$@"; do
  case "$arg" in
    --fresh) FRESH=1 ;;
    --force-project-files) FORCE_PROJECT_FILES=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

HANDOFF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$HANDOFF_DIR/.." && pwd)"
RUNTIME_DIR="$ROOT_DIR/.handoff-runtime"
PROJECT_FILES_DIR="$HANDOFF_DIR/project-files"

mkdir -p "$HANDOFF_DIR/tools" "$HANDOFF_DIR/project-files" "$HANDOFF_DIR/prompts"

if [ "$FRESH" = "1" ] && [ -e "$RUNTIME_DIR" ]; then
  if [ "$(basename "$RUNTIME_DIR")" != ".handoff-runtime" ]; then
    echo "refusing to remove unexpected runtime path: $RUNTIME_DIR" >&2
    exit 2
  fi
  rm -rf "$RUNTIME_DIR"
  echo "Removed previous runtime state: $RUNTIME_DIR"
fi

mkdir -p "$RUNTIME_DIR/notes" "$RUNTIME_DIR/claims" "$RUNTIME_DIR/locks" "$RUNTIME_DIR/cursors" "$RUNTIME_DIR/archive"
for name in claude-to-codex.jsonl codex-to-claude.jsonl; do
  if [ ! -f "$RUNTIME_DIR/$name" ]; then
    : > "$RUNTIME_DIR/$name"
  fi
done

for name in .codex-cursor .codex-seq .claude-cursor .claude-seq; do
  if [ ! -f "$RUNTIME_DIR/$name" ]; then
    printf '0\n' > "$RUNTIME_DIR/$name"
  fi
done

for name in PROJECT.md AGENTS.md CLAUDE.md; do
  if [ -f "$PROJECT_FILES_DIR/$name" ]; then
    if [ "$FORCE_PROJECT_FILES" = "1" ] || [ ! -f "$ROOT_DIR/$name" ]; then
      cp "$PROJECT_FILES_DIR/$name" "$ROOT_DIR/$name"
      echo "Wrote $name"
    else
      echo "Kept existing $name"
    fi
  fi
done

echo
echo "Handoff setup complete."
echo "Protocol/template dir: $HANDOFF_DIR"
echo "Runtime state dir:     $RUNTIME_DIR"
echo "Next: fill PROJECT.md if needed, then start Claude and Codex sessions; they will read CLAUDE.md / AGENTS.md and .handoff/PROTOCOL.md."
