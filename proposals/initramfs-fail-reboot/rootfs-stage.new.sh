# ---- rootfs stage (test-boot5, injected by scripts/49-rootfs-test.sh before the ACM shell) ----
# Looks for the partition named "userdata" (by GPT name, NOT by a fixed number), probe-mounts it read-only and only
# switches root if it carries the test rootfs marker. Anything else: unmount, log, fall through to the init2 shell + LED loop.
NEWROOT=${NEWROOT:-/newroot}
SYSBLK=${SYSBLK:-/sys/class/block}
CMDLINE_FILE=${CMDLINE_FILE:-/proc/cmdline}
WAIT_SECS=${INITRAMFS_WAIT_SECS:-20}          # give up (and reboot to the bootloader) after this many seconds
POLL_SLEEP=${INITRAMFS_POLL_SLEEP:-0.25}
REBOOT_BL=${REBOOT_BL:-/sbin/reboot-bootloader}
RP=""
SWOK=no
mkdir -p $NEWROOT
i=0; MAXPOLLS=$((WAIT_SECS*4)); NOROOT=no
case "$(cat $CMDLINE_FILE)" in *testboot.noroot*) NOROOT=yes; MAXPOLLS=0; log "rootfs: testboot.noroot on the cmdline, stage skipped";; esac
# Poll until userdata exists AND carries the marker; transient failures (partition not enumerated yet) are retried.
while [ $SWOK = no ] && [ $i -lt $MAXPOLLS ]; do
    RP=""
    for u in $SYSBLK/mmcblk0p*/uevent; do
        [ -e "$u" ] || continue
        grep -qx 'PARTNAME=userdata' "$u" && RP=/dev/$(basename "$(dirname "$u")")
    done
    if [ -n "$RP" ]; then
        if mount -t ext4 -o ro $RP $NEWROOT 2>>$LOG; then
            if [ -e $NEWROOT/etc/openstick-test-rootfs ] && [ -x $NEWROOT/usr/lib/systemd/systemd ] && [ "$(readlink $NEWROOT/usr/sbin/init)" = /lib/systemd/systemd ]; then
                SWOK=yes
                log "rootfs: marker found: $(cat $NEWROOT/etc/openstick-test-rootfs)"
                break
            fi
            [ $i -eq 0 ] && log "rootfs: $RP mounts as ext4 but has no test-rootfs marker / systemd: NOT touching it, unmounting"
            umount $NEWROOT 2>>$LOG
        else
            [ $i -eq 0 ] && log "rootfs: ro mount of $RP failed: $(tail -n1 $LOG)"
        fi
    fi
    i=$((i+1)); sleep $POLL_SLEEP
done
log "rootfs: userdata=${RP:-NOT FOUND} SWOK=$SWOK after $i polls (limit $MAXPOLLS, WAIT_SECS=$WAIT_SECS)"
if [ $SWOK = yes ]; then
    mount -o remount,rw $NEWROOT 2>>$LOG || log "rootfs: remount rw FAILED, continuing read-only (systemd-remount-fs will retry)"
    mkdir -p $NEWROOT/var/log
    cp $LOG $NEWROOT/var/log/initramfs-init.log 2>/dev/null
    dmesg > $NEWROOT/var/log/initramfs-dmesg.log 2>/dev/null
    sync
    log "rootfs: tearing down initramfs services, then switch_root"
    killall udhcpd telnetd 2>/dev/null
    # hand /dev/watchdog0 to systemd (RuntimeWatchdogSec in the rootfs): stop only its feeder, every other feeder keeps running
    if [ -f /tmp/wd.watchdog0 ]; then kill $(cat /tmp/wd.watchdog0) 2>/dev/null; sleep 1; fi
    for m in proc sys dev; do mount -n -o move /$m $NEWROOT/$m 2>/dev/null || log "rootfs: move /$m failed"; done
    exec switch_root $NEWROOT /sbin/init
    # only reached if exec itself failed
fi
if [ $NOROOT = no ]; then
    log "rootfs: no usable rootfs after ${WAIT_SECS}s: rebooting to the bootloader"
    sync
    if [ -x "$REBOOT_BL" ]; then "$REBOOT_BL" 2>>$LOG; log "rootfs: $REBOOT_BL returned rc=$? (reboot did not happen)"; else log "rootfs: $REBOOT_BL missing"; fi
fi
log "rootfs: staying in the initramfs shell (SWOK=$SWOK)"
# ---- end rootfs stage ----
