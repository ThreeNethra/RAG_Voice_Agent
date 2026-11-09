"""Run every test file.   python tests/run_all.py"""
import subprocess
import sys
from pathlib import Path

here = Path(__file__).parent
bad = []
for f in sorted(here.glob("test_*.py")):
    r = subprocess.run([sys.executable, str(f)], cwd=here, capture_output=True, text=True, timeout=600)
    tail = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
    print(f"{f.name:<20} {'ok' if r.returncode == 0 else 'FAILED'}   {tail}")
    if r.returncode:
        bad.append(f.name)
        print(r.stdout[-1500:], r.stderr[-800:])
print("\nALL TESTS PASSED" if not bad else f"\nFAILED: {bad}")
sys.exit(1 if bad else 0)
