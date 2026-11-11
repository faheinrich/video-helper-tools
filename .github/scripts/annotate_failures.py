"""Turns the failed tests of a JUnit report into GitHub annotations (::error), so the reasons show on the run's page
and in its public check-run data, not only in the logs. Without a report (pytest stopped before running tests), the
end of the log given as second argument becomes the annotation."""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

def escape(text):
    return text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


report = Path(sys.argv[1])
log = Path(sys.argv[2]) if len(sys.argv) > 2 else None
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
