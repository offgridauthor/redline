# redline -- `make deps` once, then `make check` (compile, lint, test).

EMACS  ?= emacs
PYTHON ?= python3
DEPS   ?= .deps

# Dependencies (cm-mode, package-lint) install into $(DEPS), never into
# your own Emacs packages.
INIT = --eval "(progn (require 'package) \
  (setq package-user-dir (expand-file-name \"$(DEPS)\")) \
  (add-to-list 'package-archives '(\"melpa\" . \"https://melpa.org/packages/\") t) \
  (package-initialize))"

EL = redline-pane.el redline.el

.PHONY: deps compile lint test test-el test-py check clean

deps:
	$(EMACS) -Q --batch $(INIT) --eval "(progn (package-refresh-contents) \
	  (dolist (p '(cm-mode package-lint)) (unless (package-installed-p p) (package-install p))))"

compile:
	$(EMACS) -Q --batch $(INIT) -L . --eval "(setq byte-compile-error-on-warn t)" \
	  -f batch-byte-compile $(EL)
	rm -f *.elc

lint:
	$(EMACS) -Q --batch $(INIT) -L . --eval "(progn (require 'checkdoc) \
	  (dolist (f '($(patsubst %,\"%\",$(EL)))) \
	    (with-current-buffer (find-file-noselect f) \
	      (let ((checkdoc-autofix-flag 'never)) (checkdoc-current-buffer t)))))"
	$(EMACS) -Q --batch $(INIT) -L . --eval "(progn (require 'package-lint) \
	  (setq package-lint-main-file \"redline.el\"))" \
	  -f package-lint-batch-and-exit $(EL)

test: test-el test-py

test-el:
	$(EMACS) -Q --batch $(INIT) -L . -l test/redline-test.el -f ert-run-tests-batch-and-exit

test-py:
	$(PYTHON) test/test_bridge.py

check: compile lint test

clean:
	rm -f *.elc
