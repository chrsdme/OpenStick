#!/bin/sh
# SPDX-License-Identifier: MIT
# Static checks of the built rootfs (work/rootfs after 20-rootfs.sh). Needs root (reads /etc/shadow).
. "$(dirname "$0")/../scripts/common.sh"
. "$ROOT/tests/lib-ownership.sh"
fail=0
chk() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }
# --- rootless part: ownership/permission policy (FLASH-PLAN.md section 9), runs before the root-only checks
chk "ownership detector self-test (negative + positive controls)" "own_selftest"
TGZ=$OUT/rootfs-$BOARD.tgz
if [ -f "$TGZ" ]; then chk "rootfs tarball: no policy-rc.d left over from the build" "! tar tzf $TGZ | grep -q 'usr/sbin/policy-rc.d'"
    chk "rootfs tarball: no uid/gid >= 1000 outside /home/$DEFAULT_USER, nothing group/world-writable in /etc /usr /boot" "own_scan_tar $TGZ"
else echo "SKIP rootfs tarball ownership check ($TGZ missing)"; fi
if [ -d "$WORK/gt-dist" ]; then chk "gt-dist overlay (added by 40-images.sh): same policy" "own_scan_dir $WORK/gt-dist"
else echo "SKIP gt-dist ownership check (run scripts/30-gt.sh)"; fi
need_root; safe_dir "$CHROOT"
R=$CHROOT
chk "rootfs tree: same ownership/permission policy" "own_scan_dir $R"
chk "policy-rc.d absent from the rootfs" "! test -e $R/usr/sbin/policy-rc.d"
IMG_PROFILE=$(sed -n 's/^PROFILE=//p' $R/etc/openstick/profile 2>/dev/null | head -1)
chk "profile file matches PROFILE=$PROFILE" "test \"$IMG_PROFILE\" = $PROFILE"
if [ "$PROFILE" = dev ]; then
    chk "default user exists" "grep -q '^$DEFAULT_USER:' $R/etc/passwd"
    chk "default user in sudo group" "grep -q '^sudo:.*[:,]$DEFAULT_USER\$' $R/etc/group"
    [ "$FORCE_PASSWD_CHANGE" != 1 ] || chk "default password expired (lastchg=0)" "awk -F: -v u=$DEFAULT_USER '\$1==u{exit !(\$3==0)}' $R/etc/shadow"
    [ -n "${ROOT_PASSWORD_HASH:-}" ] || chk "root password empty" "awk -F: '\$1==\"root\"{exit !(\$2==\"\")}' $R/etc/shadow"
    chk "motd marks a DEV image" "grep -q 'DEV IMAGE - not for production. Run: openstick-harden' $R/etc/motd"
    chk "openstick-harden installed 755 root:root" "test \"\$(stat -c '%a %u:%g' $R/usr/sbin/openstick-harden)\" = '755 0:0'"
    chk "dev: sudo installed" "test -x $R/usr/bin/sudo"
    chk "ttyMSM0 and ttyGS0 autologin drop-ins present" "grep -q -- --autologin $R/etc/systemd/system/serial-getty@ttyMSM0.service.d/override.conf && grep -q -- --autologin $R/etc/systemd/system/serial-getty@ttyGS0.service.d/override.conf"
else
    chk "prod: root locked or hashed (not empty)" "awk -F: '\$1==\"root\"{exit !(\$2 ~ /^(!|\\*|\\\$)/)}' $R/etc/shadow"
    chk "prod: no autologin anywhere in the image" "! grep -rq -- --autologin $R/etc/systemd $R/lib/systemd/system 2>/dev/null"
    chk "prod: no serial-getty@ttyGS0 (masked)" "test \"\$(readlink $R/etc/systemd/system/serial-getty@ttyGS0.service)\" = /dev/null && ! test -e $R/etc/systemd/system/getty.target.wants/serial-getty@ttyGS0.service"
    chk "prod: no sudo binary, no sudoers.d, no user in the sudo group" "! test -e $R/usr/bin/sudo && ! test -e $R/etc/sudoers && ! grep -q '^sudo:[^:]*:[^:]*:.\\+' $R/etc/group"
    chk "prod: no DEV motd, no openstick-harden" "! grep -qs 'DEV IMAGE' $R/etc/motd && ! test -e $R/usr/sbin/openstick-harden"
    chk "prod: no test user '$DEFAULT_USER' unless explicitly named" "[ $DEFAULT_USER != openstick ] || ! grep -q '^openstick:' $R/etc/passwd"
fi
# --- block-device image layout (boot.img + rootfs.img): root by label, marker, growfs, usb0, gadget, watchdog, journal
chk "fstab: LABEL=$ROOT_LABEL / ext4 defaults,noatime 0 1" "grep -qx 'LABEL=$ROOT_LABEL[[:space:]]/[[:space:]]ext4[[:space:]]defaults,noatime[[:space:]]0[[:space:]]1' $R/etc/fstab"
chk "fstab: no /boot line" "! awk '\$2==\"/boot\"{f=1} END{exit !f}' $R/etc/fstab"
chk "marker /etc/openstick-rootfs (profile=$PROFILE board=$BOARD)" "grep -qx 'openstick rootfs' $R/etc/openstick-rootfs && grep -qx 'profile=$PROFILE' $R/etc/openstick-rootfs && grep -qx 'board=$BOARD' $R/etc/openstick-rootfs"
chk "/sbin/init resolves to systemd" "test \"\$(readlink $R/sbin/init)\" = /lib/systemd/systemd || test \"\$(readlink $R/sbin/init)\" = /usr/lib/systemd/systemd"
chk "growfs: script 755, unit, enable symlink, resize2fs present" "test \"\$(stat -c '%a %u:%g' $R/usr/sbin/openstick-growfs)\" = '755 0:0' && test -f $R/etc/systemd/system/openstick-growfs.service && test -L $R/etc/systemd/system/multi-user.target.wants/openstick-growfs.service && test -x $R/sbin/resize2fs -o -x $R/usr/sbin/resize2fs"
chk "growfs script refuses non-mmcblk0p* root" "grep -q 'mmcblk0p' $R/usr/sbin/openstick-growfs && grep -q growfs.done $R/usr/sbin/openstick-growfs"
chk "usb0: networkd static 172.16.42.1/24 + DHCP server (pool 1, no router/DNS/NTP)" "grep -qx 'Name=usb0' $R/etc/systemd/network/10-usb0.network && grep -qx 'Address=172.16.42.1/24' $R/etc/systemd/network/10-usb0.network && grep -qx 'DHCPServer=yes' $R/etc/systemd/network/10-usb0.network && grep -qx 'PoolSize=1' $R/etc/systemd/network/10-usb0.network && grep -qx 'EmitRouter=no' $R/etc/systemd/network/10-usb0.network && grep -qx 'EmitDNS=no' $R/etc/systemd/network/10-usb0.network"
chk "systemd-networkd service + socket enabled" "test -L $R/etc/systemd/system/multi-user.target.wants/systemd-networkd.service && test -L $R/etc/systemd/system/sockets.target.wants/systemd-networkd.socket"
chk "NetworkManager: usb0 unmanaged; no usb.nmconnection, no 99-nm-usb0.rules" "grep -rqx 'unmanaged-devices=interface-name:usb0' $R/etc/NetworkManager/conf.d && ! test -e $R/etc/NetworkManager/system-connections/usb.nmconnection && ! test -e $R/etc/udev/rules.d/99-nm-usb0.rules"
chk "gadget: script 755, conf with a valid GADGET_MODE, unit enabled, old gt unit not enabled" "test \"\$(stat -c '%a %u:%g' $R/usr/sbin/openstick-gadget)\" = '755 0:0' && grep -Eq '^GADGET_MODE=(ecm|ecm\\+acm|rndis)\$' $R/etc/openstick/gadget.conf && test -f $R/etc/systemd/system/openstick-gadget.service && test -L $R/etc/systemd/system/multi-user.target.wants/openstick-gadget.service && ! test -e $R/etc/systemd/system/usb-gadget.target.wants/usb-gadget.service"
chk "gadget: negative control (bad mode is detected by the conf pattern)" "! printf 'GADGET_MODE=foo\n' | grep -Eq '^GADGET_MODE=(ecm|ecm\\+acm|rndis)\$'"
GMODE=$(sed -n 's/^GADGET_MODE=//p' $R/etc/openstick/gadget.conf | head -1)
if [ "$PROFILE" = dev ]; then
    case "$GMODE" in *acm*) chk "dev + acm: serial-getty@ttyGS0 enabled" "test -L $R/etc/systemd/system/getty.target.wants/serial-getty@ttyGS0.service";;
        *) chk "dev, no acm: serial-getty@ttyGS0 not enabled" "! test -e $R/etc/systemd/system/getty.target.wants/serial-getty@ttyGS0.service";; esac
fi
chk "watchdog drop-in (WatchdogDevice, RuntimeWatchdogSec)" "grep -qx 'WatchdogDevice=/dev/watchdog0' $R/etc/systemd/system.conf.d/10-openstick-watchdog.conf && grep -qx 'RuntimeWatchdogSec=30s' $R/etc/systemd/system.conf.d/10-openstick-watchdog.conf"
chk "persistent journal dir" "test -d $R/var/log/journal"
chk "systemd-firstboot masked" "test \"\$(readlink $R/etc/systemd/system/systemd-firstboot.service)\" = /dev/null"
chk "no NOPASSWD in sudoers" "! grep -rq NOPASSWD $R/etc/sudoers $R/etc/sudoers.d"
chk "no ssh host keys in image" "! ls $R/etc/ssh/ssh_host_* >/dev/null 2>&1"
chk "nftables.service not enabled (its conf does flush ruleset)" "! ls $R/etc/systemd/system/*.wants/nftables.service >/dev/null 2>&1"
chk "msm-firmware-loader.service is a real unit (not masked)" "test -f $R/etc/systemd/system/msm-firmware-loader.service && ! test -L $R/etc/systemd/system/msm-firmware-loader.service"
chk "msm-firmware-loader enabled in sysinit.target.wants" "test -L $R/etc/systemd/system/sysinit.target.wants/msm-firmware-loader.service"
chk "msm-firmware-loader not also in multi-user.target.wants" "! test -e $R/etc/systemd/system/multi-user.target.wants/msm-firmware-loader.service"
chk "rmtfs drop-in orders after the loader" "grep -q '^Requires=msm-firmware-loader.service' $R/etc/systemd/system/rmtfs.service.d/10-firmware.conf && grep -q '^After=msm-firmware-loader.service' $R/etc/systemd/system/rmtfs.service.d/10-firmware.conf"
chk "qrtr-ns and rmtfs enabled, rmtfs not masked" "test -L $R/etc/systemd/system/multi-user.target.wants/qrtr-ns.service && test -L $R/etc/systemd/system/multi-user.target.wants/rmtfs.service && ! test \"\$(readlink $R/etc/systemd/system/rmtfs.service)\" = /dev/null"
chk "msm-firmware-loader.sh installed" "test -x $R/usr/sbin/msm-firmware-loader.sh"
keys_of() { awk 'NF>=2 && $1 ~ /^(ssh-|ecdsa-|sk-)/ {print $2}' "$1" | sort; }
[ -z "${SSH_PUBKEY_FILE:-}" ] || chk "root authorized_keys holds the configured key(s) (0600, dir 0700)" "test \"\$(stat -c %a $R/root/.ssh/authorized_keys)\" = 600 && test \"\$(stat -c %a $R/root/.ssh)\" = 700 && test \"\$(keys_of $SSH_PUBKEY_FILE)\" = \"\$(keys_of $R/root/.ssh/authorized_keys)\""
[ -z "${WIFI_PSK:-}" ] || chk "client WiFi profile installed 0600" "test \"\$(stat -c %a $R/etc/NetworkManager/system-connections/wifi-client.nmconnection)\" = 600"
chk "openstick-fw.service enabled" "test -L $R/etc/systemd/system/sysinit.target.wants/openstick-fw.service"
chk "ssh-cellular state file" "grep -q '^SSH_ON_CELLULAR=' $R/etc/openstick/ssh-cellular.conf"
chk "ssh-cellular off in image" "grep -qx SSH_ON_CELLULAR=0 $R/etc/openstick/ssh-cellular.conf"
# H: firewall
chk "firewall tool installed 755 root:root" "test \"\$(stat -c '%a %u:%g' $R/usr/sbin/openstick-ssh-cellular)\" = '755 0:0'"
chk "firewall ruleset: policy drop, ssh only usb0/wlan0, no wwan" "NM_DIR=$R/etc/NetworkManager/system-connections sh $R/usr/sbin/openstick-ssh-cellular print > /tmp/verify-fw-rs.\$\$; grep -q 'policy drop' /tmp/verify-fw-rs.\$\$ && grep -q 'iifname { \"usb0\", \"wlan0\" } tcp dport 22 accept' /tmp/verify-fw-rs.\$\$ && ! grep -q wwan /tmp/verify-fw-rs.\$\$; rc=\$?; rm -f /tmp/verify-fw-rs.\$\$; exit \$rc"
if [ -e $R/etc/NetworkManager/system-connections/hotspot.nmconnection ]; then
    chk "hotspot installed: firewall serves dhcp/dns on wlan0" "NM_DIR=$R/etc/NetworkManager/system-connections sh $R/usr/sbin/openstick-ssh-cellular print | grep -q 'iifname \"wlan0\" udp dport { 53, 67 } accept'"
fi
# I: sshd drop-in
chk "sshd drop-in 10-openstick-hardening.conf present, root:root 644" "test \"\$(stat -c '%a %u:%g' $R/etc/ssh/sshd_config.d/10-openstick-hardening.conf)\" = '644 0:0'"
chk "sshd drop-in: PasswordAuthentication no, KbdInteractiveAuthentication no, PermitRootLogin prohibit-password" "grep -qx 'PasswordAuthentication no' $R/etc/ssh/sshd_config.d/10-openstick-hardening.conf && grep -qx 'KbdInteractiveAuthentication no' $R/etc/ssh/sshd_config.d/10-openstick-hardening.conf && grep -qx 'PermitRootLogin prohibit-password' $R/etc/ssh/sshd_config.d/10-openstick-hardening.conf"
if [ "$PROFILE" = dev ]; then
    chk "dev: password only for the test user (Match User $DEFAULT_USER)" "grep -qx 'Match User $DEFAULT_USER' $R/etc/ssh/sshd_config.d/10-openstick-hardening.conf && grep -c 'PasswordAuthentication yes' $R/etc/ssh/sshd_config.d/10-openstick-hardening.conf | grep -qx 1"
    chk "dev: motd warns about SSH password login" "grep -q 'SSH password login is enabled' $R/etc/motd"
else
    chk "prod: no 'PasswordAuthentication yes' and no Match in the drop-in" "! grep -Eq 'PasswordAuthentication yes|^Match' $R/etc/ssh/sshd_config.d/10-openstick-hardening.conf"
fi
# J: headless units masked
for u in $DISABLE_UNITS; do chk "masked: $u" "test \"\$(readlink $R/etc/systemd/system/$u)\" = /dev/null"; done
chk "systemd-timesyncd not masked" "! test \"\$(readlink $R/etc/systemd/system/systemd-timesyncd.service)\" = /dev/null"
chk "DTB present in /boot" "test -e $R/boot/dtbs/qcom/$DTB_NAME"
chk "NM firewall-backend=nftables" "grep -q '^firewall-backend=nftables' $R/etc/NetworkManager/conf.d/10-openstick.conf"
chk "NM dns=dnsmasq not active" "! grep -rhq '^dns=dnsmasq' $R/etc/NetworkManager"
chk "machine-id empty/absent" "! test -s $R/etc/machine-id"
chk "dbus machine-id absent or symlink" "! test -f $R/var/lib/dbus/machine-id || test -L $R/var/lib/dbus/machine-id"
chk "no shell history" "! find $R/root $R/home -name '.*_history' -type f -size +0 2>/dev/null | grep -q ."
chk "apt lists empty" "! find $R/var/lib/apt/lists -type f ! -name lock 2>/dev/null | grep -q ."
chk "apt cache empty" "! ls $R/var/cache/apt/*.bin $R/var/cache/apt/archives/*.deb >/dev/null 2>&1"
chk "/etc/hosts has hostname" "grep -q '^127.0.1.1.*$HOST_NAME' $R/etc/hosts"
echo "--- installed sizes (KiB) of selected packages:"
for p in sudo wireless-regdb dnsmasq-base nftables openssh-server network-manager; do
    printf '%s ' "$p"; chroot "$R" dpkg-query -W -f='${Installed-Size}\n' "$p" 2>/dev/null || echo "not installed"
done
echo "--- rootfs size:"; du -sm --one-file-system "$R" | cut -f1 | sed 's/$/ MiB/'
exit $fail
