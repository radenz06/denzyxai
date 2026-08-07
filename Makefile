PY      := python3
PWD     := $(shell pwd)

.PHONY: run install test check backup help

help:
	@echo "denzyz AI — target:"
	@echo "  run       buka TUI"
	@echo "  install   jalankan install.sh (shortcut shell + izin)"
	@echo "  test      jalankan pytest"
	@echo "  check     kompilasi + import check semua file inti"
	@echo "  backup    arsip sesi & config ke storage"

run:
	$(PY) denzyx.py

install:
	./install.sh

test:
	$(PY) -m pytest -q tests/

check:
	$(PY) -m py_compile denzyx.py dscli.py auto-denz.py
	$(PY) -c "import sys; sys.path.insert(0,'.'); import dscli; print('tools:', len(dscli.TOOLS))"

backup:
	./scripts/backup.sh
