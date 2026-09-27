PYTHON := .venv312/bin/python
SERVICE := wamf.service

.PHONY: help start stop restart status logs test check

help:
	@printf '%s\n' \
		'WAMF developer targets:' \
		'  make help     Show these targets.' \
		'  make start    Start the WAMF systemd service.' \
		'  make stop     Stop the WAMF systemd service.' \
		'  make restart  Restart the WAMF systemd service.' \
		'  make status   Show WAMF systemd service status.' \
		'  make logs     Follow the WAMF systemd journal.' \
		'  make test     Run the complete Python test suite.' \
		'  make check    Run tests, syntax checks, and diff checks.'

start:
	sudo systemctl start $(SERVICE)

stop:
	sudo systemctl stop $(SERVICE)

restart:
	sudo systemctl restart $(SERVICE)

status:
	systemctl status $(SERVICE) --no-pager

logs:
	journalctl --unit=$(SERVICE) --follow --full --no-pager --output=cat

test:
	$(PYTHON) -m pytest tests/ -v

check: test
	$(PYTHON) -m compileall -q \
		app integrations routes tests \
		retention.py speciesid.py version.py wamf_paths.py webui.py
	git diff --check
