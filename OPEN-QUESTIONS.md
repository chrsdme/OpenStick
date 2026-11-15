# Open questions and specification differences

M0 defaults are recorded as recommendations, not verified device behavior. Hardware-only questions remain unverified.

## Existing tree versus specification

| Spec area | Observed tree | Recommended default / question |
|---|---|---|
| §3 build outputs | README presents old GPT-B sparse boot/rootfs flow; ROLLBACK.md describes GPT-T and verified `boot.img` plus rootfs in `userdata`. | Treat ROLLBACK.md/current GPT-T flow as authoritative. Confirm docs should be corrected in M7 as acceptance says. |
| §3 scripts | Existing Path A and flash-board engines remain unchanged. M4 wraps them with verification and typed-confirmation gates. | Hardware execution remains forbidden and unverified here. |
| §3 profile | Tree has dev/prod behavior. M3 configure records the dev/prod account policy in the seed. M4 generic mode omits personal settings and allows an unseeded prod rootfs for later injection. | Generic prod behavior is covered by wrapper tests; image assembly was not run. |
| §4 commands | `./openstick` now provides M1-M4 build and flash commands plus prior commands. | Verify/restore/package/wizard remain later milestones. |
| §5 store/registry | M1 private store and registry exist; M4 registers immutable build artifacts and partial flash state. | Registry paths are absolute and need re-verification after moving the store. |
| §6 seed | M3 strict parser, configurator, apply utility/service, injection and push exist. | Seed encoding and deletion choice are resolved below and in DESIGN.md. Hardware behavior remains unverified. |
| §6.6 baseline | No generated images/artifacts/caches in sanitized workspace; image hashes unavailable without external inputs/build. | Keep this explicit limitation; before first installer build, capture hashes only if reproducible inputs are already present. Do not run an external build to fabricate baseline. |
| §7 restore package | No package code exists. Full recovery guarantee depends on dump/GPT and stand-alone scripts. | Include entire verified backup dump, both original GPTs, plan, and standalone restore script. Do not omit partitions. |
| §8 gates | M4 implements flash backup, live identity, input hash, typed confirmation, non-interactive and interrupted-state gates. | Real EDL, firmware provenance and hardware readback remain unverified. |
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

8. **M4 prepared base image:** The requested wrapper order starts at `20-rootfs.sh`, but that script requires `out/base-bookworm-arm64.tar.gz`, which is produced by `10-base.sh`. M4 accepts only an already prepared base artifact and deliberately does not run `10-base.sh`; no real build was run. Recommended: prepare and verify the base explicitly before using `openstick build`.
9. **M4 build output versus first-install input:** `50-boot-img.sh` and `51-rootfs-img.sh` produce `boot.img` and `rootfs.img`. The existing `flash-board.sh first-install` instead requires sparse `files-BOARD/boot.bin` and `rootfs.bin`, a board layout, and Path A firmware/GPT/rollback assets. The M4 wrapper requires that exact separately prepared image-set layout and will not infer a conversion or fallback. Recommended: define a later verified image-set packaging/conversion step; until then, a registered M4 build alone is not a flash image set.
10. **M4 Path A firmware provenance:** No Path A firmware/GPT/rollback blobs or package checksum file are present in the sanitized tree. The wrapper requires all assets in the selected private image set with SHA256SUMS and checks the pinned Firehose loader, but cannot establish the historical provenance of newly supplied Path A blobs offline. The wrapper refuses missing or mismatched entries. Hardware and provenance review remain required before using such a set.
11. **M4 reset/final behavior:** `pathA-flash.sh final write` sends EDL reset. To honor the no-automatic-reset requirement, M4 runs `final` dry-only and then invokes first-install. Recommended: leave device reset/boot to an explicit later manual step after reviewing the phase report.
12. **M4 image baseline:** The sanitized workspace has no M0 boot/rootfs images or pinned kernel/BusyBox artifacts. The build manifest records baseline comparison as unavailable and names the documented seed hook; a reproducible byte comparison remains open until a complete pinned input set exists.

## M0 legacy test failures

See INSTALLER-REPORT.md for exact assertion, exit code, confirmed cause and classification. No legacy script was changed to make these tests pass. They are baseline exceptions only when demonstrably unrelated; an installer regression must still block its milestone.
