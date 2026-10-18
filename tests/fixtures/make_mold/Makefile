CC = clang
LDFLAGS = -Wl,--fork -Wl,--perf -Wl,--quick-exit -Wl,-z,now
app: main.o
	mold -run $(CC) $(LDFLAGS) -o $@ $^
main.o: main.c
	$(CC) -c -o $@ $<
