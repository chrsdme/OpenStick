#!/bin/sh
# SPDX-License-Identifier: MIT
# Runs INSIDE one `fakeroot` session (called by 49-rootfs-test.sh): extract, edit, mke2fs -d, check. Rootless.
# usage: 49-inner-fakeroot.sh TGZ TREE IMG SIZE_MIB PUBKEY_FILE CONFIGS_DIR LABEL UUID
set -eu
umask 022
TGZ=$1 TREE=$2 IMG=$3 SIZE_MIB=$4 PUBKEY=$5 CFG=$6 LABEL=$7 UUID=$8
[ -d "$TREE" ] && [ -z "$(ls -A "$TREE")" ] || { echo "TREE must be an existing empty dir" >&2; exit 1; }
tar --numeric-owner -xpzf "$TGZ" -C "$TREE"
E=$TREE/etc

# --- slim: the test kernel comes from fastboot, nothing in /boot is needed; no dnsmasq binary (spec) ---
find "$TREE/boot" -mindepth 1 -delete
rm -f "$TREE/usr/sbin/dnsmasq"

# --- test marker checked by the initramfs before it switches root ---
printf 'openstick test rootfs, built %s, tgz sha256 %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(sha256sum "$TGZ" | cut -d' ' -f1)" > "$E/openstick-test-rootfs"

# --- fstab: the old PARTUUID /boot entry does not exist on the device (would drop systemd into emergency mode) ---
printf 'LABEL=%s\t/\text4\tdefaults,noatime\t0\t1\n' "$LABEL" > "$E/fstab"

# --- usb0: systemd-networkd (static + built-in DHCP server, NO dnsmasq); NetworkManager must not touch usb0 ---
rm -f "$E/NetworkManager/system-connections/usb.nmconnection" "$E/udev/rules.d/99-nm-usb0.rules"
cat > "$E/NetworkManager/conf.d/30-usb0-unmanaged.conf" <<'EOT'
[keyfile]
unmanaged-devices=interface-name:usb0
EOT
mkdir -p "$E/systemd/network"
cat > "$E/systemd/network/10-usb0.network" <<'EOT'
[Match]
Name=usb0

[Network]
Address=172.16.42.1/24
ConfigureWithoutCarrier=yes
DHCPServer=yes

[DHCPServer]
PoolOffset=2
PoolSize=1
EmitRouter=no
EmitDNS=no
EmitNTP=no
EOT
mkdir -p "$E/systemd/system/multi-user.target.wants" "$E/systemd/system/sockets.target.wants" "$E/systemd/system/getty.target.wants"
ln -sf /lib/systemd/system/systemd-networkd.service "$E/systemd/system/multi-user.target.wants/systemd-networkd.service"
ln -sf /lib/systemd/system/systemd-networkd.socket "$E/systemd/system/sockets.target.wants/systemd-networkd.socket"

# --- masks (rm first: some of these are regular files in /etc/systemd/system) ---
mask() { rm -f "$E/systemd/system/$1"; ln -s /dev/null "$E/systemd/system/$1"; }
mask usb-gadget.service            # RNDIS scheme; would fight the ecm+acm gadget created by the initramfs
mask systemd-firstboot.service     # would block on the console asking questions (machine-id is empty on purpose)
# msm-firmware-loader and rmtfs are NOT masked any more (owner decision 2026-11-14: board #6 runs them, backup verified).
# They come from configs/system via 20-rootfs.sh. rmtfs writes modemst1/2 (+fsc/fsg): project rule 4, flash only with the backup gate.
rm -f "$E/systemd/system/usb-gadget.target.wants/usb-gadget.service"

# --- second console channel: ACM gadget serial (host /dev/ttyACM0). TEST ONLY: autologin root, same exposure as the telnetd of the earlier tests ---
mkdir -p "$E/systemd/system/serial-getty@ttyGS0.service.d"
cat > "$E/systemd/system/serial-getty@ttyGS0.service.d/override.conf" <<'EOT'
[Service]
ExecStart=
ExecStart=-/sbin/agetty -o '-p -- \\u' --keep-baud 115200,38400,9600 --noclear --autologin root ttyGS0 $TERM
EOT
ln -sf /lib/systemd/system/serial-getty@.service "$E/systemd/system/getty.target.wants/serial-getty@ttyGS0.service"

# --- watchdog handover: the initramfs stops ONLY the feeder of /dev/watchdog0, systemd takes that device ---
mkdir -p "$E/systemd/system.conf.d"
cat > "$E/systemd/system.conf.d/10-openstick-watchdog.conf" <<'EOT'
[Manager]
WatchdogDevice=/dev/watchdog0
RuntimeWatchdogSec=30s
EOT

# --- persistent journal (post-mortem after a reset: mount the partition in the initramfs shell or read it from the host) ---
mkdir -p "$TREE/var/log/journal"

# --- first boot: grow the filesystem to the whole partition (online, only touches the device it is mounted from) ---
cat > "$TREE/usr/sbin/openstick-growfs" <<'EOT'
#!/bin/sh
# grow the mounted root ext4 to its partition, once
set -u
DEV=$(findmnt -n -o SOURCE /) || exit 1
case "$DEV" in /dev/mmcblk0p*) ;; *) echo "refusing: root device is '$DEV'"; exit 1;; esac
resize2fs "$DEV" && mkdir -p /var/lib/openstick && : > /var/lib/openstick/growfs.done
EOT
chmod 755 "$TREE/usr/sbin/openstick-growfs"
cat > "$E/systemd/system/openstick-growfs.service" <<'EOT'
[Unit]
Description=Grow root filesystem to the partition size (once)
ConditionPathExists=!/var/lib/openstick/growfs.done
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/openstick-growfs

[Install]
WantedBy=multi-user.target
EOT
ln -sf /etc/systemd/system/openstick-growfs.service "$E/systemd/system/multi-user.target.wants/openstick-growfs.service"

# --- ssh: root by key only (sshd refuses empty passwords, PermitRootLogin prohibit-password); key without comment ---
if [ -s "$PUBKEY" ]; then
    mkdir -p "$TREE/root/.ssh"
    awk 'NR==1{print $1" "$2}' "$PUBKEY" > "$TREE/root/.ssh/authorized_keys"
    chmod 700 "$TREE/root/.ssh"; chmod 600 "$TREE/root/.ssh/authorized_keys"
fi

# --- ext4 image: size is a multiple of 1 MiB; fixed UUID; no orphan_file / metadata_csum_seed (old e2fsprogs on the device) ---
rm -f "$IMG"
truncate -s "${SIZE_MIB}M" "$IMG"
mke2fs -q -t ext4 -L "$LABEL" -U "$UUID" -m 1 -O ^orphan_file,^metadata_csum_seed \
    -E root_owner=0:0,hash_seed=$UUID,lazy_itable_init=0 -d "$TREE" "$IMG"
e2fsck -fn "$IMG"
debugfs -R 'stat /etc/shadow' "$IMG" 2>/dev/null | grep -E 'User:|Mode:' | head -2
debugfs -R 'stat /usr/lib/systemd/systemd' "$IMG" 2>/dev/null | grep -E 'User:|Mode:' | head -2
debugfs -R 'ls -l /root/.ssh' "$IMG" 2>/dev/null | head -4
