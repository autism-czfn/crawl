"""Guard against a real bug found while running the 0019/0020 migrations
against the live dev DB: alembic_version.version_num is VARCHAR(32), so a
revision id longer than 32 chars fails at the very end of `alembic upgrade`
with StringDataRightTruncationError — after all the migration's DDL has
already executed, forcing a full rollback of the whole upgrade run. Purely
static analysis (this repo has no test infra to run `alembic upgrade`
against a real DB in CI) would never have caught this; only actually running
it did. This test at least stops a future migration from repeating it.
"""
import ast
from pathlib import Path

_VERSIONS_DIR = Path(__file__).parent.parent / "src" / "storage" / "migrations" / "versions"
_MAX_LEN = 32  # alembic_version.version_num column width in this DB


def _module_level_str_assignments(tree: ast.Module) -> dict[str, str]:
    values: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                values[name] = node.value.value
    return values


def test_all_revision_ids_fit_in_alembic_version_column():
    too_long = []
    for path in sorted(_VERSIONS_DIR.glob("*.py")):
        if path.name == "__pycache__":
            continue
        tree = ast.parse(path.read_text())
        values = _module_level_str_assignments(tree)
        for field in ("revision", "down_revision"):
            value = values.get(field)
            if value and len(value) > _MAX_LEN:
                too_long.append((path.name, field, value, len(value)))
    assert not too_long, (
        f"revision id(s) exceed alembic_version.version_num's VARCHAR({_MAX_LEN}): {too_long}"
    )
