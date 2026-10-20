#!/usr/bin/env bash
# Boot an arm64 kernel Image on qemu's virt machine and check that it reached userspace.
#
# usage: boot.sh IMAGE [LOG]
#
# Passes when the log has init's marker and the kernel's version banner names eld, i.e. the Image
# was linked by the linker under test and still boots. Needs aarch64-linux-gnu-gcc, cpio and
# qemu-system-aarch64.
set -euo pipefail

image=${1:?usage: boot.sh IMAGE [LOG]}
log=${2:-boot.log}
timeout_s=${BOOT_TIMEOUT:-180}
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

"${CROSS_COMPILE:-aarch64-linux-gnu-}gcc" -static -nostdlib -ffreestanding -fno-stack-protector -O2 -Wall -Werror \
	-o "$work/init" "$here/init.c"
(cd "$work" && echo init | cpio -o -H newc -R 0:0 --quiet > initramfs.cpio)

# a kernel that cannot power off keeps qemu running until the timeout; the log decides, not the exit code.
# -nic none: the default NIC wants efi-virtio.rom, which a --no-install-recommends qemu does not ship
set +e
timeout "$timeout_s" qemu-system-aarch64 \
	-machine virt,gic-version=3 -cpu cortex-a57 -smp 1 -m 256 \
	-nographic -no-reboot \
	-nic none \
	-kernel "$image" -initrd "$work/initramfs.cpio" \
	-append "console=ttyAMA0 earlycon rdinit=/init panic=-1" \
	> "$log" 2>&1 < /dev/null
rc=$?
set -e
echo "qemu exited with status $rc"

fail=0
grep -a '^Linux version' "$log" | head -1 || true
if ! grep -aq 'ELDCONV_BOOT_OK' "$log"; then
	echo "::error::the kernel never reached init (no ELDCONV_BOOT_OK in the console log)" >&2
	fail=1
fi
if ! grep -a '^Linux version' "$log" | head -1 | grep -aqi 'eld'; then
	echo "::error::the kernel's version banner does not name eld as its linker" >&2
	fail=1
fi
if [ "$fail" -ne 0 ]; then
	echo "--- last 60 lines of $log"
	tail -n 60 "$log"
fi
exit "$fail"
