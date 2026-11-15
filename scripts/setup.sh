#!/bin/sh
# SPDX-License-Identifier: MIT
# Runs INSIDE the chroot (stage 2). Reads /build.env written by 20-rootfs.sh.
set -eu
. /build.env
export DEBIAN_FRONTEND=noninteractive
PROFILE=${PROFILE:-dev}
ROOT_PASSWORD_HASH=${ROOT_PASSWORD_HASH:-}

# No service may be started inside the build chroot by maintainer scripts; removed again at the end of this script.
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 755 /usr/sbin/policy-rc.d

# Do not install docs/man/locales/info; keep only copyright files.
cat > /etc/dpkg/dpkg.cfg.d/01-slim <<'EOT'
path-exclude=/usr/share/man/*
path-exclude=/usr/share/info/*
path-exclude=/usr/share/locale/*
path-exclude=/usr/share/doc/*
path-include=/usr/share/doc/*/copyright
EOT

# timezone and locale
ln -sf "/usr/share/zoneinfo/$TIMEZONE" /etc/localtime
echo "$TIMEZONE" > /etc/timezone
PKGS="$ROOTFS_PACKAGES $EXTRA_PACKAGES"
case "$LOCALE" in C.UTF-8|POSIX|C) ;; *) PKGS="$PKGS locales";; esac
# sudo is dev-only: prod has no sudo binary, no sudo group membership (also dropped when set through EXTRA_PACKAGES)
NEWPKGS=
for p in $PKGS; do [ "$p" = sudo ] || NEWPKGS="$NEWPKGS $p"; done
PKGS=$NEWPKGS
[ "$PROFILE" != dev ] || PKGS="$PKGS sudo"

apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq --no-install-recommends $PKGS

case "$LOCALE" in C.UTF-8|POSIX|C) ;; *)
    echo "$LOCALE UTF-8" > /etc/locale.gen
    locale-gen
    echo "LANG=$LOCALE" > /etc/default/locale;;
esac

# --- accounts, by profile ---------------------------------------------------
# dev:  root has an empty password (serial console / ADB; sshd refuses it) and a test user with a documented password.
# prod: no default credentials. root is locked unless ROOT_PASSWORD_HASH is given; no test user.
install -d -m 755 /etc/openstick
if [ "$PROFILE" = prod ]; then
    if [ -n "$ROOT_PASSWORD_HASH" ]; then usermod -p "$ROOT_PASSWORD_HASH" root; else passwd -l root; fi
    # optional named user, only when explicitly set to something other than the dev test name; key-only, no sudo
    if [ -n "$DEFAULT_USER" ] && [ "$DEFAULT_USER" != openstick ]; then
        useradd -m -s /bin/bash "$DEFAULT_USER"
        passwd -l "$DEFAULT_USER"
        PROD_USER=$DEFAULT_USER
    else
        PROD_USER=
    fi
    printf 'PROFILE=prod\n' > /etc/openstick/profile
else
    if [ -n "$ROOT_PASSWORD_HASH" ]; then usermod -p "$ROOT_PASSWORD_HASH" root; else passwd -d root; fi
    # sudo group (password required; no NOPASSWD anywhere)
    if getent group sudo >/dev/null; then GRP="-G sudo"; else GRP=""; fi
    useradd -m -s /bin/bash $GRP "$DEFAULT_USER"
    echo "$DEFAULT_USER:$DEFAULT_PASS" | chpasswd
    if [ "$FORCE_PASSWD_CHANGE" = 1 ]; then chage -d 0 "$DEFAULT_USER"; fi
    PROD_USER=$DEFAULT_USER
    printf 'PROFILE=dev\nTEST_USER=%s\n' "$DEFAULT_USER" > /etc/openstick/profile
fi
if [ -f /tmp/ssh_pubkeys ]; then
    if [ -n "$PROD_USER" ]; then
        install -d -m 700 -o "$PROD_USER" -g "$PROD_USER" "/home/$PROD_USER/.ssh"
        install -m 600 -o "$PROD_USER" -g "$PROD_USER" /tmp/ssh_pubkeys "/home/$PROD_USER/.ssh/authorized_keys"
    fi
    # root: key-only access (sshd: PermitRootLogin prohibit-password); comments stripped, public keys only
    install -d -m 700 -o root -g root /root/.ssh
    awk 'NF>=2 && $1 ~ /^(ssh-|ecdsa-|sk-)/ {print $1" "$2}' /tmp/ssh_pubkeys > /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    rm -f /tmp/ssh_pubkeys
fi

# sshd hardening drop-in is installed by 20-rootfs.sh (configs/sshd/, per profile)

# host keys are generated on first boot, never baked into the image
rm -f /etc/ssh/ssh_host_*

# cleanup
# /var/cache/apt/archives is a bind mount of the HOST apt cache here: do NOT clean it in the chroot.
# 20-rootfs.sh empties var/cache/apt after unmounting it.
rm -rf --one-file-system /var/lib/apt/lists/* /var/log/apt /var/log/dpkg.log /var/log/alternatives.log
rm -rf --one-file-system /usr/share/man/* /usr/share/info/* /usr/share/locale/* /usr/share/lintian
find /usr/share/doc -type f ! -name copyright -delete
find /usr/share/doc -type l -delete
find /usr/share/doc -type d -empty -delete
rm -f /usr/sbin/policy-rc.d
