# rigol-fastrec — build + check entry points.

.PHONY: build check check-agent check-python neon-blob clean

## (re)build the agent bundle — frida-compile writes python/rigol_fastrec/_agent.js
build:
	cd agent && npm install && npm run build

## offline tests (no scope needed): agent typecheck/tests + Python tests
check: check-agent check-python

check-agent:
	cd agent && npm run check && npm test

check-python:
	python -m pip install -e '.[dev]' -q && python -m pytest -q

## regenerate the NEON accumulator blob from accum.S (needs Docker)
neon-blob:
	agent/src/native/accum/regen_blob.sh

clean:
	rm -rf agent/dist agent/node_modules
	find python -name __pycache__ -type d -prune -exec rm -rf {} +
