# ---- rootfs stage (test-boot5, injected by scripts/49-rootfs-test.sh before the ACM shell) ----
# Looks for the partition named "userdata" (by GPT name, NOT by a fixed number), probe-mounts it read-only and only
# switches root if it carries the test rootfs marker. Anything else: unmount, log, fall through to the init2 shell + LED loop.
NEWROOT=/newroot
RP=""
mkdir -p $NEWROOT
i=0
case "$(cat /proc/cmdline)" in *testboot.noroot*) i=99; log "rootfs: testboot.noroot on the cmdline, stage skipped";; esac
while [ -z "$RP" ] && [ $i -lt 40 ]; do
    for u in /sys/class/block/mmcblk0p*/uevent; do
        [ -e "$u" ] || continue
        grep -qx 'PARTNAME=userdata' "$u" && RP=/dev/$(basename "$(dirname "$u")")
    done
    [ -n "$RP" ] || { i=$((i+1)); sleep 0.25; }
done
log "rootfs: userdata resolved to ${RP:-NOT FOUND} after $i polls (the task text said /dev/mmcblk0p26)"
log "rootfs: partitions: $(for u in /sys/class/block/mmcblk0p*/uevent; do [ -e "$u" ] && echo "$(basename "$(dirname "$u")")=$(sed -n 's/^PARTNAME=//p' "$u")/$(cat "$(dirname "$u")/size")s"; done | tr '\n' ' ')"
SWOK=no
if [ -n "$RP" ]; then
    if mount -t ext4 -o ro $RP $NEWROOT 2>>$LOG; then
        if [ -e $NEWROOT/etc/openstick-test-rootfs ] && [ -x $NEWROOT/usr/lib/systemd/systemd ] && [ "$(readlink $NEWROOT/usr/sbin/init)" = /lib/systemd/systemd ]; then
            SWOK=yes
            log "rootfs: marker found: $(cat $NEWROOT/etc/openstick-test-rootfs)"
        else
            log "rootfs: $RP mounts as ext4 but has no test-rootfs marker / systemd: NOT touching it, unmounting"
            umount $NEWROOT 2>>$LOG
        fi
    else
        log "rootfs: ro mount of $RP failed (expected for an untouched Android userdata that needs journal recovery): $(tail -n1 $LOG)"
    fi
fi
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
log "rootfs: staying in the initramfs shell (SWOK=$SWOK)"
# ---- end rootfs stage ----
