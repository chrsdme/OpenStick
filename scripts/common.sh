#!/bin/sh
# SPDX-License-Identifier: MIT
# Shared helpers. Source this file; do not execute it.
set -eu

ROOT=${OPENSTICK_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}   # tools/ scripts set OPENSTICK_ROOT
# Precedence: environment > config/user.env (or OPENSTICK_USER_CONFIG) > config/local.env > boards/<BOARD>/board.env > config/defaults.env
_env=$(export -p)
set -a
. "$ROOT/config/defaults.env"
[ -f "$ROOT/config/local.env" ] && . "$ROOT/config/local.env"
# user config (ssh key file, WiFi, APN): secrets stay out of git. Default config/user.env (git-ignored) or OPENSTICK_USER_CONFIG.
_uc=${OPENSTICK_USER_CONFIG:-$ROOT/config/user.env}
if [ -f "$_uc" ]; then
    case "$(stat -c %a "$_uc")" in
        ?00) ;;
        *) echo "ERROR: $_uc must not be group/world accessible (chmod 600)" >&2; exit 1;;
    esac
    . "$_uc"
fi
eval "$_env"   # re-apply the caller's environment over defaults/local.env
[ -f "$ROOT/boards/$BOARD/board.env" ] || { echo "ERROR: unknown BOARD '$BOARD' (no boards/$BOARD/board.env)" >&2; exit 1; }
. "$ROOT/boards/$BOARD/board.env"
set +a

# PROFILE: dev (test account, empty root password, serial autologin) or prod (no default credentials)
PROFILE=${PROFILE:-dev}
case "$PROFILE" in dev|prod) ;; *) echo "ERROR: PROFILE must be 'dev' or 'prod' (got '$PROFILE')" >&2; exit 1;; esac

OUT=${OUT:-$ROOT/out}
WORK=${WORK:-$ROOT/work}
CHROOT=${CHROOT:-$WORK/rootfs}
BASE_TAR=$OUT/base-$RELEASE-arm64.tar.gz
# final images; ROOTLESS=1 builds go to a separate '-rootless' directory so they can never be mistaken for canonical ones
FILES_DIR=${FILES_DIR:-$OUT/files-$BOARD}
[ "${ROOTLESS:-0}" = 1 ] && FILES_DIR=$FILES_DIR-rootless

die() { echo "ERROR: $*" >&2; exit 1; }

need_root() { [ "$(id -u)" -eq 0 ] || die "run as root (sudo $0)"; }

# Refuse to touch any directory that is not strictly inside $WORK.
safe_dir() {
    case "$1" in
        "$WORK"/?*) ;;
        *) die "unsafe path '$1' (must be under $WORK)";;
    esac
    case "$1" in *..*|*" "*) die "unsafe path '$1'";; esac
}

safe_rm_dir() {
    safe_dir "$1"
    if [ -d "$1" ]; then
        umount_chroot "$1"
        # refuse if anything is still mounted below it
        if grep -q " $1/" /proc/mounts; then die "mounts still active under $1"; fi
        rm -rf --one-file-system -- "$1"
    fi
}

mount_chroot() {
    safe_dir "$1"
    mount -t proc proc "$1/proc"
    mount -t sysfs sys "$1/sys"
    mount --bind /dev "$1/dev"
    mount --bind /dev/pts "$1/dev/pts"
    # tmpfs, not the host /run: maintainer scripts must not see the host's systemd/dbus sockets
    mount -t tmpfs tmpfs "$1/run"
    mkdir -p "$1/run/lock"
}

umount_chroot() {
    # var/cache/apt/archives (host apt cache bind mount, see 20-rootfs.sh) FIRST: it must never be reached by a cleanup
    for a in var/cache/apt/archives run dev/pts dev sys proc; do
        if grep -q " $1/$a " /proc/mounts; then umount "$1/$a" || die "cannot umount $1/$a"; fi
    done
}

# Run a command in a chroot with a fixed, non-interactive environment and stdin from /dev/null,
# independent of how the user started the script (tty, sudo env handling).
chroot_run() {
    env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root TERM=dumb \
        DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none LANG=C.UTF-8 LC_ALL=C.UTF-8 \
        chroot "$@" </dev/null
}

# install_tree SRC DST: copy a config tree into the image owned root:root with fixed modes (dirs 755, files 644, 755 if
# the source is executable). Never preserves uid/gid/mode of the build host (cp -a did: uid 1000, 664/775 leaked into the image).
install_tree() {
    [ -d "$1" ] || die "install_tree: no such directory $1"
    install -d -m 755 -o root -g root "$2"
    ( cd "$1" && find . -mindepth 1 | sort ) | while IFS= read -r _p; do
        _p=${_p#./}
        if [ -L "$1/$_p" ]; then ln -sfn "$(readlink "$1/$_p")" "$2/$_p"; chown -h root:root "$2/$_p"
        elif [ -d "$1/$_p" ]; then install -d -m 755 -o root -g root "$2/$_p"
        elif [ -x "$1/$_p" ]; then install -m 755 -o root -g root "$1/$_p" "$2/$_p"
        else install -m 644 -o root -g root "$1/$_p" "$2/$_p"; fi
    done
}

require_binfmt() {
    [ -r /proc/sys/fs/binfmt_misc/qemu-aarch64 ] || die "binfmt qemu-aarch64 not registered (install qemu-user-binfmt)"
    grep -q 'flags:.*F' /proc/sys/fs/binfmt_misc/qemu-aarch64 || echo "WARN: binfmt entry has no F flag; chroot may need qemu inside it" >&2
}

require_space() { # path, MB
    avail=$(df -Pm "$1" | awk 'NR==2{print $4}')
    [ "$avail" -ge "$2" ] || die "need ${2} MB free on $1, have ${avail} MB"
}

# prod refuses to build without a way in: a valid SSH public key file or a root password hash.
# Pure checks (no root needed); 20-rootfs.sh calls it BEFORE need_root. tests/verify-profile.sh exercises it.
validate_profile_credentials() {
    [ "$PROFILE" = prod ] || return 0
    _have=0
    if [ -n "${SSH_PUBKEY_FILE:-}" ]; then
        [ -r "$SSH_PUBKEY_FILE" ] || die "PROFILE=prod: SSH_PUBKEY_FILE '$SSH_PUBKEY_FILE' is not readable"
        grep -Eq '^(ssh-|ecdsa-|sk-)[A-Za-z0-9@.-]* [A-Za-z0-9+/=]{20,}' "$SSH_PUBKEY_FILE" \
            || die "PROFILE=prod: SSH_PUBKEY_FILE has no valid public key line"
        _have=1
    fi
    if [ -n "${ROOT_PASSWORD_HASH:-}" ]; then
        case "$ROOT_PASSWORD_HASH" in
            '$y$'*|'$6$'*|'$5$'*) ;;
            *) die "PROFILE=prod: ROOT_PASSWORD_HASH must be a crypt hash starting with \$y\$, \$6\$ or \$5\$";;
        esac
        case "$ROOT_PASSWORD_HASH" in *[!A-Za-z0-9./\$,=-]*) die "PROFILE=prod: ROOT_PASSWORD_HASH has characters outside the crypt alphabet";; esac
        _have=1
    fi
    [ "$_have" = 1 ] || die "PROFILE=prod needs credentials: set SSH_PUBKEY_FILE (public key) and/or ROOT_PASSWORD_HASH (config/user.env). Refusing to build an image nobody can log in to."
}

profile_banner() {
    [ "$PROFILE" = dev ] || { echo "PROFILE=prod: no default credentials, key/hash only" >&2; return 0; }
    cat >&2 <<'EOT'
##############################################################################
# WARNING: PROFILE=dev. This image has an EMPTY root password, serial autologin
# (ttyMSM0 and ttyGS0) and a documented test password. NOT for production.
# Use PROFILE=prod (needs SSH_PUBKEY_FILE or ROOT_PASSWORD_HASH) or run
# openstick-harden on the device.
##############################################################################
EOT
}
