import ast
import os
import subprocess
import sys
import xml.etree.ElementTree as ET


def _ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def _load_test_metadata():
    descriptions = {}
    categories = {}
    test_file = os.path.join("tests", "test_function_app.py")
    if not os.path.exists(test_file):
        return descriptions, categories
    with open(test_file, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=test_file)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            doc = ast.get_docstring(node)
            if doc:
                first_line = doc.splitlines()[0].strip()
                if first_line.startswith("Category:"):
                    parts = first_line.split("|", 1)
                    category = parts[0].replace("Category:", "").strip()
                    description = parts[1].strip() if len(parts) > 1 else node.name
                else:
                    category = "General"
                    description = first_line
                descriptions[node.name] = description
                categories[node.name] = category
    return descriptions, categories


def _parse_junit(xml_path, descriptions, categories):
    tree = ET.parse(xml_path)
    root = tree.getroot()
    testcases = []

    for testcase in root.iter("testcase"):
        name = testcase.get("name", "unknown")
        full_name = descriptions.get(name, name)
        category = categories.get(name, "General")
        status = "Passed"
        details = ""

        failure = testcase.find("failure")
        error = testcase.find("error")
        skipped = testcase.find("skipped")
        if failure is not None:
            status = "Failed"
            details = (failure.get("message") or "").strip()
        elif error is not None:
            status = "Failed"
            details = (error.get("message") or "").strip()
        elif skipped is not None:
            status = "Skipped"
            details = (skipped.get("message") or "").strip()

        testcases.append((category, full_name, status, details))

    return testcases


def _render_markdown_table(testcases):
    lines = [
        "| Category | Test Case | Status | Details |",
        "| --- | --- | --- | --- |",
    ]
    for category, name, status, details in testcases:
        safe_details = details.replace("\n", " ").strip()
        lines.append(f"| {category} | {name} | {status} | {safe_details} |")
    return "\n".join(lines)


def main():
    results_dir = os.path.join("test-results")
    _ensure_dir(results_dir)
    junit_path = os.path.join(results_dir, "junit.xml")
    report_path = os.path.join(results_dir, "report.md")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--junitxml", junit_path],
        text=True,
    )

    if not os.path.exists(junit_path):
        print("JUnit report not generated.", file=sys.stderr)
        return result.returncode

    descriptions, categories = _load_test_metadata()
    testcases = _parse_junit(junit_path, descriptions, categories)
    markdown = _render_markdown_table(testcases)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(markdown + "\n")

    print(markdown)
    print(f"\nReport saved to {report_path}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
