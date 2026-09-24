"""Compile the exact installed production declaration table against local headers."""
import importlib.util
from pathlib import Path
import sys
from agent_desktop import libei_binding as production

root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('m1_audit', root / 'tools/libei_binding.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
# Reuse the finite compiler driver, not its independent feasibility declarations.
for name in ('TYPES', 'DECLARATIONS', 'CONSTANTS', 'load', '__file__'):
    setattr(audit, name, getattr(production, name))
audit.audit(sys.argv[1])
