"""eldconv - convert GNU/LLVM build setups (bfd/lld/gold/mold) to link with eld."""

__version__ = "1.1.0"
SHIM_DIR = "eld-bin"   # directory (next to eld.mk) holding the linker shim

LINKERS = ("bfd", "lld", "gold", "mold")
