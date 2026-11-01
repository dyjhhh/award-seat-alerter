.PHONY: test demo scan
test:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
	PYTHONDONTWRITEBYTECODE=1 python3 tools/test_sensitivity_scan.py
demo:
	PYTHONDONTWRITEBYTECODE=1 python3 demo.py
scan:
	bash tools/sensitivity-scan.sh
