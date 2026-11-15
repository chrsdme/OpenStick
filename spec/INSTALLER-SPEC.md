# OpenStick installer: specification (v1, Linux only)

## 1. Goal
One command line tool, `openstick`, that takes a user with an MSM8916 OpenStick LTE stick from "stock Android" to "slim Debian 12, personalised" and back, safely. Stages: doctor, backup, configure, build, seed, flash, verify, restore, package. It wraps the scripts that are already validated on real hardware. It must never be the reason a board gets bricked.

Target hardware today: board class `ufi103s` only (MSM8916, 512 MB RAM, about 3.9 GB eMMC). Any other board: refuse unless `--i-accept-untested` is given.

## 2. Principles
- P1 Generic by default. A build produces a generic image with nothing personal in it: no ssh keys, no passwords, no WiFi, no APN, no user hostname. Personalisation is a separate step (the "seed"), applied before flashing (inject into a copy of the image) or after (push over ssh).
- P2 Private store outside the repo. Everything personal (registry, backups, seeds, personal images, packages) lives in a private directory that the repo never sees.
- P3 Wrap, do not rewrite. The validated scripts are the engine. The installer adds questions, gates, state, logging and resume.
- P4 No silent writes. Every write to a board is preceded by a dry-run and a typed confirmation. No `--yes` for writes.
- P5 Honest status. Every stage reports what it verified, how (hash, read-back, ssh check) and what it could not verify.
- P6 Each user owns their own devices and data. Nothing is shared, uploaded or phoned home.

## 3. Existing building blocks (inventory these first; names may differ, verify in the tree)
- Build: scripts/20-rootfs.sh (needs root), scripts/50-boot-img.sh, scripts/51-rootfs-img.sh (must run WITHOUT root). Config precedence: env > config/local.env > boards/<BOARD>/board.env > config/defaults.env. config/user.env is personal (git-ignored, chmod 600).
- Profiles: `dev` (default; test user, autologin) and `prod` (root locked or ssh-key only, no sudo, no users with uid >= 1000, ttyGS0 masked; prod ignores DEFAULT_USER).
- Initramfs: finds the `userdata` partition via sysfs PARTNAME, requires the marker /etc/openstick-rootfs, switch_root; on failure waits INITRAMFS_WAIT_SECS and reboots to the bootloader.
- Flash: tools/flash/flash-board.sh (first-install: composite boot = lk2nd head of 524288 bytes + boot.img, written through fastboot in the lk1st bootloader), tools/pathA/* (pathA-flash.sh phases pre, gpt, tz, rpm, sbl1, hyp, aboot, final; pathA-rollback.sh; fwlib.sh), guarded by COMMIT=1 and CONFIRM=<token> environment variables.
- Restore: tools/rollback-plan.py and tools/rollback-plan.sh (print a gated restore plan from a dump, a partitions CSV and the original GPTs; they NEVER execute), ROLLBACK.md.
- Tests: tests/verify-*.sh.
- External inputs that are not in the repo (list each in DESIGN.md, with a pinned sha256 where one exists): EDL client (edl.py plus its venv), Firehose loader prog_emmc_firehose_8916.mbn, Path A firmware blobs (sbl1, rpm, tz, hyp, lk1st aboot), lk2nd head, fastboot, e2fsprogs (debugfs, e2fsck, mke2fs).

## 4. Commands
Entry point: `./openstick` launcher at the repo root (python3, stdlib only); code under `tools/installer/`. Global flags: `--home DIR` (private store), `--dry-run`, `--verbose`, `--no-color`, `--board NAME`, `--device ID`. Exit codes: 0 ok, 1 error, 2 refused by a gate, 3 verification failed.

- `openstick doctor`: read-only checks: python, bash, e2fsprogs, fastboot, EDL client (found via OPENSTICK_EDL_DIR or PATH), loader file and its sha256, USB permission (print a udev rule suggestion, never install it), free disk (at least 12 GB for backup plus images), private store location and permissions, not inside a git tree. Prints install hints for apt and dnf families.
- `openstick device list | show ID | label ID TEXT | note ID TEXT`: registry views.
- `openstick backup`: board in EDL. Reads identity (EDL serial, HWID), reads the full eMMC through the EDL client (wrapping the proven full read, `rf`), checks the size against the GPT (alternate header LBA + 1, times 512), parses and CRC-checks both GPTs, records sha256, captures identity fingerprints (sha256 of the partitions fsg, sec and fsc, located with the dump's own GPT), writes manifest.json and SHA256SUMS, registers the device and the backup. `--paranoid` reads twice and compares. Prints the sensitivity warning (IMEI, calibration, persist) and tells the user to keep an offline copy. Refuses when a verified backup for this device already exists unless `--again`.
- `openstick configure [--seed NAME]`: interactive questionnaire that writes a seed file (section 6) into the private store, mode 600. Hidden prompts for secrets. Can import an existing seed. Never prints secrets back. `configure --show` prints the seed with secrets masked.
- `openstick build [--profile dev|prod] [--board B]`: builds a GENERIC image set by wrapping the build scripts with a clean environment (no user.env, no personal variables). Records build.json (git describe or tree hash, profile, board, sha256 of boot.img and rootfs.img, tool versions) in the registry; images go under builds/<id>/. Steps that need root run through the user's own interactive sudo, with the exact command printed first. The installer never stores a sudo password.
- `openstick seed inject --build ID --seed NAME`: copies the generic rootfs.img to builds/<id>/personal-<seedname>/ and writes the seed into the COPY with debugfs (no root, no mount), then runs e2fsck -fn, reads the file back and compares sha256. Refuses to modify the generic original.
- `openstick seed push --seed NAME [--host H]`: pushes the seed to a RUNNING stick over ssh and starts the apply service. Only works where ssh access exists (see 6.4).
- `openstick flash --device ID --image-set PATH`: the gated flash. Runs the pathA-flash.sh phases and `flash-board.sh first-install` in order. Per write phase: show the plan, run the script's dry-run, ask the user to type the confirmation, then run with COMMIT=1 and CONFIRM=<token> as the script requires. Stops at the first failure. Writes one event per phase.
- `openstick verify [--host H]`: ssh smoke test: firewall loaded (`nft -c` on the ruleset), watchdog, modem, NTP, WiFi state, kernel and rootfs versions. Read-only on the stick. PASS or FAIL per check, nothing secret in the output.
- `openstick restore --device ID [--backup ID]`: back to original Android from the registry backup. Uses tools/rollback-plan.py to print the gated plan, then executes it phase by phase with the same typed confirmations, then does the full re-read and compares the hash with the backup manifest BEFORE telling the user to boot. Never skips the final read.
- `openstick package restore|install --device ID [--out DIR]` and `openstick package verify FILE`: section 7.
- `openstick wizard`: interactive chain: doctor, backup (or pick an existing one), configure, build or choose an image set, seed inject, flash, verify. Resumable.

## 5. Private store, registry, schemas
Location: `$OPENSTICK_HOME`, else `${XDG_DATA_HOME:-$HOME/.local/share}/openstick`. Created mode 700. Refuse to start if it resolves inside a git work tree. Never create files readable by group or others.

```
<home>/
  devices/<device_id>/device.json
  devices/<device_id>/events.jsonl      append-only, one JSON object per line
  backups/<device_id>/<UTC stamp>/emmc-full.bin, manifest.json, SHA256SUMS
  seeds/<name>.seed                     mode 600
  builds/<build_id>/build.json, boot.img, rootfs.img, personal-<seed>/rootfs.img
  packages/
  cache/                                external inputs with pinned hashes
```

`device_id`: lowercase hex of the EDL chip serial the board reports in EDL (for example "0ec2064f"). Also store the serials the board shows in other modes (Android USB serial, Debian USB serial) as aliases, the user's label and the PCB text the user types.

`device.json` (schema_version 1): device_id; label; board; pcb_label; notes; edl {serial, hwid, msm_id, pk_hash}; usb_aliases[]; identity {fsg_sha256, sec_sha256, fsc_sha256}; backups[] {id, path, size, sha256, verified_by, created}; builds[] {build_id, profile, board, commit, boot_sha256, rootfs_sha256, created}; flashes[] {ts, image_set, phases[{name, result}], outcome}; seeds_applied[] {seed_name, seed_sha256, method, ts}; packages[]; created; updated.

Rules: no secret value anywhere in the registry (only hashes of seed files and key fingerprints). Paths may be relocated by the user, so every use re-verifies the sha256 of what it finds and says so when it differs. events.jsonl never contains secrets.

Identity gate: the fingerprints above are compared with the board in EDL (read the fsg, sec and fsc ranges, hash, compare) before any write. This is the check already proven on real hardware (the partitions were identical to the dump slices). It does not rely on serial numbers shown by adb.

## 6. Seed: personalisation without personal images
6.1 What it is. A small text file of KEY=VALUE lines (no shell syntax, no quoting tricks, strict parser) holding everything personal. The same seed can be applied to many sticks. Placeholder resolved on the stick at apply time: `{serial8}` (last 8 hex of the board serial as the stick sees it).

6.2 Proposed v1 keys. Confirm against what the current build bakes from config/defaults.env and config/user.env: the seed must be able to express exactly the same personalisation. SEED_VERSION=1; HOSTNAME; TIMEZONE; SSH_AUTHORIZED_KEYS (public keys, one per line, from a file or paste); ROOT_PASSWORD_HASH (crypt hash produced by the configurator, plaintext never stored); DEFAULT_USER and USER_PASSWORD_HASH (dev profile); WIFI_SSID, WIFI_PSK, WIFI_COUNTRY; APN (and APN_USER, APN_PASSWORD if needed); USB_NET_ADDR; NTP_SERVERS. Reserved, not implemented, ignored with a notice: LED_*.

6.3 On the image. `/usr/lib/openstick/apply-seed` (POSIX sh, idempotent) plus a systemd oneshot `openstick-seed.service`, ordered before networking, which runs when `/etc/openstick/seed.env` exists. It validates the file, applies each key with the same mechanisms the build used today (NetworkManager profile for WiFi, APN through the modem stack the image already uses, hostnamectl, timedatectl, authorized_keys, chpasswd -e), logs to the journal WITHOUT values, and on success deletes the seed and writes `/etc/openstick/seed.applied` containing only the sha256 of the seed. An invalid seed must never break the boot: log, leave the generic state, continue. Decision to confirm in DESIGN.md: whether the seed is removed after a successful apply (recommended: yes).

6.4 Access reality. The generic `prod` image has no ssh key and a locked root, so an unseeded prod stick has no remote access by design. So: `seed inject` (before flashing) works for dev and prod. `seed push` (after flashing) only works where ssh access exists (generic dev image, or a stick already provisioned). The tool must say this clearly and refuse `seed push` when it cannot work. Document recovery for a prod stick that was flashed without a seed: reflash with a seeded image.

6.5 Backward compatibility. The owner's current flow (config/user.env baked at build time) must keep working and stay reproducible. Keep it as an explicit `--bake-user-env` mode of `build`, off by default.

6.6 Regression guard. Before changing any build script, record the sha256 of the boot.img and rootfs.img that the CURRENT scripts produce, where that is possible without root (some steps need root: say which ones were not reproducible here). The new generic build must differ only where the seed hooks (apply-seed, the service) are added. DESIGN.md lists those differences.

## 7. Packages (both directions)
Format: one `.ostpkg.tar` (optionally gzip) per package with `manifest.json`, `SHA256SUMS`, `README.txt`, `payload/`, `scripts/`. `package verify` checks every hash and the manifest schema without touching a board. A package must be usable WITHOUT this repo: it carries its own copies of the scripts it calls and needs only bash, sha256sum, python3, the EDL client and fastboot as applicable.

- Restore package: the backup dump (or only the partition slices the restore plan needs; decide in DESIGN.md), the original GPTs, the generated restore plan for that device, `restore.sh` with the same gates (identity check against the manifest fingerprints, dry-run, typed confirmation, final full read and hash compare), README with recovery notes (EDL entry with the reset button held while plugging USB, cold boot by unplugging, never `edl reset`). Marked SENSITIVE.
- Install package: image set (generic by default, personal if the user picks a seed), the Path A firmware chain only if the user opts in and acknowledges that redistribution is their responsibility (provenance and sha256 in the manifest), flash scripts, `install.sh` with the same gates, README. Marked SENSITIVE if it holds a personal image.

Packages go to `<home>/packages/` by default. Refuse to write them inside a git work tree.

## 8. Gates (enforced in code, each covered by a refusal test)
- G1 `flash` refuses without a verified backup whose identity fingerprints match the board currently in EDL (exit 2).
- G2 Every write phase: dry-run first, then a typed confirmation: the user types `flash <last 8 of device_id> <phase>` (restore: `restore <last 8> <phase>`). No flag bypasses it. Non-interactive runs may only do dry-runs.
- G3 Secrets only through hidden prompts or files; never in argv, environment, logs, registry or exception text. Test: run with a recognisable fake secret and grep every produced file and all captured output for it.
- G4 prod seeds without an ssh key: the configurator warns that the stick would be unreachable and asks for explicit confirmation.
- G5 Board class gate: only `ufi103s` without `--i-accept-untested`.
- G6 External blobs: supplied or fetched only with a pinned sha256; mismatch is a hard stop; nothing downloaded is ever added to a git tree.
- G7 Stop at the first failure; never auto-retry a write; print the safe next step and where the log is.
- G8 Private store and package output never inside a git work tree; seeds mode 600; directories mode 700.
- G9 sudo is never cached or stored; the exact command is shown before it runs.

## 9. UX
Plain English, short sentences. Every prompt says what will happen, what it changes and what is reversible. Safe choice is the default. Colour is optional (`--no-color`, automatically off when not a TTY). Every write stage ends with a summary: done, verified by, NOT verified. `openstick --help` and each subcommand's help are complete. Write `docs/INSTALLER.md`: quickstart, stage reference, sensitivity notes, recovery steps, what the registry holds, how to move the private store to another machine.

## 10. Out of scope
Windows and macOS. The modem and LED dashboard. Other board classes. Debian 13. Kernel changes. apt-updatable kernel. Any change to initramfs, kernel or firmware-flash logic. Publishing, committing to the real repo, pushing.
