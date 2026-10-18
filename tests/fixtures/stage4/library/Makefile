.PHONY: all install check
all: libprobe.so
libprobe.so: probe.c
	$(CC) -shared -fPIC -o $@ $<
install: all
	mkdir -p $(prefix)/lib
	cp libprobe.so $(prefix)/lib/
check: all
	$(CC) -o check-probe check.c -L. -lprobe
	LD_LIBRARY_PATH=. ./check-probe
