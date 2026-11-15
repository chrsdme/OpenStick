# Open questions and specification differences

M0 defaults are recorded as recommendations, not verified device behavior. Hardware-only questions remain unverified.

## Existing tree versus specification

| Spec area | Observed tree | Recommended default / question |
|---|---|---|
| §3 build outputs | README presents old GPT-B sparse boot/rootfs flow; ROLLBACK.md describes GPT-T and verified `boot.img` plus rootfs in `userdata`. | Treat ROLLBACK.md/current GPT-T flow as authoritative. Confirm docs should be corrected in M7 as acceptance says. |
| §3 scripts | Current `tools/flash/flash-board.sh`, `tools/pathA/*`, `tools/rollback-plan.py` exist. Installer wrapper and state code absent. | Wrap current scripts; do not change their write logic. |
| §3 profile | Tree has dev/prod checks, but no dedicated installer interface. | Preserve profiles; generic build must scrub all personal environment/config. |
| §4 commands | `./openstick` and `tools/installer/` absent. | Implement per spec in M1+. |
| §5 store/registry | No registry/store exists. | Implement schema v1; separate immutable hardware ID, USB aliases, user label. |
| §6 seed | No seed parser/apply script/service exists. | Keys as listed in DESIGN.md; remove seed after successful apply and retain hash receipt. How to represent multi-line SSH keys in strict KEY=VALUE seed remains open. Recommended: one base64-encoded value or explicitly specified escaped newline syntax, never shell evaluation. |
| §6.6 baseline | No generated images/artifacts/caches in sanitized workspace; image hashes unavailable without external inputs/build. | Keep this explicit limitation; before first installer build, capture hashes only if reproducible inputs are already present. Do not run an external build to fabricate baseline. |
| §7 restore package | No package code exists. Full recovery guarantee depends on dump/GPT and stand-alone scripts. | Include entire verified backup dump, both original GPTs, plan, and standalone restore script. Do not omit partitions. |
| §8 gates | Existing engine has per-script guards; installer-level gates/tests absent. | Add fake-device tests before any wrapper can invoke engine write mode. Interrupted writes require explicit reconciliation against current state; no retry. |
| §9 docs | No docs/INSTALLER.md. | Add during M7. |
| §10 dashboard | No dashboard should be implemented. | Keep out of scope. |

## Decision questions

1. **M0 commit ancestry:** The clean copy had no Git history. A new repo was initialized and the source snapshot committed as root commit `e4cf01b` labeled `M0: import clean builder snapshot`. The acceptance wording expects `baseline..installer`, but no baseline exists. Recommended: preserve this root commit and make future milestone commits on `installer`; do not invent or rewrite a baseline commit.
2. **SSH keys in seed:** Which encoding should the v1 strict parser use for multiple authorized key lines? Recommended: a separate input file during configure encoded into a single base64 seed value, decoded only by apply-seed; hash and redact in logs.
3. **`seed push` transport:** What address/interface should default `--host` use for the current gadget network? Recommended: require explicit host unless a documented default is validated from current config.
4. **USB identity aliases:** Which exact EDL/Android/Debian serial fields are available via the installed EDL client and USB tools? Resolve with fake shim interfaces first; hardware capture remains hardware-only.
5. **Seed APN application:** The existing build installs LTE profile templates, but the exact runtime-safe APN mutation interface is not documented in this snapshot. Recommended: use NetworkManager profile editing if validated in a fake root; do not invoke modem writes.
6. **Feature scope conflict:** Installer specs require flashing/restore, while global rules prohibit real hardware. Recommended: implement only wrappers tested against fake EDL/fastboot shims; report actual board operation as hardware-only.

## M0 legacy test failures

See INSTALLER-REPORT.md for exact assertion, exit code, confirmed cause and classification. No legacy script was changed to make these tests pass. They are baseline exceptions only when demonstrably unrelated; an installer regression must still block its milestone.
