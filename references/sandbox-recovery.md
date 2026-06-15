# Sandbox recovery — when `./pokerkit` is blocked

The kit's CLI wrapper is `./pokerkit`. Some sandboxes (Claude Code in
strict mode, Codex CLI without workspace trust, Gemini CLI on an
untrusted directory, locked-down CI environments) block direct
execution of repo-local scripts even after the first "allow" prompt.

When this happens, surface this block to the user verbatim. They pick
ONE option — don't try multiple in parallel.

---

## Option 1 — Pre-grant via settings file (preferred)

The repo ships pre-grant examples for the three major agents. Copy the
one for your agent and reload:

```bash
# Claude Code
cp .claude/settings.json.example .claude/settings.json

# Codex CLI
cp .codex/config.toml.example ~/.codex/config.toml
# (or click "Allow Workspace" when Codex prompts)

# Gemini CLI
cp .gemini/settings.json.example .gemini/settings.json
# OR: export GEMINI_CLI_TRUST_WORKSPACE=true
# OR: gemini --skip-trust ...   (Gemini refuses untrusted dirs otherwise)
```

Then re-run the original command. This is the cleanest fix — pre-grants
the small fixed set of commands the kit actually uses (`./pokerkit *`,
`uv run *`, `uv sync`, `pytest`, basic `git` introspection).

---

## Option 2 — Use the wrapper-less `uv run` equivalents

Every `./pokerkit X` command has a documented `uv run python ...`
equivalent. Some sandboxes auto-allow `uv run` while blocking arbitrary
`./` invocations:

```bash
# Instead of:                       Use:
./pokerkit test                     uv run python -m pytest tests/ -q
./pokerkit selfplay --hands 200     uv run python examples/selfplay.py --hands 200
./pokerkit run --dry-run            uv run python examples/agent.py --dry-run
./pokerkit run                      uv run python examples/agent.py
./pokerkit analyze                  uv run python examples/analyze.py
./pokerkit replay --latest          uv run python examples/replay.py --latest
```

Same output, same `.env` handling, same `.arena-credentials` writing.
The shell wrapper is convenience-only; the Python entrypoints are the
truth.

If `uv run` is also blocked, see Option 3.

---

## Option 3 — STOP and ask the user

If both Option 1 and Option 2 are blocked, **do not try to escalate
the sandbox yourself**. Stop and surface this to the user:

```
⚠ Your sandbox is blocking both `./pokerkit` and `uv run`. I can't
safely proceed without execution. Three things you can do:

  1. Run the kit in a less restrictive shell (Terminal.app, iTerm,
     or your IDE's built-in terminal) and re-paste the SKILL.md URL
     there. The agent there will pick up where this one stopped.

  2. Switch to a different coding agent that has fewer restrictions
     for your project type (Claude Code, Codex CLI, Cursor all work).

  3. If you control the sandbox config, add `./pokerkit` and `uv run`
     to your allowlist:
       - Claude Code: edit `.claude/settings.json` permissions
       - Codex: trust the workspace
       - Gemini: GEMINI_CLI_TRUST_WORKSPACE=true

Tell me which path you picked and I'll continue.
```

**Never bypass the sandbox.** Don't try `sh -c`, don't try writing
files to elevated paths, don't try `sudo`. The sandbox is the user's
defense — respect it.

---

## What this file is NOT

- A path to elevated execution. There's no "agent override" trick.
- A list of every sandbox's full permission model. The three lines
  above cover the common cases; for anything else, the user owns the
  trust boundary.
- A reason to skip the pre-action Arena confirmation. Even with full
  permissions, `./pokerkit run` still requires explicit user `yes`
  before every match.
