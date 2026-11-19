"""Check boundaries that also apply when adding a new event feature."""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLICATION_SUFFIXES = {".py", ".js", ".mjs", ".ts", ".html", ".css", ".sqlite", ".sqlite3"}


def errors():
    for source in sorted(ROOT.iterdir()):
        if source.is_file() and source.suffix in APPLICATION_SUFFIXES:
            yield f"{source.name} belongs in the frontend, backend, tests, or tools directory"
    for source in sorted((ROOT / "backend" / "gotc").rglob("*.py")):
        if source.name.startswith("test_"):
            yield f"Move {source.relative_to(ROOT)} to tests/backend"
        for statement in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(statement, ast.Import):
                modules = [item.name for item in statement.names]
            elif isinstance(statement, ast.ImportFrom) and statement.level == 0:
                modules = [statement.module or ""]
            else:
                continue
            for module in modules:
                if module.split(".")[0] in {"tests", "tools", "ops"}:
                    yield f"{source.relative_to(ROOT)}:{statement.lineno} imports developer-only module {module}"
    for name in ("index.html", "platform.html", "teams.html"):
        if not (ROOT / "frontend" / name).is_file():
            yield f"The event URL for {name} needs an entry page under frontend/"


if __name__ == "__main__":
    problems = list(errors())
    if problems:
        sys.exit("\n".join(problems))
    print("Application layout and backend dependency boundaries passed.")
