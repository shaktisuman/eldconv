CC ?= gcc
LD = ld.gold
CFLAGS := -O2 -Wall
LDFLAGS := -fuse-ld=gold -Wl,--icf=all -Wl,--gc-sections -Wl,--incremental-full -Wl,-Map=out.map
all: app
app: main.o
	$(CC) $(LDFLAGS) -o $@ $^
main.o: main.c
	$(CC) $(CFLAGS) -c -o $@ $<
raw: main.o
	$(LD) --gc-sections --icf=safe --no-map-whole-files -o raw.elf $^
clean:
	rm -f *.o app raw.elf
