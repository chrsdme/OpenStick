# Open questions and specification differences

M0 defaults are recorded as recommendations, not verified device behavior. Hardware-only questions remain unverified.

## Existing tree versus specification

| Spec area | Observed tree | Recommended default / question |
|---|---|---|
| §3 build outputs | README presents old GPT-B sparse boot/rootfs flow; ROLLBACK.md describes GPT-T and verified `boot.img` plus rootfs in `userdata`. | Treat ROLLBACK.md/current GPT-T flow as authoritative. Confirm docs should be corrected in M7 as acceptance says. |
| §3 scripts | Current validated flash and rollback tools remain separate; M1-M3 installer code does not change their write logic. | Later milestones may wrap current scripts with shims and gates; no engine rewrite. |
| §3 profile | Tree has dev/prod behavior. M3 configure records the dev/prod account policy in the seed. | Generic build and build environment isolation remain M4 work. |
| §4 commands | `./openstick` and `tools/installer/` provide M1 registry/backup and M3 configure/seed commands. | Build/flash/verify/restore/package/wizard commands remain later milestones. |
| §5 store/registry | M1 private store and registry exist; M3 uses `seeds/` and `builds/` under the same private root. | Device identity and backup rules are in M2; future build registry fields remain M4. |
| §6 seed | M3 strict parser, configurator, apply utility/service, injection and push exist. | Seed encoding and deletion choice are resolved below and in DESIGN.md. Hardware behavior remains unverified. |
| §6.6 baseline | No generated images/artifacts/caches in sanitized workspace; image hashes unavailable without external inputs/build. | Keep this explicit limitation; before first installer build, capture hashes only if reproducible inputs are already present. Do not run an external build to fabricate baseline. |
| §7 restore package | No package code exists. Full recovery guarantee depends on dump/GPT and stand-alone scripts. | Include entire verified backup dump, both original GPTs, plan, and standalone restore script. Do not omit partitions. |
| §8 gates | Existing engine guards remain unchanged; G4 applies to production configuration. | Remaining flash/restore gates are later milestones and must use fake-device tests before invoking write modes. |
| §9 docs | No docs/INSTALLER.md. | Add during M7. |
| §10 dashboard | No dashboard should be implemented. | Keep out of scope. |

## Decision questions

1. **M0 commit ancestry:** The clean copy had no Git history. A new repo was initialized and the source snapshot committed as root commit `e4cf01b` labeled `M0: import clean builder snapshot`. The acceptance wording expects `baseline..installer`, but no baseline exists. The installer branch preserves this root commit; no baseline was invented or rewritten.
2. **SSH keys in seed, resolved in M3:** `SSH_AUTHORIZED_KEYS` is a base64 value carrying newline-separated public-key lines. The public key file is imported during configure; no private key data is accepted. SHA-256 applies to the complete seed file.
3. **`seed push` transport, resolved in M3:** Require explicit `USER@HOST` and verify batch SSH access before transfer. No default gadget address is assumed. If SSH access fails, production recovery is to flash an image containing the seed.
4. **USB identity aliases:** Which exact EDL/Android/Debian serial fields are available via the installed EDL client and USB tools? Fake shims cover the interface; hardware capture remains hardware-only.
5. **Seed APN application, resolved in M3:** Apply APN and optional credentials by writing the existing NetworkManager GSM keyfile format. No modem writes are performed.
6. **Feature scope conflict:** Installer specs require later flash/restore work, while global rules prohibit real hardware. M1-M3 test through offline fixtures and shims only; actual board operation remains hardware-only.

7. **M2 EDL identity fields:** The fake client exposes `serial`, `hwid`, `msm_id` and `pk_hash` as simple key/value text. The installed real EDL client may report these through different output or flags. Recommended: keep serial as the stable registry key and record only fields the client reports; confirm output parsing on hardware before relying on optional identifiers.

## M0 legacy test failures

See INSTALLER-REPORT.md for exact assertion, exit code, confirmed cause and classification. No legacy script was changed to make these tests pass. They are baseline exceptions only when demonstrably unrelated; an installer regression must still block its milestone.
