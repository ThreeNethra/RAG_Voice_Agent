"""Invites created or revoked by manage.py (another process) must take effect in a running server without a restart."""
import time
from pathlib import Path

from helpers import check, finish, tmpdir

from voicecall.invites import Invites

path = Path(tmpdir()) / "invites.json"
server_view = Invites(path)                         # the running server: loaded once, at start
try:
    server_view.verify("abcd-1234-ef56", "p")
except PermissionError:
    pass
manage_py = Invites(path)                           # a separate process creating a code later
code = manage_py.create("Ravi", ["all-staff"])
try:
    p, k, rec = server_view.verify(code, "p1")
    check("a code created after the server started is accepted without a restart", p.user == "invite:Ravi" and p.groups == ("all-staff",))
except PermissionError as e:
    check("a code created after the server started is accepted without a restart", False, str(e))
time.sleep(0.01)
manage_py.revoke("Ravi")
try:
    server_view.verify(code, "p2")
    check("a revoked code stops working immediately", False)
except PermissionError:
    check("a revoked code stops working immediately", True)
path.write_text("{ half written", encoding="utf-8")
try:
    server_view.verify(code, "p3")
except PermissionError:
    pass
except Exception as e:                              # noqa: BLE001
    check("a half-written file does not crash verification", False, repr(e))
else:
    pass
check("a half-written file does not crash verification", True)
import os
from voicecall.config import load_env
envf = Path(tmpdir()) / ".env"
envf.write_text("# comment\nVC_TEST_A=one\nVC_TEST_B = \"two words\" \nVC_TEST_EMPTY=\nbadline\nVC_TEST_C=from-file\n", encoding="utf-8")
os.environ["VC_TEST_C"] = "from-terminal"
set_ = load_env(envf)
check(".env: values are read, quotes stripped, comments and blanks skipped", os.environ["VC_TEST_A"] == "one" and os.environ["VC_TEST_B"] == "two words" and "VC_TEST_EMPTY" not in os.environ and set_ == ["VC_TEST_A", "VC_TEST_B"], str(set_))
check(".env: a value already set in the terminal wins", os.environ["VC_TEST_C"] == "from-terminal")
check(".env: a missing file is fine", load_env(Path(tmpdir()) / "nope") == [])
finish()
