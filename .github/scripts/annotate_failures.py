"""Turns the failed tests of a JUnit report into GitHub annotations (::error), so the reasons show on the run's page
and in its public check-run data, not only in the logs. Without a report (pytest stopped before running tests), the
end of the log given as second argument becomes the annotation."""
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows' console encoding cannot print every character


def escape(text):
    return text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


report = Path(sys.argv[1])
log = Path(sys.argv[2]) if len(sys.argv) > 2 else None
# A crashed or hanging test leaves no report entry with a reason; its stack is only in the log.
if log is not None and log.exists():
    lines = log.read_text(errors="replace").splitlines()
    for start, line in enumerate(lines):
        # pytest-timeout writes "+++ Timeout +++", faulthandler (a hang that holds the GIL) "Timeout (0:05:30)!",
        # the latter right after the progress dots of the same line
        if "Fatal Python error" in line or "Timeout +++" in line or re.search(r"Timeout \(\d+:\d\d:\d\d\)!", line):
            title = "Test process crashed" if "Fatal" in line else "Test hung (timeout)"
            # pytest's and pluggy's own frames say nothing and would push the other threads' stacks out
            stack = [l.strip() for l in lines[start:start + 400]
                     if l.strip() and not re.search(r"[/\\](_pytest|pluggy)[/\\]", l)][:80]
            print(f"::error title={title}::{escape(chr(10).join(stack)[:3500])}")
if not report.exists():
    if log is not None and log.exists():
        tail = [line for line in log.read_text(errors="replace").splitlines() if line.strip()][-30:]
        print(f"::error title=pytest stopped before running the tests::{escape(chr(10).join(tail)[-3000:])}")
    sys.exit(0)
count = 0
for case in ET.parse(report).iter("testcase"):
    for problem in list(case.findall("failure")) + list(case.findall("error")):
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        text = (problem.get("message") or "") + "\n" + (problem.text or "")
        lines = [line for line in text.strip().splitlines() if line.strip()]
        detail = " | ".join(lines[-12:])[:1500]   # the end of a traceback says what went wrong
        print(f"::error title={name[:200]}::{escape(detail)}")
        count += 1
        if count >= 40:   # GitHub keeps at most 50 annotations per step
            sys.exit(0)
