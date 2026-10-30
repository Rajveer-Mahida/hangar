# Convenience targets. Everything also works through the scripts directly.
.PHONY: run test deps deb install uninstall clean

run:
	./run.sh

test:
	python3 -m pytest -q tests

deps:
	./scripts/install-deps.sh --with-optional

deb:
	./scripts/build-deb.sh

install:
	sudo ./scripts/install.sh $(PREFIX)

uninstall:
	sudo ./scripts/uninstall.sh $(PREFIX)

clean:
	rm -rf dist build .pytest_cache
	find . -name __pycache__ -prune -exec rm -rf {} +
