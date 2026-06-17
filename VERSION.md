# 版本记录 / Changelog

Codex ↔ Claude 协作 kit 的协议文档版本历史。当前版本见 `PROTOCOL.md` 第 1 行标题，两段式 `MAJOR.MINOR`：

- **MAJOR** — 破坏向后兼容（改消息字段语义、删/改字段、旧 runtime 需迁移、不兼容的流/目录格式）。
- **MINOR** — 向后兼容新增（新 tool、新可选字段、新 prompt step、新可选流程）。
- 纯文档措辞 / typo / 不改行为的 bugfix 不 bump。

消息 schema 字段 `v` 固定 `"1.0"`，不随协议文档版本变化。

---

## v1.10 — 2026-06-17

- 新增 `tools/poll-gate.py`：确定性 wake/idle pre-gate，让每轮 cron / heartbeat 用程序而非模型判定「该 idle 还是该处理」。退出码 `0` process / `10` proactive / `20` idle / `2` error；默认只读无状态，`--proactive-every N` 维护 per-session idle streak（状态写 `.handoff-runtime/.<side>-pollgate.json`）。
- `prompts/cron-prompt.md` 与 `prompts/codex-heartbeat-prompt.md` 对称新增 deterministic pre-gate 步骤，并把 `had_peer_reply` 绑到 gate 退出码。
- `tools/doctor.py` 将 `.<side>-pollgate.json` 加入已知运行时文件白名单。
- `PROTOCOL.md` §1 layout 与 §8 文档化 pre-gate；新增本 `VERSION.md`。

## v1.9 — 2026-06-16

- 首个公开发布（initial commit）。在 v1.8 基础上向后兼容引入：每-session cursor（消除同侧多 session 队头阻塞）、重放幂等、租约续期、流归档、信任边界、可选 liveness、自适应 cadence。
- 随后补充：`costart` 别名、触发词内联任务目标、自主协作（autonomous-collaboration）指引。

## v1.8 及更早

- 早于本仓库 git 历史，仅在 `PROTOCOL.md` 中作为 v1.9 的演进基线被引用，未单独留存变更记录。
