.PHONY: install check

check:
	sh -n install.sh
	python3 -c "import ast, pathlib; [ast.parse(p.read_text(), filename=str(p)) for p in pathlib.Path('.').glob('*.py')]"
	python3 -B 42usb.py --help
	python3 -B login_check.py --help
	sh install.sh --help

install:
	sh install.sh
