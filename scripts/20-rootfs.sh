#!/bin/sh
# SPDX-License-Identifier: MIT
# Stage 2: base tarball -> configured rootfs (packages, users, services, kernel).
. "$(dirname "$0")/common.sh"
. "$ROOT/tests/lib-ownership.sh"
: "${CHROOT:?}"; : "${WORK:?}"; : "${OUT:?}"
ROOT_PASSWORD_HASH=${ROOT_PASSWORD_HASH:-}
profile_banner
validate_profile_credentials   # before need_root: refusal is testable without sudo
need_root; require_binfmt
[ -f "$BASE_TAR" ] || die "missing $BASE_TAR (run scripts/10-base.sh)"
mkdir -p "$OUT/cache"; require_space "$WORK" 3000
safe_dir "$CHROOT"

case "$GADGET_MODE" in ecm|ecm+acm|rndis) ;; *) die "GADGET_MODE must be ecm, ecm+acm or rndis";; esac
case "$HOTSPOT_PSK" in "") ;; *) [ ${#HOTSPOT_PSK} -ge 8 ] && [ ${#HOTSPOT_PSK} -le 63 ] || die "HOTSPOT_PSK must be 8..63 chars";; esac
WIFI_SSID=${WIFI_SSID:-}; WIFI_PSK=${WIFI_PSK:-}
case "$WIFI_PSK" in "") ;; *) [ ${#WIFI_PSK} -ge 8 ] && [ ${#WIFI_PSK} -le 63 ] || die "WIFI_PSK must be 8..63 chars";; esac
[ -z "$WIFI_PSK" ] || [ -n "$WIFI_SSID" ] || die "WIFI_PSK set but WIFI_SSID empty"
case "$HOTSPOT_SSID$HOTSPOT_PSK$LTE_APN$WIFI_SSID$WIFI_PSK" in
    *"|"*|*"&"*|*"\\"*) die "HOTSPOT_*, WIFI_SSID, WIFI_PSK and LTE_APN must not contain | & or backslash";;
esac
[ -z "$SSH_PUBKEY_FILE" ] || [ -r "$SSH_PUBKEY_FILE" ] || die "cannot read SSH_PUBKEY_FILE"

# kernel package, verified
APK=$OUT/cache/$(basename "$KERNEL_APK_URL")
[ -f "$APK" ] || wget -O "$APK" "$KERNEL_APK_URL"
echo "$KERNEL_APK_SHA256  $APK" | sha256sum -c - || die "kernel apk sha256 mismatch"

safe_rm_dir "$CHROOT"
mkdir -p "$CHROOT"
tar --numeric-owner -xpzf "$BASE_TAR" -C "$CHROOT"
trap 'umount_chroot "$CHROOT" 2>/dev/null || true' EXIT
mount_chroot "$CHROOT"
# host apt cache bind-mounted over the chroot's archives dir; umounted (first in umount_chroot) BEFORE any cleanup
mkdir -p "$OUT/cache/apt/partial" "$CHROOT/var/cache/apt/archives"
mount --bind "$OUT/cache/apt" "$CHROOT/var/cache/apt/archives"

# build.env carries the default password: root-only, removed right after
umask 077
cat > "$CHROOT/build.env" <<EOT
ROOTFS_PACKAGES='$ROOTFS_PACKAGES'
EXTRA_PACKAGES='$EXTRA_PACKAGES'
TIMEZONE='$TIMEZONE'
LOCALE='$LOCALE'
DEFAULT_USER='$([ "$PROFILE" = prod ] && echo "" || echo "$DEFAULT_USER")'
DEFAULT_PASS='$([ "$PROFILE" = prod ] && echo "" || echo "$DEFAULT_PASS")'
PROFILE='$PROFILE'
ROOT_PASSWORD_HASH='$ROOT_PASSWORD_HASH'
FORCE_PASSWD_CHANGE='$FORCE_PASSWD_CHANGE'
EOT
umask 022
[ -z "$SSH_PUBKEY_FILE" ] || install -m 600 -o root -g root "$SSH_PUBKEY_FILE" "$CHROOT/tmp/ssh_pubkeys"
install -m 755 -o root -g root "$ROOT/scripts/setup.sh" "$CHROOT/setup.sh"
chroot_run "$CHROOT" /bin/sh /setup.sh
rm -f "$CHROOT/build.env" "$CHROOT/setup.sh" "$CHROOT/usr/sbin/policy-rc.d"
umount_chroot "$CHROOT"
if grep -qF -- " $CHROOT/var/cache/apt/archives " /proc/mounts; then die "apt cache still mounted, refusing to clean"; fi
mkdir -p "$CHROOT/root" "$CHROOT/etc"
: > "$CHROOT/root/.bash_history"

# hostname and hosts are written, not edited: minbase has no /etc/hosts
echo "$HOST_NAME" > "$CHROOT/etc/hostname"
cat > "$CHROOT/etc/hosts" <<EOT
127.0.0.1	localhost
::1		localhost ip6-localhost ip6-loopback
127.0.1.1	$HOST_NAME
EOT

# nothing host- or build-specific may stay in the image (public image, many boards)
rm -f "$CHROOT"/etc/ssh/ssh_host_*          # regenerated on first boot by openstick-ssh-hostkeys.service
: > "$CHROOT/etc/machine-id"                # empty: systemd generates it on first boot
[ -L "$CHROOT/var/lib/dbus/machine-id" ] || rm -f "$CHROOT/var/lib/dbus/machine-id"
# image ends with no apt lists/cache (the host cache in $OUT/cache/apt is already unmounted and untouched)
rm -rf --one-file-system -- "${CHROOT:?}"/var/lib/apt/lists/* "${CHROOT:?}"/var/cache/apt/*
find "$CHROOT/root" "$CHROOT/home" -name '.*_history' -type f -exec truncate -s 0 {} + 2>/dev/null || true

# systemd units
# install_tree, not cp -a: cp -a carried the build host uid/gid 1000 and modes 664/775 into the image
install_tree "$ROOT/configs/system" "$CHROOT/etc/systemd/system"
# .wants links are created here, not stored in the repo (absolute targets are only valid inside the image)
enable_unit() { # $1 target, $2 unit, $3 unit path inside the image
    mkdir -p "$CHROOT/etc/systemd/system/$1.wants"
    ln -sfn "$3" "$CHROOT/etc/systemd/system/$1.wants/$2"
}
for u in msm-firmware-loader.service openstick-growfs.service openstick-gadget.service openstick-seed.service; do
    [ -f "$CHROOT/etc/systemd/system/$u" ] || die "missing unit $u (configs/system)"
done
enable_unit sysinit.target msm-firmware-loader.service /etc/systemd/system/msm-firmware-loader.service
enable_unit multi-user.target openstick-growfs.service /etc/systemd/system/openstick-growfs.service
enable_unit multi-user.target openstick-gadget.service /etc/systemd/system/openstick-gadget.service
# Optional first-boot personalization, before either network manager starts.
enable_unit network-pre.target openstick-seed.service /etc/systemd/system/openstick-seed.service
# usb0 by systemd-networkd (units ship in the systemd package on bookworm)
enable_unit multi-user.target systemd-networkd.service /lib/systemd/system/systemd-networkd.service
enable_unit sockets.target systemd-networkd.socket /lib/systemd/system/systemd-networkd.socket
# the stock gt/RNDIS unit (configs/legacy/usb-gadget.service) is not installed any more; gt itself is still built (30-gt.sh)
# the console would block on systemd-firstboot questions (machine-id is empty on purpose)
ln -sfn /dev/null "$CHROOT/etc/systemd/system/systemd-firstboot.service"
install -m 755 -o root -g root "$ROOT/configs/openstick-growfs" "$ROOT/configs/openstick-gadget" "$CHROOT/usr/sbin/"
install -d -m 755 -o root -g root "$CHROOT/usr/lib/openstick"
install -m 755 -o root -g root "$ROOT/configs/apply-seed" "$CHROOT/usr/lib/openstick/apply-seed"
install -d -m 755 -o root -g root "$CHROOT/etc/openstick" "$CHROOT/etc/systemd/network"
sed "s|@GADGET_MODE@|$GADGET_MODE|" "$ROOT/configs/gadget.conf.in" > "$CHROOT/etc/openstick/gadget.conf"
chmod 644 "$CHROOT/etc/openstick/gadget.conf"
install -m 644 -o root -g root "$ROOT/configs/network/10-usb0.network" "$CHROOT/etc/systemd/network/"
install_tree "$ROOT/configs/system.conf.d" "$CHROOT/etc/systemd/system.conf.d"   # watchdog handover
install -d -m 755 -o root -g root "$CHROOT/var/log/journal"                      # persistent journal
printf 'openstick rootfs\nprofile=%s\nboard=%s\n' "$PROFILE" "$BOARD" > "$CHROOT/etc/openstick-rootfs"
chmod 644 "$CHROOT/etc/openstick-rootfs"
enable_unit multi-user.target qrtr-ns.service /lib/systemd/system/qrtr-ns.service
enable_unit multi-user.target rmtfs.service /lib/systemd/system/rmtfs.service
install -m 644 -o root -g root "$ROOT/configs/openstick-ssh-hostkeys.service" "$CHROOT/etc/systemd/system/"
ln -sf /etc/systemd/system/openstick-ssh-hostkeys.service \
    "$CHROOT/etc/systemd/system/multi-user.target.wants/openstick-ssh-hostkeys.service"
install -m 755 -o root -g root "$ROOT/scripts/msm-firmware-loader.sh" "$CHROOT/usr/sbin/"

# profile specifics (accounts are done in setup.sh). configs/system carries the ttyMSM0 autologin drop-in: dev only.
GETTY_MASK=$CHROOT/etc/systemd/system/serial-getty@ttyGS0.service
if [ "$PROFILE" = dev ]; then
    install_tree "$ROOT/configs/dev" "$CHROOT/etc/systemd/system"
    mkdir -p "$CHROOT/etc/systemd/system/getty.target.wants"
    case "$GADGET_MODE" in *acm*) ln -sfn /lib/systemd/system/serial-getty@.service "$CHROOT/etc/systemd/system/getty.target.wants/serial-getty@ttyGS0.service";; esac
    printf 'DEV IMAGE - not for production. Run: openstick-harden\nWARNING: SSH password login is enabled for user "%s" (documented password). All other SSH logins are key-only.\n' "$DEFAULT_USER" > "$CHROOT/etc/motd"
    install -m 755 -o root -g root "$ROOT/configs/openstick-harden" "$CHROOT/usr/sbin/openstick-harden"
else
    rm -rf --one-file-system -- "${CHROOT:?}/etc/systemd/system/serial-getty@ttyMSM0.service.d"
    rm -f "$GETTY_MASK"; ln -s /dev/null "$GETTY_MASK"   # no getty on the USB gadget serial
fi

# sshd hardening drop-in (explicit, verifiable with sshd -T; tests/verify-sshd-config.sh)
install -d -m 755 -o root -g root "$CHROOT/etc/ssh/sshd_config.d"
SSHD_DROPIN=$CHROOT/etc/ssh/sshd_config.d/10-openstick-hardening.conf
if [ "$PROFILE" = prod ]; then
    install -m 644 -o root -g root "$ROOT/configs/sshd/10-openstick-hardening.prod.conf" "$SSHD_DROPIN"
    [ -n "$SSH_PUBKEY_FILE" ] || echo "WARNING: PROFILE=prod without SSH_PUBKEY_FILE: sshd is key-only and no key is installed, so SSH login is impossible (serial console only)." >&2
else
    sed "s|@TEST_USER@|$DEFAULT_USER|g" "$ROOT/configs/sshd/10-openstick-hardening.dev.conf.in" > "$SSHD_DROPIN"
    chmod 644 "$SSHD_DROPIN"; chown root:root "$SSHD_DROPIN"
fi

# headless stick on mobile data: no background timers/waits by default. Masked (symlink to /dev/null), so no dependency
# can pull them in. Re-enable: remove the name from DISABLE_UNITS in config/user.env and rebuild, or on the device:
# systemctl unmask <unit>. systemd-timesyncd is deliberately NOT in the list.
for u in $DISABLE_UNITS; do
    case "$u" in *[!A-Za-z0-9_@.-]*|"") die "bad unit name in DISABLE_UNITS: '$u'";; esac
    ln -sfn /dev/null "$CHROOT/etc/systemd/system/$u"
    rm -f "$CHROOT"/etc/systemd/system/*.wants/"$u"
done

# SSH policy on cellular: own nftables table (inet openstick_fw), always blocked in the image;
# only the user can allow it on the device: openstick-ssh-cellular on
install -m 755 -o root -g root "$ROOT/configs/openstick-ssh-cellular" "$CHROOT/usr/sbin/"
install -m 644 -o root -g root "$ROOT/configs/openstick-fw.service" "$CHROOT/etc/systemd/system/"
mkdir -p "$CHROOT/etc/systemd/system/sysinit.target.wants"
ln -sf /etc/systemd/system/openstick-fw.service "$CHROOT/etc/systemd/system/sysinit.target.wants/openstick-fw.service"
mkdir -p "$CHROOT/etc/openstick"
printf '# managed by openstick-ssh-cellular\nSSH_ON_CELLULAR=%s\n' 0 \
    > "$CHROOT/etc/openstick/ssh-cellular.conf"
mkdir -p "$CHROOT/lib/firmware/msm-firmware-loader"

# NetworkManager
NMC=$CHROOT/etc/NetworkManager/system-connections
# usb0 is NOT an NM connection (systemd-networkd, see above); no usb.nmconnection, no NM_UNMANAGED udev rule
sed "s|@LTE_APN@|$LTE_APN|" "$ROOT/configs/lte.nmconnection.in" > "$NMC/lte.nmconnection"
if [ -n "$HOTSPOT_PSK" ]; then
    sed -e "s|@HOTSPOT_SSID@|$HOTSPOT_SSID|" -e "s|@HOTSPOT_PSK@|$HOTSPOT_PSK|" \
        "$ROOT/configs/hotspot.nmconnection.in" > "$NMC/hotspot.nmconnection"
else
    echo "NOTE: HOTSPOT_PSK empty, hotspot profile not installed"
fi
if [ -n "$WIFI_PSK" ]; then
    # stable uuid derived from the SSID (no randomness: reproducible rootfs)
    WIFI_UUID=$(printf 'openstick-wifi-client:%s' "$WIFI_SSID" | sha256sum | cut -c1-32 | sed 's/\(........\)\(....\)\(....\)\(....\)\(............\)/\1-\2-\3-\4-\5/')
    sed -e "s|@WIFI_SSID@|$WIFI_SSID|g" -e "s|@WIFI_PSK@|$WIFI_PSK|" -e "s|@WIFI_UUID@|$WIFI_UUID|" \
        "$ROOT/configs/wifi-client.nmconnection.in" > "$NMC/wifi-client.nmconnection"
else
    echo "NOTE: WIFI_PSK empty, client WiFi profile not installed"
fi
chmod 600 "$NMC"/*
mkdir -p "$CHROOT/etc/NetworkManager/conf.d"
cat > "$CHROOT/etc/NetworkManager/conf.d/10-openstick.conf" <<'EOT'
[main]
firewall-backend=nftables
EOT
# dns=dnsmasq is NOT enabled (saves RAM, dnsmasq-base only provides DHCP/DNS for shared mode).
cat > "$CHROOT/etc/NetworkManager/conf.d/20-dns-dnsmasq.conf.example" <<'EOT'
# Rename to 20-dns-dnsmasq.conf to use a local caching resolver (needs the dnsmasq-base binary):
# [main]
# dns=dnsmasq
EOT
install -m 644 -o root -g root "$ROOT/configs/NetworkManager/30-usb0-unmanaged.conf" "$CHROOT/etc/NetworkManager/conf.d/"

# kernel (pmOS apk, extracted the same way as the original Builder)
tar xkzf "$APK" -C "$CHROOT" --no-same-owner --exclude=.PKGINFO --exclude='.SIGN*' 2>/dev/null || true
[ -e "$CHROOT/boot/vmlinuz" ] || ls "$CHROOT"/boot/vmlinuz* >/dev/null || die "kernel not extracted"

# boot config and DTBs
mkdir -p "$CHROOT/boot/extlinux" "$CHROOT/boot/dtbs/qcom"
sed -e "s|@DTB_NAME@|$DTB_NAME|" -e "s|@ROOT_PARTUUID@|$ROOT_PARTUUID|" \
    "$ROOT/configs/extlinux.conf.in" > "$CHROOT/boot/extlinux/extlinux.conf"
install -m 644 -o root -g root "$ROOT"/contrib/dtb/*.dtb "$CHROOT/boot/dtbs/qcom/"
# DTBs compiled from dts/ (scripts/build-dtb.sh) win over the precompiled ones
[ ! -d "$OUT/dtbs" ] || install -m 644 -o root -g root "$OUT"/dtbs/*.dtb "$CHROOT/boot/dtbs/qcom/"
[ -e "$CHROOT/boot/dtbs/qcom/$DTB_NAME" ] || die "$DTB_NAME not found in $CHROOT/boot/dtbs/qcom (not in the kernel apk, contrib/dtb/ or out/dtbs/; custom DTBs: run scripts/build-dtb.sh)"

# root by label (the rootfs image is mkfs'd with -L $ROOT_LABEL -U $ROOT_FS_UUID); no /boot entry: boot is an Android boot.img
printf 'LABEL=%s\t/\text4\tdefaults,noatime\t0\t1\n' "$ROOT_LABEL" > "$CHROOT/etc/fstab"

# gate: nothing the build created may carry a host uid/gid or be group/world-writable (tests/lib-ownership.sh).
# Deliberately a hard failure, not a silent chown: a violation means a copy step is wrong.
own_scan_dir "$CHROOT" || die "ownership/permission violations in $CHROOT (listed above); no tarball written"
tar --numeric-owner -cpzf "$OUT/rootfs-$BOARD.tgz" -C "$CHROOT" .
ls -l "$OUT/rootfs-$BOARD.tgz"
