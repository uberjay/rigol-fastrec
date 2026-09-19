"""Shared AFG control, failure reporting and reproducible-run metadata."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess

from rigol_fastrec import WaveRecorder, __version__
from rigol_fastrec.agent import load_agent_source


class Validator:
    """Tiny PASS/FAIL harness: each check prints a line; exit code reflects
    whether any failed. A raised exception inside a check is itself a FAIL, so
    one broken step doesn't abort the rest of the run."""

    def __init__(self, output_dir=None, configuration=None) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.output_dir = output_dir
        self.report = dict(started_utc=datetime.now(timezone.utc).isoformat(),
                           configuration=configuration or {}, checks=[])
        self.save_report()

    def save_report(self):
        self.report.update(passed=self.passed, failed=self.failed, skipped=self.skipped)
        if self.output_dir is not None:
            (self.output_dir / 'report.json').write_text(json.dumps(self.report, indent=2)+'\n')

    def skip(self, name, detail):
        self.skipped += 1
        self.report['checks'].append(dict(name=name, status='SKIP', detail=detail))
        print(f'  [SKIP] {name} -- {detail}', flush=True)
        self.save_report()

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        ok = bool(ok)
        tag = "PASS" if ok else "FAIL"
        self.passed += ok
        self.failed += not ok
        print(f"  [{tag}] {name}" + (f" -- {detail}" if detail else ""), flush=True)
        self.report['checks'].append(dict(name=name, status=tag, detail=detail))
        self.save_report()
        return ok

    def run(self, name: str, fn) -> bool:
        """Run a check function that returns (ok, detail); trap exceptions."""
        try:
            ok, detail = fn()
        except Exception as e:  # a throwing check is a failure, not a crash
            return self.check(name, False, f"raised {type(e).__name__}: {e}")
        return self.check(name, ok, detail)

    def summary(self) -> int:
        total = self.passed + self.failed
        print(f"\n{self.passed}/{total} checks passed"
              + (f", {self.failed} FAILED" if self.failed else " -- all good")
              + (f", {self.skipped} skipped" if self.skipped else ""))
        self.report['finished_utc'] = datetime.now(timezone.utc).isoformat()
        self.save_report()
        return 1 if self.failed else 0


@contextmanager
def validation_session(v):
    try:
        yield
    except (Exception, KeyboardInterrupt) as exc:
        v.check('validation session completed', False, f'{type(exc).__name__}: {exc}')
    finally:
        v.save_report()


@contextmanager
def managed_afg(rec, v, sources):
    """Turn off every AFG this run owns, even when capture raises or is interrupted."""
    try:
        yield
    finally:
        for prefix, _, _ in sources:
            def shutdown(prefix=prefix):
                rec.scpi.write(f'{prefix}:OUTPut:STATe OFF')
                state = rec.scpi.query(f'{prefix}:OUTPut:STATe?')
                return state in ('0', 'OFF'), f'{prefix} output={state}'
            v.run(f'{prefix} output disabled on exit', shutdown)


# --- built-in AFG control (SCPI) ---------------------------------------------
# The exact mnemonics vary by model/option. These are the standard Rigol AFG
# forms; confirm against your scope on the first `-vv` run (they log at DEBUG),
# and override --afg-prefix / the output command if needed.

def afg_drain_errors(rec: WaveRecorder, limit: int = 20) -> None:
    """Empty the SCPI error queue so stale errors don't confuse afg_setup."""
    for _ in range(limit):
        if rec.scpi.query(":SYSTem:ERRor?").lstrip().startswith("0"):
            break


def afg_setup(rec: WaveRecorder, *, prefix: str, freq: float, vpp: float,
              offset: float, wave: str = "SIN", impedance: float = 1e6) -> list[tuple[str, str]]:
    """Program the built-in AFG. Returns [(command, scope-error), …] for any
    command the scope rejected -- so a wrong mnemonic is reported precisely
    instead of silently producing no signal. Mnemonics vary by model; if one
    errors, check the programming guide and adjust here / pass --afg-prefix."""
    # Headers per the DHO800/900 (== MHO900) programming guide, :SOURce subsystem.
    cmds = [
        f"{prefix}:OUTPut:STATe OFF",
        f"{prefix}:IMPedance {'FIFTy' if impedance == 50 else 'OMEG'}",
        f"{prefix}:MOD:STATe OFF",
        f"{prefix}:FUNCtion {wave}",
        f"{prefix}:FREQuency {freq:g}",
        f"{prefix}:VOLTage:AMPLitude {vpp:g}",  # peak-to-peak
        f"{prefix}:VOLTage:OFFSet {offset:g}",
        f"{prefix}:OUTPut:STATe ON",            # output enable
    ]
    afg_drain_errors(rec)
    errs = []
    for c in cmds:
        rec.scpi.write(c)
        e = rec.scpi.query(":SYSTem:ERRor?")
        if not e.lstrip().startswith("0"):      # 0,"No error" → fine
            errs.append((c, e))
    return errs


def scpi_errors(rec):
    errors = []
    for _ in range(20):
        error = rec.scpi.query(':SYSTem:ERRor?')
        if error.lstrip().startswith('0'):
            return errors
        errors.append(error)
    raise RuntimeError(f'SCPI error queue did not drain: {errors}')


def evidence_metadata(args) -> dict:
    """Identify the exact bundle and diagnostic sources used by this checkout.

    Hashes include uncommitted edits. No scope queries are issued here.
    """
    tools_dir = Path(__file__).resolve().parents[1]
    sources = {str(p.relative_to(tools_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(tools_dir.rglob('*')) if p.suffix in ('.py', '.js')}
    try:
        result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=tools_dir.parent,
                                capture_output=True, text=True, timeout=2, check=True)
        revision = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        revision = None
    config = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}
    return dict(configuration=config, provenance=dict(
        package_version=__version__, python_version=platform.python_version(),
        revision=revision,
        agent_sha256=hashlib.sha256(load_agent_source().encode('utf-8')).hexdigest(),
        tool_sources_sha256=sources))
