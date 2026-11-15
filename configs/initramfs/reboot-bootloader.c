/* SPDX-License-Identifier: MIT
 * reboot-bootloader: restart the board into the bootloader (lk2nd fastboot) via reboot(2) LINUX_REBOOT_CMD_RESTART2 "bootloader".
 * Freestanding aarch64 (no libc, no libc.a needed): only a cross gcc is required, the binary is ~1 KiB.
 *   aarch64-linux-gnu-gcc -Os -static -nostdlib -ffreestanding -fno-stack-protector -Wl,--build-id=none -s -o reboot-bootloader reboot-bootloader.c
 * Host test only: -DREBOOT_DRYRUN builds a HOSTED program that never calls reboot and prints the intended arguments. */
#ifdef REBOOT_DRYRUN
#include <stdio.h>
int main(void)
{
    printf("DRYRUN reboot(magic1=0x%x, magic2=0x%x, cmd=0x%x, arg=\"%s\")\n", 0xfee1deadu, 672274793u, 0xa1b2c3d4u, "bootloader");
    return 0;
}
#else
#if !defined(__aarch64__)
#error "freestanding build is aarch64 only (use -DREBOOT_DRYRUN for host tests)"
#endif
#define NR_REBOOT 142
#define NR_SYNC   81
#define NR_EXIT   93
static long sc4(long n, long a, long b, long c, long d)
{
    register long x8 __asm__("x8") = n;
    register long x0 __asm__("x0") = a;
    register long x1 __asm__("x1") = b;
    register long x2 __asm__("x2") = c;
    register long x3 __asm__("x3") = d;
    __asm__ volatile("svc 0" : "+r"(x0) : "r"(x8), "r"(x1), "r"(x2), "r"(x3) : "memory", "cc");
    return x0;
}
void _start(void)
{
    static const char arg[] = "bootloader";
    sc4(NR_SYNC, 0, 0, 0, 0);
    sc4(NR_REBOOT, 0xfee1dead, 672274793, 0xa1b2c3d4, (long)arg);
    sc4(NR_EXIT, 1, 0, 0, 0);          /* only reached if reboot failed */
    for (;;) { }
}
#endif
