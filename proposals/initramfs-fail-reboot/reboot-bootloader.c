/* reboot-bootloader: restart the board into the bootloader (fastboot) via reboot(2) RESTART2 "bootloader".
 * Static, no libc dependencies beyond syscall(). Intended for the initramfs failure path.
 * Host tests: build with -DREBOOT_DRYRUN, which NEVER calls the reboot syscall and only prints the intended arguments. */
#define _GNU_SOURCE
#include <stdio.h>
#include <unistd.h>
#include <sys/syscall.h>
#include <linux/reboot.h>

int main(void)
{
    static const char arg[] = "bootloader";
#ifdef REBOOT_DRYRUN
    printf("DRYRUN reboot(magic1=0x%x, magic2=0x%x, cmd=0x%x, arg=\"%s\")\n",
           (unsigned)LINUX_REBOOT_MAGIC1, (unsigned)LINUX_REBOOT_MAGIC2,
           (unsigned)LINUX_REBOOT_CMD_RESTART2, arg);
    return 0;
#else
    sync();
    syscall(SYS_reboot, LINUX_REBOOT_MAGIC1, LINUX_REBOOT_MAGIC2, LINUX_REBOOT_CMD_RESTART2, arg);
    perror("reboot-bootloader: reboot");   /* only reached on failure */
    return 1;
#endif
}
