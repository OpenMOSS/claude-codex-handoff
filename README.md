# Claude ↔ Codex Handoff

A drop-in kit that lets two AI coding agents — for example **Claude Code** and
**Codex** — collaborate asynchronously inside one repository. They talk through
append-only files: one message stream per direction, atomic leases, and
per-session cursors with legacy shared cursor anchors. No server, no daemon.
Each side wakes on a timer, processes unread
messages, optionally hands work back, and exits.

It installs as a single **`.handoff/` folder in your project**, shared by both
agents. There is no separate global "skill" copy to keep in sync — Claude Code
and Codex read the same files.

## Install (into your project)

From your project root, clone this repo as `.handoff/`, then run setup:

```bash
cd your-project
git clone https://github.com/OpenMOSS/claude-codex-handoff.git .handoff

# macOS / Linux / WSL:
bash .handoff/setup.sh
# Windows:
powershell -ExecutionPolicy Bypass -File .handoff\setup.ps1
```

`setup` creates `.handoff-runtime/` (live state) and copies `PROJECT.md` /
`CLAUDE.md` / `AGENTS.md` to your project root if they're absent. From then on
Claude Code (via `CLAUDE.md`) and Codex (via `AGENTS.md`) both read the same
`.handoff/`. Update later with `git -C .handoff pull`.

> **Git note.** `.handoff/` is itself a git clone. Always add **`.handoff-runtime/`**
> to your project's `.gitignore`. For `.handoff/` itself, either keep it as an
> updatable dependency (add `.handoff/` to `.gitignore`, `git -C .handoff pull` to
> update) or vendor it into your repo (`rm -rf .handoff/.git`, then commit the files).

## Layout

```
your-project/
├── .handoff/                       # this repo — protocol + helper + templates (shared, one copy)
│   ├── PROTOCOL.md                 # the spec — source of truth
│   ├── setup.ps1 / setup.sh
│   ├── tools/send.py               # protocol-safe JSONL sender (stdlib only)
│   ├── tools/archive.py            # consumed-stream archive helper
│   ├── tools/doctor.py             # read-only runtime diagnostics
│   ├── project-files/              # PROJECT.md / CLAUDE.md / AGENTS.md templates
│   └── prompts/                    # cron-prompt.md (Claude) / codex-heartbeat-prompt.md (Codex)
├── .handoff-runtime/               # message streams, per-session cursors, claims, notes (gitignore)
├── PROJECT.md                      # shared scope + task boundaries (fill the <FILL_IN>s)
├── CLAUDE.md                       # Claude-side entry
└── AGENTS.md                       # Codex-side entry
```

## Quick start: start a collaboration

In **each** agent, open the project and say **`启动协作`** (start collaboration). That's all you type — the agents do the rest, including calling `send.py` themselves.

**Claude side:** in the project, say `启动协作`. Claude reads `CLAUDE.md` → `.handoff/PROTOCOL.md`, runs setup if needed, helps you fill `PROJECT.md`, creates its recurring cron (`prompts/cron-prompt.md`), does a first poll, and sends Codex your first task.

**Codex side:**

Open the project in the Codex App and say `启动协作`. Codex reads `AGENTS.md` → `.handoff/PROTOCOL.md`, runs setup if needed, sets up its ~10-min heartbeat automation (`prompts/codex-heartbeat-prompt.md`), and does a first poll.

**Then it runs itself.** Each side wakes on its timer, reads its inbox stream, claims and completes any `task` / `handoff` / `question`, replies with `done`, and goes quiet when there's nothing to do.

## How it works

- **Two streams.** `claude-to-codex.jsonl` (Claude writes, Codex reads) and `codex-to-claude.jsonl` (the reverse). Append-only; a written line is never edited.
- **Message types.** `task`, `handoff`, `question`, `done`, `status`, `error`, `cancel`. Lease-bearing types need an atomic claim before processing; `status` and `done` are pure-consumption.
- **Per-session cursors + leases.** Each running session tracks its own cursor under `.handoff-runtime/cursors/`; legacy `.codex-cursor` / `.claude-cursor` files are compatibility anchors. Work items are claimed via atomic file creation under `.handoff-runtime/claims/`. Iron law: **side effects first, advance cursor last** — so nothing is lost on a crash or interruption.
- **No daemons.** Claude uses a recurring cron (every ~10 min); Codex uses a Codex App heartbeat. Each fire is one bounded pass; idle fires exit quietly.
- **Multi-session safe.** Optional `from_session` / `to_session` labels let two same-side sessions run without stealing each other's replies; a per-side send lock serializes id assignment.

See [`PROTOCOL.md`](PROTOCOL.md) for the full specification.

## The two sides

|                | Claude side                               | Codex side                                                |
| -------------- | ----------------------------------------- | --------------------------------------------------------- |
| Entry file     | `CLAUDE.md`                               | `AGENTS.md`                                                |
| Wake mechanism | recurring cron (`prompts/cron-prompt.md`) | Codex App heartbeat (`prompts/codex-heartbeat-prompt.md`) |
| Writes to      | `claude-to-codex.jsonl`                   | `codex-to-claude.jsonl`                                   |

## Sending a message

The agents use the helper themselves; you rarely call it directly:

```bash
python .handoff/tools/send.py --side claude --type task \
  --summary "review the auth refactor" --goal "list correctness risks"

python .handoff/tools/send.py --side codex --type done \
  --reply-to claude-000001 --summary "reviewed, 2 findings in notes" \
  --notes-file notes/codex-000005.md
```

The helper assigns ids under a lock, stamps every required field, and serializes concurrent sends. Add `--dry-run` to preview without writing.

## Runtime diagnostics

When a collaboration appears stuck or noisy, run the read-only doctor from the
project root:

```bash
python .handoff/tools/doctor.py
```

It checks stream JSON, local seq files, session cursors, legacy cursor anchors,
claims, and unexpected runtime files. It does not modify project or runtime
state.

## License

MIT © OpenMOSS
