"""Catch missing imported names without starting Telegram polling or using secrets."""
import ast
import importlib
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent


class StartupImportTests(unittest.TestCase):
    def test_real_main_import_with_installed_dependencies(self):
        """Catch ImportError that compileall and AST-only tests cannot detect."""
        module = importlib.import_module('main')
        self.assertTrue(callable(module.main))

    def test_price07_ui_imports_exist(self):
        main = ast.parse((ROOT / 'main.py').read_text(encoding='utf-8'))
        ui = ast.parse((ROOT / 'price07_ui.py').read_text(encoding='utf-8'))
        exports = {node.name for node in ui.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        exports.update(name for node in ui.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                       for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                       for name in ([target.id] if isinstance(target, ast.Name) else []))
        exports.update(alias.asname or alias.name for node in ui.body if isinstance(node, ast.ImportFrom)
                       for alias in node.names)
        requested = {alias.name for node in main.body if isinstance(node, ast.ImportFrom)
                     and node.module == 'price07_ui' for alias in node.names}
        self.assertTrue(requested <= exports, f'Missing price07_ui imports: {requested - exports}')
        self.assertEqual({'router'}, requested)
