VENV ?= .venv

.PHONY: test
test:
	$(VENV)/bin/python -m pytest $(ARGS)
