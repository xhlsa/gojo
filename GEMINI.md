# Gemini Agent Playbook

Gemini automations should treat this repository as sensor-grade software: always pick up context from the docs listed below before proposing edits.

## Quick Links
- [README.md](README.md) – live project status, supported entrypoints (`motion_tracker_v2.sh`, `test_ekf.sh`), and troubleshooting cheatsheets.
- [AGENTS.md](AGENTS.md) – contributor workflow, coding conventions, and the exact commands reviewers expect to see in PRs.
- [.claude/CLAUDE.md](.claude/CLAUDE.md) – rolling technical journal with memory fixes, incident thresholds, and experiment notes to cite when automating decisions.

## Suggested Workflow
1. Skim `README.md` for the scenario at hand (deployment, investigation, or benchmarking), note that `./test_ekf.sh` is the canonical harness, and record version-specific constraints.
2. Jump to `AGENTS.md` to confirm file layout, build/test steps, and how to format commits or artifacts.
3. Mine `.claude/CLAUDE.md` for prior art (sensor settings, queue tuning, incident math) so Gemini output references real experiments rather than guesses.

## Output Handoff
- When generating code or fixes, echo the commands run (from `README.md`/`AGENTS.md`) and cite the relevant sections of `.claude/CLAUDE.md`.
- Attach session JSON, analyzer logs, or crash artifacts from the directories enumerated in `AGENTS.md` to keep reviewer expectations aligned.
