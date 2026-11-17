# OpenStick installer guide

This guide describes the installer commands available in this source tree. The only supported board profile currently present is `ufi103s`. Installer verification in this workspace uses synthetic fixtures and fake EDL, fastboot and SSH shims; no physical device flash or boot was attempted.

## Host and private store

Use Linux with Debian 12 Bookworm and Python 3.9 or newer. The launcher uses the Python standard library. Individual commands need additional tools: build needs the existing builder toolchain and an already prepared `out/base-bookworm-arm64.tar.gz`; image assembly needs the existing filesystem tools; backup, flash and restore need the matching external EDL/fastboot tools when run on real hardware. The `doctor` output lists local prerequisites, but passing `doctor` is not proof that a board or firmware is supported.

`doctor` resolves the same EDL client and SHA-256-pinned Firehose loader used by backup and flash. It scans `/sys/bus/usb/devices` for a Qualcomm `05c6:9008` EDL device and checks read/write access to that device node. With no EDL device attached, USB access is reported as INFO and does not fail doctor. If access fails, the output recommends a narrow `uaccess` udev rule for `05c6:9008`; doctor never changes system files.

Doctor does not invoke the EDL client, so it cannot determine whether an attached device has a fresh or consumed Sahara session. When a 9008 device is present it reports that the session state is unknown until the first command; an absent device or a connected Qualcomm device in normal mode is reported separately.

The private store path uses this precedence: `--home DIR`, `OPENSTICK_HOME`, `${XDG_DATA_HOME}/openstick`, then `~/.local/share/openstick`. The store must be outside every Git worktree, must not pass through symlinks, and is created with private directory and file permissions. Keep backups and personalized artifacts on encrypted storage and make a second offline copy of a verified backup. A full eMMC backup contains unique identity and calibration data; do not share it or put it in Git.

The current registry uses a single `registry.json` plus an append-only `events.jsonl` in the store root. It does not use the specification's per-device `devices/<id>/device.json` and per-device event files. Seed application history is recorded in the registry; other device details remain in its device entry.

## First-run inspection

Start with read-only diagnostics and inspect registered state:

```sh
./openstick doctor
./openstick device list
./openstick wizard --id my-first-run --stage doctor
```

The current wizard runs one named stage at a time, recording run history under the private store:

CLI details: `build` requires both `--profile` and `--board`; `verify` requires `--host`; and `wizard` requires an explicit `--stage` on every call. `package restore` and `package install` accept either `--output PATH` or its `--out PATH` alias.

```sh
./openstick wizard --id my-first-run --stage backup --resume
```

Backup performs actual EDL reads and writes sensitive backup files; never run it as an unattended diagnostic or without owner supervision on hardware. The wizard does not suppress existing confirmation prompts. Resume only a recorded safe stage; an interrupted backup, package, flash or restore stage may be refused and require manual inspection. It never repeats a destructive stage automatically. Flash and restore reconciliation are separate no-write calls:

```sh
./openstick wizard --id my-first-run --stage flash --resume --reconcile-state
./openstick wizard --id my-first-run --stage restore --resume --reconcile-state
```

After reviewing the device and completing reconciliation, a later explicit stage call requests fresh gates. The reconciliation call itself never proceeds to a write. An uncertain backup or package stage has no automatic retry path; preserve its state and inspect the store before starting a new run.

The required base tarball and pinned build artifacts are not included in this sanitized workspace. M4 produces `boot.img` and `rootfs.img`; the historical legacy GPT-B first-install wrapper consumes `boot.bin` and `rootfs.bin`. The [current GPT-T image-set adapter](#build-install-and-verify) validates the Android boot image header, ext4 bounds, and paired GPT CRC/geometry, then maps boot to `boot` and rootfs to `userdata`. The current flash wrapper still refuses a GPT-T set because no safe live-identity-preserving hardware write and read-back path is validated. Do not rename or concatenate files to bypass this refusal. Install package creation and a complete first-install run remain blocked until their matching image, backup, firmware and write-path requirements are satisfied.

The synthetic M7 wizard run exercises the preparation stages, SSH evidence parsing, package fixture creation, and fake-disk restore. Its first-install path stops at flash with `HARDWARE_REQUIRED`; it does not demonstrate a successful stock-to-Debian E2E or a real boot. No physical device flash was attempted.

## Backup and identity

A backup must be a complete verified eMMC read with a valid primary and secondary GPT and matching `fsg`, `sec` and `fsc` fingerprints. The restore path compares the live EDL serial and identity fingerprints with the selected backup before writing. Identity is not inferred across EDL, Android or fastboot mode transitions. Stop if the live identity cannot be read or does not match.

The manifest's default `verified_by` entries are `full-read-size`, `gpt-primary-and-backup-crc`, and `sha256`. They record internal completeness and consistency checks; they do not mean the device was read a second time or compared with the board. `backup --paranoid` performs the stronger check by taking a second full read and comparing it with the first; its manifest also records `paranoid-second-read`.

The expected differences between the pristine dump and a post-boot backup are `modemst1`, `modemst2`, `system` (1 MiB chunk), `persist` (3 MiB chunks), `cache`, `recovery` (1 MiB chunk), and `userdata`. These observed differences are expected and are not alarms.

Backups include all partitions and are sensitive. Keep the whole backup, manifest and checksums together. Do not omit calibration or identity partitions. Restore uses the existing rollback plan and its phase ordering; each destructive phase has its own dry-run and typed confirmation. A failure or power interruption requires manual device-state inspection and explicit reconciliation. Never retry a possibly partial write automatically.

## Configure and seed

`configure` creates a private personalization seed. Public SSH keys are accepted; private keys are not. Wi-Fi and APN credentials are secrets and stay in the private seed and device configuration. Do not paste secret values into command arguments or tickets. Review a seed only with the masked `configure --show` operation.

For a generic build, the installer excludes personal `local.env` and `user.env` settings unless the explicit legacy `--bake-user-env` option is selected. Prefer injecting a validated seed into a copy of a generic rootfs. Injection checks and hashes the copy and leaves the generic image untouched. A seed file being present does not prove SSH access; an unseeded production image without working access must be recovered through an explicitly prepared seeded image.

## Build, install and verify

The generic build command is:

```sh
./openstick build --profile prod --board ufi103s
```

Build runs the existing rootfs and production image stages. Only the rootfs configuration stage requests elevation through the existing wrapper; do not run the launcher itself as root. Generic builds exclude personal `local.env` and `user.env` values. The generic dev image still uses the public `DEFAULT_USER=openstick` and `DEFAULT_PASS=openstick` values from `config/defaults.env`; the password expires at first login. `build --dry-run` prints this fact. This unattended offline session did not perform a real build because the base image and pinned external inputs are missing and root/network access is outside its operating boundary.

For the supported UFI103S GPT-T geometry, a registered build can be adapted into a self-contained image set when the owner has supplied the matching paired GPT-T layout files:

```sh
./openstick image-set prepare --build BUILD_ID --layout VERIFIED_LAYOUT_DIR --output /private/path/ufi103s-image-set
./openstick image-set verify /private/path/ufi103s-image-set
```

`VERIFIED_LAYOUT_DIR` must contain `gpt-T-primary.bin` and `gpt-T-secondary.bin`. The adapter checks their CRCs and the documented partition geometry, verifies the Android boot v0 header and ext4 size, and records `boot.img -> boot` and `rootfs.img -> userdata`. It does not prove that the files came from a real target or that a device can be written safely. The paired layout inputs are absent from this workspace. `flash` explicitly refuses this GPT-T set with `HARDWARE_REQUIRED` until a safe live-identity-preserving write and complete read-back path has been validated.

The intended install sequence is: verify a full backup, select a matching board and image set, inject or select a verified seed, review dry-runs, then confirm each flash phase explicitly. `flash` validates the image checksums, board, backup, live identity, pinned loader and Path A assets before each write. A successful command sequence is not proof that the device booted. `verify` reports subsystem checks individually as pass, fail or skip based on direct SSH evidence; missing evidence must not be treated as a pass.

Package commands should be used only after inspecting package manifests and checksums. `package verify` is read-only and never executes package content. Packaged install and restore scripts are **UNVALIDATED on hardware**. Their help and package README state this, and any destructive operation still requires the normal typed flash or restore confirmation. These packages contain sensitive recovery material; store and transfer them as private files. A restore package is not a substitute for a separate offline copy of the verified full backup.

## Separate build and flash machines

Build artifacts may be transferred to another machine only as a self-contained, hash-verified image set or package. Copy the entire manifest and every listed payload file, verify checksums at the destination, and keep secrets out of the generic build. The destination machine still needs the explicitly documented EDL/fastboot prerequisites and the matching per-device verified backup. Current image-format incompatibility blocks claiming that an ordinary M4 build directory is directly flashable.

## Hardware acceptance boundary

No physical device flash was attempted by this work. Before a human performs hardware acceptance, independently confirm the exact board label and provenance, capture device identity in EDL, compare the live serial and `fsg`/`sec`/`fsc` fingerprints to the verified backup, validate that the build outputs match the target's actual boot and GPT-T layout, and inspect every phase plan. The owner must supervise the original Android-to-Debian test cycle, verify a real boot and SSH/network behavior, restore the original Android image through the documented rollback plan, compare a complete final readback against the original full-disk SHA-256, and verify the restored device boots. This is a separate owner-supervised procedure; synthetic shims cannot establish those results.

## Exit status and recovery

The launcher uses exit `0` for success, `1` for operational error or interruption, `2` for a refused safety gate, and `3` for a specific integrity or read-back verification failure. Unexpected exceptions exit `1`; add `--debug` to request a traceback. The launcher does not redact arbitrary exception text, so secrets must never be included in exception messages. Preserve the event log and phase state after any failure. For uncertain destructive state, stop, keep the device in its current mode if safe, and have the owner inspect it against the phase plan before any explicit reconciliation. Do not clear state by editing registry files.
