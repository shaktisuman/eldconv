#!/usr/bin/env bash
# Configure an arm64 kernel tree (the working directory) as a small kernel that boots on qemu's
# virt machine. `eldconv check --pre-build` runs this in the converted tree, with ARCH and
# CROSS_COMPILE exported.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
make_args=(ARCH="${ARCH:-arm64}" CROSS_COMPILE="${CROSS_COMPILE:-aarch64-linux-gnu-}")

make "${make_args[@]}" tinyconfig
cat "$here/boot.config" >> .config
make "${make_args[@]}" olddefconfig

# olddefconfig drops a symbol whose dependencies are not met without a word, and the failure then
# shows up much later as a kernel that prints nothing. Check the ones the boot cannot do without.
missing=0
for sym in PRINTK TTY SERIAL_AMBA_PL011 SERIAL_AMBA_PL011_CONSOLE BLK_DEV_INITRD BINFMT_ELF; do
	if ! grep -qx "CONFIG_$sym=y" .config; then
		echo "::error::CONFIG_$sym did not survive olddefconfig; the kernel would not boot" >&2
		missing=1
	fi
done
for sym in RELOCATABLE RANDOMIZE_BASE; do
	if grep -qx "CONFIG_$sym=y" .config; then
		echo "::error::CONFIG_$sym is on; eld cannot link a relocatable arm64 kernel correctly" >&2
		missing=1
	fi
done
# poweroff only; without it the boot test falls back to a timeout
grep -qx "CONFIG_ARM_PSCI_FW=y" .config || echo "::warning::no PSCI; qemu will not power off and the boot test waits for its timeout" >&2
exit "$missing"
