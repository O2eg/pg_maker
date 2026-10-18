.PHONY: all install check
all: probe-tool
probe-tool: main.c
	$(CC) -o $@ $<
install: all
	mkdir -p $(prefix)/bin
	cp probe-tool $(prefix)/bin/
check: all
	./probe-tool
