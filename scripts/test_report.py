"""Turn a pytest JUnit XML file into a Markdown summary (for the GitHub Actions job summary)."""
import sys
import xml.etree.ElementTree as ET  # noqa: S405 (parsing our own CI output)
from collections import defaultdict


def main(path: str) -> int:
    root = ET.parse(path).getroot()  # noqa: S314
    by_file: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for case in root.iter("testcase"):
        module = case.get("classname", "").split(".")[-1]
        state = "failed" if case.find("failure") is not None or case.find("error") is not None else (
            "skipped" if case.find("skipped") is not None else "passed")
        by_file[module].append((case.get("name", ""), state, float(case.get("time", 0))))

    total = sum(len(v) for v in by_file.values())
    failed = sum(1 for v in by_file.values() for _, s, _ in v if s == "failed")
    icon = {"passed": "✅", "failed": "❌", "skipped": "⏭️"}
    out = [f"## Tests: {total - failed} passed" + (f", {failed} failed" if failed else ""), "",
           "| Area | Tests | Passed | Time |", "|---|---|---|---|"]
    for module, cases in sorted(by_file.items()):
        passed = sum(1 for _, s, _ in cases if s == "passed")
        out.append(f"| {module.removeprefix('test_').replace('_', ' ')} | {len(cases)} | {passed} | "
                   f"{sum(t for *_, t in cases):.2f}s |")
    for module, cases in sorted(by_file.items()):
        out += ["", f"<details><summary>{module} ({len(cases)})</summary>", ""]
        out += [f"- {icon[s]} {name}" for name, s, _ in cases]
        out += ["", "</details>"]
    print("\n".join(out))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
