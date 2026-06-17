from __future__ import annotations

import os
import json
import shlex
import subprocess
import re
import tomllib
from pathlib import Path
from typing import Any


RELEVANT_SUFFIXES = {".json", ".html", ".css", ".js", ".md", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".py", ".rs", ".toml", ".yaml", ".yml"}
IGNORED_DIR_PARTS = {".git", ".aca-worktrees", "node_modules", "dist", "build", "coverage", "target", ".venv", "__pycache__"}
ROOT_FALLBACK_FILES = [
    "index.html",
    "index.htm",
    "package.json",
    "styles.css",
    "README.md",
    "readme.md",
    "README.markdown",
    "ACA_SMOKE_TEST.md",
    "src/main.tsx",
    "src/main.ts",
    "src/main.jsx",
    "src/main.js",
    "src/index.tsx",
    "src/index.ts",
    "src/index.jsx",
    "src/index.js",
    "src/app.tsx",
    "src/app.ts",
    "src/app.jsx",
    "src/app.js",
    "src/App.tsx",
    "src/App.ts",
    "src/App.jsx",
    "src/App.js",
]
CONTENT_SATISFACTION_MARKERS = {
    "localstorage",
    "createdat",
    "created_at",
    "due-date-input",
    "due-date",
    "edit-modal",
    "filter-btn",
    "isoverdue",
    "isduetoday",
    "todo-item",
    "todo-list",
    "rendertodos",
}
TASK_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "be",
    "before",
    "by",
    "can",
    "completed",
    "controls",
    "correctly",
    "creating",
    "date",
    "dates",
    "edit",
    "existing",
    "filter",
    "filters",
    "highlighting",
    "for",
    "from",
    "have",
    "has",
    "in",
    "into",
    "is",
    "it",
    "its",
    "todo",
    "todos",
    "of",
    "on",
    "or",
    "overdue",
    "optional",
    "our",
    "remove",
    "set",
    "task",
    "tasks",
    "the",
    "their",
    "this",
    "those",
    "to",
    "when",
    "with",
    "work",
    "users",
}


def _is_ignored_path(path: Path) -> bool:
    return any(part in IGNORED_DIR_PARTS or part.startswith(".git") for part in path.parts)


def _walk_repo_candidate_paths(repo_path: Path) -> list[str]:
    candidates: list[str] = []
    for root, dirnames, filenames in os.walk(repo_path):
        root_path = Path(root)
        if _is_ignored_path(root_path):
            dirnames[:] = []
            continue
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname not in IGNORED_DIR_PARTS and not dirname.startswith(".git")
        ]
        for name in filenames:
            path = root_path / name
            if path.suffix.lower() not in RELEVANT_SUFFIXES:
                continue
            if _is_ignored_path(path):
                continue
            try:
                candidates.append(path.relative_to(repo_path).as_posix())
            except Exception:
                continue
    return candidates


def _task_keywords(task: dict[str, Any] | None) -> list[str]:
    if not task:
        return []
    text = " ".join(
        [
            str(task.get("title") or ""),
            str(task.get("description") or ""),
            " ".join(str(entry or "") for entry in (task.get("acceptance_criteria") or [])),
        ]
    ).lower()
    tokens = []
    for token in re.findall(r"[a-z0-9]+", text):
        if len(token) < 3 or token in TASK_STOPWORDS:
            continue
        tokens.append(token)
    # Keep order stable while de-duplicating.
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def discover_repo_files(repo_path: Path, task: dict[str, Any] | None = None, limit: int = 12) -> list[str]:
    keywords = _task_keywords(task)
    if not keywords:
        return []

    scored: dict[str, int] = {}

    file_globs = [
        "*.json",
        "*.html",
        "*.css",
        "*.js",
        "*.md",
        "*.ts",
        "*.tsx",
        "*.jsx",
        "*.mjs",
        "*.cjs",
        "*.py",
        "*.rs",
        "*.toml",
        "*.yaml",
        "*.yml",
    ]
    rg_file_args = ["--files"]
    for pattern in file_globs:
        rg_file_args.extend(["--glob", pattern])
    for ignore in ("node_modules", ".git", ".aca-worktrees", "dist", "build", "coverage", "target", ".venv", "__pycache__"):
        rg_file_args.extend(["--glob", f"!**/{ignore}/**"])
    try:
        file_listing = subprocess.run(
            ["rg", *rg_file_args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except Exception:
        file_listing = ""

    candidate_paths = {
        line.strip().lstrip("./")
        for line in file_listing.splitlines()
        if line.strip()
    }
    candidate_paths.update(_walk_repo_candidate_paths(repo_path))

    search_terms = list(dict.fromkeys(keywords + [
        "todo",
        "todos",
        "task",
        "tasks",
        "filter",
        "filters",
        "due",
        "date",
        "dates",
        "overdue",
        "complete",
        "completed",
        "createdat",
        "created_at",
    ]))
    bug_monitor_mode = "bug" in keywords and "monitor" in keywords
    content_matches: set[str] = set()
    if search_terms:
        pattern = "|".join(re.escape(term) for term in search_terms if term)
        try:
            rg_match_args = [
                "-l",
                "-i",
                pattern,
            ]
            for ignore in ("node_modules", ".git", ".aca-worktrees", "dist", "build", "coverage", "target", ".venv", "__pycache__"):
                rg_match_args.extend(["--glob", f"!**/{ignore}/**"])
            for ext in file_globs:
                rg_match_args.extend(["--glob", ext])
            output = subprocess.run(
                ["rg", *rg_match_args],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            content_matches = {
                line.strip().lstrip("./")
                for line in output.splitlines()
                if line.strip()
            }
        except Exception:
            content_matches = set()

    for rel_path in candidate_paths | content_matches:
        lower_rel = rel_path.lower()
        if Path(rel_path).suffix.lower() not in RELEVANT_SUFFIXES:
            continue
        if _is_ignored_path(Path(rel_path)):
            continue
        score = 0
        todo_mode = any("todo" in keyword for keyword in keywords)
        if todo_mode:
            if lower_rel.endswith("src/app.tsx"):
                score += 8
            if lower_rel.endswith("src/hooks/usetodos.ts"):
                score += 8
            if lower_rel.endswith("src/components/tasks/tasksidebar.tsx"):
                score += 10
            if lower_rel.endswith("src/lib/tauri.ts"):
                score += 9
            if lower_rel.endswith("src/components/chat/chat.tsx"):
                score += 4
            if lower_rel.startswith("src/"):
                score += 2
            if lower_rel.startswith("examples/"):
                score -= 2
        if bug_monitor_mode:
            if lower_rel.startswith("crates/tandem-server/src/bug_monitor/"):
                score += 16
            if lower_rel.endswith("crates/tandem-server/src/bug_monitor/service.rs"):
                score += 14
            if lower_rel.endswith("crates/tandem-server/src/bug_monitor/types.rs"):
                score += 6
            if lower_rel.startswith("crates/tandem-server/src/http/bug_monitor"):
                score += 12
            if lower_rel.startswith("crates/tandem-server/src/http/tests/bug_monitor"):
                score += 18
            if "packages/tandem-client-ts/test/bug-monitor" in lower_rel:
                score += 10
            if lower_rel.startswith("scripts/bug-monitor"):
                score += 8
            if "quality_gate" in lower_rel or "quality-gate" in lower_rel:
                score += 6
            if lower_rel.startswith("docs/internal/"):
                score -= 6
        for keyword in keywords:
            if keyword in lower_rel:
                score += 4
        if rel_path in content_matches:
            score += 3
        if any(token in lower_rel for token in ("todo", "task", "filter", "due", "overdue", "component", "hook")):
            score += 1
        if Path(rel_path).suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
            score += 1
        if score:
            scored[rel_path] = max(scored.get(rel_path, 0), score)

    def _fallback_root_files() -> list[str]:
        priority = {name.lower(): index for index, name in enumerate(ROOT_FALLBACK_FILES)}
        root_candidates = []
        for rel_path in candidate_paths:
            path = Path(rel_path)
            if path.suffix.lower() not in RELEVANT_SUFFIXES:
                continue
            if _is_ignored_path(path):
                continue
            if len(path.parts) > 1:
                continue
            root_candidates.append(rel_path)
        root_candidates.sort(key=lambda rel_path: (priority.get(Path(rel_path).name.lower(), len(priority)), rel_path.lower()))
        return root_candidates

    scored_items = sorted(scored.items(), key=lambda item: (-item[1], item[0]))
    result: list[str] = []
    for rel_path, _ in scored_items:
        if rel_path in result:
            continue
        result.append(rel_path)
        if len(result) >= limit:
            break

    if len(result) < limit:
        for rel_path in _fallback_root_files():
            if rel_path in result:
                continue
            result.append(rel_path)
            if len(result) >= limit:
                break

    return result


def collect_expected_repo_files(subtasks: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    files: list[str] = []
    for subtask in subtasks:
        ignored_paths = {
            str(raw_path or "").strip().replace("\\", "/")
            for raw_path in list(subtask.get("ignored_target_files") or [])
            if str(raw_path or "").strip()
        }
        for raw_path in list(subtask.get("files") or []) + list(subtask.get("target_files") or []):
            rel_path = str(raw_path or "").strip().replace("\\", "/")
            while rel_path.startswith("./"):
                rel_path = rel_path[2:]
            if rel_path in ignored_paths:
                continue
            if rel_path.startswith("/") or rel_path == ".." or rel_path.startswith("../") or "/../" in f"/{rel_path}/":
                continue
            if not rel_path or rel_path in seen:
                continue
            seen.add(rel_path)
            files.append(rel_path)
    return files


def file_is_readable(path: Path) -> bool:
    try:
        path.read_bytes()
    except Exception:
        return False
    return True


def _subtask_text(subtask: dict[str, Any]) -> str:
    return " ".join(
        [
            str(subtask.get("title") or "").strip().lower(),
            str(subtask.get("goal") or "").strip().lower(),
            str(subtask.get("description") or "").strip().lower(),
        ]
    ).strip()


def _subtask_requires_content_changes(subtask: dict[str, Any]) -> bool:
    text = _subtask_text(subtask)
    if not text:
        return False
    strong_change_signals = (
        "add ",
        "implement",
        "modify",
        "update",
        "enhance",
        "refine",
        "render",
        "logic",
        "interaction",
        "behavior",
        "functionality",
        "feature",
        "todo creation",
        "list rendering",
        "empty or whitespace",
        "reject",
        "stable id",
        "stable state",
    )
    return any(token in text for token in strong_change_signals)


def subtask_satisfied(repo_path: Path, subtask: dict[str, Any]) -> bool:
    files = [
        str(raw_path or "").strip().lstrip("./")
        for raw_path in list(subtask.get("files") or []) + list(subtask.get("target_files") or [])
    ]
    files = [path for path in files if path]
    if not files:
        return False
    for rel_path in files:
        target = repo_path / rel_path
        if not target.exists() or not target.is_file() or not file_is_readable(target):
            return False

    repo_text_parts: list[str] = []
    for rel_path in files:
        try:
            repo_text_parts.append((repo_path / rel_path).read_text(encoding="utf-8", errors="ignore").lower())
        except Exception:
            continue
    repo_text = "\n".join(repo_text_parts)
    if not repo_text:
        return False

    subtask_like = {
        "title": subtask.get("title") or "",
        "description": subtask.get("goal") or subtask.get("description") or "",
        "acceptance_criteria": subtask.get("acceptance_criteria") or [],
    }
    keywords = _task_keywords(subtask_like)
    keyword_hits = {keyword for keyword in keywords if keyword in repo_text}
    if len(keyword_hits) >= max(8, len(keywords) // 2):
        return True

    marker_hits = {marker for marker in CONTENT_SATISFACTION_MARKERS if marker in repo_text}
    if len(marker_hits) >= 3:
        return True

    return False


def repo_context_summary(repo_path: Path, task: dict[str, Any] | None = None, limit: int = 12) -> str:
    entries: list[str] = []
    discovered = discover_repo_files(repo_path, task, limit=limit)
    if discovered:
        entries.append("Likely relevant repo files:")
        for rel_path in discovered[:limit]:
            path = repo_path / rel_path
            state = "readable" if file_is_readable(path) else "unreadable"
            try:
                size = path.stat().st_size
            except Exception:
                size = 0
            entries.append(f"- {rel_path} ({state}, {size} bytes)")
        return "\n".join(entries)

    for path in sorted(repo_path.rglob("*")):
        if len(entries) >= limit:
            break
        if not path.is_file():
            continue
        if _is_ignored_path(path):
            continue
        if path.suffix.lower() not in RELEVANT_SUFFIXES:
            continue
        rel_path = path.relative_to(repo_path)
        state = "readable" if file_is_readable(path) else "unreadable"
        entries.append(f"- {rel_path} ({state}, {path.stat().st_size} bytes)")
    if not entries:
        return "No relevant repo files were discovered."
    return "\n".join(entries)


def extract_command_checks(manager_plan: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    safe_prefixes = (
        "ls ",
        "cat ",
        "grep ",
        "test ",
        "find ",
        "wc ",
        "head ",
        "tail ",
        "sed ",
        "git diff",
        "cargo check ",
        "cargo test ",
        "cargo fmt ",
        "cargo clippy ",
    )
    unsafe_tokens = ("npm start", "npm run", "curl http://", "curl https://", "localhost:", "serve ", "&")
    for entry in manager_plan.get("tests") or []:
        if isinstance(entry, dict):
            command = str(entry.get("command") or "").strip()
        else:
            command = str(entry or "").strip()
        if not command:
            continue
        lower = command.lower()
        if "manual" in lower or "visual" in lower or "browser check" in lower:
            continue
        if any(token in lower for token in unsafe_tokens):
            continue
        if not lower.startswith(safe_prefixes):
            continue
        commands.append(command)
    return commands


_COMMAND_CHECK_PROGRAMS = {
    "bash",
    "bun",
    "cargo",
    "cat",
    "deno",
    "find",
    "git",
    "go",
    "grep",
    "head",
    "just",
    "ls",
    "make",
    "node",
    "npm",
    "npx",
    "pnpm",
    "python",
    "python3",
    "pytest",
    "sed",
    "sh",
    "tail",
    "test",
    "wc",
    "yarn",
}


def command_check_is_executable(command: str) -> bool:
    """Return true for verification strings that look like shell commands.

    Linear issue verification often contains prose like "Evals fail before X
    and pass after Y." Those lines are useful acceptance context, but running
    them under bash creates false verification failures. Keep this check
    conservative and let deterministic inference supply extra commands.
    """
    command = str(command or "").strip()
    if not command:
        return False
    try:
        parts = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return False
    if not parts:
        return False
    program = parts[0].strip()
    if program.startswith("./") or program.startswith("scripts/") or "/" in program:
        return True
    return program in _COMMAND_CHECK_PROGRAMS


def filter_executable_command_checks(commands: list[str]) -> list[str]:
    filtered: list[str] = []
    for raw_command in commands:
        command = str(raw_command or "").strip()
        if command and command_check_is_executable(command) and command not in filtered:
            filtered.append(command)
    return filtered


def _safe_rel_path(raw_path: Any) -> str:
    rel_path = str(raw_path or "").strip().replace("\\", "/")
    while rel_path.startswith("./"):
        rel_path = rel_path[2:]
    if not rel_path or rel_path.startswith("/") or rel_path == ".." or rel_path.startswith("../") or "/../" in f"/{rel_path}/":
        return ""
    return rel_path


def _load_package_scripts(package_json: Path) -> dict[str, str]:
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except Exception:
        return {}
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {str(name): str(command) for name, command in scripts.items() if str(name).strip() and str(command).strip()}


def _script_references_changed_file(repo_path: Path, package_dir: Path, script_command: str, changed_files: list[str]) -> bool:
    command = str(script_command or "").replace("\\", "/")
    if not command:
        return False
    for rel_path in changed_files:
        candidates = [rel_path]
        try:
            package_rel_path = (repo_path / rel_path).relative_to(package_dir).as_posix()
        except ValueError:
            package_rel_path = ""
        if package_rel_path and package_rel_path not in candidates:
            candidates.append(package_rel_path)
        for candidate in candidates:
            if candidate and candidate in command:
                return True
    return False


def _nearest_package_dir(repo_path: Path, rel_path: str) -> Path | None:
    target_parent = (repo_path / rel_path).parent
    try:
        target_parent.relative_to(repo_path)
    except ValueError:
        return None
    for candidate in [target_parent, *target_parent.parents]:
        if candidate == repo_path.parent:
            break
        if (candidate / "package.json").is_file():
            return candidate
        if candidate == repo_path:
            break
    return None


def _package_runner(repo_path: Path, package_dir: Path) -> str:
    rel = package_dir.relative_to(repo_path)
    rel_arg = "." if str(rel) == "." else rel.as_posix()
    quoted = shlex.quote(rel_arg)
    if (package_dir / "pnpm-lock.yaml").is_file() or (repo_path / "pnpm-lock.yaml").is_file():
        return f"pnpm -C {quoted} run"
    if (package_dir / "yarn.lock").is_file() or (repo_path / "yarn.lock").is_file():
        return f"yarn --cwd {quoted} run"
    if (package_dir / "bun.lockb").is_file() or (repo_path / "bun.lockb").is_file():
        return f"bun --cwd {quoted} run"
    return f"npm --prefix {quoted} run"


def _nearest_cargo_package_dir(repo_path: Path, rel_path: str) -> Path | None:
    target = repo_path / rel_path
    target_parent = target if target.is_dir() else target.parent
    try:
        target_parent.relative_to(repo_path)
    except ValueError:
        return None
    for candidate in [target_parent, *target_parent.parents]:
        if candidate == repo_path.parent:
            break
        manifest = candidate / "Cargo.toml"
        if manifest.is_file():
            try:
                data = tomllib.loads(manifest.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if isinstance(data.get("package"), dict) and str(data["package"].get("name") or "").strip():
                return candidate
        if candidate == repo_path:
            break
    return None


def _cargo_package_name(manifest_path: Path) -> str:
    try:
        data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    package = data.get("package")
    if not isinstance(package, dict):
        return ""
    return str(package.get("name") or "").strip()


def _python_unittest_module(rel_path: str) -> str:
    path = Path(rel_path)
    if path.suffix != ".py":
        return ""
    stem = path.stem
    if not (stem.startswith("test_") or stem.endswith("_test")):
        return ""
    parts = path.with_suffix("").parts
    if not parts or any(not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", part) for part in parts):
        return ""
    return ".".join(parts)


def _python_test_commands(repo_path: Path, normalized_files: list[str]) -> list[str]:
    commands: list[str] = []
    for rel_path in normalized_files:
        path = Path(rel_path)
        candidates: list[str] = []
        if path.suffix == ".py" and path.is_relative_to(Path("src")):
            if path.stem.startswith("test_") or path.stem.endswith("_test"):
                candidates.append(path.as_posix())
            else:
                sibling_suffix = path.with_name(f"{path.stem}_test.py")
                sibling_prefix = path.with_name(f"test_{path.name}")
                candidates.extend([sibling_suffix.as_posix(), sibling_prefix.as_posix()])
        for candidate in candidates:
            module = _python_unittest_module(candidate)
            if not module:
                continue
            if not (repo_path / candidate).exists() and candidate != rel_path:
                continue
            command = f"python3 -m unittest {module}"
            if command not in commands:
                commands.append(command)
    return commands


def infer_command_checks(
    repo_path: Path,
    changed_files: list[str],
    task: dict[str, Any] | None = None,
) -> list[str]:
    """Infer deterministic verification commands from changed package files.

    This is intentionally conservative: it only uses scripts already declared
    by the nearest package.json for a changed file. That gives coding runs a
    real verification gate without letting the model invent shell commands.
    """
    normalized_files = [_safe_rel_path(path) for path in changed_files]
    normalized_files = [path for path in normalized_files if path and not _is_ignored_path(Path(path))]
    if not normalized_files:
        return []

    package_dirs: list[Path] = []
    seen_dirs: set[Path] = set()
    cargo_package_dirs: list[Path] = []
    seen_cargo_dirs: set[Path] = set()
    for rel_path in normalized_files:
        package_dir = _nearest_package_dir(repo_path, rel_path)
        if package_dir is not None and package_dir not in seen_dirs:
            seen_dirs.add(package_dir)
            package_dirs.append(package_dir)
        cargo_package_dir = _nearest_cargo_package_dir(repo_path, rel_path)
        if cargo_package_dir is not None and cargo_package_dir not in seen_cargo_dirs:
            seen_cargo_dirs.add(cargo_package_dir)
            cargo_package_dirs.append(cargo_package_dir)

    commands: list[str] = []
    for cargo_package_dir in sorted(cargo_package_dirs, key=lambda path: path.relative_to(repo_path).as_posix()):
        package_name = _cargo_package_name(cargo_package_dir / "Cargo.toml")
        if not package_name:
            continue
        command = f"cargo check -p {shlex.quote(package_name)}"
        if command not in commands:
            commands.append(command)
    for command in _python_test_commands(repo_path, normalized_files):
        if command not in commands:
            commands.append(command)

    raw_acceptance = (task or {}).get("acceptance_criteria")
    acceptance = raw_acceptance if isinstance(raw_acceptance, (list, tuple, set)) else [raw_acceptance]
    task_text = " ".join(
        [
            str((task or {}).get("title") or ""),
            str((task or {}).get("description") or ""),
            " ".join(str(item or "") for item in acceptance if str(item or "").strip()),
        ]
    ).lower()
    prefer_tests = any(token in task_text for token in ("test", "smoke", "verify", "verification", "lint", "typecheck"))
    test_script_priority = ("test:smoke", "test:ci", "test:unit", "test")

    for package_dir in sorted(package_dirs, key=lambda path: path.relative_to(repo_path).as_posix()):
        scripts = _load_package_scripts(package_dir / "package.json")
        if not scripts:
            continue
        runner = _package_runner(repo_path, package_dir)
        package_commands: list[str] = []
        if "build" in scripts:
            package_commands.append(f"{runner} build")
        if prefer_tests or "build" not in scripts:
            for script_name in test_script_priority:
                if script_name in scripts:
                    package_commands.append(f"{runner} {script_name}")
                    break
        if prefer_tests:
            for script_name, script_command in sorted(scripts.items()):
                name_text = str(script_name).lower()
                command_text = str(script_command).lower()
                if not any(token in f"{name_text} {command_text}" for token in ("test", "smoke", "check")):
                    continue
                if not _script_references_changed_file(repo_path, package_dir, script_command, normalized_files):
                    continue
                package_commands.append(f"{runner} {script_name}")
        for command in package_commands:
            if command not in commands:
                commands.append(command)
    return commands


def _command_check_env() -> dict[str, str]:
    env = dict(os.environ)
    existing_path = [part for part in str(env.get("PATH") or "").split(os.pathsep) if part]
    path_parts = [
        "/home/node/npm/bin",
        "/usr/local/bin",
        "/usr/local/sbin",
        "/usr/bin",
        "/usr/sbin",
        "/bin",
        "/sbin",
    ]
    env["PATH"] = os.pathsep.join(dict.fromkeys(path_parts + existing_path))
    return env


def _command_check_attempt_count() -> int:
    raw = str(os.environ.get("ACA_COMMAND_CHECK_ATTEMPTS") or "").strip()
    if raw:
        try:
            return max(1, min(5, int(raw)))
        except ValueError:
            pass
    return 2


def _run_single_command_check(repo_path: Path, command: str, timeout_seconds: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["/bin/bash", "-c", command],
            cwd=str(repo_path),
            env=_command_check_env(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "command": command,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "status": "pass" if proc.returncode == 0 else "fail",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "returncode": None,
            "stdout": (exc.stdout or "").strip(),
            "stderr": ((exc.stderr or "").strip() or "timed out"),
            "status": "fail",
        }


def run_command_checks(repo_path: Path, commands: list[str], timeout_seconds: int = 60) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    attempt_count = _command_check_attempt_count()
    for command in commands:
        attempts: list[dict[str, Any]] = []
        result: dict[str, Any] | None = None
        for attempt in range(1, attempt_count + 1):
            result = _run_single_command_check(repo_path, command, timeout_seconds)
            result["attempt"] = attempt
            attempts.append(dict(result))
            if result.get("status") == "pass":
                break
        if result is None:
            continue
        result["attempt_count"] = len(attempts)
        if len(attempts) > 1:
            result["attempts"] = attempts
        results.append(result)
    return results


def deterministic_repo_validation(
    repo_path: Path,
    expected_files: list[str],
    command_checks: list[str] | None = None,
) -> dict[str, Any]:
    checked: list[str] = []
    missing: list[str] = []
    unreadable: list[str] = []
    present: list[str] = []
    for rel_path in expected_files:
        target = repo_path / rel_path
        if not target.exists() or not target.is_file():
            missing.append(rel_path)
            continue
        present.append(rel_path)
        if not file_is_readable(target):
            unreadable.append(rel_path)
            continue
        checked.append(rel_path)
    command_results = run_command_checks(repo_path, command_checks or [])
    command_failures = [result for result in command_results if result.get("status") != "pass"]
    return {
        "expected_files": expected_files,
        "present_files": present,
        "checked_files": checked,
        "missing_files": missing,
        "unreadable_files": unreadable,
        "command_checks": command_results,
        "command_failures": command_failures,
        "ok": not missing and not unreadable and not command_failures,
    }


def repo_validation_blocker_message(repo_validation: dict[str, Any]) -> str | None:
    if repo_validation.get("verification_missing"):
        return "Verification commands are missing."
    unexpected = list(repo_validation.get("unexpected_files") or [])
    missing = list(repo_validation.get("missing_files") or [])
    unreadable = list(repo_validation.get("unreadable_files") or [])
    command_failures = list(repo_validation.get("command_failures") or [])
    if unexpected:
        return "Unexpected repository files changed: " + ", ".join(unexpected)
    if missing:
        return "Expected repository files are missing: " + ", ".join(missing)
    if unreadable:
        return "Expected repository files are unreadable: " + ", ".join(unreadable)
    if command_failures:
        first = command_failures[0]
        return "Repository validation command failed: " + str(first.get("command") or "").strip()
    return None


def repo_validation_blocker_kind(repo_validation: dict[str, Any]) -> str | None:
    if repo_validation.get("verification_missing"):
        return "verification_missing"
    if repo_validation.get("unexpected_files"):
        return "unexpected_repo_changes"
    if repo_validation.get("missing_files"):
        return "expected_files_missing"
    if repo_validation.get("unreadable_files"):
        return "expected_files_unreadable"
    if repo_validation.get("command_failures"):
        return "verification_failed"
    return None


def shell_quote_path(path: Path) -> str:
    return shlex.quote(str(path))
