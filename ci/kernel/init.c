/* PID 1 for the boot test: print a marker on the console, then power the machine off.
 *
 * Built -nostdlib with raw arm64 syscalls so it runs on a tinyconfig kernel, which lacks futexes,
 * epoll and the rest of what a libc start-up may probe. Nothing but write/openat/mknodat/reboot.
 */

#define SYS_mknodat   33
#define SYS_mkdirat   34
#define SYS_openat    56
#define SYS_write     64
#define SYS_sched_yield 124
#define SYS_reboot    142

#define AT_FDCWD      (-100)
#define O_WRONLY      1
#define S_IFCHR       0020000
#define MAKEDEV_5_1   ((5 << 8) | 1)   /* /dev/console */

static long sys(long nr, long a0, long a1, long a2, long a3)
{
	register long x8 __asm__("x8") = nr;
	register long x0 __asm__("x0") = a0;
	register long x1 __asm__("x1") = a1;
	register long x2 __asm__("x2") = a2;
	register long x3 __asm__("x3") = a3;

	__asm__ volatile("svc 0" : "+r"(x0) : "r"(x8), "r"(x1), "r"(x2), "r"(x3) : "memory");
	return x0;
}

static long len(const char *s)
{
	long n = 0;

	while (s[n])
		n++;
	return n;
}

static void say(int fd, const char *s)
{
	sys(SYS_write, fd, (long)s, len(s), 0);
}

void _start(void)
{
	static const char msg[] = "ELDCONV_BOOT_OK: init reached userspace\n";

	/* stdout is /dev/console when the kernel found the node in the initramfs; if it did not,
	 * make the node and open it ourselves */
	if (sys(SYS_write, 1, (long)msg, sizeof msg - 1, 0) < 0) {
		long fd;

		sys(SYS_mkdirat, AT_FDCWD, (long)"/dev", 0755, 0);
		sys(SYS_mknodat, AT_FDCWD, (long)"/dev/console", S_IFCHR | 0600, MAKEDEV_5_1);
		fd = sys(SYS_openat, AT_FDCWD, (long)"/dev/console", O_WRONLY, 0);
		if (fd >= 0)
			say(fd, msg);
	}

	/* LINUX_REBOOT_MAGIC1, MAGIC2, CMD_POWER_OFF */
	sys(SYS_reboot, 0xfee1dead, 672274793, 0x4321fedc, 0);

	/* no PSCI: the host side times qemu out and judges the log */
	for (;;)
		sys(SYS_sched_yield, 0, 0, 0, 0);
}
