# Full pytest remains explicit; docs routing never changes its selection.
PYTHON ?= python3
BASE ?= origin/v8
VALIDATION_ENVIRONMENT ?=
VALIDATION_ENVIRONMENT_ARG = $(if $(VALIDATION_ENVIRONMENT),--validation-environment "$(VALIDATION_ENVIRONMENT)",)
VALIDATION_PYTHON = $(PYTHON) -I -S -B -c 'import os,runpy,sys; sys.path.insert(0,os.getcwd()); runpy.run_module("tools.validation_route",run_name="__main__")'

.PHONY: fast-feedback docs-check review-ready import-cleanup-check
fast-feedback:
	$(VALIDATION_PYTHON) --base "$(BASE)" $(VALIDATION_ENVIRONMENT_ARG)

docs-check:
	$(VALIDATION_PYTHON) --docs-check --base "$(BASE)"

review-ready:
	$(VALIDATION_PYTHON) --full-checks --base "$(BASE)" $(VALIDATION_ENVIRONMENT_ARG)

# Explicit local pilot; never selected by automatic or hosted routing.
import-cleanup-check:
	$(PYTHON) -I -S -B -c 'import os,runpy,sys; sys.path.insert(0,os.getcwd()); runpy.run_module("tools.import_cleanup_gate",run_name="__main__")' --approval "$(APPROVAL)" --approval-sha256 "$(APPROVAL_SHA256)"
