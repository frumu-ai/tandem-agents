"""phases/planning.py -- Manager prompt, subtask decomposition, and pre-satisfaction.

This module owns the manager (planning) prompt phase:
1. Build and run the Tandem manager prompt
2. Parse the JSON plan (or fall back to a plain-text plan)
3. Decompose the plan into subtasks via ``derive_subtasks``
4. Pre-screen subtasks against the repository to discover already-satisfied ones
5. Detect the "no-targets" early-exit condition
6. Prepare the pending-subtask list for worker dispatch

The planning phase repeats inside the repair loop (max_loops iterations).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

from src.tandem_agents.core.engine.engine import delete_tandem_session
from src.tandem_agents.core.phases.context import RunContext
from src.tandem_agents.core.task_contract import task_contract_payload

logger = logging.getLogger("aca.phases.planning")

_MANAGER_PLAN_REQUIRED_KEYS = {"summary", "subtasks", "risks", "tests"}
_PRODUCTION_SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".mjs",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".swift",
    ".ts",
    ".tsx",
}


def _active_engine_sessions_path(ctx: RunContext) -> Path:
    return ctx.run_dir / "active_worker_engine_sessions.json"


def _pop_active_engine_session(ctx: RunContext, role: str) -> dict[str, Any]:
    role = str(role or "").strip()
    if not role:
        return {}
    path = _active_engine_sessions_path(ctx)
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    info = dict(loaded.pop(role, {}) or {})
    if loaded:
        from src.tandem_agents.utils.utils import atomic_write_json

        atomic_write_json(path, loaded)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return info


def _cancel_active_manager_engine_session(ctx: RunContext, reason: str) -> None:
    info = _pop_active_engine_session(ctx, "manager")
    session_id = str(info.get("session_id") or "").strip()
    if not session_id:
        return
    run_id = str(info.get("run_id") or "").strip()
    reason = str(reason or "manager_cancelled").strip() or "manager_cancelled"
    from src.tandem_agents.runtime.runstate import append_event

    append_event(
        ctx.layout["events"],
        "manager.engine_cancel_requested",
        ctx.run_id,
        {
            "session_id": session_id,
            "engine_run_id": run_id,
            "reason": reason,
        },
        task_id=ctx.task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )

    def _delete() -> None:
        try:
            delete_tandem_session(ctx.cfg, session_id)
            append_event(
                ctx.layout["events"],
                "manager.engine_cancelled",
                ctx.run_id,
                {
                    "session_id": session_id,
                    "engine_run_id": run_id,
                    "reason": reason,
                },
                task_id=ctx.task.get("task_id"),
                role="manager",
                repo={"path": ctx.repo.get("path")},
            )
        except Exception as exc:
            append_event(
                ctx.layout["events"],
                "manager.engine_cancel_failed",
                ctx.run_id,
                {
                    "session_id": session_id,
                    "engine_run_id": run_id,
                    "reason": reason,
                    "error": str(exc)[:500],
                },
                task_id=ctx.task.get("task_id"),
                role="manager",
                repo={"path": ctx.repo.get("path")},
            )

    thread = threading.Thread(
        target=_delete,
        name="aca-cancel-engine-session-manager",
        daemon=True,
    )
    thread.start()


def _normalize_repo_relative_path(value: Any) -> str:
    rel_path = str(value or "").strip().replace("\\", "/")
    while rel_path.startswith("./"):
        rel_path = rel_path[2:]
    if not rel_path or rel_path.startswith("/") or rel_path == ".." or rel_path.startswith("../"):
        return ""
    if "/../" in f"/{rel_path}/":
        return ""
    return rel_path


def _subtask_declared_files(subtask: dict[str, Any]) -> set[str]:
    files: set[str] = set()
    for key in ("files", "target_files"):
        raw_values = subtask.get(key)
        if not isinstance(raw_values, list):
            continue
        for raw_path in raw_values:
            rel_path = _normalize_repo_relative_path(raw_path)
            if rel_path:
                files.add(rel_path)
    return files


def _task_target_files(task: Any) -> list[str]:
    if not isinstance(task, dict):
        return []
    parent_contract = task_contract_payload(task)
    raw_values = parent_contract.get("target_files") or task.get("target_files") or []
    return list(
        dict.fromkeys(
            rel_path
            for rel_path in (_normalize_repo_relative_path(raw_path) for raw_path in raw_values)
            if rel_path
        )
    )


def _task_or_explicit_target_files(task: Any, repo_path: Path) -> list[str]:
    declared = _task_target_files(task)
    if declared:
        return declared
    if not isinstance(task, dict):
        return []
    try:
        from src.tandem_agents.core.execution import runner_core as _rc  # noqa: PLC0415

        explicit = _rc._explicit_task_target_files(repo_path, task)
    except Exception:
        explicit = []
    return list(
        dict.fromkeys(
            rel_path
            for rel_path in (_normalize_repo_relative_path(raw_path) for raw_path in explicit)
            if rel_path
        )
    )


def _partial_diff_changed_files(artifact: dict[str, Any]) -> list[str]:
    raw_files = artifact.get("changed_files")
    if not isinstance(raw_files, list):
        return []
    return list(
        dict.fromkeys(
            rel_path
            for rel_path in (_normalize_repo_relative_path(raw_path) for raw_path in raw_files)
            if rel_path and rel_path != "__aca_temp_probe.txt"
        )
    )


def _weak_source_test_artifact_after_latest_one_sided_pair(artifacts: list[Any]) -> bool:
    latest_one_sided_index = -1
    latest_weak_source_test_index = -1
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            continue
        failure_reason = str(artifact.get("failure_reason") or "").strip()
        if failure_reason in {"WORKER_OFF_TRACK_TESTLESS_DIFF", "WORKER_TEST_ONLY_DIFF"}:
            latest_one_sided_index = index
        excerpt = str(artifact.get("worker_output_excerpt") or "").lower()
        weak_test_rejection = failure_reason in {
            "WORKER_VERIFIABLE_DIFF_WEAK_TEST",
            "WORKER_VERIFIABLE_DIFF_MISALIGNED_TEST",
        } or any(
            marker in excerpt
            for marker in (
                "test diff did not add a test method",
                "test diff did not add a test method or assertion",
                "did not add a test method or assertion",
                "did not exercise newly introduced production symbol",
                "newly introduced production api",
                "weak test",
            )
        )
        if weak_test_rejection and _changed_files_include_source_and_test(_partial_diff_changed_files(artifact)):
            latest_weak_source_test_index = index
    return latest_weak_source_test_index >= 0 and latest_weak_source_test_index > latest_one_sided_index


def _compact_partial_diff_repair_context(worker_output_excerpt: str) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for raw_line in str(worker_output_excerpt or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if "aca preserved this partial worker diff" in lowered:
            continue
        if "partial diff is not treated as a completed worker result" in lowered:
            continue
        if line == "- __aca_temp_probe.txt":
            continue
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
    context = " ".join(lines).strip()
    if not context and "engine_prompt_timeout" in str(worker_output_excerpt or "").lower():
        context = "ENGINE_PROMPT_TIMEOUT before the worker returned a terminal response."
    return context[:1200].rstrip()


def _markdown_section(text: str, heading: str) -> str:
    marker = f"## {heading}"
    start = text.find(marker)
    if start < 0:
        return ""
    body_start = text.find("\n", start)
    if body_start < 0:
        return ""
    next_heading = text.find("\n## ", body_start + 1)
    if next_heading < 0:
        return text[body_start + 1 :].strip()
    return text[body_start + 1 : next_heading].strip()


def _compact_focused_test_failure_output(output: str) -> str:
    raw_lines = [line.rstrip() for line in str(output or "").splitlines()]
    assertion_lines = [
        line.strip()
        for line in raw_lines
        if line.strip().startswith("AssertionError:")
    ]
    if assertion_lines:
        fail_lines = [
            line.strip()
            for line in raw_lines
            if line.strip().startswith("FAIL:")
        ]
        selected_assertions = list(dict.fromkeys([*fail_lines[:2], *assertion_lines[:4]]))
        return " ".join(selected_assertions)[:700].rstrip()
    selected: list[str] = []
    selected_indexes: set[int] = set()
    marker_indexes: list[int] = []
    root_cause_markers = (
        "assertionerror",
        "attributeerror",
        "importerror",
        "keyerror",
        "modulenotfounderror",
        "nameerror",
        "runtimeerror",
        "syntaxerror",
        "typeerror",
        "valueerror",
    )
    for index, raw_line in enumerate(raw_lines):
        line = raw_line.strip()
        if not line or set(line) <= {"=", "-"}:
            continue
        lowered = line.lower()
        if any(
            marker in lowered
            for marker in (
                "error",
                "failed",
                "failure",
                "traceback",
                "importerror",
                "modulenotfounderror",
                "syntaxerror",
                "cannot import",
                "attributeerror",
                "assert",
                "expected",
                "actual",
            )
        ):
            marker_indexes.append(index)

    for index, raw_line in enumerate(raw_lines):
        line = raw_line.strip()
        lowered = line.lower()
        if not line or set(line) <= {"=", "-"}:
            continue
        if not any(marker in lowered for marker in root_cause_markers):
            continue
        if index in selected_indexes:
            continue
        selected_indexes.add(index)
        selected.append(line)
        if len(selected) >= 4:
            break

    for marker_index in marker_indexes[:4]:
        for index in range(max(0, marker_index - 3), min(len(raw_lines), marker_index + 2)):
            if index in selected_indexes:
                continue
            line = raw_lines[index].strip()
            if not line or set(line) <= {"=", "-"}:
                continue
            selected_indexes.add(index)
            selected.append(line)
            if len(selected) >= 12:
                return " ".join(selected)[:900].rstrip()

    if selected:
        return " ".join(selected)[:900].rstrip()

    lines: list[str] = []
    seen: set[str] = set()
    interesting_markers = (
        "error",
        "failed",
        "failure",
        "traceback",
        "importerror",
        "modulenotfounderror",
        "syntaxerror",
        "cannot import",
        "attributeerror",
        "assert",
        "expected",
        "actual",
    )
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if not line or set(line) <= {"=", "-"}:
            continue
        lowered = line.lower()
        if not any(marker in lowered for marker in interesting_markers):
            continue
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
        if len(lines) >= 8:
            break
    return " ".join(lines)[:900].rstrip()


def _missing_import_repair_instruction(text: str) -> str:
    matches = list(
        re.finditer(
            r"ImportError:\s+cannot import name ['\"]([^'\"]+)['\"] from ['\"]([^'\"]+)['\"]",
            str(text or ""),
        )
    )
    if not matches:
        return ""
    symbol, module = matches[-1].groups()
    return (
        "Focused import repair: make `"
        + symbol
        + "` an exported production symbol from `"
        + module
        + "` or change the paired test import to the real production symbol before rerunning verification. "
        "Do not leave the test importing a name the source file does not define."
    )


def _missing_dependency_import_repair_instruction(text: str) -> str:
    matches = list(
        re.finditer(
            r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]",
            str(text or ""),
        )
    )
    if not matches:
        return ""
    module = matches[-1].group(1)
    if module == "pytest":
        return (
            "Focused missing dependency repair: remove the new `pytest` dependency from the carried unittest-style "
            "test file and express the assertion with `unittest` or standard-library helpers before rerunning "
            "the focused `python3 -m unittest ...` command. Do not add `pytest` as a project dependency for this repair."
        )
    return (
        "Focused missing dependency repair: the carried patch imports unavailable module `"
        + module
        + "`. Prefer removing or replacing that new test dependency with existing project or standard-library "
        "code before rerunning focused verification; do not install new dependencies unless the parent task explicitly requires it."
    )


def _missing_import_failure_key(text: str) -> tuple[str, str] | None:
    matches = list(
        re.finditer(
            r"ImportError:\s+cannot import name ['\"]([^'\"]+)['\"] from ['\"]([^'\"]+)['\"]",
            str(text or ""),
        )
    )
    if not matches:
        return None
    symbol, module = matches[-1].groups()
    return symbol, module


def _partial_diff_artifact_failure_text(artifact: dict[str, Any]) -> str:
    return "\n".join(
        str(artifact.get(key) or "")
        for key in (
            "worker_output_excerpt",
            "verification_output_excerpt",
            "recovery_action",
            "failure_reason",
        )
        if str(artifact.get(key) or "").strip()
    )


def _repeated_missing_import_failure(
    artifact: dict[str, Any],
    prior_artifacts: list[Any],
    current_failure_text: str,
) -> tuple[str, str] | None:
    missing_import = _missing_import_failure_key(current_failure_text)
    if not missing_import:
        return None
    subtask_id = str(artifact.get("subtask_id") or "").strip()
    for prior in prior_artifacts:
        if not isinstance(prior, dict):
            continue
        if str(prior.get("failure_reason") or "").strip() != "WORKER_VERIFIABLE_DIFF_TEST_FAILED":
            continue
        prior_subtask_id = str(prior.get("subtask_id") or "").strip()
        if subtask_id and prior_subtask_id and prior_subtask_id != subtask_id:
            continue
        if _missing_import_failure_key(_partial_diff_artifact_failure_text(prior)) == missing_import:
            return missing_import
    return None


def _assertion_failure_test_key(text: str) -> tuple[str, str] | None:
    matches = list(
        re.finditer(
            r"(?:FAIL|ERROR):\s+([A-Za-z_][\w]*)\s+\(([^)]+)\)",
            str(text or ""),
        )
    )
    if matches:
        test_name, qualified_name = matches[-1].groups()
        return test_name, qualified_name
    matches = list(re.finditer(r"\bin\s+(test_[A-Za-z_][\w]*)\b", str(text or "")))
    if matches:
        return matches[-1].group(1), ""
    return None


def _repeated_assertion_failure(
    artifact: dict[str, Any],
    prior_artifacts: list[Any],
    current_failure_text: str,
) -> tuple[str, str] | None:
    if "assertionerror:" not in current_failure_text.lower() and "\nfail:" not in current_failure_text.lower():
        return None
    failure_key = _assertion_failure_test_key(current_failure_text)
    if not failure_key:
        return None
    subtask_id = str(artifact.get("subtask_id") or "").strip()
    for prior in prior_artifacts:
        if not isinstance(prior, dict):
            continue
        if str(prior.get("failure_reason") or "").strip() != "WORKER_VERIFIABLE_DIFF_TEST_FAILED":
            continue
        prior_subtask_id = str(prior.get("subtask_id") or "").strip()
        if subtask_id and prior_subtask_id and prior_subtask_id != subtask_id:
            continue
        if _assertion_failure_test_key(_partial_diff_artifact_failure_text(prior)) == failure_key:
            return failure_key
    return None


def _unexpected_keyword_repair_instruction(text: str) -> str:
    matches = list(
        re.finditer(
            r"TypeError:\s+([A-Za-z_][\w.]*)\(\) got an unexpected keyword argument ['\"]([^'\"]+)['\"]",
            str(text or ""),
        )
    )
    if not matches:
        return ""
    function_keywords: dict[str, list[str]] = {}
    for function_name, keyword in (match.groups() for match in matches):
        function_keywords.setdefault(function_name, [])
        if keyword not in function_keywords[function_name]:
            function_keywords[function_name].append(keyword)
    function_text = ", ".join("`" + function_name + "`" for function_name in function_keywords)
    keyword_text = ", ".join(
        "`" + keyword + "`"
        for keyword in dict.fromkeys(
            keyword
            for keywords in function_keywords.values()
            for keyword in keywords
        )
    )
    return (
        "Focused TypeError repair: update production function(s) "
        + function_text
        + " to accept and handle keyword(s) "
        + keyword_text
        + " together, or change the paired test to call the real production API. Prefer changing the newly added "
        "test/caller to the existing production API unless the parent task explicitly requires a widened public API. "
        "Do not add unused keyword parameters that leave the old required object as `None`; either build the real "
        "task/worktree data from the keywords or keep the test on the existing dict-based API. Do not spend the repair on imports, "
        "formatting, or unrelated scaffolding while this root-cause TypeError remains."
    )


def _name_error_repair_instruction(text: str) -> str:
    matches = list(
        re.finditer(
            r"NameError:\s+name ['\"]([^'\"]+)['\"] is not defined",
            str(text or ""),
        )
    )
    if not matches:
        return ""
    symbols = list(dict.fromkeys(symbol for symbol in (match.group(1) for match in matches) if symbol))
    symbol_text = ", ".join("`" + symbol + "`" for symbol in symbols[:4])
    return (
        "Focused NameError repair: resolve undefined symbol(s) "
        + symbol_text
        + " before chasing later failures. Inspect the changed source/test files for existing helpers or public "
        "APIs, then either define/export the missing production helper or update the new code/test to call the real "
        "existing helper. If the undefined name is a module alias used only by the new test, fix the test import or "
        "call the already-imported function instead of changing production code for a test-local alias failure. Do "
        "not leave a preserved patch calling a name that is still undefined."
    )


def _focused_failure_first_repair_instruction(context: str) -> str:
    matches = list(re.finditer(r"Focused failure:\s*(.+)", str(context or ""), flags=re.IGNORECASE | re.DOTALL))
    failure = (
        " ".join(matches[-1].group(1).split())
        if matches
        else _compact_focused_test_failure_output(context)
    )
    if not failure:
        return ""
    return (
        "First repair the exact focused verification failure before broader edits: "
        + failure[:420].rstrip()
        + "."
    )


def _positional_argument_repair_instruction(text: str) -> str:
    matches = list(
        re.finditer(
            r"TypeError:\s+([A-Za-z_][\w.]*)\(\) takes (?:from )?[\w\s]+ positional arguments? but (\d+) (?:were|was) given",
            str(text or ""),
        )
    )
    if not matches:
        return ""
    function_names = list(dict.fromkeys(match.group(1) for match in matches))
    function_text = ", ".join("`" + function_name + "`" for function_name in function_names)
    given_counts = list(dict.fromkeys(match.group(2) for match in matches))
    count_text = "/".join(given_counts)
    return (
        "Focused TypeError repair: production function(s) "
        + function_text
        + " are being called with "
        + count_text
        + " positional arguments. Inspect the current function definition and existing call sites before editing. "
        "Prefer changing the newly added test/caller to the existing production API unless the parent task explicitly "
        "requires a widened public API. Only update the production signature when you also update all internal call "
        "sites and implement real behavior for the new values. Do not merely add unused optional parameters; the "
        "implementation must handle the same values the test passes. Scan the entire failing test method for sibling "
        "calls that use the same invented API shape and fix them in the same edit before rerunning verification."
    )


def _missing_required_argument_repair_instruction(text: str) -> str:
    matches = list(
        re.finditer(
            r"TypeError:\s+([A-Za-z_][\w.]*)\(\) missing \d+ required positional arguments?: ['\"]([^'\"]+)['\"]",
            str(text or ""),
        )
    )
    if not matches:
        return ""
    function_args: dict[str, list[str]] = {}
    for function_name, argument in (match.groups() for match in matches):
        function_args.setdefault(function_name, [])
        if argument not in function_args[function_name]:
            function_args[function_name].append(argument)
    function_text = ", ".join("`" + function_name + "`" for function_name in function_args)
    argument_text = ", ".join(
        "`" + argument + "`"
        for argument in dict.fromkeys(
            argument
            for arguments in function_args.values()
            for argument in arguments
        )
    )
    return (
        "Focused TypeError repair: production function(s) "
        + function_text
        + " require positional argument(s) "
        + argument_text
        + ". Inspect the current production signature and existing callers before editing. Prefer changing the "
        "newly added test/caller to pass the real required values unless the parent task explicitly requires a "
        "new public API shape. Do not hide the error by adding optional defaults that ignore missing data. Scan the "
        "entire failing test method for sibling calls that use the same incomplete argument list."
    )


def _string_dict_attribute_repair_instruction(text: str) -> str:
    raw_text = str(text or "")
    if (
        "AttributeError: 'str' object has no attribute 'get'" not in raw_text
        and "AttributeError: 'NoneType' object has no attribute 'get'" not in raw_text
    ):
        return ""
    return (
        "Focused AttributeError repair: the failing path passes a string or `None` into production code that "
        "expects a task dict. Inspect the current production signature and callers before editing. Prefer updating "
        "the new test to pass the task dict shape the helper already expects, or call the helper/API that actually "
        "accepts issue/run strings. Only change the production function signature when the parent task explicitly "
        "requires that new API and all call sites are updated together. Do not leave a string or `None` argument "
        "flowing into dict-only `.get(...)` code. Scan the whole failing test method for sibling helper calls and "
        "assertions that were written around the same invented input shape."
    )


def _future_import_syntax_repair_instruction(text: str) -> str:
    lowered = str(text or "").lower()
    if (
        "syntaxerror:" not in lowered
        or "from __future__ imports must occur at the beginning of the file" not in lowered
    ):
        return ""
    return (
        "Focused SyntaxError repair: keep the module docstring, comments, and `from __future__` imports at the "
        "top of the Python file before any other code. Move new imports, dataclasses, helpers, and constants below "
        "the existing future import before changing behavior or tests."
    )


def _assertion_failure_repair_instruction(text: str) -> str:
    lowered = str(text or "").lower()
    if "assertionerror:" not in lowered and "\nfail:" not in lowered and "failed (failures=" not in lowered:
        return ""
    return (
        "Focused assertion repair: reconcile the expected and actual values against the original task contract, "
        "not against the current broken patch. Inspect existing production callers or tests that define the public "
        "format before choosing an expected value. If the product behavior is wrong, update the production code; if "
        "the new test expected an invented or unsupported public behavior, update the test expectation to the real "
        "contract. Scan the entire failing test method and every assertion/caller it contains before editing; if one "
        "assertion mismatch was repaired but the same method still fails, update all related expectations to the same "
        "naming/serialization contract in one pass. For branch, worktree, run, or issue names, assume existing production helpers may slugify, "
        "normalize case, or truncate values; assert the real returned format or stable semantic properties rather "
        "than a raw mixed-case/full-id string unless the parent task explicitly requires that exact format. Keep the "
        "fix scoped to the preserved source/test files and rerun the focused verification."
    )


def _zero_division_assertion_repair_instruction(text: str) -> str:
    raw_text = str(text or "")
    if "ZeroDivisionError" not in raw_text or "assertRaises(ValueError)" not in raw_text:
        return ""
    return (
        "Focused zero-division repair: the carried patch raises `ZeroDivisionError`, but at least one new "
        "test expectation uses `assertRaises(ValueError)`. If the parent task requires a zero-division guard, "
        "fix the paired test expectations to assert `ZeroDivisionError`; do not change production to raise "
        "`ValueError` and do not only edit the exception message. Scan both direct `divide(..., 0)` coverage "
        "and `describe_operation(\"divide\", ..., 0)` coverage before rerunning focused verification."
    )


def _partial_diff_patch_text(artifact: dict[str, Any]) -> str:
    patch_path = str(artifact.get("patch_path") or "").strip()
    if not patch_path:
        return ""
    try:
        return Path(patch_path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _partial_diff_focused_verification_context(artifact: dict[str, Any]) -> tuple[list[str], str]:
    commands: list[str] = []

    def _append_command(raw_command: Any) -> None:
        if isinstance(raw_command, list):
            command = " ".join(str(part or "").strip() for part in raw_command if str(part or "").strip())
        else:
            command = str(raw_command or "").strip()
        if command and command not in commands:
            commands.append(command)

    _append_command(artifact.get("verification_command"))
    output = str(artifact.get("verification_output_excerpt") or "").strip()
    text = _partial_diff_patch_text(artifact)
    if text:
        verification_section = _markdown_section(text, "focused verification")
        for raw_line in verification_section.splitlines():
            line = raw_line.strip()
            if line.lower().startswith("- command:"):
                _append_command(line.split(":", 1)[1].strip())
        artifact_output = _markdown_section(text, "test output")
        if artifact_output and artifact_output not in output:
            output = f"{output}\n{artifact_output}".strip()

    summary_parts: list[str] = []
    if commands:
        summary_parts.append("Focused verification command: " + commands[0])
    failure_summary = _compact_focused_test_failure_output(output)
    if failure_summary:
        summary_parts.append("Focused failure: " + failure_summary)
    return commands, " ".join(summary_parts).strip()


def _repo_path_looks_like_test_file(path: str) -> bool:
    rel_path = _normalize_repo_relative_path(path)
    if not rel_path:
        return False
    name = Path(rel_path).name.lower()
    return (
        "/tests/" in f"/{rel_path.lower()}/"
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(".test.py")
        or name.endswith("_test.rs")
        or name.endswith(".test.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".spec.ts")
        or name.endswith(".spec.tsx")
    )


def _repo_path_looks_like_production_source_file(path: str) -> bool:
    rel_path = _normalize_repo_relative_path(path)
    if not rel_path or _repo_path_looks_like_test_file(rel_path):
        return False
    return Path(rel_path).suffix.lower() in _PRODUCTION_SOURCE_EXTENSIONS


def _repo_path_looks_like_support_only_file(path: str) -> bool:
    rel_path = _normalize_repo_relative_path(path)
    if not rel_path:
        return False
    lowered = rel_path.lower()
    if lowered.startswith("docs/") or "/docs/" in f"/{lowered}/":
        return Path(lowered).suffix.lower() in {".md", ".mdx", ".rst", ".adoc", ""}
    return Path(lowered).suffix.lower() in {".md", ".mdx", ".rst", ".adoc", ".json", ".yml", ".yaml", ".toml"}


def _all_repo_paths_are_support_only(paths: list[str]) -> bool:
    normalized = [
        rel_path
        for rel_path in (_normalize_repo_relative_path(raw_path) for raw_path in paths)
        if rel_path
    ]
    return bool(normalized) and all(_repo_path_looks_like_support_only_file(path) for path in normalized)


def _all_changed_files_are_tests(changed_files: list[str]) -> bool:
    return bool(changed_files) and all(_repo_path_looks_like_test_file(path) for path in changed_files)


def _changed_files_include_source_and_test(changed_files: list[str]) -> bool:
    normalized = [
        rel_path
        for rel_path in (_normalize_repo_relative_path(raw_path) for raw_path in changed_files)
        if rel_path
    ]
    return bool(normalized) and any(_repo_path_looks_like_test_file(path) for path in normalized) and any(
        not _repo_path_looks_like_test_file(path) for path in normalized
    )


def _partial_diff_retry_should_verify_first(subtask: dict[str, Any], changed_files: list[str]) -> bool:
    if not _changed_files_include_source_and_test(changed_files):
        return False
    excerpt = str(subtask.get("repair_worker_output_excerpt") or "").lower()
    return (
        "worker_incomplete_verifiable_diff" in excerpt
        or "source plus required-test partial diff" in excerpt
        or "verify/fix the preserved patch" in excerpt
    )


def _paired_production_path_for_test_file(test_path: str, candidate_files: list[str]) -> str:
    test_rel = _normalize_repo_relative_path(test_path)
    if not test_rel:
        return ""
    candidate_set = set(candidate_files)
    test_obj = Path(test_rel)
    name = test_obj.name
    direct_candidates: list[str] = []
    if name.endswith("_test.py"):
        direct_candidates.append(test_obj.with_name(f"{name.removesuffix('_test.py')}.py").as_posix())
    for test_suffix, source_suffix in (
        (".test.py", ".py"),
        (".test.ts", ".ts"),
        (".test.tsx", ".tsx"),
        (".spec.ts", ".ts"),
        (".spec.tsx", ".tsx"),
    ):
        if name.endswith(test_suffix):
            direct_candidates.append(test_obj.with_name(f"{name.removesuffix(test_suffix)}{source_suffix}").as_posix())
    if name.startswith("test_") and name.endswith(".py"):
        direct_candidates.append(test_obj.with_name(f"{name.removeprefix('test_')}").as_posix())
        parent = test_obj.parent
        if parent.name == "tests":
            direct_candidates.append(parent.parent.joinpath(name.removeprefix("test_")).as_posix())
    for candidate in direct_candidates:
        if candidate in candidate_set and _repo_path_looks_like_production_source_file(candidate):
            return candidate

    test_stem = test_obj.stem
    for suffix in (".test", ".spec", "_test"):
        test_stem = test_stem.removesuffix(suffix)
    test_stem = test_stem.removeprefix("test_")
    scored_candidates: list[tuple[int, str]] = []
    for candidate in candidate_files:
        if not _repo_path_looks_like_production_source_file(candidate):
            continue
        candidate_obj = Path(candidate)
        candidate_stem = candidate_obj.stem
        if test_stem != candidate_stem and not test_stem.endswith(f"_{candidate_stem}"):
            continue
        same_parent_score = 0 if candidate_obj.parent == test_obj.parent else 1
        scored_candidates.append((same_parent_score, candidate))
    if not scored_candidates:
        return ""
    return sorted(scored_candidates)[0][1]


def _direct_production_candidates_for_test_file(test_path: str) -> list[str]:
    test_rel = _normalize_repo_relative_path(test_path)
    if not test_rel:
        return []
    test_obj = Path(test_rel)
    name = test_obj.name
    candidates: list[str] = []
    if name.endswith("_test.py"):
        candidates.append(test_obj.with_name(f"{name.removesuffix('_test.py')}.py").as_posix())
    for test_suffix, source_suffix in (
        (".test.py", ".py"),
        (".test.ts", ".ts"),
        (".test.tsx", ".tsx"),
        (".spec.ts", ".ts"),
        (".spec.tsx", ".tsx"),
    ):
        if name.endswith(test_suffix):
            candidates.append(test_obj.with_name(f"{name.removesuffix(test_suffix)}{source_suffix}").as_posix())
    if name.startswith("test_") and name.endswith(".py"):
        candidates.append(test_obj.with_name(f"{name.removeprefix('test_')}").as_posix())
        if test_obj.parent.name == "tests":
            candidates.append(test_obj.parent.parent.joinpath(name.removeprefix("test_")).as_posix())
    return [
        rel_path
        for rel_path in dict.fromkeys(_normalize_repo_relative_path(candidate) for candidate in candidates)
        if rel_path and _repo_path_looks_like_production_source_file(rel_path)
    ]


def _source_partial_declared_test_followup_files(subtask: dict[str, Any]) -> list[str]:
    changed_files = [
        rel_path
        for rel_path in (
            _normalize_repo_relative_path(raw_path)
            for raw_path in (subtask.get("repair_changed_files") or [])
        )
        if rel_path and rel_path != "__aca_temp_probe.txt"
    ]
    if not changed_files or any(_repo_path_looks_like_test_file(path) for path in changed_files):
        return []
    declared_tests = [
        path
        for path in sorted(_subtask_declared_files(subtask))
        if _repo_path_looks_like_test_file(path)
    ]
    if not declared_tests:
        return []
    text = "\n".join(
        str(part or "")
        for part in [
            subtask.get("title"),
            subtask.get("goal"),
            subtask.get("scope_note"),
            *list(subtask.get("acceptance_criteria") or []),
        ]
    ).lower()
    if not any(word in text for word in ("test", "tests", "coverage", "regression")):
        return []
    return declared_tests


def _testless_partial_required_test_files(subtask: dict[str, Any]) -> list[str]:
    excerpt = str(subtask.get("repair_worker_output_excerpt") or "")
    lowered = excerpt.lower()
    marker = "required test files were"
    if marker not in lowered:
        return []
    tail = excerpt[lowered.index(marker) + len(marker) :]
    tail = tail.split("\n", 1)[0].strip(" :.")
    files: list[str] = []
    for raw_path in re.split(r"[,;]\s*|\s+and\s+", tail):
        rel_path = _normalize_repo_relative_path(raw_path.strip(" `'.\t"))
        if rel_path and _repo_path_looks_like_test_file(rel_path):
            files.append(rel_path)
    return list(dict.fromkeys(files))


def _paired_test_files_for_source_partial(
    source_files: list[str],
    candidate_files: list[str],
) -> list[str]:
    normalized_sources = list(
        dict.fromkeys(
            rel_path
            for rel_path in (_normalize_repo_relative_path(raw_path) for raw_path in source_files)
            if rel_path and _repo_path_looks_like_production_source_file(rel_path)
        )
    )
    if not normalized_sources:
        return []
    paired_tests: list[str] = []
    for candidate in candidate_files:
        rel_path = _normalize_repo_relative_path(candidate)
        if not rel_path or not _repo_path_looks_like_test_file(rel_path):
            continue
        if _paired_production_path_for_test_file(rel_path, normalized_sources):
            paired_tests.append(rel_path)
    return list(dict.fromkeys(paired_tests))


def _source_only_timeout_required_test_files(
    artifact: dict[str, Any],
    changed_files: list[str],
    worker_output_excerpt: str,
    candidate_test_files: list[str] | None = None,
) -> list[str]:
    timeout_text = (
        str(artifact.get("failure_reason") or "")
        + "\n"
        + str(worker_output_excerpt or "")
    ).lower()
    if "engine_prompt_timeout" not in timeout_text:
        return []
    if not changed_files or not all(_repo_path_looks_like_production_source_file(path) for path in changed_files):
        return []
    artifact_targets = _partial_diff_artifact_target_files(artifact)
    return _source_partial_declared_test_followup_files(
        {
            "repair_changed_files": changed_files,
            "files": artifact_targets,
            "target_files": artifact_targets,
            "acceptance_criteria": [
                "Retry the engine timeout with required regression coverage."
            ],
        }
    ) or [
        path
        for path in artifact_targets
        if _repo_path_looks_like_test_file(path)
    ] or _paired_test_files_for_source_partial(changed_files, candidate_test_files or [])


def _test_only_partial_production_followup_files(
    subtask: dict[str, Any],
    extra_candidate_files: list[str] | None = None,
) -> list[str]:
    changed_files = [
        rel_path
        for rel_path in (
            _normalize_repo_relative_path(raw_path)
            for raw_path in (subtask.get("repair_changed_files") or [])
        )
        if rel_path and rel_path != "__aca_temp_probe.txt"
    ]
    if not _all_changed_files_are_tests(changed_files):
        return []
    declared_candidate_files = list(sorted(_subtask_declared_files(subtask)))
    candidate_files = list(declared_candidate_files)
    for raw_path in extra_candidate_files or []:
        rel_path = _normalize_repo_relative_path(raw_path)
        if rel_path and rel_path not in candidate_files:
            candidate_files.append(rel_path)
    paired_files = [
        paired_path
        for paired_path in (
            _paired_production_path_for_test_file(changed_file, candidate_files)
            for changed_file in changed_files
        )
        if paired_path
    ]
    if paired_files:
        return list(dict.fromkeys(paired_files))

    declared_production_files = [
        path
        for path in declared_candidate_files
        if _repo_path_looks_like_production_source_file(path)
    ]
    if declared_production_files:
        return declared_production_files[:1]
    sticky_production_files = [path for path in candidate_files if _repo_path_looks_like_production_source_file(path)]
    return sticky_production_files[:1]


def _test_only_partial_existing_sibling_production_files(
    ctx: RunContext,
    test_files: list[str],
) -> list[str]:
    try:
        repo_path = ctx.repo_path
    except Exception:
        repo_path = Path(".")
    production_files: list[str] = []
    for test_file in test_files:
        for candidate in _direct_production_candidates_for_test_file(test_file):
            try:
                if (repo_path / candidate).is_file():
                    production_files.append(candidate)
                    break
            except OSError:
                continue
    return list(dict.fromkeys(production_files))



def _select_partial_diff_subtask(
    artifact: dict[str, Any],
    subtasks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not subtasks:
        return None
    target_subtask_id = str(artifact.get("subtask_id") or "").strip()
    if target_subtask_id:
        for subtask in subtasks:
            if str(subtask.get("id") or "").strip() == target_subtask_id:
                return subtask
    changed_files = set(_partial_diff_changed_files(artifact))
    if changed_files:
        best_subtask: dict[str, Any] | None = None
        best_overlap = 0
        for subtask in subtasks:
            overlap = len(changed_files.intersection(_subtask_declared_files(subtask)))
            if overlap > best_overlap:
                best_subtask = subtask
                best_overlap = overlap
        if best_subtask is not None:
            return best_subtask
    return subtasks[0]


def _append_unique_repo_paths(subtask: dict[str, Any], paths: list[str]) -> None:
    if not paths:
        return
    for key in ("files", "target_files"):
        values = [
            rel_path
            for rel_path in (_normalize_repo_relative_path(raw_path) for raw_path in (subtask.get(key) or []))
            if rel_path
        ]
        for rel_path in paths:
            if rel_path not in values:
                values.append(rel_path)
        subtask[key] = values


def _partial_diff_artifact_target_files(artifact: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for key in ("subtask_target_files", "subtask_files"):
        for raw_path in artifact.get(key) or []:
            rel_path = _normalize_repo_relative_path(raw_path)
            if rel_path and rel_path not in files:
                files.append(rel_path)
        if files:
            break
    return files


def _repo_uses_sibling_python_tests(repo_path: Path) -> bool:
    if (repo_path / "tests").is_dir():
        return False
    try:
        return any((repo_path / "src").glob("**/*_test.py"))
    except Exception:
        return False


def _sibling_python_test_path_for_top_level_test(path: str, declared_files: set[str]) -> str:
    normalized = _normalize_repo_relative_path(path)
    if not normalized.startswith("tests/test_") or not normalized.endswith(".py"):
        return normalized
    test_stem = Path(normalized).stem
    if not test_stem.startswith("test_"):
        return normalized
    source_stem = test_stem.removeprefix("test_")
    source_paths = [
        Path(declared)
        for declared in sorted(declared_files)
        if declared.startswith("src/")
        and Path(declared).suffix == ".py"
        and not Path(declared).stem.endswith("_test")
        and Path(declared).name != "__init__.py"
    ]
    for source_path in source_paths:
        if source_path.stem == source_stem or source_stem.endswith(f"_{source_path.stem}"):
            return source_path.with_name(f"{source_path.stem}_test.py").as_posix()
    non_main_sources = [source_path for source_path in source_paths if source_path.stem != "main"]
    if len(non_main_sources) == 1:
        source_path = non_main_sources[0]
        return source_path.with_name(f"{source_path.stem}_test.py").as_posix()
    if len(source_paths) == 1:
        source_path = source_paths[0]
        return source_path.with_name(f"{source_path.stem}_test.py").as_posix()
    for declared in sorted(declared_files):
        source_path = Path(declared)
        if (
            declared.startswith("src/")
            and source_path.suffix == ".py"
            and source_path.stem == source_stem
        ):
            return source_path.with_name(f"{source_path.stem}_test.py").as_posix()
    return normalized


def _align_python_test_targets_to_repo_conventions(repo_path: Path, subtasks: list[dict[str, Any]]) -> None:
    if not _repo_uses_sibling_python_tests(repo_path):
        return
    for subtask in subtasks:
        declared_files = _subtask_declared_files(subtask)
        if not declared_files:
            continue
        rewrites: dict[str, str] = {}
        for rel_path in declared_files:
            rewritten = _sibling_python_test_path_for_top_level_test(rel_path, declared_files)
            if rewritten and rewritten != rel_path:
                rewrites[rel_path] = rewritten
        if not rewrites:
            continue
        for key in ("files", "target_files"):
            values: list[str] = []
            for raw_path in subtask.get(key) or []:
                rel_path = _normalize_repo_relative_path(raw_path)
                if not rel_path:
                    continue
                values.append(rewrites.get(rel_path, rel_path))
            subtask[key] = list(dict.fromkeys(values))
        existing_scope_note = str(subtask.get("scope_note") or "").strip()
        rewrite_note = (
            "ACA aligned generated Python test targets to this repository's sibling "
            "*_test.py convention: "
            + ", ".join(f"{old} -> {new}" for old, new in sorted(rewrites.items()))
            + "."
        )
        if rewrite_note not in existing_scope_note:
            subtask["scope_note"] = f"{existing_scope_note}\n{rewrite_note}".strip()


def _split_dense_serial_subtasks(
    ctx: RunContext,
    subtasks: list[dict[str, Any]],
    *,
    max_acceptance_criteria: int = 3,
) -> None:
    """Break oversized subtasks into sequential slices when ACA is dispatching serially."""
    swarm_cfg = getattr(getattr(ctx, "cfg", None), "swarm", None)
    if bool(getattr(swarm_cfg, "enabled", False)):
        return
    if max_acceptance_criteria < 1 or not subtasks:
        return
    serial_limit = _serial_subtask_limit(ctx.cfg)

    split: list[dict[str, Any]] = []
    changed = False
    for subtask in subtasks:
        criteria = [
            str(entry).strip()
            for entry in (subtask.get("acceptance_criteria") or [])
            if str(entry).strip()
        ]
        skip_repair_split = bool(
            subtask.get("carry_forward_patch")
            or subtask.get("carry_forward_patches")
            or subtask.get("discarded_partial_diff_patch")
            or subtask.get("repair_parent_target_files")
        )
        if skip_repair_split or len(criteria) <= max_acceptance_criteria:
            split.append(subtask)
            continue

        chunks = [
            criteria[index : index + max_acceptance_criteria]
            for index in range(0, len(criteria), max_acceptance_criteria)
        ]
        if len(chunks) <= 1:
            split.append(subtask)
            continue

        changed = True
        base_id = str(subtask.get("id") or f"subtask-{len(split) + 1}").strip()
        base_title = str(subtask.get("title") or "Subtask").strip()
        original_scope_note = str(subtask.get("scope_note") or "").strip()
        split_note = (
            "ACA split this dense manager subtask into serial slices because swarm is disabled. "
            "Only one worker slice runs at a time; focus on this slice's acceptance criteria, "
            "then later slices cover the remaining criteria against the same target files."
        )
        for chunk_index, chunk in enumerate(chunks, start=1):
            cloned = dict(subtask)
            cloned["id"] = f"{base_id}-part-{chunk_index}"
            cloned["title"] = f"{base_title} (part {chunk_index}/{len(chunks)})"
            cloned["acceptance_criteria"] = list(chunk)
            cloned["dense_parent_subtask_id"] = base_id
            cloned["dense_part_index"] = chunk_index
            cloned["dense_part_count"] = len(chunks)
            cloned["scope_note"] = (
                f"{original_scope_note}\n{split_note}".strip()
                if split_note not in original_scope_note
                else original_scope_note
            )
            split.append(cloned)

    if changed:
        subtasks[:] = split

    fallback_cap_active = bool(
        getattr(ctx, "_manager_fallback_required", False)
        or (
            isinstance(getattr(ctx, "blackboard", None), dict)
            and (
                "manager_invalid_plan" in ctx.blackboard
                or "manager_deterministic_repo_context_plan" in ctx.blackboard
            )
        )
    )
    if not fallback_cap_active:
        return

    limit = serial_limit
    if len(subtasks) <= limit:
        return

    retained = subtasks[:limit]
    deferred = subtasks[limit:]
    cap_note = (
        f"ACA capped deterministic fallback planning to {limit} serial slice(s) "
        "for spend safety after manager planning failed."
    )
    for subtask in retained:
        scope_note = str(subtask.get("scope_note") or "").strip()
        if cap_note not in scope_note:
            subtask["scope_note"] = f"{scope_note}\n{cap_note}".strip()
    if isinstance(getattr(ctx, "blackboard", None), dict):
        ctx.blackboard["manager_fallback_serial_cap"] = {
            "limit": limit,
            "original_planned_workers": len(subtasks),
            "retained_subtask_ids": [str(item.get("id") or "").strip() for item in retained],
            "deferred_subtask_ids": [str(item.get("id") or "").strip() for item in deferred],
        }
    subtasks[:] = retained


def _remote_code_task_requires_worker_execution(task: dict[str, Any]) -> bool:
    """Remote code tasks need an explicit worker verdict, not file-presence proof."""
    source = task.get("source") if isinstance(task, dict) else {}
    source_type = str(source.get("type") or "").strip() if isinstance(source, dict) else ""
    execution_kind = str(task.get("execution_kind") or "").strip()
    return execution_kind == "code_edit" and source_type in {"linear", "github_project"}


def _apply_repo_context_required_files_to_task(task: dict[str, Any], required_files: list[str] | None) -> bool:
    """Promote graph-required files into the task target contract when absent."""
    if not isinstance(task, dict):
        return False
    normalized = list(
        dict.fromkeys(
            str(entry).strip().replace("\\", "/")
            for entry in (required_files or [])
            if str(entry).strip()
        )
    )
    if not normalized:
        return False
    existing_contract = task_contract_payload(task)
    existing_targets = [
        str(entry).strip().replace("\\", "/")
        for entry in (existing_contract.get("target_files") or task.get("target_files") or [])
        if str(entry).strip()
    ]
    if existing_targets:
        return set(normalized).issubset(set(existing_targets))
    task["target_files"] = normalized
    task.setdefault("task_contract", {})
    if isinstance(task["task_contract"], dict):
        task["task_contract"].setdefault("target_files", normalized)
    return True


def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from model output (imported from runner_core)."""
    # Import here to avoid circular imports; runner_core is the owner of this util
    from src.tandem_agents.core.execution import runner_core as _rc  # noqa: PLC0415
    return _rc._extract_json(text)


def _manager_plan_from_stdout(stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse and validate the manager JSON contract."""
    payload = _extract_json(stdout)
    if not isinstance(payload, dict):
        return None, "Manager planning did not return a valid JSON object."
    missing = sorted(_MANAGER_PLAN_REQUIRED_KEYS.difference(payload))
    if missing:
        return None, "Manager planning JSON is missing required key(s): " + ", ".join(missing)
    if not isinstance(payload.get("subtasks"), list):
        return None, "Manager planning JSON field `subtasks` must be a list."
    for key in ("risks", "tests"):
        if not isinstance(payload.get(key), list):
            return None, f"Manager planning JSON field `{key}` must be a list."
    return payload, None


def _nonfallbackable_manager_engine_failure(stdout: str) -> str:
    text = str(stdout or "").strip()
    lowered = text.lower()
    if not text:
        return ""
    provider_dispatch_failed = (
        "engine_dispatch_failed" in lowered
        or "failed to reach provider" in lowered
    )
    if not provider_dispatch_failed:
        return ""
    detail = text.splitlines()[0].strip()[:500]
    return (
        "Manager planning could not reach the configured engine provider, so ACA will not launch "
        "fallback workers from a generic plan. "
        + detail
    ).strip()


def _prepare_subtasks(ctx: RunContext) -> tuple[list[str], list[dict[str, Any]]]:
    """Call the private runner_core subtask-preparation helper.

    Returns (discovered_files, subtasks).  Kept as a thin bridge so callers
    don't need to import the private helper directly.
    """
    from src.tandem_agents.core.execution import runner_core as _rc  # noqa: PLC0415
    from pathlib import Path
    # Dispatch concurrency is limited later. When swarm is disabled, default to
    # one coherent worker slice; operators can raise ACA_SERIAL_SUBTASK_LIMIT
    # when they intentionally want a serial queue.
    if ctx.cfg.swarm.enabled:
        planning_subtask_limit = max(1, int(ctx.cfg.swarm.max_workers or 1))
    else:
        planning_subtask_limit = _serial_subtask_limit(ctx.cfg)
    discovered_files, subtasks = _rc._prepare_subtasks_with_discovery(
        ctx.task,
        ctx.manager_plan,
        Path(ctx.repo.get("path") or "."),
        planning_subtask_limit,
        merge_manager_subtasks=not bool(ctx.cfg.swarm.enabled),
    )
    return _constrain_invalid_manager_fallback(ctx, discovered_files, subtasks)


def _append_deferred_repair_subtasks(ctx: RunContext, subtasks: list[dict[str, Any]]) -> None:
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else None
    if not isinstance(repair, dict):
        repair = ctx.status.get("repair") if isinstance(ctx.status, dict) else None
    if not isinstance(repair, dict):
        return
    raw_deferred = repair.get("deferred_subtasks")
    if not isinstance(raw_deferred, list) or not raw_deferred:
        return
    existing_ids = {str(subtask.get("id") or "").strip() for subtask in subtasks}
    appended = 0
    for item in raw_deferred:
        if not isinstance(item, dict):
            continue
        subtask = dict(item)
        subtask_id = str(subtask.get("id") or "").strip()
        if not subtask_id or subtask_id in existing_ids:
            continue
        scope_note = str(subtask.get("scope_note") or "").strip()
        continuation_note = (
            "ACA deferred this serial subtask while repairing an earlier failed slice; "
            "resume it after the repair slice completes."
        )
        if continuation_note not in scope_note:
            subtask["scope_note"] = f"{scope_note}\n{continuation_note}".strip()
        subtasks.append(subtask)
        existing_ids.add(subtask_id)
        appended += 1
    if appended:
        repair["deferred_subtasks_appended"] = appended


def _serial_subtask_limit(cfg: Any) -> int:
    raw = str(getattr(cfg, "env", {}).get("ACA_SERIAL_SUBTASK_LIMIT", "") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_SERIAL_SUBTASK_LIMIT=%r", raw)
    return 1


def _safe_repo_context_path(rel_path: Any, repo_path: Path) -> str:
    rel_text = str(rel_path or "").strip()
    if not rel_text or rel_text.startswith("/"):
        return ""
    rel = Path(rel_text)
    if ".." in rel.parts:
        return ""
    if not (repo_path / rel).is_file():
        return ""
    return rel.as_posix()


def _repo_context_fallback_files(ctx: RunContext) -> list[str]:
    repo_path = Path(ctx.repo.get("path") or ctx.repo_path or ".")
    repo_context = ctx.blackboard.get("repo_context") if isinstance(ctx.blackboard, dict) else {}
    if not isinstance(repo_context, dict):
        return []
    candidates: list[str] = []
    for rel_path in repo_context.get("required_files") or []:
        candidates.append(str(rel_path or ""))
    artifact_path = str(repo_context.get("artifact_path") or "").strip()
    if artifact_path:
        try:
            artifact = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
        except Exception:
            artifact = {}
        bundle = artifact.get("bundle") if isinstance(artifact, dict) else {}
        if isinstance(bundle, dict):
            for rel_path in bundle.get("suggested_first_reads") or []:
                candidates.append(str(rel_path or ""))
            for item in bundle.get("likely_files") or []:
                if isinstance(item, dict):
                    candidates.append(str(item.get("file_path") or ""))
            for rel_path in bundle.get("test_targets") or []:
                candidates.append(str(rel_path or ""))
    fallback_files: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        rel_path = _safe_repo_context_path(candidate, repo_path)
        if rel_path and rel_path not in seen:
            fallback_files.append(rel_path)
            seen.add(rel_path)
    return fallback_files


def _criteria_matching(acceptance_criteria: list[str], keywords: tuple[str, ...]) -> list[str]:
    matches: list[str] = []
    for criterion in acceptance_criteria:
        lowered = criterion.lower()
        if any(keyword in lowered for keyword in keywords):
            matches.append(criterion)
    return matches


def _all_task_acceptance_criteria(ctx: RunContext) -> list[str]:
    criteria = task_contract_payload(ctx.task).get("acceptance_criteria") or ctx.task.get("acceptance_criteria") or []
    return [str(entry).strip() for entry in criteria if str(entry).strip()]


def _deterministic_invalid_manager_subtasks(
    ctx: RunContext,
    fallback_files: list[str],
    existing_subtasks: list[dict[str, Any]],
    fallback_label: str = "repo-context fallback targets",
    *,
    cap_disabled_swarm: bool = True,
) -> list[dict[str, Any]]:
    """Build a stable fallback plan from repo-context files after manager JSON failure."""
    if not fallback_files:
        return []

    swarm_cfg = getattr(ctx.cfg, "swarm", None)
    if bool(getattr(swarm_cfg, "enabled", False)) or cap_disabled_swarm:
        max_workers = max(1, int(getattr(swarm_cfg, "max_workers", 1) or 1))
    else:
        max_workers = max(1, len(fallback_files), len(existing_subtasks))
    title = str(ctx.task.get("title") or ctx.task.get("local_goal") or "ACA fallback plan").strip()
    acceptance_criteria = _all_task_acceptance_criteria(ctx)
    by_name = {Path(path).name: path for path in fallback_files}

    planned: list[dict[str, Any]] = []

    repository_files = [
        path
        for path in (by_name.get("repository.py"), by_name.get("repository_test.py"))
        if path
    ]
    if repository_files:
        planned.append(
            {
                "id": "fallback-repository-isolation",
                "title": f"{title} - repository isolation",
                "goal": "Implement and verify per-issue repository worktree, branch, and pinned-base planning.",
                "files": repository_files,
                "target_files": repository_files,
                "acceptance_criteria": _criteria_matching(
                    acceptance_criteria,
                    ("worktree", "branch", "base revision", "mutable working directory", "parallel"),
                )
                or acceptance_criteria,
            }
        )

    intake_files = [path for path in (by_name.get("task_intake.py"),) if path]
    if intake_files:
        planned.append(
            {
                "id": "fallback-intake-conflicts",
                "title": f"{title} - intake conflict tracking",
                "goal": "Record claimed-issue isolation metadata and detect overlapping active run edits before merge risk.",
                "files": intake_files,
                "target_files": intake_files,
                "acceptance_criteria": _criteria_matching(
                    acceptance_criteria,
                    ("claimed", "touched", "generated artifact", "overlap", "conflict", "pause", "serialize", "operator"),
                )
                or acceptance_criteria,
            }
        )

    finalize_files = [
        path
        for path in (by_name.get("finalize.py"), by_name.get("pr_body.py"))
        if path
    ]
    if finalize_files:
        planned.append(
            {
                "id": "fallback-finalize-pr-metadata",
                "title": f"{title} - finalize and PR metadata",
                "goal": "Preserve cleanup behavior and include Linear issue plus ACA run metadata in PR output.",
                "files": finalize_files,
                "target_files": finalize_files,
                "acceptance_criteria": _criteria_matching(
                    acceptance_criteria,
                    ("cleanup", "success", "failure", "lease", "pr metadata", "linear", "run id"),
                )
                or acceptance_criteria,
            }
        )

    config_types_file = by_name.get("config_types.py")
    config_loader_file = by_name.get("config_loader.py")
    config_loader_test_file = by_name.get("config_loader_test.py")
    if config_types_file:
        planned.append(
            {
                "id": "fallback-throughput-config-types",
                "title": f"{title} - scheduler budget config fields",
                "goal": "Add the explicit scheduler budget, concurrency, rate-limit, CI, and merge queue backpressure config fields.",
                "files": [config_types_file],
                "target_files": [config_types_file],
                "acceptance_criteria": [
                    "In src/tandem_agents/config/config_types.py, extend SchedulerConfig with max_concurrent_worker_runs, max_daily_model_spend_cents, rate_limit_backpressure, ci_backpressure, and merge_queue_backpressure. Use max_concurrent_worker_runs=4, max_daily_model_spend_cents=0, and True for each backpressure toggle as defaults.",
                    "Add those exact scheduler fields to ResolvedConfig.as_dict() under the scheduler payload if the scheduler payload enumerates fields explicitly.",
                    "Do not add alias helpers, legacy scheduler key translation, config.aca fields, an ACAConfig type, or new scheduler field names such as max_parallel_workers, max_pending_tasks, worker_start_interval_seconds, max_run_cost_usd, max_worker_cost_usd, max_concurrent_workers, max_queued_tasks, worker_start_rate_per_minute, cost_budget_usd, max_active_runs, or max_active_workers.",
                ],
                "scope_note": (
                    "Mechanical slice 1 of 3 for throughput config controls. Edit only config_types.py. "
                    "Do not touch config_loader.py or config_loader_test.py in this slice."
                ),
            }
        )
    if config_loader_file:
        planned.append(
            {
                "id": "fallback-throughput-config-loader",
                "title": f"{title} - scheduler budget config loader",
                "goal": "Wire exact scheduler budget and backpressure config fields from YAML and environment into SchedulerConfig.",
                "files": [config_loader_file],
                "target_files": [config_loader_file],
                "acceptance_criteria": [
                    "In src/tandem_agents/config/config_loader.py, load those fields from scheduler YAML keys and ACA_SCHEDULER_MAX_CONCURRENT_WORKER_RUNS, ACA_SCHEDULER_MAX_DAILY_MODEL_SPEND_CENTS, ACA_SCHEDULER_RATE_LIMIT_BACKPRESSURE, ACA_SCHEDULER_CI_BACKPRESSURE, and ACA_SCHEDULER_MERGE_QUEUE_BACKPRESSURE env vars.",
                    "Use the existing int and bool config loader helpers directly in the SchedulerConfig(...) construction.",
                    "Do not add alias helpers, legacy scheduler key translation, config.aca fields, an ACAConfig type, or new scheduler field names such as max_parallel_workers, max_pending_tasks, worker_start_interval_seconds, max_run_cost_usd, max_worker_cost_usd, max_concurrent_workers, max_queued_tasks, worker_start_rate_per_minute, cost_budget_usd, max_active_runs, or max_active_workers.",
                ],
                "scope_note": (
                    "Mechanical slice 2 of 3 for throughput config controls. Edit only config_loader.py. "
                    "Assume SchedulerConfig already has the exact fields; do not add tests in this slice."
                ),
            }
        )
    if config_loader_test_file:
        planned.append(
            {
                "id": "fallback-throughput-config-loader-tests",
                "title": f"{title} - scheduler budget config loader tests",
                "goal": "Add focused config loader coverage for exact scheduler budget and backpressure fields.",
                "files": [config_loader_test_file],
                "target_files": [config_loader_test_file],
                "acceptance_criteria": [
                    "In src/tandem_agents/config/config_loader_test.py, add one focused test covering defaults plus env overrides for those exact config.scheduler fields: max_concurrent_worker_runs, max_daily_model_spend_cents, rate_limit_backpressure, ci_backpressure, and merge_queue_backpressure.",
                    "The test must call resolve_config(root, env={...}) and assert only config.scheduler.max_concurrent_worker_runs, config.scheduler.max_daily_model_spend_cents, config.scheduler.rate_limit_backpressure, config.scheduler.ci_backpressure, and config.scheduler.merge_queue_backpressure.",
                    "Do not add alias helpers, legacy scheduler key translation, config.aca fields, an ACAConfig type, or new scheduler field names such as max_parallel_workers, max_pending_tasks, worker_start_interval_seconds, max_run_cost_usd, max_worker_cost_usd, max_concurrent_workers, max_queued_tasks, worker_start_rate_per_minute, cost_budget_usd, max_active_runs, or max_active_workers.",
                ],
                "scope_note": (
                    "Mechanical slice 3 of 3 for throughput config controls. Edit only config_loader_test.py. "
                    "This is a test-only slice after the config fields and loader wiring slices; do not edit production files here."
                ),
            }
        )

    throughput_scheduler_files = [
        path
        for path in (
            by_name.get("scheduler.py"),
            by_name.get("scheduler_test.py"),
        )
        if path
    ]
    if throughput_scheduler_files:
        planned.append(
            {
                "id": "fallback-throughput-scheduler-controls",
                "title": f"{title} - scheduler backpressure and caps",
                "goal": "Apply scheduler admission caps and backpressure decisions to queued task planning.",
                "files": throughput_scheduler_files,
                "target_files": throughput_scheduler_files,
                "acceptance_criteria": [
                    "In src/tandem_agents/core/scheduling/scheduler.py, make plan_task_admissions enforce cfg.scheduler.max_concurrent_worker_runs by subtracting currently active work before admitting new tasks.",
                    "Include max_concurrent_worker_runs and remaining worker slots in the plan_task_admissions limits payload so the operator can see which worker cap was applied.",
                    "When queued work remains because active-plus-admitted work reaches max_concurrent_worker_runs, append blocked entries for those remaining candidates with reason worker_concurrency_reached, preserving task_key, project_key, repo_key, scope_mode, and scope_paths.",
                    "In src/tandem_agents/core/scheduling/scheduler_test.py, add a focused test with max_active_tasks above max_concurrent_worker_runs that proves otherwise-admissible work is blocked with worker_concurrency_reached.",
                    "Update any existing scheduler test that intentionally admits six tasks so it sets max_concurrent_worker_runs high enough for that scenario.",
                    "Keep the change limited to scheduler behavior and its direct tests; config parsing is handled by the config-control slice.",
                ],
                "scope_note": (
                    "Suggested edit order: read only plan_task_admissions in scheduler.py, then add the max_concurrent_worker_runs cap and remaining-queue blocked entries; "
                    "next read the existing scheduler concurrency tests and add one focused worker-cap regression. "
                    "Do not add unrelated spend, CI, or merge queue plumbing in this slice."
                ),
            }
        )

    throughput_worker_files = [
        path
        for path in (by_name.get("worker_dispatch.py"), by_name.get("runner_core.py"))
        if path
    ]
    if throughput_worker_files:
        planned.append(
            {
                "id": "fallback-throughput-worker-metrics",
                "title": f"{title} - worker and run metrics",
                "goal": "Track worker/run timing, retry, token/cost, tool-call, test, and failure metrics in ACA run state.",
                "files": throughput_worker_files,
                "target_files": throughput_worker_files,
                "acceptance_criteria": _criteria_matching(
                    acceptance_criteria,
                    (
                        "cycle time",
                        "queue wait",
                        "active time",
                        "pr time",
                        "repair",
                        "merge time",
                        "token",
                        "tool calls",
                        "test time",
                        "failure rate",
                    ),
                )
                or acceptance_criteria,
            }
        )

    throughput_operator_files = [
        path
        for path in (
            by_name.get("operator_view.py"),
            by_name.get("operator_dashboard.py"),
            by_name.get("operator_dashboard_test.py"),
        )
        if path
    ]
    if throughput_operator_files:
        planned.append(
            {
                "id": "fallback-throughput-operator-cockpit",
                "title": f"{title} - operator cockpit visibility",
                "goal": "Expose active workers, queued issues, blocked issues, costs, failures, and scheduler state in the operator cockpit.",
                "files": throughput_operator_files,
                "target_files": throughput_operator_files,
                "acceptance_criteria": _criteria_matching(
                    acceptance_criteria,
                    ("operator", "active workers", "queued issues", "blocked issues", "costs", "failures", "cockpit"),
                )
                or acceptance_criteria,
            }
        )

    covered = {path for subtask in planned for path in subtask.get("files", [])}
    remaining = [path for path in fallback_files if path not in covered]
    if remaining:
        planned.append(
            {
                "id": "fallback-remaining-repo-context",
                "title": f"{title} - remaining repo-context targets",
                "goal": "Finish the remaining repo-context target files needed by the task contract.",
                "files": remaining,
                "target_files": remaining,
                "acceptance_criteria": acceptance_criteria,
            }
        )

    if not planned and existing_subtasks:
        planned = [dict(existing_subtasks[0])]
        planned[0]["files"] = list(fallback_files)
        planned[0]["target_files"] = list(fallback_files)
        planned[0]["acceptance_criteria"] = planned[0].get("acceptance_criteria") or acceptance_criteria

    fallback_note = (
        "ACA replaced invalid manager JSON with deterministic "
        + fallback_label
        + ": "
        + ", ".join(fallback_files)
        + "."
    )
    for index, subtask in enumerate(planned, start=1):
        subtask.setdefault("id", f"fallback-{index}")
        subtask.setdefault("title", f"{title} - fallback slice {index}")
        subtask.setdefault("goal", title)
        subtask.setdefault("acceptance_criteria", acceptance_criteria)
        subtask.setdefault("files", list(fallback_files))
        subtask.setdefault("target_files", list(subtask.get("files") or fallback_files))
        existing_scope_note = str(subtask.get("scope_note") or "").strip()
        if fallback_note not in existing_scope_note:
            subtask["scope_note"] = f"{existing_scope_note}\n{fallback_note}".strip()

    return planned[:max_workers]


def _constrain_invalid_manager_fallback(
    ctx: RunContext,
    discovered_files: list[str],
    subtasks: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    if not getattr(ctx, "_manager_fallback_required", False):
        return discovered_files, subtasks
    if "manager_invalid_plan" not in ctx.blackboard:
        return discovered_files, subtasks
    repo_path = Path(ctx.repo.get("path") or ctx.repo_path or ".")
    explicit_files = _task_or_explicit_target_files(ctx.task, repo_path)
    fallback_files = explicit_files or _repo_context_fallback_files(ctx)
    if not fallback_files:
        return [], []
    fallback_label = "explicit task fallback targets" if explicit_files else "repo-context fallback targets"
    fallback_subtasks = _deterministic_invalid_manager_subtasks(
        ctx,
        fallback_files,
        subtasks,
        fallback_label,
        cap_disabled_swarm=False,
    )
    if fallback_subtasks:
        ctx.blackboard["manager_deterministic_repo_context_plan"] = {
            "reason": (
                "invalid_manager_explicit_task_targets_fallback"
                if explicit_files
                else "invalid_manager_repo_context_fallback"
            ),
            "planned_workers": len(fallback_subtasks),
            "required_files": fallback_files,
        }
    return fallback_files, fallback_subtasks


def _should_use_deterministic_repo_context_plan(ctx: RunContext) -> bool:
    repo_context = ctx.blackboard.get("repo_context") if isinstance(ctx.blackboard, dict) else {}
    if not isinstance(repo_context, dict) or not bool(repo_context.get("required_files_applied_as_target_files")):
        return False
    source = ctx.task.get("source") if isinstance(ctx.task, dict) else {}
    source_type = str(source.get("type") or "").strip() if isinstance(source, dict) else ""
    execution_kind = str(ctx.task.get("execution_kind") or "").strip()
    if execution_kind != "code_edit" or source_type not in {"linear", "github_project", "manual"}:
        return False
    if "manager_invalid_plan" in ctx.blackboard or getattr(ctx, "_manager_fallback_required", False):
        return True
    raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_MANAGER_GRAPH_FIRST_REPO_CONTEXT_PLAN") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return (
        source_type == "linear"
        and str(repo_context.get("source") or "").strip() == "repo.context_bundle"
        and not bool(repo_context.get("fallback_used"))
    )


def _docs_only_target_files(paths: list[str]) -> bool:
    if not paths:
        return False
    return all(path.startswith("docs/") and Path(path).suffix.lower() in {".md", ".mdx"} for path in paths)


def _should_use_deterministic_explicit_target_plan(ctx: RunContext) -> bool:
    source = ctx.task.get("source") if isinstance(ctx.task, dict) else {}
    source_type = str(source.get("type") or "").strip() if isinstance(source, dict) else ""
    execution_kind = str(ctx.task.get("execution_kind") or "").strip()
    if execution_kind != "code_edit" or source_type not in {"linear", "github_project", "manual"}:
        return False
    raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_MANAGER_EXPLICIT_TARGET_PLAN") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    repo_path = Path(ctx.repo.get("path") or ctx.repo_path or ".")
    target_files = _task_or_explicit_target_files(ctx.task, repo_path)
    return _docs_only_target_files(target_files)


def _deterministic_explicit_target_manager_result(ctx: RunContext) -> dict[str, Any] | None:
    if not _should_use_deterministic_explicit_target_plan(ctx):
        return None
    repo_path = Path(ctx.repo.get("path") or ctx.repo_path or ".")
    target_files = _task_or_explicit_target_files(ctx.task, repo_path)
    if not target_files:
        return None
    title = str(ctx.task.get("title") or ctx.task.get("local_goal") or "ACA explicit target task").strip()
    acceptance_criteria = _all_task_acceptance_criteria(ctx)
    ctx.manager_plan = {
        "summary": (
            "ACA used explicit docs-only task target files to build a deterministic worker plan "
            "without spending a manager prompt."
        ),
        "subtasks": [
            {
                "id": "explicit-task-targets",
                "title": title,
                "goal": str(ctx.task.get("local_goal") or title),
                "files": target_files,
                "target_files": target_files,
                "acceptance_criteria": acceptance_criteria,
                "scope_note": (
                    "ACA skipped manager prompting because the task already declares a docs-only "
                    "target contract. Keep edits limited to: " + ", ".join(target_files) + "."
                ),
            }
        ],
        "risks": [
            "Deterministic explicit-target planning is scoped to docs-only task contracts."
        ],
        "tests": [],
    }
    ctx.blackboard["manager_plan"] = ctx.manager_plan
    ctx.blackboard["manager_deterministic_explicit_target_plan"] = {
        "reason": "explicit_docs_task_targets_graph_first",
        "planned_workers": 1,
        "required_files": target_files,
    }
    from src.tandem_agents.runtime.runstate import append_event, save_blackboard
    from src.tandem_agents.runtime.run_output import write_blackboard_snapshot

    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    append_event(
        ctx.layout["events"],
        "manager.deterministic_explicit_target_plan",
        ctx.run_id,
        ctx.blackboard["manager_deterministic_explicit_target_plan"],
        task_id=ctx.task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )
    return {
        "role": "manager",
        "output": json.dumps(ctx.manager_plan, indent=2),
        "returncode": 0,
        "engine": {"skipped": True, "reason": "explicit_docs_task_targets_graph_first"},
    }


def _deterministic_repo_context_manager_result(ctx: RunContext) -> dict[str, Any] | None:
    if not _should_use_deterministic_repo_context_plan(ctx):
        return None
    fallback_files = _repo_context_fallback_files(ctx)
    subtasks = _deterministic_invalid_manager_subtasks(ctx, fallback_files, [])
    if not fallback_files or not subtasks:
        return None
    reason = (
        "repo_context_required_files_after_manager_failure"
        if "manager_invalid_plan" in ctx.blackboard or getattr(ctx, "_manager_fallback_required", False)
        else "repo_context_required_files_graph_first"
    )
    ctx.manager_plan = {
        "summary": (
            "ACA used graph-required repo-context files to build a deterministic worker plan "
            "without spending a manager prompt."
        ),
        "subtasks": subtasks,
        "risks": [
            "Deterministic repo-context planning is scoped to graph-required files."
        ],
        "tests": [],
    }
    ctx.blackboard["manager_plan"] = ctx.manager_plan
    ctx.blackboard["manager_deterministic_repo_context_plan"] = {
        "reason": reason,
        "planned_workers": len(subtasks),
        "required_files": fallback_files,
    }
    from src.tandem_agents.runtime.runstate import append_event, save_blackboard
    from src.tandem_agents.runtime.run_output import write_blackboard_snapshot

    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    append_event(
        ctx.layout["events"],
        "manager.deterministic_repo_context_plan",
        ctx.run_id,
        ctx.blackboard["manager_deterministic_repo_context_plan"],
        task_id=ctx.task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )
    return {
        "role": "manager",
        "returncode": 0,
        "stdout": json.dumps(ctx.manager_plan),
        "stderr": "",
        "engine": {"skipped": True, "reason": reason},
    }


def _deterministic_repo_context_repair_manager_result(ctx: RunContext) -> dict[str, Any] | None:
    if "manager_deterministic_repo_context_plan" not in ctx.blackboard:
        return None
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    if not isinstance(repair, dict):
        return None
    attempt = _repair_int(repair.get("attempt"))
    if attempt <= 1:
        return None

    completed_ids = {
        str(subtask_id or "").strip()
        for subtask_id in repair.get("completed_subtask_ids") or []
        if str(subtask_id or "").strip()
    }
    subtasks: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_subtask(raw_subtask: Any, note: str) -> None:
        if not isinstance(raw_subtask, dict):
            return
        subtask = dict(raw_subtask)
        subtask_id = str(subtask.get("id") or "").strip()
        if not subtask_id or subtask_id in completed_ids or subtask_id in seen:
            return
        scope_note = str(subtask.get("scope_note") or "").strip()
        if note not in scope_note:
            subtask["scope_note"] = f"{scope_note}\n{note}".strip()
        subtasks.append(subtask)
        seen.add(subtask_id)

    add_subtask(
        repair.get("failed_subtask"),
        "ACA is retrying this deterministic repo-context slice exactly after the previous worker failed.",
    )
    for item in repair.get("deferred_subtasks") or []:
        add_subtask(
            item,
            "ACA deferred this deterministic repo-context slice while retrying an earlier failed slice.",
        )
    if not subtasks:
        return None

    ctx.manager_plan = {
        "summary": "ACA reused the deterministic repo-context repair queue instead of broad replanning.",
        "subtasks": subtasks,
        "risks": [
            "Repair planning was constrained to the failed deterministic slice plus deferred deterministic slices."
        ],
        "tests": [],
    }
    ctx.blackboard["manager_plan"] = ctx.manager_plan
    ctx.blackboard["manager_deterministic_repo_context_repair_plan"] = {
        "reason": "deterministic_repo_context_repair_queue",
        "attempt": attempt,
        "completed_subtask_ids": sorted(completed_ids),
        "planned_workers": len(subtasks),
        "subtask_ids": [str(subtask.get("id") or "").strip() for subtask in subtasks],
    }
    from src.tandem_agents.runtime.runstate import append_event, save_blackboard
    from src.tandem_agents.runtime.run_output import write_blackboard_snapshot

    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    append_event(
        ctx.layout["events"],
        "manager.deterministic_repo_context_repair_plan",
        ctx.run_id,
        ctx.blackboard["manager_deterministic_repo_context_repair_plan"],
        task_id=ctx.task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )
    return {
        "role": "manager",
        "returncode": 0,
        "stdout": json.dumps(ctx.manager_plan),
        "stderr": "",
        "engine": {"skipped": True, "reason": "deterministic_repo_context_repair_queue"},
    }


def _carry_forward_partial_diff_artifacts(ctx: RunContext, subtasks: list[dict[str, Any]]) -> None:
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    artifacts = repair.get("partial_diff_artifacts") if isinstance(repair, dict) else []
    if not isinstance(artifacts, list) or not artifacts:
        return
    for artifact_index in range(len(artifacts) - 1, -1, -1):
        artifact = artifacts[artifact_index]
        if not isinstance(artifact, dict):
            continue
        patch_path = str(artifact.get("patch_path") or "").strip()
        if not patch_path:
            continue
        subtask = _select_partial_diff_subtask(artifact, subtasks)
        if (
            subtask is None
            or subtask.get("carry_forward_patch")
            or subtask.get("carry_forward_patches")
            or subtask.get("discarded_partial_diff_patch")
        ):
            continue
        changed_files = _partial_diff_changed_files(artifact)
        worker_output_excerpt = str(artifact.get("worker_output_excerpt") or "").strip()
        focused_verification_commands, focused_verification_context = _partial_diff_focused_verification_context(artifact)
        if focused_verification_context:
            worker_output_excerpt = (
                focused_verification_context
                if not worker_output_excerpt
                else f"{focused_verification_context}\n{worker_output_excerpt}"
            )
        failure_reason = str(artifact.get("failure_reason") or "").strip()
        repeated_missing_import = _repeated_missing_import_failure(
            artifact,
            artifacts[:artifact_index],
            worker_output_excerpt,
        )
        if repeated_missing_import:
            symbol, module = repeated_missing_import
            repeated_summary = (
                "Repeated missing import failure after a focused repair retry: `"
                + symbol
                + "` is still not exported from `"
                + module
                + "`."
            )
            worker_output_excerpt = (
                repeated_summary
                if not worker_output_excerpt
                else f"{repeated_summary}\n{worker_output_excerpt}"
            )
        focused_test_failed_source_and_test_patch = (
            failure_reason in {
                "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                "WORKER_VERIFIABLE_DIFF_UNTERMINATED",
            }
            and _changed_files_include_source_and_test(changed_files)
            and not repeated_missing_import
        )
        parent_target_files = _task_target_files(getattr(ctx, "task", None))
        off_track_source_patch_needs_tests = (
            failure_reason == "WORKER_OFF_TRACK_TESTLESS_DIFF"
            and changed_files
            and all(_repo_path_looks_like_production_source_file(path) for path in changed_files)
            and bool(
                _testless_partial_required_test_files({"repair_worker_output_excerpt": worker_output_excerpt})
                or _source_partial_declared_test_followup_files(
                    {
                        "repair_changed_files": changed_files,
                        "files": _partial_diff_artifact_target_files(artifact),
                        "target_files": _partial_diff_artifact_target_files(artifact),
                        "acceptance_criteria": ["Retry the source partial diff with required regression coverage."],
                    }
                )
            )
        )
        source_only_timeout_required_tests = _source_only_timeout_required_test_files(
            artifact,
            changed_files,
            worker_output_excerpt,
            parent_target_files,
        )
        explicitly_non_reusable = artifact.get("patch_reusable") is False
        should_reapply_patch = _partial_diff_patch_is_reusable(worker_output_excerpt)
        artifact_targets = _partial_diff_artifact_target_files(artifact)
        support_only_partial = _all_repo_paths_are_support_only(list(dict.fromkeys([*changed_files, *artifact_targets])))
        if focused_test_failed_source_and_test_patch or (
            off_track_source_patch_needs_tests and not explicitly_non_reusable
        ):
            should_reapply_patch = True
        if support_only_partial and not explicitly_non_reusable:
            should_reapply_patch = True
        if repeated_missing_import or explicitly_non_reusable or source_only_timeout_required_tests:
            should_reapply_patch = False
        excerpt_limit = 1200 if should_reapply_patch else 360
        if len(worker_output_excerpt) > excerpt_limit:
            worker_output_excerpt = worker_output_excerpt[:excerpt_limit].rstrip() + "\n..."
        rejected_failure_summary = (
            _partial_diff_rejected_failure_summary(worker_output_excerpt)
            if not should_reapply_patch
            else ""
        )
        if should_reapply_patch:
            _append_unique_repo_paths(subtask, changed_files)
            subtask["carry_forward_patch"] = patch_path
        else:
            subtask["discarded_partial_diff_patch"] = patch_path
            if rejected_failure_summary:
                subtask["repair_failure_summary"] = rejected_failure_summary
            repair_target_files = _partial_diff_artifact_target_files(artifact)
            if (
                not repair_target_files
                and explicitly_non_reusable
                and failure_reason == "WORKER_OFF_TRACK_TESTLESS_DIFF"
            ):
                repair_target_files = list(
                    dict.fromkeys(
                        [
                            *changed_files,
                            *_testless_partial_required_test_files(
                                {"repair_worker_output_excerpt": worker_output_excerpt}
                            ),
                        ]
                    )
                )
            if source_only_timeout_required_tests:
                repair_target_files = list(
                    dict.fromkeys([*repair_target_files, *changed_files, *source_only_timeout_required_tests])
                )
            if not repair_target_files:
                repair_target_files = parent_target_files
            if repair_target_files:
                _append_unique_repo_paths(subtask, repair_target_files)
                subtask["repair_parent_target_files"] = repair_target_files
                criteria = [
                    str(entry).strip()
                    for entry in (subtask.get("acceptance_criteria") or [])
                    if str(entry).strip()
                ]
                rewritten: list[str] = []
                replacement = (
                    "Keep repair edits scoped to the active repair target files: "
                    + ", ".join(repair_target_files)
                    + "."
                )
                for entry in criteria:
                    lowered = entry.lower()
                    if "do not expand" in lowered and "beyond" in lowered:
                        if replacement not in rewritten:
                            rewritten.append(replacement)
                        continue
                    rewritten.append(entry)
                if replacement not in rewritten:
                    rewritten.append(replacement)
                subtask["acceptance_criteria"] = rewritten
        subtask["repair_source_subtask_id"] = str(artifact.get("subtask_id") or "").strip()
        subtask["repair_source_worker_id"] = str(artifact.get("worker_id") or "").strip()
        subtask["repair_source_failure_reason"] = failure_reason
        subtask["repair_changed_files"] = changed_files
        if focused_verification_commands:
            existing_commands = [
                str(command or "").strip()
                for command in (subtask.get("verification_commands") or [])
                if str(command or "").strip()
            ]
            subtask["verification_commands"] = list(
                dict.fromkeys([*focused_verification_commands, *existing_commands])
            )
        if worker_output_excerpt:
            subtask["repair_worker_output_excerpt"] = worker_output_excerpt
            criteria = [str(entry).strip() for entry in (subtask.get("acceptance_criteria") or []) if str(entry).strip()]
            if should_reapply_patch:
                repair_criterion = (
                    "Resolve the recovered partial-diff blocker before expanding scope: "
                    + worker_output_excerpt.replace("\n", " ")[:500]
                )
            else:
                repair_criterion = (
                    "Replace the rejected or incomplete partial-diff approach before expanding scope; do not carry "
                    "forward helper-only, unverified, self-referential, or local-oracle coverage."
                )
            if repair_criterion not in criteria:
                subtask["acceptance_criteria"] = [repair_criterion, *criteria]
        existing_scope_note = str(subtask.get("scope_note") or "").strip()
        repair_context = _compact_partial_diff_repair_context(worker_output_excerpt)
        changed_file_note = (
            " The saved diff touched these files; read and finish them before adding new scope: "
            + ", ".join(changed_files)
            + "."
            if changed_files
            else ""
        )
        blocker_note = (
            "\nRecovered partial-diff blocker/context:\n" + repair_context
            if repair_context and should_reapply_patch
            else ""
        )
        if should_reapply_patch:
            carry_note = (
                "ACA will apply the preserved partial worker diff before this retry so the worker can continue "
                "from the previous attempt instead of repeating it."
                f"{changed_file_note}{blocker_note}"
            )
        else:
            rejected_changed_file_note = (
                " The rejected diff touched these files: "
                + ", ".join(changed_files)
                + ". Inspect the current target files instead of copying that patch."
                if changed_files
                else ""
            )
            parent_target_note = (
                " Active repair targets are: "
                + ", ".join(subtask.get("repair_parent_target_files") or [])
                + "."
                if subtask.get("repair_parent_target_files")
                else ""
            )
            summary_note = f" Failure summary: {rejected_failure_summary}." if rejected_failure_summary else ""
            carry_note = (
                "ACA rejected the preserved partial worker diff for this retry because the recovered notes describe "
                "incomplete, unverified, helper-only, self-referential, or test-only coverage. Start from the clean "
                "target files, remove that approach if present, and add coverage that calls existing production code "
                "instead."
                f"{rejected_changed_file_note}{parent_target_note}{summary_note}"
            )
        subtask["scope_note"] = f"{existing_scope_note}\n{carry_note}".strip()
        if should_reapply_patch:
            _narrow_carried_partial_diff_subtask(subtask)
        else:
            _compact_rejected_partial_diff_subtask(subtask)


def _partial_diff_rejected_failure_summary(worker_output_excerpt: str) -> str:
    text = worker_output_excerpt.lower()
    reasons: list[str] = []
    if "engine_prompt_timeout" in text:
        reasons.append("the worker timed out before a terminal response")
    if "worker_off_track_testless_diff" in text or (
        "changed only non-test files" in text and "required test files" in text
    ):
        reasons.append("the worker drifted off the required test-first path")
    if "verification not run" in text:
        reasons.append("verification did not run")
    if "focused tests failed" in text or "focused verification" in text:
        reasons.append("focused verification failed")
    if "config.aca" in text:
        reasons.append("the diff used the wrong config namespace")
    if any(marker in text for marker in ("helper-only", "test-only helper", "local oracle", "self-referential")):
        reasons.append("the diff appeared helper-only or self-referential")
    if "newly introduced production symbol" in text or "did not exercise newly introduced production symbol" in text:
        reasons.append("required tests did not exercise newly introduced production API")
    if any(marker in text for marker in ("unproductive partial diff", "unproductive diff")):
        reasons.append("ACA flagged the diff as unproductive")
    if any(marker in text for marker in ("runaway guard", "diff exceeded aca runaway", "giant patch")):
        reasons.append("ACA flagged the diff as runaway-sized")
    if any(marker in text for marker in ("changes only string wording", "comment-only", "tautological")):
        reasons.append("the diff did not add meaningful regression coverage")
    if "missing production helper" in text:
        reasons.append("the test called a missing production helper")
    if any(marker in text for marker in ("not wired", "does not show", "limited to message formatting")):
        reasons.append("the diff was not wired into the production path")
    if not reasons:
        first_line = next((line.strip() for line in worker_output_excerpt.splitlines() if line.strip()), "")
        if first_line:
            reasons.append(first_line[:160])
    return "; ".join(dict.fromkeys(reasons))[:260]


def _partial_diff_patch_is_reusable(worker_output_excerpt: str) -> bool:
    text = worker_output_excerpt.lower()
    rejected_markers = (
        "self-referential",
        "test-only constant",
        "test-only enum",
        "test-only helper",
        "does not appear to exercise actual",
        "does not exercise actual",
        "only asserts those same",
        "standalone simulation",
        "local oracle",
        "limited to message formatting",
        "only message formatting",
        "not wired into",
        "does not show this readiness error being wired",
        "not covered by the added test",
        "verification not run",
        "focused tests failed",
        "focused verification",
        "newly introduced production symbol",
        "did not exercise newly introduced production symbol",
        "did not exercise newly introduced production api",
        "unproductive partial diff",
        "unproductive diff",
        "runaway guard",
        "diff exceeded aca runaway",
        "giant patch",
        "changes only string wording",
        "comment-only",
        "tautological",
        "missing production helper",
        "no-op",
        "redundant",
        "worker_off_track_testless_diff",
        "changed only non-test files",
        "changed only required test files",
        "required test files",
        "worker drifted off the required regression/test coverage path",
        "config.aca",
        "aca.throughput",
        "metrics_window_seconds",
        "backpressure_queue_limit",
        "max_concurrent_workers",
        "max_active_cost_usd",
    )
    return not any(marker in text for marker in rejected_markers)


def _sanitize_partial_diff_artifact_paths_in_plan(plan: dict[str, Any]) -> None:
    subtasks = plan.get("subtasks") if isinstance(plan, dict) else None
    if not isinstance(subtasks, list):
        return
    replacement = (
        "Use ACA's repair directive and failure summary as evidence; do not read absolute "
        "patch paths or replay rejected partial diffs."
    )
    for subtask in subtasks:
        if not isinstance(subtask, dict):
            continue
        criteria = subtask.get("acceptance_criteria")
        if not isinstance(criteria, list):
            continue
        sanitized: list[Any] = []
        for entry in criteria:
            text = str(entry)
            lowered = text.lower()
            if (
                "patch" in lowered
                and ("/workspace/" in text or "runs/" in lowered or "artifacts/" in lowered)
            ):
                if replacement not in sanitized:
                    sanitized.append(replacement)
                continue
            sanitized.append(entry)
        subtask["acceptance_criteria"] = sanitized


def _active_partial_diff_artifacts_for_repair(ctx: RunContext, artifacts: list[Any]) -> list[Any]:
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    completed_ids = {
        str(subtask_id or "").strip()
        for subtask_id in ((repair or {}).get("completed_subtask_ids") or [])
        if str(subtask_id or "").strip()
    } if isinstance(repair, dict) else set()
    if not completed_ids:
        return artifacts
    active = [
        artifact
        for artifact in artifacts
        if not isinstance(artifact, dict)
        or str(artifact.get("subtask_id") or "").strip() not in completed_ids
    ]
    return active or artifacts


def _syntax_errors_for_partial_diff_artifact(artifact: dict[str, Any]) -> list[str]:
    raw_errors = artifact.get("syntax_errors")
    errors: list[str] = []
    if isinstance(raw_errors, list):
        errors.extend(str(error or "").strip() for error in raw_errors if str(error or "").strip())
    excerpt = str(artifact.get("worker_output_excerpt") or "").strip()
    if excerpt:
        for match in re.finditer(r"[\w./-]+\.py:\d+:\d+:[^\n;]+", excerpt):
            errors.append(match.group(0).strip())
    return list(dict.fromkeys(error for error in errors if error))


def _verification_commands_for_syntax_repair(active_files: list[str]) -> list[str]:
    py_files = [path for path in active_files if path.endswith(".py")]
    commands: list[str] = []
    if py_files:
        commands.append("python3 -m py_compile " + " ".join(py_files))
    for path in py_files:
        if _repo_path_looks_like_test_file(path):
            module = path[:-3].replace("/", ".")
            commands.append("python3 -m unittest " + module)
    return list(dict.fromkeys(commands))


def _deterministic_syntax_invalid_partial_diff_repair_plan(
    artifacts: list[Any],
    parent_targets: list[str],
) -> dict[str, Any] | None:
    for artifact in reversed(artifacts):
        if not isinstance(artifact, dict):
            continue
        if str(artifact.get("failure_reason") or "").strip() != "WORKER_SYNTAX_INVALID_DIFF":
            continue
        changed_files = _partial_diff_changed_files(artifact)
        if not _changed_files_include_source_and_test(changed_files):
            continue
        patch_path = str(artifact.get("patch_path") or "").strip()
        if not patch_path:
            continue
        active_files = list(dict.fromkeys(changed_files or _partial_diff_artifact_target_files(artifact) or parent_targets))
        active_files = [path for path in active_files if _normalize_repo_relative_path(path)]
        if not active_files:
            continue
        syntax_errors = _syntax_errors_for_partial_diff_artifact(artifact)
        target_text = ", ".join(active_files)
        syntax_text = "; ".join(syntax_errors[:5]) if syntax_errors else "the changed Python files do not parse"
        verification_commands = _verification_commands_for_syntax_repair(active_files)
        worker_output_excerpt = str(artifact.get("worker_output_excerpt") or "").strip()
        if syntax_errors:
            syntax_context = "Syntax errors: " + syntax_text
            worker_output_excerpt = (
                syntax_context
                if not worker_output_excerpt
                else f"{syntax_context}\n{worker_output_excerpt}"
            )
        subtask = {
            "id": str(artifact.get("subtask_id") or "syntax-invalid-diff-repair").strip()
            or "syntax-invalid-diff-repair",
            "title": "Repair syntax-invalid source+test partial diff",
            "goal": "Apply the preserved source+test patch, fix syntax first, and verify " + target_text + ".",
            "files": active_files,
            "target_files": active_files,
            "acceptance_criteria": [
                "ACA applies the preserved source+test patch before this worker starts; inspect it in: "
                + target_text
                + ".",
                "Fix the reported Python syntax error(s) before changing behavior: " + syntax_text + ".",
                "Run Python compile or the narrowest deterministic verification for "
                + target_text
                + " before returning.",
                "If syntax and verification pass, return a terminal completion note without rebuilding from clean files.",
                "If verification fails after syntax is fixed, change only the preserved source/test files needed for that failure.",
            ],
            "carry_forward_patch": patch_path,
            "repair_source_subtask_id": str(artifact.get("subtask_id") or "").strip(),
            "repair_source_worker_id": str(artifact.get("worker_id") or "").strip(),
            "repair_source_failure_reason": "WORKER_SYNTAX_INVALID_DIFF",
            "repair_changed_files": active_files,
            "repair_syntax_errors": syntax_errors,
            "repair_worker_output_excerpt": worker_output_excerpt[:1200],
            "repair_failure_summary": _partial_diff_rejected_failure_summary(worker_output_excerpt),
            "repair_parent_target_files": active_files,
            "repair_verification_first": True,
            "deterministic_partial_diff_repair": True,
            "write_required": True,
            "scope_note": (
                "ACA generated this repair plan deterministically after a preserved source+test partial diff "
                "failed Python syntax validation. The latest source+test patch is applied before this worker "
                "starts; fix the reported syntax errors first, then run narrow verification. Active repair "
                "targets are: "
                + target_text
                + "."
            ),
        }
        if verification_commands:
            subtask["verification_commands"] = verification_commands
        _narrow_carried_partial_diff_subtask(subtask)
        return {
            "kind": "syntax_invalid_source_test_diff",
            "summary": (
                "Deterministic repair for a syntax-invalid source+test partial diff; ACA carried the latest "
                "patch forward and narrowed the retry to syntax repair plus focused verification."
            ),
            "subtasks": [subtask],
            "risks": [
                "The preserved patch may apply cleanly but still need narrow syntax or verification fixes."
            ],
            "tests": [
                "Run Python compile and the narrowest deterministic verification for " + target_text + "."
            ],
        }
    return None


def _deterministic_failed_verifiable_partial_diff_repair_plan(
    artifacts: list[Any],
    parent_targets: list[str],
) -> dict[str, Any] | None:
    for artifact_index in range(len(artifacts) - 1, -1, -1):
        artifact = artifacts[artifact_index]
        if not isinstance(artifact, dict):
            continue
        failure_reason = str(artifact.get("failure_reason") or "").strip()
        if failure_reason not in {
            "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
            "WORKER_VERIFIABLE_DIFF_UNTERMINATED",
        }:
            continue
        changed_files = _partial_diff_changed_files(artifact)
        if not _changed_files_include_source_and_test(changed_files):
            continue
        patch_path = str(artifact.get("patch_path") or "").strip()
        if not patch_path:
            continue
        focused_verification_commands, focused_verification_context = _partial_diff_focused_verification_context(
            artifact
        )
        worker_output_excerpt = str(artifact.get("worker_output_excerpt") or "").strip()
        if focused_verification_context:
            worker_output_excerpt = (
                focused_verification_context
                if not worker_output_excerpt
                else f"{focused_verification_context}\n{worker_output_excerpt}"
            )
        repeated_assertion = _repeated_assertion_failure(
            artifact,
            artifacts[:artifact_index],
            worker_output_excerpt,
        )
        if repeated_assertion:
            test_name, qualified_name = repeated_assertion
            repeated_summary = (
                "Repeated assertion failure after a focused repair retry in `"
                + (qualified_name or test_name)
                + "`: scan the entire failing test method and update all related expectations consistently."
            )
            worker_output_excerpt = (
                repeated_summary
                if not worker_output_excerpt
                else f"{repeated_summary}\n{worker_output_excerpt}"
            )
        active_files = list(dict.fromkeys(changed_files or _partial_diff_artifact_target_files(artifact) or parent_targets))
        active_files = [path for path in active_files if _normalize_repo_relative_path(path)]
        if not active_files:
            continue
        target_text = ", ".join(active_files)
        unterminated = failure_reason == "WORKER_VERIFIABLE_DIFF_UNTERMINATED"
        acceptance_criteria = [
            "Apply and inspect the preserved source+test patch in: " + target_text + ".",
            (
                "Run the focused verification command captured from the unterminated worker and do not count the patch complete without a terminal worker verdict."
                if unterminated
                else "Run the focused verification command captured from the failed worker before expanding scope."
            ),
            (
                "If verification passes, verify the patch satisfies the subtask acceptance criteria before returning a completion note; if it does not, fix only the preserved source/test files and rerun it."
                if unterminated
                else "If verification still fails, fix only the failing behavior in the preserved source/test files and rerun it."
            ),
        ]
        focused_exception_instruction = _zero_division_assertion_repair_instruction(
            "\n".join([worker_output_excerpt, _partial_diff_patch_text(artifact)])
        )
        if focused_exception_instruction:
            acceptance_criteria.insert(1, focused_exception_instruction)
        subtask = {
            "id": str(artifact.get("subtask_id") or "failed-verifiable-diff-repair").strip()
            or "failed-verifiable-diff-repair",
            "title": "Finish unterminated source+test partial diff"
            if unterminated
            else "Repair failed source+test partial diff",
            "goal": (
                (
                    "Finish or reject the preserved source+test partial worker diff and return a terminal verdict for "
                    if unterminated
                    else "Verify the preserved source+test partial worker diff and fix only narrow verification failures for "
                )
                + target_text
                + "."
            ),
            "files": active_files,
            "target_files": active_files,
            "acceptance_criteria": acceptance_criteria,
            "carry_forward_patch": patch_path,
            "repair_source_subtask_id": str(artifact.get("subtask_id") or "").strip(),
            "repair_source_worker_id": str(artifact.get("worker_id") or "").strip(),
            "repair_source_failure_reason": failure_reason,
            "repair_changed_files": changed_files,
            "repair_worker_output_excerpt": worker_output_excerpt[:1200],
            "repair_failure_summary": _partial_diff_rejected_failure_summary(worker_output_excerpt),
            "repair_parent_target_files": active_files,
            "deterministic_partial_diff_repair": True,
            "write_required": True,
            "scope_note": (
                "ACA generated this repair plan deterministically after a preserved source+test partial diff "
                + (
                    "timed out without a terminal worker verdict. "
                    if unterminated
                    else "failed focused verification. "
                )
                + "The latest source+test patch is applied before this worker starts; "
                "active repair targets are: "
                + target_text
                + "."
                + ((" " + focused_exception_instruction) if focused_exception_instruction else "")
            ),
        }
        if focused_exception_instruction:
            subtask["repair_focus_instruction"] = focused_exception_instruction
            subtask["repair_focus_instructions"] = [focused_exception_instruction]
        if focused_verification_commands:
            subtask["verification_commands"] = focused_verification_commands
        _narrow_carried_partial_diff_subtask(subtask)
        return {
            "kind": "failed_verifiable_source_test_diff",
            "summary": (
                "Deterministic repair for an unterminated source+test partial diff; ACA carried the latest patch "
                "forward and narrowed the retry to finishing the patch with a terminal verdict."
                if unterminated
                else "Deterministic repair for a failed source+test partial diff; ACA carried the latest patch "
                "forward and narrowed the retry to the focused verification failure."
            ),
            "subtasks": [subtask],
            "risks": [
                (
                    "The preserved source+test patch may pass focused tests but still need a terminal worker verdict and acceptance-criteria check."
                    if unterminated
                    else "The preserved source+test patch may still need a narrow fix before the focused verification passes."
                )
            ],
            "tests": [
                "Run the focused verification command for " + target_text + "."
            ],
        }
    return None


def _one_sided_partial_diff_attempt_counts(
    artifacts: list[Any],
    *,
    source_files: list[str],
    test_files: list[str],
    subtask_id: str,
) -> dict[str, int]:
    source_set = set(source_files)
    test_set = set(test_files)
    source_only_count = 0
    test_only_count = 0
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        artifact_subtask_id = str(artifact.get("subtask_id") or "").strip()
        if subtask_id and artifact_subtask_id and artifact_subtask_id != subtask_id:
            continue
        changed_files = _partial_diff_changed_files(artifact)
        changed_set = set(changed_files)
        reason = str(artifact.get("failure_reason") or "").strip()
        if (
            reason == "WORKER_OFF_TRACK_TESTLESS_DIFF"
            and changed_files
            and all(_repo_path_looks_like_production_source_file(path) for path in changed_files)
            and (not source_set or bool(source_set.intersection(changed_set)))
        ):
            source_only_count += 1
        elif (
            reason == "WORKER_TEST_ONLY_DIFF"
            and changed_files
            and all(_repo_path_looks_like_test_file(path) for path in changed_files)
            and (not test_set or bool(test_set.intersection(changed_set)))
        ):
            test_only_count += 1
    return {"source_only": source_only_count, "test_only": test_only_count}


def _deterministic_testless_partial_diff_repair_plan(ctx: RunContext) -> dict[str, Any] | None:
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    artifacts = repair.get("partial_diff_artifacts") if isinstance(repair, dict) else []
    if not isinstance(artifacts, list) or not artifacts:
        return None
    try:
        stalled_no_diff_repair_attempts = max(
            0,
            int(repair.get("stalled_no_diff_repair_attempts") or 0) if isinstance(repair, dict) else 0,
        )
    except (TypeError, ValueError):
        stalled_no_diff_repair_attempts = 0
    stalled_no_diff_repair = (
        stalled_no_diff_repair_attempts > 0
        or (
            isinstance(repair, dict)
            and str(repair.get("last_repair_stall_kind") or "").strip() == "engine_tool_loop_stalled_no_diff"
        )
    )
    artifacts = _active_partial_diff_artifacts_for_repair(ctx, artifacts)
    parent_targets = _task_target_files(ctx.task)
    syntax_plan = _deterministic_syntax_invalid_partial_diff_repair_plan(
        artifacts,
        parent_targets,
    )
    if syntax_plan is not None:
        return syntax_plan
    verifiable_plan = _deterministic_failed_verifiable_partial_diff_repair_plan(
        artifacts,
        parent_targets,
    )
    if verifiable_plan is not None:
        return verifiable_plan
    if not _weak_source_test_artifact_after_latest_one_sided_pair(artifacts):
        complementary_plan = _deterministic_complementary_partial_diff_repair_plan(ctx, artifacts, parent_targets)
        if complementary_plan is not None:
            return complementary_plan
    for artifact in reversed(artifacts):
        if not isinstance(artifact, dict):
            continue
        excerpt = str(artifact.get("worker_output_excerpt") or "").strip()
        if not excerpt:
            continue
        lowered = excerpt.lower()
        changed_files = _partial_diff_changed_files(artifact)
        patch_path = str(artifact.get("patch_path") or "").strip()
        failure_summary = _partial_diff_rejected_failure_summary(excerpt)
        artifact_targets = _partial_diff_artifact_target_files(artifact) or parent_targets
        failure_reason = str(artifact.get("failure_reason") or "").strip()
        weak_test_rejection = failure_reason in {
            "WORKER_VERIFIABLE_DIFF_WEAK_TEST",
            "WORKER_VERIFIABLE_DIFF_MISALIGNED_TEST",
        } or any(
            marker in lowered
            for marker in (
                "test diff did not add a test method",
                "test diff did not add a test method or assertion",
                "did not add a test method or assertion",
                "did not exercise newly introduced production symbol",
                "newly introduced production api",
                "weak test",
            )
        )
        if weak_test_rejection:
            source_files = [
                path
                for path in changed_files
                if _repo_path_looks_like_production_source_file(path)
            ] or [
                path
                for path in artifact_targets
                if _repo_path_looks_like_production_source_file(path)
            ]
            required_test_files = [
                path
                for path in changed_files
                if _repo_path_looks_like_test_file(path)
            ] or [
                path
                for path in artifact_targets
                if _repo_path_looks_like_test_file(path)
            ]
            if not source_files or not required_test_files:
                continue
            active_files = list(dict.fromkeys([*source_files, *required_test_files]))
            source_text = ", ".join(source_files)
            test_text = ", ".join(required_test_files)
            one_sided_attempt_counts = _one_sided_partial_diff_attempt_counts(
                artifacts,
                source_files=source_files,
                test_files=required_test_files,
                subtask_id=str(artifact.get("subtask_id") or "").strip(),
            )
            has_repeated_one_sided_history = (
                one_sided_attempt_counts["source_only"] > 0
                and one_sided_attempt_counts["test_only"] > 0
            )
            patch_is_reusable = artifact.get("patch_reusable") is not False
            misaligned_test_rejection = (
                failure_reason == "WORKER_VERIFIABLE_DIFF_MISALIGNED_TEST"
                or "did not exercise newly introduced production symbol" in lowered
                or "newly introduced production api" in lowered
            )
            focused_exception_instruction = _zero_division_assertion_repair_instruction(
                "\n".join([excerpt, _partial_diff_patch_text(artifact)])
            )
            focused_failure_instruction = _focused_failure_first_repair_instruction(excerpt)
            focused_exception_instructions = [
                instruction
                for instruction in (
                    focused_exception_instruction,
                    _missing_import_repair_instruction(excerpt),
                    _missing_dependency_import_repair_instruction(excerpt),
                    _name_error_repair_instruction(excerpt),
                    _unexpected_keyword_repair_instruction(excerpt),
                    _positional_argument_repair_instruction(excerpt),
                    _missing_required_argument_repair_instruction(excerpt),
                    _string_dict_attribute_repair_instruction(excerpt),
                    _future_import_syntax_repair_instruction(excerpt),
                    _assertion_failure_repair_instruction(excerpt),
                )
                if instruction
            ]
            focused_exception_instructions = list(dict.fromkeys(focused_exception_instructions))
            if patch_is_reusable:
                acceptance_criteria = [
                    "The preserved weak source+test patch is applied before this worker starts; inspect it in: "
                    + ", ".join(active_files)
                    + ".",
                    "Make the first new repair edit in the required test file(s): "
                    + test_text
                    + "; add a real test method or assertion that exercises production behavior.",
                    "Treat the preserved source patch as the paired production behavior; adjust production only if the new assertion or narrow verification proves it is wrong: "
                    + source_text
                    + ".",
                    "Do not mark this repair complete until the diff includes meaningful regression coverage in "
                    + test_text
                    + " plus production-backed behavior, and narrow verification has run or a concrete blocker is recorded.",
                ]
                scope_note = (
                    "ACA generated this repair plan deterministically after rejecting a source+test partial "
                    "diff whose test changes lacked a real test method or assertion. The preserved weak source+test "
                    "patch is applied before this worker starts so the retry can add missing assertion coverage "
                    "instead of rediscovering production edits. Active repair targets are: "
                    + ", ".join(active_files)
                    + "."
                )
            else:
                acceptance_criteria = [
                    "Do not copy or replay the rejected weak source+test partial patch.",
                    "Make the first new repair edit in the required test file(s): "
                    + test_text
                    + "; add a real test method or assertion that exercises production behavior.",
                    "Then make only the minimal semantic production change needed in: " + source_text + ".",
                    "Do not mark this repair complete until the current diff includes meaningful regression coverage in "
                    + test_text
                    + " plus production-backed behavior, and narrow verification has run or a concrete blocker is recorded.",
                ]
                scope_note = (
                    "ACA generated this repair plan deterministically after rejecting a non-reusable weak "
                    "source+test partial diff. The rejected patch is not applied before this worker starts; "
                    "rebuild the repair from the clean target files. Active repair targets are: "
                    + ", ".join(active_files)
                    + "."
                )
            if focused_failure_instruction:
                acceptance_criteria.insert(1, focused_failure_instruction)
                scope_note += " " + focused_failure_instruction
            for instruction in reversed(focused_exception_instructions):
                acceptance_criteria.insert(2 if focused_failure_instruction else 1, instruction)
                scope_note += " " + instruction
            if has_repeated_one_sided_history:
                acceptance_criteria.insert(
                    1,
                    "Previous retries for these same targets already failed as source-only and test-only diffs; this attempt must stay under about 80 changed diff lines, must not rewrite or duplicate whole files, and must stop with a concrete blocker if the exact paired production path is not clear after reading the listed source/test files.",
                )
            if misaligned_test_rejection:
                acceptance_criteria.insert(
                    1,
                    "Do not add or keep newly introduced public production helpers unless the required test file "
                    "imports or calls those exact helpers; otherwise remove the unexercised helpers and implement "
                    "the behavior behind the existing production API under test.",
                )
                scope_note += (
                    " ACA rejected the previous source+test diff because required test additions did not exercise "
                    "newly introduced production symbols; the retry must either test those exact symbols or remove "
                    "them and use the existing API being asserted."
                )
            subtask = {
                "id": str(artifact.get("subtask_id") or "weak-test-diff-repair").strip() or "weak-test-diff-repair",
                "title": "Repair weak source+test partial diff",
                "goal": (
                    "Finish the preserved weak-test partial worker diff with production-backed regression coverage for "
                    + ", ".join(active_files)
                    + "."
                ),
                "files": active_files,
                "target_files": active_files,
                "acceptance_criteria": acceptance_criteria,
                "repair_source_subtask_id": str(artifact.get("subtask_id") or "").strip(),
                "repair_source_worker_id": str(artifact.get("worker_id") or "").strip(),
                "repair_changed_files": changed_files,
                "repair_requires_production_followup": source_files,
                "repair_requires_test_followup": required_test_files,
                "repair_requires_paired_source_test": True,
                "repair_requires_paired_source_test_diff": True,
                "repair_mode": "weak_source_test_diff",
                "repair_focus_instructions": [
                    "Produce one small paired source+test diff in this attempt; do not stop after editing only tests or only production.",
                    "Use the existing production API or call path under test when possible; do not add new public helpers unless the required test imports or calls those exact helpers.",
                ],
                "repair_worker_output_excerpt": excerpt[:1200],
                "repair_failure_summary": failure_summary,
                "repair_parent_target_files": active_files,
                "deterministic_partial_diff_repair": True,
                "write_required": True,
                "scope_note": scope_note,
            }
            if has_repeated_one_sided_history:
                subtask["repair_precision_edit"] = True
                subtask["repair_diff_line_budget"] = 80
                subtask["repair_focus_instructions"].append(
                    "Prior one-sided retries already consumed the broad repair path; keep the replacement surgical, deletion-averse, and below the stated diff-line budget."
                )
            if focused_failure_instruction:
                subtask["repair_failure_focus"] = focused_failure_instruction
            if focused_exception_instructions:
                for instruction in reversed(focused_exception_instructions):
                    subtask["repair_focus_instructions"].insert(0, instruction)
                subtask["repair_focus_instruction"] = focused_exception_instructions[0]
            if patch_is_reusable:
                subtask["carry_forward_patch"] = patch_path
            else:
                subtask["discarded_partial_diff_patch"] = patch_path
            return {
                "kind": "weak_source_test_diff",
                "summary": (
                    "Deterministic repair for a weak source+test partial diff; ACA "
                    + (
                        "carried the patch forward and narrowed the retry to adding meaningful required-test coverage."
                        if patch_is_reusable
                        else "discarded the non-reusable patch and narrowed the retry to rebuilding source+test coverage."
                    )
                ),
                "subtasks": [subtask],
                "risks": [
                    (
                        "The preserved weak patch may still need production adjustment after meaningful coverage is added."
                        if patch_is_reusable
                        else "The rejected weak patch is intentionally not replayed."
                    )
                ],
                "tests": [
                    "Run the narrowest deterministic verification for " + test_text + "."
                ],
            }
        if (
            changed_files
            and all(_repo_path_looks_like_production_source_file(path) for path in changed_files)
            and any(
                marker in lowered
                for marker in (
                    "unproductive partial diff",
                    "worker_unproductive_diff",
                    "comment-only",
                    "tautological",
                    "changes only string wording",
                )
            )
        ):
            source_files = list(dict.fromkeys(changed_files))
            parent_target_set = set(artifact_targets)
            if parent_target_set:
                source_files = [path for path in source_files if path in parent_target_set] or source_files
            required_test_files = [
                path
                for path in artifact_targets
                if _repo_path_looks_like_test_file(path)
            ]
            active_files = list(dict.fromkeys([*source_files, *required_test_files]))
            if source_files and required_test_files:
                source_text = ", ".join(source_files)
                test_text = ", ".join(required_test_files)
                subtask = {
                    "id": str(artifact.get("subtask_id") or "unproductive-diff-repair").strip()
                    or "unproductive-diff-repair",
                    "title": "Repair unproductive partial diff",
                    "goal": (
                        "Replace the rejected comment-only partial diff with a production-backed repair for "
                        + ", ".join(active_files)
                        + "."
                    ),
                    "files": active_files,
                    "target_files": active_files,
                    "acceptance_criteria": [
                        "Do not apply or copy the rejected comment-only partial patch.",
                        "Make the first new repair edit in the required test file(s): " + test_text + ".",
                        "Then make only the minimal semantic production change needed in: " + source_text + ".",
                        "Do not mark this repair complete until the diff includes real coverage in "
                        + test_text
                        + " plus a non-comment production behavior change.",
                    ],
                    "discarded_partial_diff_patch": patch_path,
                    "repair_source_subtask_id": str(artifact.get("subtask_id") or "").strip(),
                    "repair_source_worker_id": str(artifact.get("worker_id") or "").strip(),
                    "repair_source_failure_reason": failure_reason,
                    "repair_changed_files": changed_files,
                    "repair_requires_test_followup": required_test_files,
                    "repair_worker_output_excerpt": excerpt[:1200],
                    "repair_failure_summary": failure_summary,
                    "repair_parent_target_files": artifact_targets,
                    "deterministic_partial_diff_repair": True,
                    "write_required": True,
                    "scope_note": (
                        "ACA generated this repair plan deterministically after rejecting a comment-only or "
                        "unproductive partial diff. Active repair targets are the changed source file(s) plus "
                        "required test file(s): "
                        + ", ".join(active_files)
                        + "."
                    ),
                }
                return {
                    "summary": (
                        "Deterministic repair for an unproductive source-only partial diff; ACA discarded the "
                        "patch and narrowed the retry to the source files plus required tests."
                    ),
                    "subtasks": [subtask],
                    "risks": [
                        "The rejected comment-only patch is intentionally not replayed."
                    ],
                    "tests": [
                        "Run the narrowest deterministic verification for " + test_text + "."
                    ],
                }
        if (
            "engine_prompt_timeout" in lowered
            and changed_files
            and all(_repo_path_looks_like_production_source_file(path) for path in changed_files)
        ):
            source_files = list(dict.fromkeys(changed_files))
            declared_tests = [
                path
                for path in artifact_targets
                if _repo_path_looks_like_test_file(path)
            ]
            required_test_files = _source_partial_declared_test_followup_files(
                {
                    "repair_changed_files": changed_files,
                    "files": artifact_targets,
                    "target_files": artifact_targets,
                    "acceptance_criteria": [
                        "Retry the engine timeout with required regression coverage."
                    ],
                }
            ) or declared_tests or _paired_test_files_for_source_partial(source_files, parent_targets)
            active_files = list(dict.fromkeys([*source_files, *required_test_files]))
            if source_files:
                source_text = ", ".join(source_files)
                test_text = ", ".join(required_test_files)
                verification_target_text = test_text or source_text
                patch_is_reusable = (
                    artifact.get("patch_reusable") is not False
                    and _partial_diff_patch_is_reusable(excerpt)
                )
                should_carry_timeout_patch = patch_is_reusable and not required_test_files
                if should_carry_timeout_patch:
                    criteria = [
                        "The timed-out source patch is applied before this worker starts; inspect it in: "
                        + source_text
                        + ".",
                    ]
                else:
                    criteria = [
                        "Do not copy or replay the timed-out source-only partial patch.",
                    ]
                if required_test_files:
                    criteria.extend(
                        [
                            "Read and edit the required test file(s) first: " + test_text + ".",
                            "Then make only the minimal semantic production change needed in: " + source_text + ".",
                            "Do not mark this repair complete until the current diff includes both source behavior and real coverage in "
                            + test_text
                            + ".",
                        ]
                    )
                elif should_carry_timeout_patch:
                    criteria.extend(
                        [
                            "No required test target was declared for this timed-out worker slice; preserve the carried production change and add or run the narrowest existing verification available for "
                            + source_text
                            + ".",
                            "Do not discard or restart the carried source patch merely because no test file was declared; adjust the carried implementation only if verification or inspection proves it incomplete.",
                        ]
                    )
                else:
                    criteria.extend(
                        [
                            "No required test target was declared and the timed-out patch is marked non-reusable; rebuild the smallest production repair from clean target files: "
                            + source_text
                            + ".",
                            "Do not mark this repair complete until narrow verification has run or a concrete blocker is recorded.",
                        ]
                    )
                criteria.append(
                    "Run the narrowest deterministic verification for "
                    + verification_target_text
                    + ", or record the exact unavailable command/blocker."
                )
                subtask = {
                    "id": str(artifact.get("subtask_id") or "source-timeout-diff-repair").strip()
                    or "source-timeout-diff-repair",
                    "title": "Repair source-only engine timeout partial diff",
                    "goal": (
                        "Finish the timed-out source partial diff with required test coverage for "
                        + ", ".join(active_files)
                        + "."
                    ),
                    "files": active_files,
                    "target_files": active_files,
                    "acceptance_criteria": criteria,
                    "repair_source_subtask_id": str(artifact.get("subtask_id") or "").strip(),
                    "repair_source_worker_id": str(artifact.get("worker_id") or "").strip(),
                    "repair_source_failure_reason": "ENGINE_PROMPT_TIMEOUT",
                    "repair_changed_files": changed_files,
                    "repair_requires_test_followup": required_test_files,
                    "repair_worker_output_excerpt": excerpt[:1200],
                    "repair_failure_summary": failure_summary,
                    "repair_parent_target_files": active_files,
                    "deterministic_partial_diff_repair": True,
                    "write_required": True,
                }
                if should_carry_timeout_patch:
                    subtask["carry_forward_patch"] = patch_path
                    subtask["scope_note"] = (
                        "ACA generated this repair plan deterministically after an engine prompt timeout left a "
                        "source-only partial diff. The preserved source patch is applied before this worker starts; "
                        "active repair targets are: "
                        + ", ".join(active_files)
                        + "."
                    )
                else:
                    subtask["discarded_partial_diff_patch"] = patch_path
                    subtask["scope_note"] = (
                        "ACA generated this repair plan deterministically after an engine prompt timeout left a "
                        "source-only partial diff that still needs required test coverage. The timed-out source "
                        "patch is not applied before this worker starts; rebuild the repair from clean target files. "
                        "Active repair targets are: "
                        + ", ".join(active_files)
                        + "."
                    )
                return {
                    "kind": "source_engine_timeout_partial_diff",
                    "summary": (
                        "Deterministic repair for a source-only engine timeout partial diff; ACA narrowed the retry "
                        "to the timed-out source files plus required tests and "
                        + (
                            "carried the source patch forward."
                            if should_carry_timeout_patch
                            else "discarded the timed-out source patch before rebuilding source+test coverage."
                        )
                    ),
                    "subtasks": [subtask],
                    "risks": [
                        (
                            "The preserved patch may need adjustment after required coverage is added."
                            if should_carry_timeout_patch
                            else "The timed-out source-only patch is intentionally not replayed."
                        )
                    ],
                    "tests": [
                        "Run the narrowest deterministic verification for " + verification_target_text + "."
                    ],
                }
        if "changed only non-test files" in lowered and "required test files" in lowered:
            parent_target_set = set(artifact_targets)
            source_files = [path for path in changed_files if _repo_path_looks_like_production_source_file(path)]
            if parent_target_set:
                source_files = [path for path in source_files if path in parent_target_set]
            required_test_files = _testless_partial_required_test_files({"repair_worker_output_excerpt": excerpt})
            parsed_required_test_files = list(required_test_files)
            if parent_target_set:
                required_test_files = [path for path in required_test_files if path in parent_target_set]
            if not required_test_files:
                required_test_files = [
                    path
                    for path in artifact_targets
                    if _repo_path_looks_like_test_file(path)
                ]
            if artifact_targets:
                active_files = list(dict.fromkeys([*required_test_files, *artifact_targets]))
                source_files = [
                    path
                    for path in active_files
                    if _repo_path_looks_like_production_source_file(path)
                ]
            else:
                active_files = list(dict.fromkeys([*required_test_files, *source_files]))
            deferred_files = [
                path
                for path in list(dict.fromkeys([*parsed_required_test_files, *changed_files]))
                if path not in active_files
            ]
            if not source_files or not required_test_files or not active_files:
                continue
            source_text = ", ".join(source_files)
            test_text = ", ".join(required_test_files)
            patch_is_reusable = artifact.get("patch_reusable") is not False
            if patch_is_reusable:
                acceptance_criteria = [
                    "The preserved source patch is applied before this worker starts; inspect it in: " + source_text + ".",
                    "Read and edit the required test file(s) first: " + test_text + ".",
                    "Treat the preserved source patch as the paired production change; adjust production only if the new test or narrow verification proves it is wrong.",
                    "Do not mark this repair complete until the diff includes real coverage in "
                    + test_text
                    + " and narrow verification has run or a concrete blocker is recorded.",
                ]
                scope_note = (
                    "ACA generated this repair plan deterministically after detecting a worker_off_track "
                    "testless diff. The preserved source patch is applied before this worker starts so "
                    "the retry can add the missing required test coverage instead of rediscovering the "
                    "production edit. Active repair targets are the in-contract required test file(s) plus "
                    "the parent task target file(s): "
                    + ", ".join(active_files)
                    + "."
                )
            else:
                acceptance_criteria = [
                    "Do not copy or replay the rejected source-only partial patch.",
                    "Read and edit the required test file(s) first: " + test_text + ".",
                    "Then make only the minimal semantic production change needed in: " + source_text + ".",
                    "Do not mark this repair complete until the current diff includes real coverage in "
                    + test_text
                    + " plus production-backed behavior, and narrow verification has run or a concrete blocker is recorded.",
                ]
                scope_note = (
                    "ACA generated this repair plan deterministically after detecting a non-reusable "
                    "worker_off_track testless diff. The rejected source-only patch is not applied before "
                    "this worker starts; rebuild the repair from the clean target files. Active repair targets "
                    "are the in-contract required test file(s) plus the parent task target file(s): "
                    + ", ".join(active_files)
                    + "."
                )
            repair_focus_instructions = []
            if stalled_no_diff_repair:
                stalled_instruction = (
                    "The previous deterministic repair worker stalled without producing any diff; before any "
                    "additional broad exploration, make a concrete first edit in the required test file(s) "
                    + test_text
                    + ", then make the smallest paired production edit in "
                    + source_text
                    + "."
                )
                acceptance_criteria.insert(1, stalled_instruction)
                repair_focus_instructions.append(stalled_instruction)
                scope_note += " " + stalled_instruction
            subtask = {
                "id": str(artifact.get("subtask_id") or "testless-diff-repair").strip() or "testless-diff-repair",
                "title": "Repair testless partial diff",
                "goal": (
                    "Replace the rejected source-only partial worker diff with a test-backed repair for "
                    + ", ".join(active_files)
                    + "."
                ),
                "files": active_files,
                "target_files": active_files,
                "acceptance_criteria": acceptance_criteria,
                "repair_source_subtask_id": str(artifact.get("subtask_id") or "").strip(),
                "repair_source_worker_id": str(artifact.get("worker_id") or "").strip(),
                "repair_changed_files": changed_files,
                "repair_requires_test_followup": required_test_files,
                "repair_worker_output_excerpt": excerpt[:1200],
                "repair_failure_summary": failure_summary,
                "repair_parent_target_files": artifact_targets,
                "deterministic_testless_repair": True,
                "deterministic_partial_diff_repair": True,
                "write_required": True,
                "scope_note": scope_note,
            }
            if repair_focus_instructions:
                subtask["repair_focus_instructions"] = repair_focus_instructions
                subtask["repair_focus_instruction"] = repair_focus_instructions[0]
                subtask["repair_stalled_no_diff_retry"] = True
            if patch_is_reusable:
                subtask["carry_forward_patch"] = patch_path
            else:
                subtask["discarded_partial_diff_patch"] = patch_path
            if deferred_files:
                subtask["repair_deferred_files"] = deferred_files
                subtask["scope_note"] += (
                    " ACA deferred out-of-contract partial-diff files for later manager scope: "
                    + ", ".join(deferred_files)
                    + "."
                )
            return {
                "kind": "worker_off_track_testless_diff",
                "summary": (
                    "Deterministic repair for a worker_off_track testless partial diff; skipped a second "
                    "manager planning round-trip and "
                    + (
                        "carried the source patch into a required-test follow-up."
                        if patch_is_reusable
                        else "discarded the non-reusable source patch before rebuilding source+test coverage."
                    )
                ),
                "subtasks": [subtask],
                "risks": [
                    (
                        "The preserved source patch may need narrow adjustment once required coverage is added."
                        if patch_is_reusable
                        else "The rejected source-only patch is intentionally not replayed."
                    )
                ],
                "tests": [
                    "Run the narrowest deterministic verification for " + ", ".join(active_files) + "."
                ],
            }
        if "changed only required test files" not in lowered and "test-only" not in lowered:
            continue
        test_files = [path for path in changed_files if _repo_path_looks_like_test_file(path)]
        production_followup_files = _test_only_partial_production_followup_files(
            {
                "repair_changed_files": changed_files,
                "files": artifact_targets,
                "target_files": artifact_targets,
            },
            artifact_targets,
        )
        if not production_followup_files:
            production_followup_files = _test_only_partial_existing_sibling_production_files(ctx, test_files)
        active_files = list(dict.fromkeys([*production_followup_files, *test_files]))
        if not test_files or not production_followup_files or not active_files:
            continue
        production_text = ", ".join(production_followup_files)
        test_text = ", ".join(test_files)
        subtask = {
            "id": str(artifact.get("subtask_id") or "test-only-diff-repair").strip() or "test-only-diff-repair",
            "title": "Repair test-only partial diff",
            "goal": (
                "Replace the rejected test-only partial worker diff with a production-backed repair for "
                + ", ".join(active_files)
                + "."
            ),
            "files": active_files,
            "target_files": active_files,
            "acceptance_criteria": [
                "Read the changed test file(s): " + test_text + ".",
                "Make the first new repair edit in the required production file(s): " + production_text + ".",
                "Do not copy or replay the rejected partial patch; use it only as failure evidence.",
                "Do not mark this repair complete with a test-only diff; leave a substantive production diff in "
                + production_text
                + " or record a concrete blocker explaining why no production edit is safe.",
            ],
            "discarded_partial_diff_patch": patch_path,
            "repair_source_subtask_id": str(artifact.get("subtask_id") or "").strip(),
            "repair_source_worker_id": str(artifact.get("worker_id") or "").strip(),
            "repair_changed_files": changed_files,
            "repair_requires_production_followup": production_followup_files,
            "repair_worker_output_excerpt": excerpt[:1200],
            "repair_failure_summary": failure_summary,
            "deterministic_partial_diff_repair": True,
            "write_required": True,
            "scope_note": (
                "ACA generated this repair plan deterministically after detecting a worker test-only diff. "
                "Active repair targets are the required production follow-up file(s) plus the changed test file(s): "
                + ", ".join(active_files)
                + "."
            ),
        }
        return {
            "kind": "worker_test_only_diff",
            "summary": (
                "Deterministic repair for a worker test-only partial diff; skipped a second manager "
                "planning round-trip and narrowed the retry to the changed tests plus required production follow-up."
            ),
            "subtasks": [subtask],
            "risks": [
                "The rejected partial patch is not replayed automatically; the worker must rebuild the repair from a clean checkout."
            ],
            "tests": [
                "Run the narrowest deterministic verification for " + ", ".join(active_files) + "."
            ],
        }
    return None


def _one_sided_guard_artifact_allows_complementary_carry_forward(
    artifact: dict[str, Any],
    expected_failure_reason: str,
) -> bool:
    """Return whether a rejected one-sided patch may be composed with its missing half."""
    failure_reason = str(artifact.get("failure_reason") or "").strip()
    if failure_reason != expected_failure_reason:
        return False
    if not str(artifact.get("patch_path") or "").strip():
        return False
    reusable_reason = str(artifact.get("patch_reusable_reason") or "").strip()
    if artifact.get("patch_reusable") is False and reusable_reason not in {
        "",
        "one_sided_guard",
        expected_failure_reason.lower(),
    }:
        return False
    excerpt = str(artifact.get("worker_output_excerpt") or "").lower()
    unsafe_markers = (
        "self-referential",
        "test-only helper",
        "local oracle",
        "unproductive partial diff",
        "unproductive diff",
        "comment-only",
        "tautological",
        "changes only string wording",
        "runaway guard",
        "diff exceeded aca runaway",
        "destructive rewrite",
        "giant patch",
    )
    return not any(marker in excerpt for marker in unsafe_markers)


def _deterministic_complementary_partial_diff_repair_plan(
    ctx: RunContext,
    artifacts: list[Any],
    parent_targets: list[str],
) -> dict[str, Any] | None:
    test_artifacts: list[dict[str, Any]] = []
    source_artifacts: list[dict[str, Any]] = []
    destructive_artifacts: list[dict[str, Any]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        changed_files = _partial_diff_changed_files(artifact)
        failure_reason = str(artifact.get("failure_reason") or "").strip()
        excerpt = str(artifact.get("worker_output_excerpt") or "").lower()
        changed_only_tests = _all_changed_files_are_tests(changed_files)
        changed_only_sources = bool(changed_files) and all(
            _repo_path_looks_like_production_source_file(path) for path in changed_files
        )
        if failure_reason in {"WORKER_DESTRUCTIVE_DIFF", "WORKER_RUNAWAY_DIFF"}:
            destructive_artifacts.append(artifact)
        if changed_only_tests and (
            failure_reason == "WORKER_TEST_ONLY_DIFF"
            or "changed only required test files" in excerpt
            or "test-only" in excerpt
        ):
            test_artifacts.append(artifact)
        elif changed_only_sources and (
            failure_reason == "WORKER_OFF_TRACK_TESTLESS_DIFF"
            or "changed only non-test files" in excerpt
            or "worker_off_track_testless_diff" in excerpt
        ):
            source_artifacts.append(artifact)
    if not test_artifacts or not source_artifacts:
        return None
    for test_artifact in test_artifacts:
        test_files = [path for path in _partial_diff_changed_files(test_artifact) if _repo_path_looks_like_test_file(path)]
        for source_artifact in source_artifacts:
            source_files = [
                path
                for path in _partial_diff_changed_files(source_artifact)
                if _repo_path_looks_like_production_source_file(path)
            ]
            paired_source_files = [
                source_file
                for source_file in source_files
                if any(_paired_production_path_for_test_file(test_file, source_files) == source_file for test_file in test_files)
            ] or source_files[:1]
            if parent_targets:
                paired_source_files = [path for path in paired_source_files if path in parent_targets] or paired_source_files
                test_files = [path for path in test_files if path in parent_targets] or test_files
            active_files = list(dict.fromkeys([*paired_source_files, *test_files]))
            patches = [
                str(source_artifact.get("patch_path") or "").strip(),
                str(test_artifact.get("patch_path") or "").strip(),
            ]
            patches = [patch for patch in dict.fromkeys(patches) if patch]
            if not paired_source_files or not test_files:
                continue
            source_text = ", ".join(paired_source_files)
            test_text = ", ".join(test_files)
            patches_reusable = (
                len(patches) >= 2
                and source_artifact.get("patch_reusable") is not False
                and test_artifact.get("patch_reusable") is not False
            )
            active_file_set = set(active_files)
            destructive_active_artifacts = [
                artifact
                for artifact in destructive_artifacts
                if active_file_set.intersection(_partial_diff_changed_files(artifact))
            ]
            destructive_retry_guard = bool(destructive_active_artifacts)
            guarded_pair_reusable = (
                len(patches) >= 2
                and not destructive_retry_guard
                and _one_sided_guard_artifact_allows_complementary_carry_forward(
                    source_artifact,
                    "WORKER_OFF_TRACK_TESTLESS_DIFF",
                )
                and _one_sided_guard_artifact_allows_complementary_carry_forward(
                    test_artifact,
                    "WORKER_TEST_ONLY_DIFF",
                )
            )
            if guarded_pair_reusable:
                patches_reusable = True
            destructive_guard_criteria = [
                "A prior complementary rebuild tripped the destructive rewrite guard; preserve existing file structure, avoid deleting or replacing existing functions, and keep deletions near zero.",
                "If satisfying this repair would require a broad rewrite, stop and record a concrete blocker instead of editing.",
            ] if destructive_retry_guard else []
            destructive_focus_instructions = [
                "A prior retry for this same source+test repair produced a destructive rewrite. This attempt must be small, additive, and deletion-averse.",
                "Preserve existing symbols and file layout; patch only the narrow behavior required by the paired source/test contract.",
            ] if destructive_retry_guard else []
            destructive_patch_paths = [
                str(artifact.get("patch_path") or "").strip()
                for artifact in destructive_active_artifacts
            ]
            destructive_patch_paths = [path for path in dict.fromkeys(destructive_patch_paths) if path]
            destructive_scope_suffix = (
                " A prior complementary rebuild tripped the destructive rewrite guard; this retry must be additive, surgical, and near-zero deletion."
                if destructive_retry_guard
                else ""
            )
            if not patches_reusable:
                return {
                    "kind": "complementary_rejected_partial_diff",
                    "summary": (
                        "Deterministic repair for complementary rejected source-only and test-only partial "
                        "diffs; ACA will rebuild both sides from a clean checkout without replaying either patch."
                    ),
                    "subtasks": [
                        {
                            "id": str(test_artifact.get("subtask_id") or "complementary-rejected-diff-repair").strip()
                            or "complementary-rejected-diff-repair",
                            "title": "Rebuild complementary source and test partial diffs",
                            "goal": (
                                "Rebuild a single source+test repair for "
                                + ", ".join(active_files)
                                + " without copying either rejected partial patch."
                            ),
                            "files": active_files,
                            "target_files": active_files,
                            "acceptance_criteria": [
                                "Do not copy or replay the rejected partial patch artifacts.",
                                *destructive_guard_criteria,
                                "Read both sides before editing: " + source_text + " and " + test_text + ".",
                                "Prefer one focused write step that edits both the required test file(s) "
                                + test_text
                                + " and paired production file(s) "
                                + source_text
                                + ".",
                                "If the edit tool cannot change both sides in one patch, make the test edit and production edit back-to-back before any more exploration or verification.",
                                "A production-only diff repeats WORKER_OFF_TRACK_TESTLESS_DIFF and fails this repair; a test-only diff repeats WORKER_TEST_ONLY_DIFF and fails this repair.",
                                "Do not mark this repair complete until the current diff includes both "
                                + source_text
                                + " and "
                                + test_text
                                + ", and narrow verification has run or a concrete blocker is recorded.",
                            ],
                            "discarded_partial_diff_patch": patches[0] if patches else "",
                            "discarded_partial_diff_patches": patches,
                            "discarded_destructive_partial_diff_patches": destructive_patch_paths,
                            "repair_source_subtask_id": str(test_artifact.get("subtask_id") or "").strip(),
                            "repair_source_worker_id": str(test_artifact.get("worker_id") or "").strip(),
                            "repair_changed_files": active_files,
                            "repair_requires_production_followup": paired_source_files,
                            "repair_requires_test_followup": test_files,
                            "repair_requires_paired_source_test": True,
                            "repair_requires_paired_source_test_diff": True,
                            "repair_mode": "complementary_rejected_partial_diff",
                            "repair_precision_edit": True,
                            "repair_diff_line_budget": 80,
                            "repair_rejected_source_only_files": paired_source_files,
                            "repair_rejected_test_only_files": test_files,
                            "repair_focus_instructions": [
                                "This is not a production-first repair and not a test-only repair. Produce one paired source+test diff in this attempt, preferably with one focused patch touching both sides.",
                                "Use the rejected source-only and test-only patches only as failure evidence; rebuild the behavior from clean files.",
                                *destructive_focus_instructions,
                            ],
                            "repair_parent_target_files": active_files,
                            "repair_failure_summary": (
                                "previous retries produced separate source-only and test-only partial diffs"
                            ),
                            "deterministic_partial_diff_repair": True,
                            "write_required": True,
                            "scope_note": (
                                "ACA detected complementary rejected partial diffs: one source-only attempt for "
                                + source_text
                                + " and one test-only attempt for "
                                + test_text
                                + ". Rebuild both sides from clean files; do not replay either rejected patch."
                                + destructive_scope_suffix
                            ),
                        }
                    ],
                    "risks": [
                        "The rejected partial patches are intentionally not replayed; the worker must recreate the useful intent."
                    ],
                    "tests": ["Run the narrowest deterministic verification for " + test_text + "."],
                }
            if len(patches) < 2:
                continue
            plan_kind = (
                "complementary_guarded_partial_diff"
                if guarded_pair_reusable
                else "complementary_partial_diff"
            )
            plan_summary = (
                "Deterministic repair for complementary one-sided guard partial diffs; "
                "ACA will compose both saved patches before asking the worker to verify and fix the combined diff."
                if guarded_pair_reusable
                else (
                    "Deterministic repair for complementary source-only and test-only partial diffs; "
                    "ACA will apply both saved patches before asking the worker to verify and fix the combined diff."
                )
            )
            repair_focus_instructions = []
            if guarded_pair_reusable:
                repair_focus_instructions.append(
                    "ACA mechanically composed a source-only guarded patch with a test-only guarded patch. "
                    "Verify the combined source+test diff first, and make only the smallest correction needed."
                )
            return {
                "kind": plan_kind,
                "summary": plan_summary,
                "subtasks": [
                    {
                        "id": str(test_artifact.get("subtask_id") or "complementary-diff-repair").strip()
                        or "complementary-diff-repair",
                        "title": "Verify complementary source and test partial diffs",
                        "goal": "Verify and minimally fix the combined source+test repair for " + ", ".join(active_files) + ".",
                        "files": active_files,
                        "target_files": active_files,
                        "acceptance_criteria": [
                            "ACA applied the preserved production patch and test patch before this worker starts; inspect the combined diff in "
                            + ", ".join(active_files)
                            + ".",
                            *destructive_guard_criteria,
                            "Run the narrowest deterministic verification for "
                            + test_text
                            + "; if it fails, fix only the paired production/test behavior in "
                            + ", ".join(active_files)
                            + ".",
                            "Do not expand into broader manager scope unless a direct import, compile, or test failure in the active files requires it.",
                        ],
                        "carry_forward_patches": patches,
                        "discarded_destructive_partial_diff_patches": destructive_patch_paths,
                        "repair_source_subtask_id": str(test_artifact.get("subtask_id") or "").strip(),
                        "repair_source_worker_id": str(test_artifact.get("worker_id") or "").strip(),
                        "repair_changed_files": active_files,
                        "repair_requires_production_followup": paired_source_files,
                        "repair_requires_test_followup": test_files,
                        "repair_requires_paired_source_test": True,
                        "repair_requires_paired_source_test_diff": True,
                        "repair_verification_first": True,
                        "repair_mode": plan_kind,
                        "repair_focus_instructions": repair_focus_instructions,
                        "deterministic_partial_diff_repair": True,
                        "write_required": False,
                        "scope_note": (
                            "ACA detected complementary partial diffs: one production-only patch for "
                            + source_text
                            + " and one test-only patch for "
                            + test_text
                            + ". Both patches are applied before this worker starts; verify/fix the combined source+test diff."
                            + destructive_scope_suffix
                        ),
                    }
                ],
                "risks": [
                    "If either preserved patch no longer applies, ACA will fail closed and retry with fresh repair evidence."
                ],
                "tests": ["Run the narrowest deterministic verification for " + test_text + "."],
            }
    return None


def _deterministic_repair_plan_kind(plan: dict[str, Any]) -> str:
    kind = str(plan.get("kind") or "").strip()
    if kind:
        return kind
    subtasks = plan.get("subtasks") if isinstance(plan, dict) else []
    if isinstance(subtasks, list):
        for subtask in subtasks:
            if not isinstance(subtask, dict):
                continue
            if str(subtask.get("repair_source_failure_reason") or "").strip() in {
                "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                "WORKER_VERIFIABLE_DIFF_UNTERMINATED",
            }:
                return "failed_verifiable_source_test_diff"
            if str(subtask.get("title") or "").strip() == "Repair weak source+test partial diff":
                return "weak_source_test_diff"
            if str(subtask.get("title") or "").strip() == "Repair source-only engine timeout partial diff":
                return "source_engine_timeout_partial_diff"
            if subtask.get("deterministic_testless_repair"):
                return "worker_off_track_testless_diff"
            if subtask.get("repair_requires_production_followup"):
                return "worker_test_only_diff"
    return "worker_off_track_testless_diff"


def _deterministic_partial_diff_repair_manager_result(ctx: RunContext) -> dict[str, Any] | None:
    deterministic_repair_plan = _deterministic_testless_partial_diff_repair_plan(ctx)
    if not deterministic_repair_plan:
        return None
    repair_kind = _deterministic_repair_plan_kind(deterministic_repair_plan)
    ctx.manager_plan = deterministic_repair_plan
    ctx.blackboard["manager_plan"] = ctx.manager_plan
    ctx.blackboard["manager_deterministic_repair_plan"] = {
        "kind": repair_kind,
        "subtask_count": len(deterministic_repair_plan.get("subtasks") or []),
    }
    from src.tandem_agents.runtime.runstate import append_event, save_blackboard, write_status
    from src.tandem_agents.runtime.run_output import write_blackboard_snapshot

    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    write_status(ctx.layout["status"], ctx.status)
    append_event(
        ctx.layout["events"],
        "manager.deterministic_repair_plan",
        ctx.run_id,
        ctx.blackboard["manager_deterministic_repair_plan"],
        task_id=ctx.task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )
    return {
        "returncode": 0,
        "stdout": json.dumps(deterministic_repair_plan),
        "stderr": "",
        "engine": {"skipped": True, "reason": repair_kind},
    }


def _mark_manager_planning_started(ctx: RunContext) -> None:
    from src.tandem_agents.runtime.run_output import set_status

    ctx.status = set_status(
        ctx.status,
        ctx.layout,
        phase="planning",
        phase_detail="manager planning",
        phase_role="manager",
        run_status="running",
    )


def _manager_prompt_timeout_grace_seconds(cfg: Any) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_MANAGER_PROMPT_TIMEOUT_GRACE_SECONDS", "") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_MANAGER_PROMPT_TIMEOUT_GRACE_SECONDS=%r", raw)
    return 10.0


def _manager_prompt_timeout_floor_seconds(cfg: Any) -> float:
    try:
        from src.tandem_agents.core.execution.worker import (
            _engine_async_dispatch_timeout_seconds,
            _scaled_async_prompt_timeout_seconds,
        )

        engine_timeout = _scaled_async_prompt_timeout_seconds(cfg, "manager", False, 1.0)
        dispatch_timeout = _engine_async_dispatch_timeout_seconds(cfg)
    except Exception:
        logger.debug("Falling back to local manager timeout floor calculation", exc_info=True)
        coordination = getattr(cfg, "coordination", None)
        try:
            lease_ttl = float(getattr(coordination, "lease_ttl_seconds", 300) or 300)
        except (TypeError, ValueError):
            lease_ttl = 300.0
        try:
            heartbeat = float(getattr(coordination, "heartbeat_interval_seconds", 30) or 30)
        except (TypeError, ValueError):
            heartbeat = 30.0
        engine_timeout = max(120.0, min(360.0, (lease_ttl * 2.0) - heartbeat))
        dispatch_timeout = 20.0
    return max(1.0, float(engine_timeout) + float(dispatch_timeout) + _manager_prompt_timeout_grace_seconds(cfg))


def _manager_prompt_timeout_seconds(cfg: Any) -> float:
    floor_seconds = _manager_prompt_timeout_floor_seconds(cfg)
    raw = str(getattr(cfg, "env", {}).get("ACA_MANAGER_PROMPT_TIMEOUT_SECONDS", "") or "").strip()
    if raw:
        try:
            return max(floor_seconds, float(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_MANAGER_PROMPT_TIMEOUT_SECONDS=%r", raw)
    return floor_seconds


def _deterministic_testless_repair_active(subtasks: list[dict[str, Any]]) -> bool:
    return any(
        bool(subtask.get("deterministic_testless_repair") or subtask.get("deterministic_partial_diff_repair"))
        for subtask in subtasks
    )


def _repair_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _write_required_after_prescreen(subtask: dict[str, Any]) -> bool:
    if subtask.get("repair_verification_first"):
        return True
    return not bool(subtask.get("pre_satisfied"))


def _subtask_has_source_or_test_targets(subtask: dict[str, Any]) -> bool:
    for raw_path in list(subtask.get("target_files") or []) + list(subtask.get("files") or []):
        rel_path = _normalize_repo_relative_path(raw_path)
        if not rel_path:
            continue
        if _repo_path_looks_like_test_file(rel_path) or _repo_path_looks_like_production_source_file(rel_path):
            return True
    return False


def _narrow_carried_partial_diff_subtask(subtask: dict[str, Any]) -> None:
    if not subtask.get("carry_forward_patch"):
        return
    changed_files = [
        rel_path
        for rel_path in (_normalize_repo_relative_path(raw_path) for raw_path in (subtask.get("repair_changed_files") or []))
        if rel_path and rel_path != "__aca_temp_probe.txt"
    ]
    active_files = list(changed_files)
    declared_files = sorted(_subtask_declared_files(subtask))
    support_only_repair = _all_repo_paths_are_support_only(list(dict.fromkeys([*changed_files, *declared_files])))
    if support_only_repair:
        active_files = list(dict.fromkeys([*changed_files, *declared_files]))
    production_followup_files = _test_only_partial_production_followup_files(subtask)
    if production_followup_files:
        active_files = list(dict.fromkeys([*production_followup_files, *changed_files]))
    required_test_files = _testless_partial_required_test_files(subtask)
    if not required_test_files:
        required_test_files = _source_partial_declared_test_followup_files(subtask)
    if required_test_files:
        active_files = list(dict.fromkeys([*changed_files, *required_test_files]))
    verify_first_partial = _partial_diff_retry_should_verify_first(subtask, changed_files)
    if verify_first_partial:
        subtask["write_required"] = True
        subtask["repair_verification_first"] = True
    if active_files:
        previous_files = sorted(_subtask_declared_files(subtask).difference(active_files))
        subtask["files"] = list(dict.fromkeys(active_files))
        subtask["target_files"] = list(dict.fromkeys(active_files))
        if previous_files:
            subtask["repair_deferred_files"] = list(
                dict.fromkeys([*(subtask.get("repair_deferred_files") or []), *previous_files])
            )
        if production_followup_files:
            subtask["repair_requires_production_followup"] = production_followup_files
        if required_test_files:
            subtask["repair_requires_test_followup"] = required_test_files
    patch_text = ""
    patch_path = str(subtask.get("carry_forward_patch") or "").strip()
    if patch_path:
        try:
            patch_text = Path(patch_path).read_text(encoding="utf-8")
        except OSError:
            patch_text = ""
    context = _compact_partial_diff_repair_context(str(subtask.get("repair_worker_output_excerpt") or ""))
    focused_exception_instructions = [
        instruction
        for instruction in (
            _zero_division_assertion_repair_instruction("\n".join([context, patch_text])),
            _missing_import_repair_instruction(context),
            _missing_dependency_import_repair_instruction(context),
            _name_error_repair_instruction(context),
            _unexpected_keyword_repair_instruction(context),
            _positional_argument_repair_instruction(context),
            _missing_required_argument_repair_instruction(context),
            _string_dict_attribute_repair_instruction(context),
            _future_import_syntax_repair_instruction(context),
            _assertion_failure_repair_instruction(context),
        )
        if instruction
    ]
    focused_failure_instruction = _focused_failure_first_repair_instruction(context)
    if focused_failure_instruction:
        subtask["repair_failure_focus"] = focused_failure_instruction
    if focused_exception_instructions:
        subtask["repair_focus_instruction"] = focused_exception_instructions[0]
        subtask["repair_focus_instructions"] = focused_exception_instructions
    target_text = ", ".join(active_files) if active_files else "the files touched by the preserved patch"
    verification_commands = [
        str(command or "").strip()
        for command in (subtask.get("verification_commands") or [])
        if str(command or "").strip()
    ]
    verification_instruction = (
        "Run this focused verification first: " + "; ".join(verification_commands[:2]) + "."
        if verification_commands
        else "Run the narrow deterministic verification first."
    )
    original_criteria = [
        str(entry).strip()
        for entry in (subtask.get("acceptance_criteria") or [])
        if str(entry).strip()
    ]
    if original_criteria and "repair_deferred_acceptance_criteria" not in subtask:
        subtask["repair_deferred_acceptance_criteria"] = original_criteria
    criteria = [
        "Resolve the recovered partial-diff blocker by finishing the preserved patch in: " + target_text + ".",
        "Run the narrowest deterministic verification for the preserved files, or record the exact unavailable command/blocker.",
    ]
    if context:
        criteria.insert(1, "Recovered blocker context: " + context)
    insert_at = 2 if context else 1
    if focused_failure_instruction:
        criteria.insert(insert_at, focused_failure_instruction)
        insert_at += 1
    for instruction in reversed(focused_exception_instructions):
        criteria.insert(insert_at, instruction)
    if verify_first_partial:
        criteria = [
            "Apply and inspect the preserved source+test patch in: " + target_text + ".",
            verification_instruction + " If it passes, return a terminal completion note without making another mandatory edit.",
            "If verification fails, fix only the failing behavior in the preserved source/test files and rerun the narrow verification or record the exact blocker.",
        ]
        if context:
            criteria.insert(1, "Recovered blocker context: " + context)
        insert_at = 2 if context else 1
        if focused_failure_instruction:
            criteria.insert(insert_at, focused_failure_instruction)
            insert_at += 1
        for instruction in reversed(focused_exception_instructions):
            criteria.insert(insert_at, instruction)
    elif production_followup_files:
        criteria.insert(
            1,
            "The preserved diff is test-only; read and complete the required production behavior in: "
            + ", ".join(production_followup_files)
            + " before changing or adding more tests.",
        )
        criteria.insert(
            2,
            "Do not mark this repair complete with a test-only diff; either leave a semantic production diff in "
            + ", ".join(production_followup_files)
            + " or report a concrete blocker explaining why no production edit is safe.",
        )
        criteria.insert(
            3,
            "After applying the carried test patch, make the first new repair edit in the production follow-up file before expanding the test patch.",
        )
        criteria.append(
            "Do not expand into unrelated manager scope beyond the preserved patch and required production follow-up files."
        )
    elif required_test_files:
        criteria.insert(
            1,
            "The preserved source diff is already applied and counts as the paired production edit unless verification proves it wrong; read and edit the required test file(s) in: "
            + ", ".join(required_test_files)
            + " before continuing production-only work.",
        )
        criteria.insert(
            2,
            "Do not mark this repair complete until the diff includes real coverage in "
            + ", ".join(required_test_files)
            + " and the narrow verification has run or a concrete blocker is recorded.",
        )
        criteria.append(
            "Do not expand into unrelated manager scope beyond the preserved patch and required test follow-up files."
        )
    else:
        if support_only_repair:
            criteria.insert(
                1 if not context else 2,
                "The preserved docs diff is already applied before this worker starts; finish any remaining declared docs target before broadening scope.",
            )
            criteria.append(
                "Keep the final repair docs-only; do not convert a documentation timeout into source, test, runtime, config, or lockfile edits."
            )
        else:
            criteria.insert(
                1 if not context else 2,
                "Do not expand into deferred manager scope unless a direct import or compile/test failure in the preserved files requires it.",
            )
    subtask["acceptance_criteria"] = criteria
    subtask["write_required"] = True
    original_goal = str(subtask.get("goal") or "").strip()
    if original_goal and "repair_original_goal" not in subtask:
        subtask["repair_original_goal"] = original_goal
    if verify_first_partial:
        subtask["goal"] = (
            "Verify the preserved source+test partial worker diff and fix only narrow verification failures for "
            + target_text
            + "."
        )
    else:
        subtask["goal"] = (
            "Finish the preserved partial worker diff and obtain a terminal worker verdict for "
            + target_text
            + "."
        )


def _compact_rejected_partial_diff_subtask(subtask: dict[str, Any]) -> None:
    if not subtask.get("discarded_partial_diff_patch"):
        return
    target_files = [
        rel_path
        for rel_path in (
            _normalize_repo_relative_path(raw_path)
            for raw_path in (subtask.get("repair_parent_target_files") or list(_subtask_declared_files(subtask)))
        )
        if rel_path
    ]
    changed_files = [
        rel_path
        for rel_path in (_normalize_repo_relative_path(raw_path) for raw_path in (subtask.get("repair_changed_files") or []))
        if rel_path and rel_path != "__aca_temp_probe.txt"
    ]
    original_criteria = [
        str(entry).strip()
        for entry in (subtask.get("acceptance_criteria") or [])
        if str(entry).strip()
    ]
    if original_criteria and "repair_deferred_acceptance_criteria" not in subtask:
        subtask["repair_deferred_acceptance_criteria"] = original_criteria
    target_text = ", ".join(target_files) if target_files else "the active repair target files"
    changed_text = ", ".join(changed_files) if changed_files else "none recorded"
    failure_summary = str(subtask.get("repair_failure_summary") or "").strip()
    criteria = [
        "Replace the rejected or incomplete partial-diff approach before expanding scope.",
        "Keep repair edits scoped to the active repair target files: " + target_text + ".",
        "Use the rejected diff only as failure evidence; do not read, apply, copy, or reference its patch artifact.",
        "Rejected diff touched: " + changed_text + ".",
        "Add one deterministic production-backed assertion or implementation slice that addresses the failure summary.",
        "Run the narrowest deterministic verification for the active target files, or record the exact unavailable command/blocker.",
    ]
    if failure_summary:
        criteria.insert(1, "Failure summary: " + failure_summary + ".")
    subtask["acceptance_criteria"] = criteria
    original_goal = str(subtask.get("goal") or "").strip()
    if original_goal and "repair_original_goal" not in subtask:
        subtask["repair_original_goal"] = original_goal
    subtask["goal"] = (
        "Replace the rejected partial worker diff with a narrow production-backed repair for "
        + target_text
        + "."
    )


def _constrain_extra_partial_diff_repair_subtasks(
    ctx: RunContext,
    subtasks: list[dict[str, Any]],
) -> None:
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    if not isinstance(repair, dict) or not repair.get("partial_diff_artifacts"):
        return
    attempt = _repair_int(repair.get("attempt"))
    base_max_loops = _repair_int(repair.get("base_max_loops"))
    if not attempt or not base_max_loops or attempt <= base_max_loops:
        return
    carried = [subtask for subtask in subtasks if subtask.get("carry_forward_patch") or subtask.get("carry_forward_patches")]
    rejected = [
        subtask
        for subtask in subtasks
        if subtask.get("discarded_partial_diff_patch") or subtask.get("repair_parent_target_files")
    ]
    candidates = carried or rejected
    if not candidates:
        return
    chosen = candidates[0]
    if len(subtasks) > 1:
        subtasks[:] = [chosen]
    parent_target_files = [
        rel_path
        for rel_path in (
            _normalize_repo_relative_path(raw_path)
            for raw_path in (chosen.get("repair_parent_target_files") or [])
        )
        if rel_path
    ]
    if chosen.get("discarded_partial_diff_patch") and parent_target_files:
        chosen["files"] = list(dict.fromkeys(parent_target_files))
        chosen["target_files"] = list(dict.fromkeys(parent_target_files))
        existing_scope_note = str(chosen.get("scope_note") or "").strip()
        parent_note = (
            "ACA kept this extra repair attempt on the active repair target files because the preserved "
            "partial diff was rejected or incomplete. Active repair targets are: "
            + ", ".join(parent_target_files)
            + "."
        )
        if parent_note not in existing_scope_note:
            chosen["scope_note"] = f"{existing_scope_note}\n{parent_note}".strip()
        return
    changed_files = [
        rel_path
        for rel_path in (_normalize_repo_relative_path(raw_path) for raw_path in (chosen.get("repair_changed_files") or []))
        if rel_path and rel_path != "__aca_temp_probe.txt"
    ]
    _narrow_carried_partial_diff_subtask(chosen)
    existing_scope_note = str(chosen.get("scope_note") or "").strip()
    narrow_note = (
        "ACA narrowed this extra repair attempt to the carried partial-diff subtask so the worker "
        "finishes the preserved files before the manager can expand into new swarm slices."
    )
    active_targets = [
        rel_path
        for rel_path in (
            _normalize_repo_relative_path(raw_path)
            for raw_path in (chosen.get("target_files") or chosen.get("files") or changed_files)
        )
        if rel_path
    ]
    if active_targets:
        narrow_note += " Active repair targets are: " + ", ".join(active_targets) + "."
    if chosen.get("repair_requires_production_followup"):
        narrow_note += (
            " Production follow-up targets required for the test-only partial diff: "
            + ", ".join(chosen["repair_requires_production_followup"])
            + "."
        )
    if chosen.get("repair_deferred_files"):
        narrow_note += " Broader manager files are deferred until the preserved patch is terminal: " + ", ".join(chosen["repair_deferred_files"]) + "."
    if narrow_note not in existing_scope_note:
        chosen["scope_note"] = f"{existing_scope_note}\n{narrow_note}".strip()


def _extra_partial_diff_repair_active(ctx: RunContext) -> bool:
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    if not isinstance(repair, dict) or not repair.get("partial_diff_artifacts"):
        return False
    attempt = _repair_int(repair.get("attempt"))
    base_max_loops = _repair_int(repair.get("base_max_loops"))
    return bool(attempt and base_max_loops and attempt > base_max_loops)


def _merge_or_defer_sticky_expected_files(
    ctx: RunContext,
    subtasks: list[dict[str, Any]],
    current_expected_files: list[str],
    sticky_expected_files: list[str],
) -> None:
    sticky_missing_from_plan = [path for path in sticky_expected_files if path not in current_expected_files]
    if not sticky_missing_from_plan or not subtasks:
        return
    first = subtasks[0]
    partial_diff_repair = bool(first.get("carry_forward_patch") or first.get("carry_forward_patches"))
    rejected_partial_diff_repair = bool(
        first.get("discarded_partial_diff_patch") or first.get("repair_parent_target_files")
    )
    production_followup_files = _test_only_partial_production_followup_files(
        first,
        sticky_missing_from_plan,
    )
    if partial_diff_repair:
        if production_followup_files:
            _append_unique_repo_paths(first, production_followup_files)
            _narrow_carried_partial_diff_subtask(first)
            existing_scope_note = str(first.get("scope_note") or "").strip()
            production_note = (
                "ACA kept sticky production follow-up targets active because the preserved partial diff is test-only: "
                + ", ".join(production_followup_files)
                + "."
            )
            if production_note not in existing_scope_note:
                first["scope_note"] = f"{existing_scope_note}\n{production_note}".strip()
        deferred = [
            str(entry).strip()
            for entry in (first.get("repair_deferred_files") or [])
            if str(entry).strip()
        ]
        for path in (path for path in sticky_missing_from_plan if path not in production_followup_files):
            if path not in deferred:
                deferred.append(path)
        if deferred:
            first["repair_deferred_files"] = deferred
        return
    if rejected_partial_diff_repair:
        parent_target_files = [
            rel_path
            for rel_path in (
                _normalize_repo_relative_path(raw_path)
                for raw_path in (first.get("repair_parent_target_files") or [])
            )
            if rel_path
        ]
        if parent_target_files:
            scoped_targets = list(dict.fromkeys(parent_target_files))
            first["files"] = scoped_targets
            first["target_files"] = scoped_targets
        deferred = [
            str(entry).strip()
            for entry in (first.get("repair_deferred_files") or [])
            if str(entry).strip()
        ]
        for path in sticky_missing_from_plan:
            if path not in deferred:
                deferred.append(path)
        if deferred:
            first["repair_deferred_files"] = deferred
        existing_scope_note = str(first.get("scope_note") or "").strip()
        deferred_note = (
            "ACA deferred sticky expected files from earlier attempts because this retry is replacing a rejected "
            "partial diff and must stay scoped to the parent repair targets: "
            + ", ".join(sticky_missing_from_plan)
            + "."
        )
        if deferred_note not in existing_scope_note:
            first["scope_note"] = f"{existing_scope_note}\n{deferred_note}".strip()
        return
    for key in ("files", "target_files"):
        values = [str(entry).strip() for entry in (first.get(key) or []) if str(entry).strip()]
        for path in sticky_missing_from_plan:
            if path not in values:
                values.append(path)
        first[key] = values
    existing_scope_note = str(first.get("scope_note") or "").strip()
    sticky_note = (
        "ACA kept these expected files from an earlier retry attempt because later manager plans "
        "must not narrow the run contract: "
        + ", ".join(sticky_missing_from_plan)
        + "."
    )
    first["scope_note"] = f"{existing_scope_note}\n{sticky_note}".strip()
    if production_followup_files and (first.get("carry_forward_patch") or first.get("carry_forward_patches")):
        _narrow_carried_partial_diff_subtask(first)


def _completed_repair_subtask_ids(ctx: RunContext) -> set[str]:
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    raw_ids = repair.get("completed_subtask_ids") if isinstance(repair, dict) else []
    if not isinstance(raw_ids, list):
        return set()
    return {str(item).strip() for item in raw_ids if str(item).strip()}


def _namespace_repair_retry_subtask_ids(ctx: RunContext, subtasks: list[dict[str, Any]]) -> None:
    """Avoid retry-plan ID collisions with completed subtasks from earlier attempts."""
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    if not isinstance(repair, dict) or not repair.get("partial_diff_artifacts"):
        return
    completed_ids = _completed_repair_subtask_ids(ctx)
    if not completed_ids:
        return
    attempt = max(1, _repair_int(repair.get("attempt")))
    seen: set[str] = set()
    for index, subtask in enumerate(subtasks, start=1):
        original_id = str(subtask.get("id") or f"subtask-{index}").strip() or f"subtask-{index}"
        new_id = original_id
        if original_id in completed_ids or original_id in seen:
            base = f"repair-attempt-{attempt}-{original_id}"
            new_id = base
            suffix = 2
            while new_id in seen:
                new_id = f"{base}-{suffix}"
                suffix += 1
            subtask["repair_original_subtask_id"] = original_id
            subtask["id"] = new_id
            note = (
                "ACA renamed this repair subtask from "
                f"{original_id} to {new_id} so completed-subtask carry-forward from an earlier "
                "attempt cannot skip or overwrite the active repair worker."
            )
            existing_scope_note = str(subtask.get("scope_note") or "").strip()
            if note not in existing_scope_note:
                subtask["scope_note"] = f"{existing_scope_note}\n{note}".strip()
        seen.add(new_id)


def _completed_repair_worker_results(
    ctx: RunContext,
    recorded_subtask_ids: set[str],
) -> list[dict[str, Any]]:
    """Return successful prior-attempt worker results missing from this retry plan."""
    from src.tandem_agents.core.execution import runner_core as _rc  # noqa: PLC0415

    completed_ids = _completed_repair_subtask_ids(ctx)
    if not completed_ids:
        return []
    missing_ids = {subtask_id for subtask_id in completed_ids if subtask_id not in recorded_subtask_ids}
    if not missing_ids:
        return []
    workers = ctx.blackboard.get("workers") if isinstance(ctx.blackboard, dict) else []
    if not isinstance(workers, list):
        return []
    carried: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in reversed(workers):
        if not isinstance(result, dict):
            continue
        subtask_id = str(result.get("subtask_id") or "").strip()
        if not subtask_id or subtask_id not in missing_ids or subtask_id in seen:
            continue
        status = _rc._normalized_text(result.get("status"))
        if status not in {"completed", "skipped_existing", "tolerated_failure"} and not result.get("verified_existing"):
            continue
        cloned = dict(result)
        cloned["worker_id"] = f"repo-check-{subtask_id}"
        cloned["subtask_index"] = 0
        cloned["status"] = "skipped_existing"
        cloned["returncode"] = 0
        cloned["worktree"] = str(ctx.repo_path)
        cloned["write_required"] = False
        cloned["verified_existing"] = True
        cloned["output_excerpt"] = (
            "Subtask carried forward from a completed repair-loop worker even though "
            "the retry manager narrowed the current plan away from this subtask."
        )
        carried.append(cloned)
        seen.add(subtask_id)
    carried.reverse()
    return carried


def run_manager_prompt(ctx: RunContext) -> None:
    """Execute the manager (planning) prompt and populate ctx.manager_plan.

    Mutates:
        ctx.manager_plan   -- parsed plan dict (summary, subtasks, risks, tests)
        ctx.blackboard     -- updated with manager_plan key
    """
    from src.tandem_agents.core.engine.engine import engine_env, engine_session_provider_model
    from src.tandem_agents.core.execution.worker import stream_tandem_prompt
    from src.tandem_agents.core.repository.repo_context import repo_context_for_task
    from src.tandem_agents.core.engine.prompts import build_manager_prompt
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.runtime.runstate import append_event, save_blackboard, write_status
    from src.tandem_agents.runtime.run_output import set_status, write_blackboard_snapshot

    manager_model_selection = engine_session_provider_model(ctx.cfg, "manager")
    manager_provider = manager_model_selection["provider"]
    manager_model = manager_model_selection["model"]
    repo_context = repo_context_for_task(
        ctx.cfg,
        ctx.repo_path,
        ctx.task,
        artifact_path=ctx.layout["artifacts"] / "repo_context_bundle.json",
    )
    ctx.blackboard["repo_context"] = {
        "source": repo_context.source,
        "fallback_used": repo_context.fallback_used,
        "error": repo_context.error,
        "artifact_path": repo_context.artifact_path,
        "path_scope": repo_context.path_scope,
        "required_files": repo_context.required_files or [],
        "index_source": repo_context.index_source,
        "index_status": repo_context.index_status,
        "index_error": repo_context.index_error,
    }
    if _apply_repo_context_required_files_to_task(ctx.task, repo_context.required_files):
        ctx.blackboard["repo_context"]["required_files_applied_as_target_files"] = True
        ctx.status["repo_context_required_files_applied_as_target_files"] = True
    ctx.status.setdefault("artifacts", {})
    if repo_context.artifact_path:
        ctx.status["artifacts"]["repo_context_bundle"] = repo_context.artifact_path
    ctx.status["repo_context"] = dict(ctx.blackboard["repo_context"])
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.layout["run_dir"], ctx.blackboard)
    write_status(ctx.layout["status"], ctx.status)

    deterministic_repair_result = _deterministic_partial_diff_repair_manager_result(ctx)
    if deterministic_repair_result is not None:
        return deterministic_repair_result

    deterministic_repo_context_repair_result = _deterministic_repo_context_repair_manager_result(ctx)
    if deterministic_repo_context_repair_result is not None:
        return deterministic_repo_context_repair_result

    deterministic_explicit_target_result = _deterministic_explicit_target_manager_result(ctx)
    if deterministic_explicit_target_result is not None:
        return deterministic_explicit_target_result

    deterministic_repo_context_result = _deterministic_repo_context_manager_result(ctx)
    if deterministic_repo_context_result is not None:
        return deterministic_repo_context_result

    manager_prompt = build_manager_prompt(
        ctx.run_id,
        ctx.task,
        ctx.repo,
        ctx.cfg,
        repo_context=repo_context.text,
        previous_feedback=getattr(ctx, "_previous_feedback", None),
    )

    _mark_manager_planning_started(ctx)
    append_event(
        ctx.layout["events"],
        "manager.started",
        ctx.run_id,
        {"role": "manager", "repo_context": dict(ctx.blackboard["repo_context"])},
        task_id=ctx.task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )


    logger.info("Running manager prompt (run_id=%s)", ctx.run_id)
    timeout_seconds = _manager_prompt_timeout_seconds(ctx.cfg)

    def _stream_manager_prompt() -> dict[str, Any]:
        return stream_tandem_prompt(
            ctx.cfg,
            role="manager",
            prompt=manager_prompt,
            cwd=ctx.repo_path,
            provider=manager_provider,
            model=manager_model,
            env=engine_env(ctx.cfg),
            log_path=ctx.layout["logs"] / "manager.log",
            config_path=None,
        )

    with _rc._coordination_heartbeat(ctx, phase="planning"):
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aca-manager")
        future = executor.submit(_stream_manager_prompt)
        try:
            manager_result = future.result(timeout=timeout_seconds)
        except FutureTimeoutError:
            grace_seconds = _manager_prompt_timeout_grace_seconds(ctx.cfg)
            try:
                manager_result = future.result(timeout=grace_seconds)
            except FutureTimeoutError:
                _cancel_active_manager_engine_session(ctx, "manager_prompt_timeout")
                waited_seconds = timeout_seconds + grace_seconds
                message = (
                    "ENGINE_PROMPT_TIMEOUT: ACA manager planning prompt exceeded "
                    f"{waited_seconds:.0f}s without producing a plan. The run will use "
                    "contract-based fallback planning instead of remaining stuck in planning."
                )
                try:
                    log_path = ctx.layout["logs"] / "manager.log"
                    with log_path.open("a", encoding="utf-8") as handle:
                        handle.write(message + "\n")
                except Exception:
                    logger.debug("Failed to append manager watchdog message", exc_info=True)
                manager_result = {
                    "role": "manager",
                    "returncode": 1,
                    "stdout": message,
                    "stderr": "",
                    "log_path": str(ctx.layout["logs"] / "manager.log"),
                    "failure_reason": message,
                    "blocker_kind": "manager_prompt_timeout",
                    "recovery_action": (
                        "Use ACA's contract-based fallback planner for this attempt; inspect manager.log "
                        "and engine run state if manager prompts repeatedly time out."
                    ),
                    "engine": {
                        "stream_reason": "manager_prompt_timeout",
                        "timeout_seconds": timeout_seconds,
                        "grace_seconds": grace_seconds,
                    },
                }
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    parsed_plan, invalid_plan_reason = _manager_plan_from_stdout(str(manager_result.get("stdout") or ""))
    if invalid_plan_reason:
        excerpt = str(manager_result.get("stdout") or "").strip()[:1000]
        nonfallbackable_engine_failure = _nonfallbackable_manager_engine_failure(excerpt)
        ctx.manager_plan = {
            "summary": excerpt,
            "subtasks": [],
            "risks": [invalid_plan_reason],
            "tests": [],
        }
        ctx.blackboard["manager_plan"] = ctx.manager_plan
        manager_result["returncode"] = 1
        if nonfallbackable_engine_failure:
            ctx.blackboard["manager_engine_failure"] = {
                "kind": "manager_engine_dispatch_failed",
                "reason": nonfallbackable_engine_failure,
                "stdout_excerpt": excerpt,
            }
            manager_result["failure_reason"] = nonfallbackable_engine_failure
            manager_result["blocker_kind"] = "manager_engine_dispatch_failed"
            manager_result["recovery_action"] = (
                "Restore configured provider connectivity or retry the run after the engine provider is reachable."
            )
            ctx.status = set_status(
                ctx.status,
                ctx.layout,
                phase="planning",
                phase_detail=nonfallbackable_engine_failure,
                run_status="blocked",
                blocker=(True, "manager_engine_dispatch_failed", nonfallbackable_engine_failure, "manager"),
            )
            append_event(
                ctx.layout["events"],
                "manager.engine_dispatch_failed",
                ctx.run_id,
                {
                    "reason": nonfallbackable_engine_failure,
                    "stdout_excerpt": excerpt,
                    "recoverable": False,
                },
                task_id=ctx.task.get("task_id"),
                role="manager",
                repo={"path": ctx.repo.get("path")},
            )
        else:
            ctx.blackboard["manager_invalid_plan"] = {
                "reason": invalid_plan_reason,
                "stdout_excerpt": excerpt,
            }
            manager_result["failure_reason"] = invalid_plan_reason
            manager_result["blocker_kind"] = "manager_invalid_plan"
            manager_result["recovery_action"] = (
                "Use ACA's contract-based fallback planner, then block only if no safe repo targets can be inferred."
            )
            ctx.status = set_status(
                ctx.status,
                ctx.layout,
                phase="planning",
                phase_detail=f"{invalid_plan_reason} Falling back to contract-based planning.",
                blocker=(False, None, None, None),
            )
            append_event(
                ctx.layout["events"],
                "manager.invalid_plan",
                ctx.run_id,
                {"reason": invalid_plan_reason, "stdout_excerpt": excerpt, "recoverable": True},
                task_id=ctx.task.get("task_id"),
                role="manager",
                repo={"path": ctx.repo.get("path")},
            )
    else:
        ctx.manager_plan = parsed_plan or {}
        _sanitize_partial_diff_artifact_paths_in_plan(ctx.manager_plan)
        ctx.blackboard["manager_plan"] = ctx.manager_plan
    if manager_result.get("engine") or manager_result.get("blocker_kind"):
        ctx.blackboard["manager_engine"] = {
            "engine": manager_result.get("engine") or {},
            "failure_reason": manager_result.get("failure_reason") or "",
            "blocker_kind": manager_result.get("blocker_kind") or "",
            "recovery_action": manager_result.get("recovery_action") or "",
        }
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    return manager_result


def pre_screen_subtasks(ctx: RunContext) -> bool:
    """Pre-screen planned subtasks against the repository.

    Separates each subtask into ``pending`` (needs work) or ``skipped_existing``
    (already satisfied by current repo state).

    Mutates:
        ctx.planned_subtasks   -- annotated with pre_satisfied and existing_files
        ctx.pending_subtasks   -- subtasks that need worker execution
        ctx.worker_results     -- extended with skipped-existing results
        ctx.expected_repo_files
        ctx.repo_validation
        ctx.blackboard

    Returns:
        True if all subtasks are pre-satisfied (no workers needed).
        False if there is work to do.
    """
    from src.tandem_agents.core.repository.repo_truth import (
        file_is_readable,
        subtask_satisfied,
    )
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.runtime.runstate import append_event, save_blackboard
    from src.tandem_agents.runtime.run_output import set_status, write_blackboard_snapshot
    from pathlib import Path
    from src.tandem_agents.core.task_contract import task_plan_validation

    repo_path = ctx.repo_path
    discovered_files, subtasks = _prepare_subtasks(ctx)
    _carry_forward_partial_diff_artifacts(ctx, subtasks)
    _constrain_extra_partial_diff_repair_subtasks(ctx, subtasks)
    _append_deferred_repair_subtasks(ctx, subtasks)
    _namespace_repair_retry_subtask_ids(ctx, subtasks)
    _align_python_test_targets_to_repo_conventions(repo_path, subtasks)
    _split_dense_serial_subtasks(ctx, subtasks)

    ctx.planned_subtasks = subtasks
    ctx.blackboard["subtasks"] = ctx.planned_subtasks
    ctx.pending_subtasks = []
    current_expected_files = _rc._collect_expected_repo_files(ctx.planned_subtasks)
    deterministic_testless_repair = _deterministic_testless_repair_active(ctx.planned_subtasks)
    if deterministic_testless_repair:
        ctx.blackboard["expected_repo_files"] = list(current_expected_files)
    else:
        sticky_expected_files = _rc._sticky_expected_repo_files(ctx.blackboard, current_expected_files)
        _merge_or_defer_sticky_expected_files(
            ctx,
            ctx.planned_subtasks,
            current_expected_files,
            sticky_expected_files,
        )
    plan_validation = task_plan_validation(ctx.task, subtasks)
    ctx.blackboard["task_plan_validation"] = plan_validation
    force_worker_execution = (
        _remote_code_task_requires_worker_execution(ctx.task)
        or bool(getattr(ctx, "_manager_fallback_required", False))
        or _rc._task_mentions_external_pr_candidates(ctx.task)
    )
    completed_repair_subtask_ids = _completed_repair_subtask_ids(ctx)
    if not plan_validation.get("ok", True):
        blocker_kind = str(plan_validation.get("blocker_kind") or "contract_incomplete")
        blocker_message = str(plan_validation.get("blocker_message") or "Subtask plan is incomplete or unsafe.")
        ctx.status = set_status(
            ctx.status,
            ctx.layout,
            phase="planning",
            phase_detail=blocker_message,
            run_status="blocked",
            blocker=(True, blocker_kind, blocker_message, "manager"),
        )
        save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
        write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
        logger.warning(
            "Manager plan blocked during pre-screen (run_id=%s): %s",
            ctx.run_id,
            blocker_message,
        )
        return False

    for subtask in ctx.planned_subtasks:
        readable_existing = [
            rel_path
            for rel_path in (subtask.get("files") or [])
            if str(rel_path or "").strip()
            and (repo_path / str(rel_path).strip()).exists()
            and (repo_path / str(rel_path).strip()).is_file()
            and file_is_readable(repo_path / str(rel_path).strip())
        ]
        subtask["existing_files"] = readable_existing
        carried_forward_success = (
            str(subtask.get("id") or "").strip() in completed_repair_subtask_ids
            and subtask_satisfied(repo_path, subtask)
        )
        repair_requires_worker = bool(
            subtask.get("carry_forward_patch")
            or subtask.get("carry_forward_patches")
            or subtask.get("discarded_partial_diff_patch")
        )
        subtask["pre_satisfied"] = (
            False
            if repair_requires_worker
            else (
                True
                if carried_forward_success
                else (False if force_worker_execution else subtask_satisfied(repo_path, subtask))
            )
        )
        subtask["write_required"] = _write_required_after_prescreen(subtask)
        if not subtask["pre_satisfied"] and force_worker_execution and _subtask_has_source_or_test_targets(subtask):
            subtask["write_required"] = True

        if subtask["pre_satisfied"]:
            skip_reason = (
                "carried forward from a completed repair-loop worker"
                if carried_forward_success
                else "already satisfied"
            )
            skipped_result = {
                "worker_id": f"repo-check-{subtask['id']}",
                "subtask_index": 0,
                "subtask_id": subtask["id"],
                "title": subtask["title"],
                "status": "skipped_existing",
                "returncode": 0,
                "worktree": str(repo_path),
                "log_path": "",
                "output_excerpt": (
                    "Subtask skipped because it was "
                    f"{skip_reason} and its target files are readable in the base repository."
                ),
                "write_required": False,
                "verified_existing": True,
            }
            _rc._record_worker_result(ctx.blackboard, ctx.worker_results, skipped_result)
            for item in ctx.blackboard["subtasks"]:
                if item.get("id") == subtask["id"]:
                    item["status"] = "skipped_existing"
                    item["write_required"] = False
                    break
            append_event(
                ctx.layout["events"],
                "worker.skipped",
                ctx.run_id,
                {"subtask_id": subtask["id"], "reason": skip_reason},
                task_id=ctx.task.get("task_id"),
                role="worker",
                repo={"path": ctx.repo.get("path")},
            )
        else:
            ctx.pending_subtasks.append(subtask)

    recorded_subtask_ids = {
        str(result.get("subtask_id") or "").strip()
        for result in ctx.worker_results
        if str(result.get("subtask_id") or "").strip()
    }
    for carried_result in _completed_repair_worker_results(ctx, recorded_subtask_ids):
        _rc._record_worker_result(ctx.blackboard, ctx.worker_results, carried_result)
        append_event(
            ctx.layout["events"],
            "worker.skipped",
            ctx.run_id,
            {
                "subtask_id": carried_result["subtask_id"],
                "reason": "carried forward from a previous repair-loop plan",
            },
            task_id=ctx.task.get("task_id"),
            role="worker",
            repo={"path": ctx.repo.get("path")},
        )

    current_expected_files = _rc._collect_expected_repo_files(ctx.planned_subtasks)
    if deterministic_testless_repair:
        ctx.expected_repo_files = list(current_expected_files)
        ctx.blackboard["expected_repo_files"] = list(ctx.expected_repo_files)
    else:
        ctx.expected_repo_files = _rc._sticky_expected_repo_files(
            ctx.blackboard,
            current_expected_files,
        )
    ctx.repo_validation = _rc._deterministic_repo_validation(repo_path, ctx.expected_repo_files)
    ctx.blackboard["repo_validation"] = ctx.repo_validation

    ctx.status["metrics"]["planned_workers"] = len(ctx.planned_subtasks)
    ctx.status["metrics"].update(_rc._worker_result_metrics(ctx.worker_results))
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)

    all_pre_satisfied = (
        not ctx.pending_subtasks
        and _rc._all_subtasks_verified_existing(ctx.planned_subtasks, ctx.worker_results, ctx.repo_validation, ctx.task)
    )
    if all_pre_satisfied:
        logger.info(
            "All %d subtask(s) pre-satisfied by existing repo files; skipping worker execution.",
            len(ctx.planned_subtasks),
        )
    return all_pre_satisfied
