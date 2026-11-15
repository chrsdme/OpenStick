# Installer design baseline

## Repository and architecture

This is the sanitized builder tree, not the complete owner checkout. Installer code does not exist yet. The existing implementation is shell scripts with `scripts/common.sh` loading `config/defaults.env`, the selected `boards/<BOARD>/board.env`, then optional `config/local.env` and `config/user.env` behavior in the build scripts. The existing build precedence is process environment, local config, board config, defaults; that is unsuitable for a generic installer build and must be isolated in a wrapper. `config/user.env` and `config/local.env` were deliberately excluded from this copy.

Build chain: `10-base.sh` creates Debian base; `20-rootfs.sh` configures packages and rootfs; `30-gt.sh` builds USB gadget tooling; `gen-gpt.sh` creates GPT artifacts; `40-images.sh` creates legacy images; the currently documented verified path instead uses `50-boot-img.sh` and `51-rootfs-img.sh`. Root-requiring scripts call `need_root`; image assembly scripts use fakeroot/e2fsprogs. Flash code is separated into fastboot (`tools/flash/flash-board.sh`) and EDL Path A (`tools/pathA/{fwlib,pathA-flash,pathA-rollback}.sh`). Restore planning is read-only (`tools/rollback-plan.py`, wrapper `tools/rollback-plan.sh`). Installer work must wrap these interfaces without changing their write logic.

## Existing script inventory

All scripts are POSIX/Bash shell except the noted Python programs. Most shell scripts exit nonzero on error (`set -e` or `set -eu`; commonly 1); gate refusals are not consistently distinguished from errors. These are observed interfaces, not a promise of stable API.

| Script | Inputs / env | Outputs and effects | Requirements / notes |
|---|---|---|---|
| `scripts/common.sh` | exports defaults, board config; `BOARD`, `PROFILE`, `OUT`, `WORK`, `CHROOT`, `FILES_DIR`; helper functions | shared build paths and helpers | `safe_dir`, `need_root`, binfmt, ownership and profile validation |
| `10-base.sh` | common config, release/packages | `out/base-<release>-arm64.tar.gz` | root, debootstrap, binfmt, keyring |
| `20-rootfs.sh` | base tar, defaults/board/local/user env, profile, package/network credentials | `out/rootfs-<board>.tgz` | root, binfmt, pinned kernel APK; modifies a chroot |
| `30-gt.sh` | source trees and cross compiler | `work/gt-dist` | root, aarch64 compiler, autotools/build dependencies |
| `40-images.sh` | rootfs tar, GPT layout, optional gadget tree; `ROOTLESS` | `out/files-<board>/{boot,rootfs}.bin`, checksums | image tools; mount path needs privilege unless rootless path applies |
| `45-testboot.sh` | cached pinned kernel APK and BusyBox deb; DTB/cmdline overrides | `out/bootimg/` test images | network-capable tools in general; do not run offline |
| `46-boot-lk2nd.sh` | rootfs tar, layout, pinned lk2nd image | `out/bootlk2nd/` | fakeroot, mke2fs, tar, truncate, dd |
| `47-testboot2.sh` | cached kernel/BusyBox, prior artifacts, DTB | `out/bootimg2/` | dtc/fdt tools, image utilities |
| `48-testboot3.sh` | cached kernel/BusyBox and v2 artifacts | `out/bootimg3/` | image utilities |
| `49-inner-fakeroot.sh` | positional tar/tree/image/size/key/config/label/UUID | test ext4 image | fakeroot caller; invokes filesystem tooling |
| `49-rootfs-test.sh` | rootfs tar, prior boot images, public key and overrides | `out/rootfs-test/` | image tools; test-image builder, no installer dependency |
| `50-boot-img.sh` | common config, cached pinned kernel APK and BusyBox; `CROSS_CC`, `REBOOT_BL_BIN`, `PROD_CMDLINE`, `PROD_OUT`, `DTB_NAME` | `out/prod-boot/boot.img`, initramfs, checksums | rootless intended; python, gzip, tar, dpkg-deb, sha256sum, cross compiler or supplied reboot binary |
| `51-rootfs-img.sh` | `out/rootfs-<board>.tgz`, `PRODROOT_OUT`, `ROOTFS_TGZ`, `ROOTFS_IMG_SLACK_MIB` | `out/prod-rootfs/rootfs.img`, checksums | rootless; fakeroot, mke2fs, e2fsck, debugfs, tar, truncate |
| `build-dtb.sh` | DTS and `src/linux-dts` | built DTB under out | dtc/compiler inputs |
| `build_hyp_aboot.sh` | pinned source/build configuration | Path A firmware outputs | cross tools; builds firmware, not a device write |
| `extract_fw.sh` | pinned archive URL and sha256 | extracted firmware under out | downloads; not run |
| `fetch-dts.sh`, `fetch-src.sh` | pin files and source URLs | populates `src/` | network; not run |
| `gen-gpt.sh` | common config, board template and eMMC size | GPT files and layout env under `out/fw` | python3 and GPT tooling; current documentation describes GPT-T, README retains stale GPT-B language |
| `preflight.sh` | common config and cached kernel artifact | checks prerequisites/artifact | read-only checks; no device access |
| `setup.sh` | `/build.env` inside target root | installs/configures packages and users | intended chroot stage; not run |
| `msm-firmware-loader.sh` | runtime sysfs and partition firmware sources | mounts firmware at runtime | device-side service, never run on host |
| `gptool.py` | image/template, board layout args | GPT image/layout data | Python stdlib plus external `sgdisk` invocation from wrapper |
| `tools/mkimg.py` | rootfs tree and image parameters | filesystem image | Python stdlib, filesystem tools called externally |
| `tools/gen-gpt-tz1m.py` | GPT/layout inputs | GPT variants | Python stdlib |
| `tools/rollback-plan.py` | dump, partition CSV, original GPTs, identity/hash arguments | prints dry-run/restore plan and comparisons; never executes | Python stdlib; see ROLLBACK.md |
| `tools/rollback-plan.sh` | forwards CLI to Python planner | printed plan | Python 3 |
| `tools/fetch-firehose.sh` | pinned URLs and hash | installs loader under out | network, sha256sum; test uses fake download shims |
| `tools/flash/flash-board.sh` | mode and dry/write; board config, image set, `COMMIT`, `CONFIRM`, backup vars | fastboot dry-run/write and readback | external fastboot; guarded; MUST only be invoked through fake shims in development |
| `tools/pathA/fwlib.sh` | `PKG`, `W`, `EDL_CMD`, loader and package hashes | common EDL read/write helpers | EDL client and loader; writes use skipwrite/dry-run and readback |
| `tools/pathA/pathA-flash.sh` | phase and dry/write, firmware/GPT/rollback assets, confirmations | Path A phased EDL writes | destructive; no execution in M0 |
| `tools/pathA/pathA-rollback.sh` | phase and dry/write, backup boot and stock rollback assets | restore phases | destructive; no execution in M0 |

## External inputs and pins

Inputs not safely included in this snapshot include the EDL client and its environment, Firehose loader, fastboot, kernel APK, BusyBox package, cross compilers, e2fsprogs, and Path A firmware blobs. `config/defaults.env` pins the kernel APK (`KERNEL_APK_SHA256`); boot image scripts pin BusyBox (`BB_SHA256`); `tools/fetch-firehose.sh` and Path A library pin loader SHA256 `959439aa...dcab14`; `scripts/extract_fw.sh` pins its archive; `scripts/46-boot-lk2nd.sh` pins the lk2nd padded image. See those files for full values. Source revisions are declared in `config/src-pins.txt` and `config/dts-pin.env`. No external artifact was downloaded in this task.

## Seed decisions for implementation

Proposed seed keys follow spec section 6.2: `SEED_VERSION`, `HOSTNAME`, `TIMEZONE`, `SSH_AUTHORIZED_KEYS`, `ROOT_PASSWORD_HASH`, `DEFAULT_USER`, `USER_PASSWORD_HASH`, `WIFI_SSID`, `WIFI_PSK`, `WIFI_COUNTRY`, `APN`, `APN_USER`, `APN_PASSWORD`, `USB_NET_ADDR`, `NTP_SERVERS`; `LED_*` remains reserved and ignored with notice. Parser must reject duplicate/unknown malformed keys and shell syntax; multi-line authorized keys need a deterministic encoding or a separate referenced file decision (OPEN-QUESTION). On successful apply, remove the seed and keep only its sha256 receipt, matching the spec's recommended choice. Add `apply-seed` and the oneshot unit as narrow additions copied into the rootfs by a dedicated hook in `20-rootfs.sh` only if testing demonstrates the hook point; do not alter existing package/configuration behavior. Invalid seed logs a redacted error and leaves generic configuration intact.

## M0 reproducibility baseline

The sanitized workspace has no `out/` directory, cached kernel/BusyBox inputs, or generated `prod-boot` and `prod-rootfs` images. Therefore the required pre-change `boot.img` and `rootfs.img` SHA256 values cannot be produced without running build steps and retrieving external artifacts; those operations are expressly excluded. SHA256 fingerprints of the current source scripts were recorded during M0 in the task notes, but are not substitutes for image hashes. No build script was modified. The source snapshot commit is `e4cf01b`; this was created before installer development and is the only available history (no upstream `baseline` commit exists in this copy).

## Specification comparison

The installer entry point, `tools/installer/`, shims, docs/INSTALLER.md, state store, registry, seed integration, backup/restore package flows and wizard do not exist in this tree. README describes older GPT-B images while ROLLBACK.md establishes current GPT-T and documents a verified userdata/rootfs path; these existing contradictions are captured in OPEN-QUESTIONS.md. See OPEN-QUESTIONS.md for section-by-section coverage and unresolved design questions.
