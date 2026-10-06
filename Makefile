.PHONY: test install check

test:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

check:
	sh -n install.sh
	python3 -c "import ast; ast.parse(open('42usb.py').read())"

install:
	sh install.sh
