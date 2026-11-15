# openstick-slim builder

Builds a small Debian (bookworm, arm64) image for MSM8916 4G/LTE sticks (512 MB RAM, ~3.9 GB eMMC).
The base contains: boot, systemd, SSH, NetworkManager, WiFi, ModemManager/rmtfs/qrtr and USB gadget tools.
Everything else (web UI, SMS forwarding, dashboards) is meant to be installed afterwards.

Derived from [kinsamanka/OpenStick-Builder](https://github.com/kinsamanka/OpenStick-Builder) (MIT). See `NOTICE.md`.

> **WARNING: flashing can brick your device.** The installer includes a guarded write path; a build alone never writes to a device.
> Before any hardware operation: (1) make and independently verify a full backup of YOUR stick, (2) confirm the live identity matches it,
> (3) make sure you have a working EDL (9008) recovery path. `modemst1`, `modemst2`, `fsg`, `fsc`, `persist`
> and `sec` are unique per device: never share them, never commit them, never flash another unit's copy.

## Status

Work in progress. First target: UFI103S (`BOARD=ufi103s`), kernel 6.6 (postmarketOS package, as in the original Builder).
Per-board config (`boards/<BOARD>/board.env`) and a GPT generated from the eMMC size are implemented.
Not yet implemented: `BOOT_MODE=stock-repack`. Kernel 6.12 is a future option (the board runs 6.6, pmOS 6.6-r5). Nothing here has been flashed or booted yet.

The separate installer wrapper is documented in [docs/INSTALLER.md](docs/INSTALLER.md). Offline installer tests use synthetic fixtures and shims only. The M4 build output (`boot.img`, `rootfs.img`) is not the legacy first-install input (`boot.bin`, `rootfs.bin`). A GPT-T adapter validates and maps the build artifacts to `boot` and `userdata`; the current flash wrapper still refuses that image set because a safe hardware write/readback path is not validated. Do not bypass the refusal by renaming files.

### Installer quickstart (offline inspection)

```sh
./openstick doctor
./openstick device list
./openstick wizard --id first-run --stage doctor
```

Each wizard stage is invoked separately with `--stage`; see the installer guide for backup, seed, build, flash and restore requirements. Do not run backup or write stages against a physical device without owner-supervised hardware acceptance. Passing `doctor` does not establish board, firmware, image or boot compatibility.

## Host requirements (Debian/Ubuntu names)

`git debootstrap debian-archive-keyring qemu-user-binfmt device-tree-compiler gdisk fdisk android-sdk-libsparse-utils
autoconf automake libtool cmake make pkg-config gcc-aarch64-linux-gnu g++-aarch64-linux-gnu gcc-arm-none-eabi
python3-cryptography python3-pyasn1-modules python3-pycryptodome unzip wget`

arm64 emulation uses binfmt_misc with the `F` flag; no `qemu-aarch64-static` copy is needed. Check:
`cat /proc/sys/fs/binfmt_misc/qemu-aarch64`. About 10 GB of free disk is recommended.

## Build

```sh
scripts/fetch-src.sh                 # external sources at pinned commits
scripts/fetch-dts.sh                 # kernel 6.6 DT headers (sparse checkout, ~25 MB)
scripts/build-dtb.sh                 # dts/<DTB_NAME>.dts -> out/dtbs/
scripts/gen-gpt.sh                   # GPT-B from BOARD_GPT_TEMPLATE (config/local.env) -> out/fw/gpt-{primary,secondary}.bin, gpt_both0.bin
sudo scripts/10-base.sh           # stage 1: minimal base -> out/base-bookworm-arm64.tar.gz
sudo scripts/20-rootfs.sh         # stage 2: packages + config -> out/rootfs-<board>.tgz
sudo scripts/30-gt.sh             # USB gadget tools (cross-built, not in the rootfs)
sudo tests/verify-sshd.sh         # prints effective sshd settings
sudo tests/verify-rootfs.sh       # accounts, sudo, firewall, package sizes
sudo tests/verify-firewall.sh     # ruleset text (rootless part) + kernel load in a throw-away netns (root part)
tests/verify-sshd-config.sh       # sshd drop-ins prod/dev with the host sshd -T (rootless)
scripts/extract_fw.sh                # sbl1/rpm/tz (sha256 verified) and a GPT image -> out/fw/
sudo scripts/40-images.sh         # out/files-<board>/{boot,rootfs}.bin (sparse) + SHA256SUMS (ROOTLESS=1: no root, out/files-<board>-rootless/)
tests/verify-images.sh            # rootless: GPT/layout checks and checks on the current out/prod-* artifacts (no mount; the old mount-based part B no longer exists)
tools/rollback-plan.sh ...           # prints (never runs) restore commands, see ROLLBACK.md
scripts/build_hyp_aboot.sh           # lk2nd + qhypstub -> out/fw/{aboot,hyp}.mbn (needs gcc-arm-none-eabi)
```

`sudo -E` is ignored on some hosts: pass variables via `config/local.env`, `boards/*/board.env` or `sudo env VAR=val scripts/...`. Scripts only touch `work/` and `out/`.
## Legacy GPT-B image notes (not the current GPT-T boot path)

The following sparse `boot.bin`/`rootfs.bin` instructions document the retained legacy GPT-B flow. They are not the supported production boot path described below and must not be used to convert or substitute for M4's `boot.img`/`rootfs.img`. The current device path and rollback evidence are documented in [ROLLBACK.md](ROLLBACK.md) and [docs/INSTALLER.md](docs/INSTALLER.md).

## Images: legacy GPT-B formats and sizes

`out/files-<board>/boot.bin` and `rootfs.bin` are **Android sparse images** (`img2simg` of a raw ext2 / ext4 file),
so they are small on disk (rootfs ~216 MB) but expand to the full partition size when flashed or converted.
That is why `rootfs.bin` is ~216 MB while the GPT `rootfs` partition is ~2.62 GiB (GPT-B): sparse files store only the used chunks
and a "don't care" marker for the rest. `file rootfs.bin` shows the expanded size in 4096-byte blocks.

Sizes: `scripts/gen-gpt.sh` rounds the `rootfs` partition DOWN to a multiple of 8 sectors (4096 B) and writes
`ROOTFS_BYTES` / `BOOT_BYTES` to `out/fw/layout-<board>.env`; `40-images.sh` builds the raw images from those values,
rounded down to 4096 B, so an image can never exceed its partition (`tests/verify-images.sh` checks this
against the GPT, and checks `SHA256SUMS`). `30-gt.sh` writes `work/gt-dist`; `40-images.sh` refuses to run without it
(override only with `ALLOW_NO_GADGET=1`: the image then has no `usb0`).

| File | Partition | Notes |
|---|---|---|
| `out/fw/gpt_both0.bin` | GPT (`partition`) | = `gpt-primary.bin` + `gpt-secondary.bin`, GPT-B from the board template; only after the backup is verified |
| `out/files-<board>/boot.bin` | `boot` | ext2, holds `extlinux/`, kernel, DTBs (read by lk2nd) |
| `out/files-<board>/rootfs.bin` | `rootfs` | ext4 |

Fastboot (lk2nd running; the sparse files are used as they are). **Do not run without the per-board confirmation
required by CLAUDE.md** (verified backup, EDL working):
```sh
fastboot flash partition out/fw/gpt_both0.bin
fastboot flash boot      out/files-<board>/boot.bin
fastboot flash rootfs    out/files-<board>/rootfs.bin
```
(aboot/hyp/rpm/sbl1/tz are flashed only if the boot path needs them, see `out/fw/`; not part of this step.)

EDL needs RAW images: convert first (`simg2img` is in `android-sdk-libsparse-utils` / `android-tools`):
```sh
simg2img out/files-<board>/boot.bin   work/boot.raw
simg2img out/files-<board>/rootfs.bin work/rootfs.raw
```
then write `boot.raw` to the `boot` partition and `rootfs.raw` to `rootfs` with your EDL tool
(e.g. bkerler/edl: `edl w boot work/boot.raw`; check `edl --help` for the exact syntax of your version, this command
is not verified here). Never write the raw files to a whole-disk device node.

## Current boot path (board #6, verified on device): boot.img + rootfs in `userdata`

State verified by the owner on board #6: lk2nd + Debian rootfs in `userdata` of the CURRENT GPT (not GPT-B), kernel 6.6
reaches userspace, systemd running, ssh, WiFi, modem and LTE work.
```sh
sudo scripts/20-rootfs.sh        # rootfs tgz (fstab LABEL=osroot, growfs, gadget, usb0, watchdog, marker /etc/openstick-rootfs)
scripts/51-rootfs-img.sh         # out/prod-boot/rootfs.img: small ext4, label osroot; openstick-growfs extends it on first boot
scripts/50-boot-img.sh           # out/prod-boot/boot.img: kernel + DTB + production initramfs (Android boot image v0)
tests/verify-boot-img.sh         # rootless checks
```
Manual flash only, after the per-board confirmation (verified backup, EDL working):
`fastboot flash boot out/prod-boot/boot.img` (lk2nd places it at offset 512 KiB) and the rootfs image into `userdata`.
The production initramfs finds `userdata` by `PARTNAME` in sysfs, checks the marker, feeds the PM8916 PON watchdog,
then `switch_root`; systemd takes over `/dev/watchdog0` (`RuntimeWatchdogSec=30s`, set in both profiles).
USB gadget is created by the rootfs (`/etc/openstick/gadget.conf`: `ecm` default, `ecm+acm`, `rndis`); `usb0` is
`172.16.42.1/24` via systemd-networkd with a built-in DHCP server. `PROFILE=prod` has no sudo.

### Two fastboots on board #6 (validated): check `getvar product` before every flash

| How you got there | `getvar product` | `partition-size:boot` | What `boot` is | Correct write |
|---|---|---|---|---|
| lk2nd running as secondary | `lk2nd-msm8916` | `0xf80000` | logical, after the 512 KiB lk2nd (partition "lk2nd" is `0x80000`) | `fastboot flash boot boot.img` |
| `reboot bootloader` from Linux | `lk1st-msm8916` | `0x1000000` | the physical partition; its first 512 KiB ARE lk2nd | lk2nd-padded-512KiB.img + boot.img at `0x80000`, written together |

`tools/flash/flash-board.sh boot [dry|write]` reads `product` and branches: `lk2nd-msm8916` flashes `boot.img`; `lk1st-msm8916`
NEVER flashes a plain `boot.img` (it would erase lk2nd). It composes the lk2nd image (the pinned `out/step1/lk2nd-padded-512KiB.img`
built by the builder, checked by sha256, never taken from the user's backup) with `boot.img` at `0x80000`, checks it fits in
`0x1000000`, writes it, reads it back with `fastboot fetch` and compares. Any other product is refused. A write also needs
`COMMIT=1 CONFIRM=flash-<BOARD>-boot` and a verified `BACKUP_DIR`. `tests/verify-flash-board.sh` covers the branching against a stub
fastboot (no device). `fastboot fetch` on lk1st (returns the physical partition, partition-size:boot = 0x1000000) validated on hardware (board #6); the `lk2nd:compatible` variable is still ASSUMED.

**Failure path:** `reboot bootloader` issued from the initramfs (e.g. when `switch_root` fails) lands in **lk1st**, not in the lk2nd fastboot.
Do not `fastboot flash boot boot.img` from there; use the `boot` mode above.

**WARNING: `boot.bin` from `40-images.sh` must NEVER be written to the `boot` partition (p21): it erases lk2nd.** The
legacy GPT-B flow below is kept for reference only. extlinux boot is future work.

Stage 1 is cached as a tarball, so changing packages or config only repeats stage 2.

## Configuration

Defaults live in `config/defaults.env`; board values in `boards/<BOARD>/board.env`.
Precedence: environment > `config/local.env` (git-ignored) > board.env > defaults.env.

| Variable | Default | Meaning |
|---|---|---|
| `RELEASE` | `bookworm` | Debian release |
| `BOARD` | `ufi103s` | board name |
| `BOOT_MODE` | `mainline` | `mainline` (lk2nd + extlinux). `stock-repack` is planned |
| `DTB_NAME` | board.env | DTB used by the extlinux `fdt` line |
| `EMMC_SECTORS` | board.env | eMMC size in 512-byte sectors; the rootfs partition fills the rest |
| `HOST_NAME` | `openstick-debian` | hostname |
| `TIMEZONE` / `LOCALE` | `Etc/UTC` / `C.UTF-8` | other locales add the `locales` package |
| `DEFAULT_USER` / `DEFAULT_PASS` | `openstick` / `openstick` | default account (public default, see below) |
| `FORCE_PASSWD_CHANGE` | `1` | `1` = password expired, first login must change it. `0` disables |
| `SSH_PUBKEY_FILE` | empty | public keys installed in the default user's `authorized_keys` |
| `HOTSPOT_SSID` / `HOTSPOT_PSK` | `OpenStick` / empty | empty PSK = hotspot profile is not installed |
| `LTE_APN` | `internet` | APN of the LTE profile |
| `ROOTFS_PACKAGES`, `EXTRA_PACKAGES` | see file | installed with `--no-install-recommends` |

## Accounts and SSH

* The default account is created at build time with `DEFAULT_USER`/`DEFAULT_PASS`. These defaults are public:
  they are only acceptable because the password is expired, so the first login asks for a new one.
  Change it later with `passwd`.
* `root` has an empty password so the serial console (and ADB) work. `sshd` refuses it
  (`PermitEmptyPasswords no`, `PermitRootLogin prohibit-password`); `tests/verify-sshd.sh` shows the effective values.
  Anyone with physical access to the serial pins has root.
* `PROFILE=dev` only: `sudo` is installed and the default user is in the `sudo` group (`PROFILE=prod` has no sudo). The password is always required (no `NOPASSWD`).
* **Firewall (dev and prod).** `openstick-fw.service` loads a private nftables table (`inet openstick_fw`) at boot:
  input policy **drop**; accepted: loopback, established/related, ICMP/ICMPv6, DHCP client (v4/v6), SSH (tcp/22) only on
  `usb0` and `wlan0` (never `wwan*`), and DHCP/DNS served on `usb0` (NetworkManager shared mode) and on `wlan0` when the
  hotspot profile is installed. It never uses `flush ruleset`, so NetworkManager's shared-mode tables are untouched. Debian's
  `nftables.service` (whose config does `flush ruleset`) is not enabled and must stay that way.
  **SSH over cellular stays off** until the user switches it on, on the device: `sudo openstick-ssh-cellular on|off|status`
  (`on` prints a warning and asks for a typed confirmation; `print` shows the ruleset). Exposing SSH to the internet with a weak password is dangerous.
* **sshd** (`/etc/ssh/sshd_config.d/10-openstick-hardening.conf`, from `configs/sshd/`): prod = `PasswordAuthentication no`,
  `KbdInteractiveAuthentication no`, `PermitRootLogin prohibit-password`. dev = same, plus password login only for the test
  user (`Match User <DEFAULT_USER>`), announced in the motd. `tests/verify-sshd-config.sh` checks both with the host `sshd -T`.
* **Headless defaults.** `DISABLE_UNITS` (config/defaults.env) masks `apt-daily.timer`, `apt-daily-upgrade.timer`,
  `dpkg-db-backup.timer`, `e2scrub_all.timer`, `e2scrub_reap.service`, `NetworkManager-wait-online.service`; override it in
  `config/user.env`, or on the device `systemctl unmask <unit>`. `systemd-timesyncd` stays enabled.
* Add a key: build with `SSH_PUBKEY_FILE=/home/<you>/.ssh/id_ed25519.pub` (absolute path: `~` would be root's under sudo), or on the device
  `ssh-copy-id user@<ip>`. For certificates add `TrustedUserCAKeys /etc/ssh/ca.pub` to a file in `/etc/ssh/sshd_config.d/`.
* SSH host keys are generated on first boot, never stored in the image.

## Security

Profiles (`PROFILE` in `config/defaults.env` or `config/user.env`; default `dev`):

* `PROFILE=dev` (default): test user, empty root password, autologin on `ttyMSM0` and `ttyGS0`. The build prints a
  WARNING, `/etc/motd` says "DEV IMAGE - not for production. Run: openstick-harden", and `/etc/openstick/profile`
  holds `PROFILE=dev`.
* `PROFILE=prod`: no default account. The build REFUSES (before sudo is needed) unless `SSH_PUBKEY_FILE` points to a
  readable file with a valid public key, or `ROOT_PASSWORD_HASH` (`$y$`, `$6$` or `$5$`) is set. Root is locked unless a
  hash is given, no autologin, no `ttyGS0` getty, key installed for root only, sshd key-only when a key is the only credential.
* `openstick-harden` (dev images only; `--dry-run` previews): aborts if no `authorized_keys` holds a key (no lockout).
  Otherwise removes autologin, locks root and the test user, sshd key-only, masks `ttyGS0` getty, sets the hardened
  motd and `PROFILE=prod-hardened`. After hardening the serial console has no login unless a root hash exists.
* `scripts/49-inner-fakeroot.sh` (test image) is TEST ONLY and re-enables `ttyGS0` autologin even from a prod tarball.
* Build safety: `safe_dir`/`safe_rm_dir`, `rm -rf --one-file-system`, `policy-rc.d` (exit 101) inside the chroot during
  the build (removed at the end), tmpfs on the chroot `/run` (host `/run` is never bound), apt cache kept on the host in
  `out/cache/apt` (unmounted before cleanup; the image ships without apt lists or cache).
* `ROOTLESS=1` only affects `40-images.sh` (fakeroot + `mke2fs -d`, output in a `-rootless` directory, never canonical);
  `20-rootfs.sh` needs root (chroot, mounts, apt).
* Third-party files, licenses and fetched blobs (firehose loader, fetched by `tools/fetch-firehose.sh`, never in git):
  see `docs/INCLUDED.md`.

## Network

`usb0` is handled by systemd-networkd (172.16.42.1/24, built-in DHCP server; NetworkManager ignores it). NetworkManager manages the `wlan0` hotspot (shared, only if `HOTSPOT_PSK` is set) and
the LTE modem profile. `polkit` is not installed, so `nmcli` commands that change settings need `sudo`
(NetworkManager only allows root without polkit; read-only commands work for everyone).
`dns=dnsmasq` is off; see `conf.d/20-dns-dnsmasq.conf.example` on the device to enable it. Shared mode needs `dnsmasq-base` (DHCP/DNS) and `nftables` (NAT); both are in the base.

## Boards

Each board is a directory `boards/<id>/board.env` (DTB, lk2nd compatible, eMMC size). To add one: copy `boards/ufi103s`,
put its DTS in `dts/`, set `BOARD=<id>`. `scripts/gen-gpt.sh` is template based: set `BOARD_GPT_TEMPLATE` (a GPT dump of YOUR unit, outside the repo) in
`config/local.env`; kept entries are copied byte-identical, only `boot` and `rootfs` are new (GPT-B, see FLASH-PLAN.md). Use the `EMMC_SECTORS` of YOUR unit, not a guess.
DTS files come from [x7780/OpenStick-Builder](https://github.com/x7780/OpenStick-Builder) and are compiled against the
`msm8916/6.6` kernel headers; `dts/msm8916-ufi.dtsi` has a small documented change for that tree.

## Licenses

MIT for this repository (see `LICENSE`, `NOTICE.md`). DTS files keep their GPL-2.0-only headers.

## Contributing

Run `ln -sf ../../tools/pre-commit .git/hooks/pre-commit`. It refuses device dumps, calibration partitions,
large binaries, IMEI-like numbers and credentials.
