#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Carbon Lint — static analysis for high-emission code patterns.

Assigns estimated CO₂ costs to anti-patterns so sustainability tradeoffs
are visible in the normal dev workflow: editor, pre-commit, and CI.

Usage:
  python carbon_lint.py src/                  # scan a directory
  python carbon_lint.py app.py --format json  # JSON output
  python carbon_lint.py . --format github     # GitHub Actions annotations
"""

import ast
import sys
import json
import argparse
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List

# Models in this set are flagged as large — task complexity should justify their use
LARGE_MODELS = {
    "gpt-4", "gpt-4-turbo", "gpt-4o", "gpt-4-32k", "gpt-4-turbo-preview",
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-3-opus-20240229", "claude-3-5-sonnet-20241022",
    "gemini-ultra", "gemini-1.5-pro", "gemini-2.0-pro",
    "llama-3.1-405b", "llama-3.3-70b-instruct",
    "mistral-large", "mistral-large-2",
    "command-r-plus", "command-a-03-2025",
}

RULES = {
    "POLL-001": {
        "message": "Fixed-interval polling loop. Use event-driven or exponential backoff instead.",
        "co2": "~200–800 g CO₂/hour extra vs. an event-driven equivalent",
        "severity": "warning",
        "hint": "Replace time.sleep(N) in a while-True loop with a webhook, queue, or backoff (e.g. time.sleep(min(2**attempt, 60))).",
    },
    "THREAD-001": {
        "message": "ThreadPoolExecutor without max_workers. Unbounded parallelism wastes CPU.",
        "co2": "Scales to all available cores — cap with max_workers to bound peak compute",
        "severity": "warning",
        "hint": "Set max_workers=min(32, os.cpu_count() + 4) or a domain-appropriate limit.",
    },
    "DATA-001": {
        "message": "pd.read_csv() without usecols — full dataset loaded into memory.",
        "co2": "2–10× more memory and I/O than loading only the columns you need",
        "severity": "info",
        "hint": "Add usecols=['col_a', 'col_b'] to skip unused columns at parse time.",
    },
    "MODEL-001": {
        "message": "Large LLM selected. Verify the task justifies the model size.",
        "co2": "GPT-4 / Opus-class models use ~10–50× more energy per token than smaller alternatives",
        "severity": "warning",
        "hint": "For classification, extraction, short Q&A, or structured output try gpt-4o-mini, claude-haiku-4-5, or mistral-small first.",
    },
    "MODEL-002": {
        "message": "LLM call without stream=True — full response buffered server-side before delivery.",
        "co2": "Same token count, but non-streaming keeps the KV cache alive longer on the server",
        "severity": "info",
        "hint": "Add stream=True and consume the response as a generator to reduce server memory pressure.",
    },
    "CACHE-001": {
        "message": "HTTP GET inside a loop — N requests where 1 (cached) might suffice.",
        "co2": "Each redundant round-trip burns compute and network on both client and server",
        "severity": "warning",
        "hint": "Hoist the request outside the loop, batch IDs into a single call, or use requests_cache / functools.lru_cache.",
    },
}


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    co2: str
    hint: str
    file: str
    line: int
    col: int


class CarbonLintVisitor(ast.NodeVisitor):
    def __init__(self, filename: str):
        self.filename = filename
        self.findings: List[Finding] = []
        self._loop_depth = 0

    def _flag(self, node: ast.AST, rule: str):
        r = RULES[rule]
        self.findings.append(Finding(
            rule=rule,
            severity=r["severity"],
            message=r["message"],
            co2=r["co2"],
            hint=r["hint"],
            file=self.filename,
            line=node.lineno,
            col=node.col_offset,
        ))

    def visit_While(self, node: ast.While):
        is_infinite = (
            isinstance(node.test, ast.Constant) and node.test.value is True
        )
        if is_infinite:
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "sleep"
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "time"
                    and child.args
                    and isinstance(child.args[0], ast.Constant)  # fixed, not computed backoff
                ):
                    self._flag(node, "POLL-001")
                    break
        self._loop_depth += 1
        self.generic_visit(node)
        self._loop_depth -= 1

    def visit_For(self, node: ast.For):
        self._loop_depth += 1
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in ("get", "post", "put", "patch", "delete")
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "requests"
                ):
                    self._flag(child, "CACHE-001")
                    break
        self.generic_visit(node)
        self._loop_depth -= 1

    def visit_Call(self, node: ast.Call):
        func = node.func

        # ThreadPoolExecutor without max_workers
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name == "ThreadPoolExecutor":
            has_max = any(kw.arg == "max_workers" for kw in node.keywords)
            if not has_max and not node.args:
                self._flag(node, "THREAD-001")

        # pd.read_csv without usecols
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "read_csv"
            and isinstance(func.value, ast.Name)
            and func.value.id in ("pd", "pandas")
        ):
            if not any(kw.arg == "usecols" for kw in node.keywords):
                self._flag(node, "DATA-001")

        # LLM API calls
        self._check_llm_call(node)

        self.generic_visit(node)

    def _check_llm_call(self, node: ast.Call):
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in ("create", "generate", "complete", "invoke")):
            return

        model_kw = next((kw for kw in node.keywords if kw.arg == "model"), None)
        if model_kw and isinstance(model_kw.value, ast.Constant):
            if str(model_kw.value.value).lower() in LARGE_MODELS:
                self._flag(node, "MODEL-001")

        has_stream = any(
            kw.arg == "stream" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )
        if model_kw and not has_stream:
            self._flag(node, "MODEL-002")


def lint_file(path: Path) -> List[Finding]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []
    visitor = CarbonLintVisitor(str(path))
    visitor.visit(tree)
    return visitor.findings


def format_text(findings: List[Finding]) -> str:
    if not findings:
        return "No carbon anti-patterns found."
    lines = []
    for f in findings:
        icon = "!" if f.severity == "warning" else "i"
        lines.append(f"{f.file}:{f.line}:{f.col}: [{f.rule}] ({icon}) {f.message}")
        lines.append(f"  CO2 impact : {f.co2}")
        lines.append(f"  Fix        : {f.hint}")
        lines.append("")
    summary = f"{len(findings)} finding(s) — {sum(1 for f in findings if f.severity == 'warning')} warning(s), {sum(1 for f in findings if f.severity == 'info')} info"
    lines.append(summary)
    return "\n".join(lines)


def format_github(findings: List[Finding]) -> str:
    lines = []
    for f in findings:
        level = "warning" if f.severity == "warning" else "notice"
        lines.append(
            f"::{level} file={f.file},line={f.line},col={f.col},title=Carbon Lint [{f.rule}]::{f.message} | CO2: {f.co2}"
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Carbon Lint — flag high-emission code patterns with CO2 estimates"
    )
    parser.add_argument("paths", nargs="+", help="Python files or directories to scan")
    parser.add_argument(
        "--format", choices=["text", "json", "github"], default="text",
        help="Output format (text, json, or github for CI annotations)"
    )
    parser.add_argument(
        "--rules", nargs="*",
        help="Only run specific rules, e.g. --rules MODEL-001 POLL-001"
    )
    parser.add_argument(
        "--exit-zero", action="store_true",
        help="Always exit 0 — useful when running as a reporting-only step in CI"
    )
    args = parser.parse_args()

    all_findings: List[Finding] = []
    for raw_path in args.paths:
        p = Path(raw_path)
        if p.is_dir():
            for py_file in sorted(p.rglob("*.py")):
                all_findings.extend(lint_file(py_file))
        elif p.suffix == ".py" and p.exists():
            all_findings.extend(lint_file(p))

    if args.rules:
        all_findings = [f for f in all_findings if f.rule in args.rules]

    if args.format == "json":
        print(json.dumps([asdict(f) for f in all_findings], indent=2))
    elif args.format == "github":
        print(format_github(all_findings))
    else:
        print(format_text(all_findings))

    if args.exit_zero:
        sys.exit(0)

    has_warnings = any(f.severity == "warning" for f in all_findings)
    sys.exit(1 if has_warnings else 0)


if __name__ == "__main__":
    main()
