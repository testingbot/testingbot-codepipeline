"""Render a TestingBot build as JUnit XML, the format CodeBuild report groups
read (`reports:` → `file-format: JUNITXML`)."""

import os
import xml.etree.ElementTree as ET
from collections import OrderedDict

from .api import browser_label, classify, test_url


def render(identifier, tests):
    # One <testsuite> per browser/OS combination, so the CodeBuild report
    # shows which platform a failure happened on without opening the test.
    suites = OrderedDict()
    for t in tests:
        suites.setdefault(browser_label(t), []).append(t)

    root = ET.Element("testsuites", name=f"TestingBot {identifier}")
    totals = {"tests": 0, "failures": 0, "skipped": 0, "time": 0}

    for label, members in suites.items():
        suite = ET.SubElement(root, "testsuite", name=label)
        counts = {"tests": 0, "failures": 0, "skipped": 0, "time": 0}
        for t in members:
            kind = classify(t)
            duration = int(t.get("duration") or 0)
            case = ET.SubElement(suite, "testcase",
                                 name=str(t.get("name") or t.get("session_id") or f"Test {t.get('id')}"),
                                 classname=label, time=str(duration))
            if kind == "failed":
                message = t.get("status_message") or "The test failed on TestingBot."
                failure = ET.SubElement(case, "failure", message=str(message), type="TestingBot")
                failure.text = f"{message}\n{test_url(t)}"
                counts["failures"] += 1
            elif kind == "unknown":
                ET.SubElement(case, "skipped", message="No pass/fail status was reported to TestingBot for this test.")
                counts["skipped"] += 1
            elif kind == "running":
                ET.SubElement(case, "skipped", message="The test was still running on TestingBot when this report was written.")
                counts["skipped"] += 1
            out = [f"TestingBot: {test_url(t)}"]
            if t.get("video"):
                out.append(f"Video: {t['video']}")
            ET.SubElement(case, "system-out").text = "\n".join(out)
            counts["tests"] += 1
            counts["time"] += duration
        for k, v in counts.items():
            suite.set(k, str(v))
            totals[k] += v

    for k, v in totals.items():
        root.set(k, str(v))
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def write(path, identifier, tests):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(identifier, tests))
