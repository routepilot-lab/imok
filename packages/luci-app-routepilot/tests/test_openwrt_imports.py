"""Import router code in a fresh interpreter with OpenWrt's reduced stdlib."""

from pathlib import Path
import subprocess
import sys
import unittest


IMPORT_CHECK = """
import ast
import builtins
import importlib
from pathlib import Path
import sys

root = Path(sys.argv[1])
library = root / "lib/routepilot"
sys.path.insert(0, str(library))
blocked = {
    "decimal", "statistics", "asyncio", "multiprocessing", "sqlite3",
    "xml", "email", "unittest", "distutils", "lzma",
}
original_import = builtins.__import__

def reduced_import(name, globals=None, locals=None, fromlist=(), level=0):
    top = name.split(".")[0]
    caller = (globals or {}).get("__name__", "").split(".")[0]
    # HTTP's header parser is part of the declared urllib dependency.
    if not level and top in blocked and not (
        top == "email" and caller in {"urllib", "http", "email"}
    ):
        raise ModuleNotFoundError(f"{caller} requires unavailable {name}", name=name)
    return original_import(name, globals, locals, fromlist, level)

def audit(event, args):
    if event in {"socket.connect", "socket.bind", "subprocess.Popen", "os.system"}:
        raise AssertionError("Router operation attempted while checking imports: " + event)

sys.addaudithook(audit)
builtins.__import__ = reduced_import
modules = sorted(library.glob("*.py"))
entries = sorted(p for p in (root / "libexec").iterdir() if p.is_file())
for source in [*modules, *entries]:
    tree = ast.parse(source.read_text(), filename=str(source))
    # Check deferred imports too. Entry points dispatch at top level, so
    # execute only their import statements, including conditional ones.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"),
                 {"__name__": source.stem})
    if source in modules:
        importlib.import_module(source.stem)
print(len(modules), len(entries))
"""


class OpenWrtImportsTest(unittest.TestCase):
    def test_router_modules_and_entrypoint_imports_fit_thin_python(self):
        root = Path(__file__).resolve().parents[1] / "root/usr"
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", IMPORT_CHECK, str(root)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        modules, entries = map(int, result.stdout.split())
        self.assertEqual(modules, len(list((root / "lib/routepilot").glob("*.py"))))
        self.assertEqual(entries, len([p for p in (root / "libexec").iterdir() if p.is_file()]))
