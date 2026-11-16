# Installer milestone ownership map

This map defines exclusive write ownership for M4 hardening and M5–M7. All contributors share this worktree. Changes to a file owned by another contributor require an explicit ownership transfer from the coordinator after the current owner releases it. No parallel Git commits, rebases, checkouts, or resets are permitted.

## Owners and files

| Owner | Exclusive write ownership | Boundaries |
|---|---|---|
| A - Integration and compatibility | `tools/installer/cli.py`; `tests/verify-installer-m4.py`; `tests/run-installer-tests.sh`; the agreed M4 image-set adapter module and its compatibility tests | Owns public CLI parser/dispatch and integration wiring. Preserves existing flash engines. Owns local milestone commits, only after workers are quiescent and acceptance gates pass. |
| B - M5 verify and restore | New M5 module(s), initially proposed as `tools/installer/verify_restore.py` subject to interface agreement; `tests/verify-installer-m5.py` | Owns M5 implementation and M5-specific synthetic tests. Does not edit the shared CLI or A/C/D-owned files. Cross-reviews D's wizard changes. |
| C - M6 packages | New package module(s), standalone package scripts/templates, and `tests/verify-installer-m6.py` | Owns package generation, verification and standalone safety gates. Does not edit the shared CLI or A/B/D-owned files. Cross-reviews D's wizard changes if assigned instead of B. |
| D - M7 wizard, documentation and independent QA | `tools/installer/wizard.py` exporting `run(args, home, repo) -> int`; `tests/verify-installer-m7.py`; `docs/INSTALLER.md`; `README.md`; `FLASH-PLAN.md`; `DESIGN.md` | Does not edit the shared CLI. A wires the agreed public parser/dispatch. D's wizard must be cross-reviewed by B or C; D does not self-certify it. D performs independent regression/security QA and reports defects to their owners. |
| Coordinator/A - shared status and question records | `INSTALLER-REPORT.md` and `OPEN-QUESTIONS.md` are single-writer files owned by the coordinator/A pending any explicit transfer | Other owners send proposed report or question updates to the coordinator/A; they do not edit these files concurrently. |

## Integration interfaces

- A owns the CLI entrypoint and wires agreed module APIs. B, C and D expose module-level APIs without editing `tools/installer/cli.py`.
- D's wizard API is `tools/installer/wizard.py:run(args, home, repo) -> int`.
- A publishes the M4 image-set adapter interface before dependent implementation. No owner may infer or convert image formats without validating the actual consumer requirements and synthetic tests.
- The existing `tools/pathA/` and `tools/flash/flash-board.sh` write engines remain outside the owners' edit scope unless the coordinator explicitly transfers a file for a narrowly justified, tested change.

## Missing inputs and blockers

- `spec/INSTALLER-SPEC.md` and `spec/ACCEPTANCE.md` are not present in this workspace. Canonical requirements, including the exact G1–G9 definitions, must be recovered or treated as unresolved; do not invent mappings or claim complete spec coverage.
- No `AGENTS.md` or `CLAUDE.md` was found in the workspace inventory. Any applicable instructions supplied later by the coordinator take precedence and must be recorded before implementation.
- Real device, root, network, external firmware/base-image inputs and hardware acceptance are outside this offline workspace's permitted validation boundary. Record such gates as blocked or hardware-required; never report synthetic evidence as hardware proof.
