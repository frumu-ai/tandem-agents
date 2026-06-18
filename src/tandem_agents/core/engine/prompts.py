from __future__ import annotations

import json
import math
import re
from typing import Any

from src.tandem_agents.config.config_types import ResolvedConfig

METADATA_ONLY_TARGET_FILENAMES = {
    "cargo.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
}
SOURCE_OR_TEST_TARGET_EXTENSIONS = {
    ".rs",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".sh",
}
SUPPORT_ONLY_TARGET_EXTENSIONS = {".md", ".mdx", ".rst", ".adoc", ".yml", ".yaml", ".toml", ".json"}
WORKER_PARENT_SCOPE_CHAR_LIMIT = 2_500
WORKER_SUBTASK_CONTRACT_CHAR_LIMIT = 2_500
WORKER_SUBTASK_TEXT_CHAR_LIMIT = 1_600
WORKER_JSON_CHAR_LIMIT = 2_000
WORKER_PR_SUMMARY_CHAR_LIMIT = 2_500


def _partial_diff_repair_prompt_mode(previous_feedback: str | None) -> str:
    feedback = str(previous_feedback or "")
    if "Preserved partial patch:" not in feedback and "worker_incomplete_diff" not in feedback:
        return ""
    lowered = feedback.lower()
    rejected_markers = (
        "verification not run",
        "rejected",
        "reset",
        "unverified",
        "self-referential",
        "test-only",
        "helper-only",
        "local oracle",
        "not wired",
        "limited to message formatting",
        "unproductive partial diff",
        "runaway guard",
        "diff exceeded aca runaway",
        "giant patch",
    )
    feedback_rejects_patch = any(marker in lowered for marker in rejected_markers)
    reusable_guidance = (
        "- Treat a preserved patch from `ENGINE_PROMPT_TIMEOUT` or a stalled engine as reusable continuation work unless the feedback explicitly rejects the patch quality.\n"
        "- Do not treat the standard phrase `not treated as a completed worker result` as rejection by itself; it means the patch still needs a terminal worker verdict.\n"
    )
    rejected_guidance = ""
    if feedback_rejects_patch:
        rejected_guidance = (
            "- If the feedback says the partial patch was rejected, reset, unverified, helper-only, self-referential, or not wired into production, use it only as failure evidence; plan a replacement repair against the parent task target files.\n"
            "- If the feedback says the partial patch was rejected, reset, unverified, helper-only, self-referential, or not wired into production, "
            "discard that approach and include the parent task target files needed to satisfy the original acceptance criteria.\n"
        )
    return (
        "PARTIAL-DIFF REPAIR MODE:\n"
        "- Return exactly one subtask unless the feedback says multiple preserved patches exist.\n"
        "- If the feedback indicates the preserved patch is reusable, the subtask must first finish that patch and fix blockers named in the worker output excerpt.\n"
        f"{reusable_guidance}"
        f"{rejected_guidance}"
        "- Keep `files` limited to changed files only when the feedback indicates the preserved patch is reusable, except when the preserved patch changed only tests and the parent task contract names a direct production/source target; in that case include that minimal production target too.\n"
        "- For a reusable test-only timeout patch, require the worker to finish the test patch and implement or verify the paired production behavior before marking the repair complete.\n"
        "- Do not plan unrelated scenario slices or broad follow-up work while repairing the partial-diff blocker.\n"
        "- Put the recovered blocker fixes in canonical `acceptance_criteria`, not only in summary or risks.\n\n"
    )


def _chunk_list(values: list[Any], chunks: int) -> list[list[Any]]:
    if not values:
        return []
    chunks = max(1, chunks)
    size = max(1, math.ceil(len(values) / chunks))
    return [values[i : i + size] for i in range(0, len(values), size)]


def _as_list(value: Any) -> list[Any]:
    if value in (None, "", [], (), {}):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def _is_metadata_only_target_path(path: str) -> bool:
    rel_path = str(path or "").strip().replace("\\", "/").rstrip("/")
    name = rel_path.rsplit("/", 1)[-1].lower()
    return bool(name) and name in METADATA_ONLY_TARGET_FILENAMES


def _is_source_or_test_target_path(path: str) -> bool:
    rel_path = str(path or "").strip().replace("\\", "/").rstrip("/")
    if not rel_path:
        return False
    lowered = rel_path.lower()
    if "/tests/" in f"/{lowered}/" or lowered.startswith("tests/"):
        return True
    if lowered.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")):
        return True
    return any(lowered.endswith(ext) for ext in SOURCE_OR_TEST_TARGET_EXTENSIONS)


def _is_test_target_path(path: str) -> bool:
    rel_path = str(path or "").strip().replace("\\", "/").rstrip("/")
    if not rel_path:
        return False
    lowered = rel_path.lower()
    return (
        lowered.startswith("tests/")
        or "/tests/" in f"/{lowered}"
        or lowered.endswith((
            "_test.rs",
            "_tests.rs",
            "_test.py",
            ".test.ts",
            ".test.tsx",
            ".spec.ts",
            ".spec.tsx",
        ))
    )


def _subtask_mentions_test_work(subtask: dict[str, Any]) -> bool:
    parts: list[Any] = [subtask.get("title"), subtask.get("goal"), subtask.get("scope_note")]
    parts.extend(_as_list(subtask.get("deliverables")))
    parts.extend(_as_list(subtask.get("acceptance_criteria")))
    text = "\n".join(str(part or "") for part in parts).lower()
    return any(word in text for word in ("test", "tests", "coverage", "regression"))


def _is_support_only_target_path(path: str) -> bool:
    rel_path = str(path or "").strip().replace("\\", "/").rstrip("/")
    if not rel_path:
        return False
    lowered = rel_path.lower()
    if _is_metadata_only_target_path(lowered):
        return True
    if lowered.startswith("docs/") or "/docs/" in f"/{lowered}/":
        return True
    return any(lowered.endswith(ext) for ext in SUPPORT_ONLY_TARGET_EXTENSIONS)


def _repair_requires_test_first(subtask: dict[str, Any]) -> bool:
    text = " ".join(
        str(subtask.get(key) or "")
        for key in (
            "repair_worker_output_excerpt",
            "repair_context",
            "failure_reason",
            "blocker_kind",
        )
    ).lower()
    return "worker_off_track_testless_diff" in text or (
        "changed only non-test files" in text and "required test files" in text
    )


def _split_substantive_and_support_targets(target_files: list[str]) -> tuple[list[str], list[str]]:
    source_or_test_targets = [path for path in target_files if _is_source_or_test_target_path(path)]
    if source_or_test_targets:
        support_targets = [path for path in target_files if path not in source_or_test_targets]
        return source_or_test_targets, support_targets
    return (
        [path for path in target_files if not _is_metadata_only_target_path(path)],
        [path for path in target_files if _is_metadata_only_target_path(path)],
    )


def _subtask_contract_for_worker(subtask: dict[str, Any], target_files: list[str]) -> dict[str, Any]:
    """Render worker contract with the active subtask target set, not the parent task target set."""
    if not target_files:
        return subtask
    contract_subtask = dict(subtask)
    contract_subtask["target_files"] = list(target_files)
    nested_contract = dict(contract_subtask.get("task_contract") or {})
    nested_contract["target_files"] = list(target_files)
    contract_subtask["task_contract"] = nested_contract
    return contract_subtask


def _repair_directive_block(subtask: dict[str, Any], target_files: list[str]) -> str:
    if not subtask.get("discarded_partial_diff_patch"):
        return ""
    carries_preserved_patch = bool(subtask.get("carry_forward_patch") or subtask.get("carry_forward_patches"))
    summary = _clip_prompt_text(subtask.get("repair_failure_summary"), 300)
    changed_files = [
        str(entry).strip()
        for entry in _as_list(subtask.get("repair_changed_files"))
        if str(entry).strip()
    ]
    target_line = json.dumps(target_files)
    changed_line = json.dumps(changed_files)
    summary_line = f"\n- Failure summary: {summary}" if summary else ""
    if carries_preserved_patch:
        return (
            "\nCarry-forward repair directive:\n"
            "- ACA already applied the preserved partial patch data into this worker worktree before the prompt started.\n"
            f"- Inspect the current target files and working diff only: {target_line}.\n"
            f"- Previous incomplete diff evidence touched: {changed_line}.\n"
            "- Do not read, apply, or copy patch artifact paths; treat artifacts only as historical failure evidence.\n"
            "- First actions: read the target files and combined diff, then run the narrowest relevant verification or "
            "make only the minimal focused fix needed for that verification.\n"
            "- Valid coverage must call production code or an existing exported behavior; a helper-only or local-oracle "
            "test fails this repair."
            f"{summary_line}\n"
        )
    return (
        "\nRepair directive:\n"
        "- The previous partial diff was rejected; do not apply or copy it as-is.\n"
        f"- Work from the current clean target files: {target_line}.\n"
        f"- Rejected diff touched: {changed_line}.\n"
        "- First actions: read the target files, identify the existing production path, make one focused edit/test, "
        "then run the narrowest relevant verification.\n"
        "- Valid coverage must call production code or an existing exported behavior; a helper-only or local-oracle "
        "test fails this repair."
        f"{summary_line}\n"
    )


def _task_contract_value(task: dict[str, Any], field: str) -> Any:
    contract = dict(task.get("task_contract") or {})
    value = task.get(field)
    if value not in (None, "", [], (), {}):
        return value
    return contract.get(field)


def _task_contract_list(task: dict[str, Any], field: str) -> list[str]:
    value = _task_contract_value(task, field)
    if value in (None, "", [], (), {}):
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(entry).strip() for entry in list(value) if str(entry).strip()]


def _task_contract_block(task: dict[str, Any], *, include_verification: bool = True) -> str:
    lines: list[str] = []
    program_goal = str(_task_contract_value(task, "program_goal") or "").strip()
    local_goal = str(_task_contract_value(task, "local_goal") or "").strip()
    if program_goal:
        lines.append(f"Program goal: {program_goal}")
    if local_goal:
        lines.append(f"Local goal: {local_goal}")
    in_scope = _task_contract_list(task, "in_scope")
    out_of_scope = _task_contract_list(task, "out_of_scope")
    dependencies = _task_contract_list(task, "dependencies")
    deliverables = _task_contract_list(task, "deliverables")
    target_files = _task_contract_list(task, "target_files")
    acceptance_criteria = _task_contract_list(task, "acceptance_criteria")
    notes_for_agent = str(_task_contract_value(task, "notes_for_agent") or "").strip()
    if in_scope:
        lines.append(f"In scope: {json.dumps(in_scope)}")
    if out_of_scope:
        lines.append(f"Out of scope: {json.dumps(out_of_scope)}")
    if dependencies:
        lines.append(f"Dependencies: {json.dumps(dependencies)}")
    if deliverables:
        lines.append(f"Deliverables: {json.dumps(deliverables)}")
    if target_files:
        lines.append(f"Target files: {json.dumps(target_files)}")
    if include_verification:
        verification_commands = _task_contract_list(task, "verification_commands")
        if verification_commands:
            lines.append(f"Verification commands: {json.dumps(verification_commands)}")
    if acceptance_criteria:
        lines.append(f"Acceptance criteria: {json.dumps(acceptance_criteria)}")
    if notes_for_agent:
        lines.append(f"Notes for agent: {notes_for_agent}")
    return "\n".join(lines).strip()


def _task_scope_block(task: dict[str, Any]) -> str:
    lines: list[str] = []
    program_goal = str(_task_contract_value(task, "program_goal") or "").strip()
    local_goal = str(_task_contract_value(task, "local_goal") or "").strip()
    in_scope = _task_contract_list(task, "in_scope")
    out_of_scope = _task_contract_list(task, "out_of_scope")
    target_files = _task_contract_list(task, "target_files")
    deliverables = _task_contract_list(task, "deliverables")
    verification_commands = _task_contract_list(task, "verification_commands")
    if program_goal:
        lines.append(f"Program goal: {program_goal}")
    if local_goal:
        lines.append(f"Local goal: {local_goal}")
    if in_scope:
        lines.append(f"In scope: {json.dumps(in_scope)}")
    if out_of_scope:
        lines.append(f"Out of scope: {json.dumps(out_of_scope)}")
    if target_files:
        lines.append(f"Target files: {json.dumps(target_files)}")
    if deliverables:
        lines.append(f"Deliverables: {json.dumps(deliverables)}")
    if verification_commands:
        lines.append(f"Verification commands: {json.dumps(verification_commands)}")
    return "\n".join(lines).strip()


def _referenced_pr_numbers(task: dict[str, Any], subtask: dict[str, Any] | None = None) -> list[str]:
    text = "\n".join(
        [
            str(task.get("title") or ""),
            str(task.get("description") or task.get("raw_issue_body") or ""),
            "\n".join(str(entry or "") for entry in _as_list(task.get("acceptance_criteria"))),
            str((subtask or {}).get("goal") or ""),
            "\n".join(str(entry or "") for entry in _as_list((subtask or {}).get("acceptance_criteria"))),
        ]
    )
    seen: set[str] = set()
    numbers: list[str] = []
    for match in re.finditer(r"(?:^|[\s(])#(\d+)\b", text):
        number = match.group(1)
        if number in seen:
            continue
        seen.add(number)
        numbers.append(number)
    return numbers


def derive_subtasks(task: dict[str, Any], max_workers: int) -> list[dict[str, Any]]:
    provided = [item for item in _as_list(task.get("subtasks")) if isinstance(item, dict)]
    if provided:
        result = []
        for index, item in enumerate(provided, start=1):
            target_files = [str(entry).strip() for entry in _as_list(item.get("target_files") or item.get("files")) if str(entry).strip()]
            deliverables = [str(entry).strip() for entry in _as_list(item.get("deliverables") or task.get("deliverables")) if str(entry).strip()]
            verification_commands = [
                str(entry).strip()
                for entry in _as_list(item.get("verification_commands") or task.get("verification_commands"))
                if str(entry).strip()
            ]
            result.append(
                {
                    "id": item.get("id") or f"subtask-{index}",
                    "title": item.get("title") or f"Subtask {index}",
                    "goal": item.get("goal") or item.get("description") or item.get("title") or task["title"],
                    "acceptance_criteria": [str(entry).strip() for entry in _as_list(item.get("acceptance_criteria")) if str(entry).strip()],
                    "deliverables": deliverables,
                    "files": target_files,
                    "target_files": target_files,
                    "verification_commands": verification_commands,
                    "dependencies": [str(entry).strip() for entry in _as_list(item.get("dependencies") or task.get("dependencies")) if str(entry).strip()],
                    "program_goal": item.get("program_goal") or task.get("program_goal"),
                    "local_goal": item.get("local_goal") or task.get("local_goal") or item.get("goal") or item.get("description") or item.get("title") or task["title"],
                    "in_scope": [str(entry).strip() for entry in _as_list(item.get("in_scope") or task.get("in_scope")) if str(entry).strip()],
                    "out_of_scope": [str(entry).strip() for entry in _as_list(item.get("out_of_scope") or task.get("out_of_scope")) if str(entry).strip()],
                }
            )
        return result[: max(1, max_workers)]

    target_files = [str(entry).strip() for entry in _as_list(task.get("target_files") or task.get("files")) if str(entry).strip()]
    criteria = [str(entry).strip() for entry in _as_list(task.get("acceptance_criteria")) if str(entry).strip()]
    deliverables = [str(entry).strip() for entry in _as_list(task.get("deliverables")) if str(entry).strip()]
    verification_commands = [str(entry).strip() for entry in _as_list(task.get("verification_commands")) if str(entry).strip()]
    dependencies = [str(entry).strip() for entry in _as_list(task.get("dependencies")) if str(entry).strip()]
    in_scope = [str(entry).strip() for entry in _as_list(task.get("in_scope")) if str(entry).strip()]
    out_of_scope = [str(entry).strip() for entry in _as_list(task.get("out_of_scope")) if str(entry).strip()]
    program_goal = str(task.get("program_goal") or "").strip() or None
    local_goal = str(task.get("local_goal") or task["title"] or "").strip()
    if target_files:
        chunks = _chunk_list(target_files, max_workers)
        criteria_chunks = _chunk_list(criteria, len(chunks)) if criteria else []
        subtasks = []
        for index, chunk in enumerate(chunks, start=1):
            chunk_criteria = criteria_chunks[index - 1] if index - 1 < len(criteria_chunks) else criteria
            goal_bits = [task["title"], f"file slice {index}"]
            if chunk:
                goal_bits.append(", ".join(chunk))
            subtasks.append(
                {
                    "id": f"subtask-{index}",
                    "title": f"{task['title']} - slice {index}",
                    "goal": "; ".join(bit for bit in goal_bits if bit),
                    "acceptance_criteria": chunk_criteria,
                    "deliverables": deliverables,
                    "files": list(chunk),
                    "target_files": list(chunk),
                    "verification_commands": verification_commands,
                    "dependencies": dependencies,
                    "program_goal": program_goal,
                    "local_goal": local_goal,
                    "in_scope": in_scope,
                    "out_of_scope": out_of_scope,
                }
            )
        return subtasks
    if criteria:
        chunks = _chunk_list(criteria, max_workers)
        subtasks = []
        for index, chunk in enumerate(chunks, start=1):
            subtasks.append(
                {
                    "id": f"subtask-{index}",
                    "title": f"{task['title']} - slice {index}",
                    "goal": "; ".join(chunk),
                    "acceptance_criteria": chunk,
                    "deliverables": deliverables,
                    "files": list(target_files),
                    "target_files": list(target_files),
                    "verification_commands": verification_commands,
                    "dependencies": dependencies,
                    "program_goal": program_goal,
                    "local_goal": local_goal,
                    "in_scope": in_scope,
                    "out_of_scope": out_of_scope,
                }
            )
        return subtasks

    return [
        {
            "id": "subtask-1",
            "title": task["title"],
            "goal": task["description"] or task["title"],
            "acceptance_criteria": [],
            "deliverables": deliverables,
            "files": list(target_files),
            "target_files": list(target_files),
            "verification_commands": verification_commands,
            "dependencies": dependencies,
            "program_goal": program_goal,
            "local_goal": local_goal,
            "in_scope": in_scope,
            "out_of_scope": out_of_scope,
        }
    ]


def build_manager_prompt(
    run_id: str,
    task: dict[str, Any],
    repo: dict[str, Any],
    cfg: ResolvedConfig,
    *,
    repo_context: str | None = None,
    previous_feedback: str | None = None,
) -> str:
    from src.tandem_agents.core.engine.engine_runtime import engine_session_provider_model

    contract_block = _task_contract_block(task)
    provider_model = engine_session_provider_model(cfg, "manager")
    prompt = f"You are the ACA manager for run {run_id}.\n"
    if previous_feedback:
        prompt += (
            "CRITICAL: The previous attempt failed to meet the acceptance criteria and was rejected.\n"
            "Review the following feedback and plan subtasks specifically to fix the missing or incorrect functionality.\n\n"
            f"--- PREVIOUS ATTEMPT FEEDBACK ---\n{previous_feedback}\n----------------------------------\n\n"
            f"{_partial_diff_repair_prompt_mode(previous_feedback)}"
        )
    return prompt + (
        "Do not edit files in this planning pass.\n"
        "Return JSON only with keys: summary, subtasks, risks, tests.\n"
        "Each subtask should be independent and suitable for a dedicated worker worktree.\n\n"
        "Each subtask must include title, goal, files, and acceptance_criteria. "
        "Use acceptance_criteria for the concrete worker completion checklist; do not put the only completion criteria in a non-canonical field like scope.\n\n"
        "Keep each subtask narrow: prefer 1-3 high-signal files. For large split test suites or subsystem-wide tasks, "
        "choose the smallest existing test/API surface plus the direct implementation file, and leave other follow-up slices as separate subtasks or risks.\n\n"
        "If several lifecycle behaviors share the same source/test files, split them into multiple sequential subtasks with no more than three concrete acceptance criteria each instead of giving one worker the whole checklist.\n\n"
        "When listing files in subtasks, use repository-relative paths only, such as `package.json` or `src/app.js`.\n"
        "Do not use absolute container paths like `/workspace/...`.\n\n"
        "Do not use git-ignored or private source-note paths such as `docs/internal/...` as worker deliverables. "
        "Those paths may be context only; plan tracked source, tests, or public docs that can produce a reviewable Git diff. "
        "If the task only names ignored/private files and no tracked implementation target is clear, return a blocker risk instead of a docs/internal write plan.\n\n"
        "Plan around the contract below. Respect out-of-scope boundaries, dependency ordering, and target files.\n"
        "If dependencies are unresolved, call that out instead of pretending the work can be completed.\n\n"
        "For smoke, verification, quality-gate, or end-to-end tasks, plan around the existing product implementation "
        "and its existing tests/API surfaces. Do not plan a standalone duplicate implementation of the behavior under "
        "test, and do not replace a live smoke/API path with a local-only mock unless the task explicitly asks for that.\n\n"
        f"{contract_block}\n\n"
        "If the repository already contains relevant files, prefer planning only missing or refinement work.\n"
        "Do not recreate files that already exist and appear readable unless the task clearly requires changing them.\n\n"
        "Repo context may include graph-derived required edit files, likely files, symbols, tests, and uncertainty. "
        "If Required edit files or target_files are present, plan worker deliverables around those paths first. "
        "Treat Suggested first reads, Likely files, Relevant symbols, and Graph evidence as discovery/read-only context "
        "unless a required edit file is missing or proves unrelated after inspection. Exact files named in the task "
        "contract still take precedence, and every planned edit must require the worker to read concrete files before "
        "changing code or making final claims.\n\n"
        f"Task title: {task['title']}\n"
        f"Task description:\n{task.get('description') or ''}\n\n"
        f"Acceptance criteria: {json.dumps(task.get('acceptance_criteria') or [])}\n"
        f"Repository: {repo['path']}\n"
        f"Existing relevant repo files:\n{repo_context or 'No relevant repo files were discovered.'}\n"
        f"Board lane: {task.get('lane') or 'ready'}\n"
        f"Provider/model: {provider_model['provider']} / {provider_model['model']}\n"
    )


def _compact_pr_context(pr_context: Any) -> Any:
    """Drop heavy full ``patch`` bodies from inline PR context.

    The full per-file patches live in the on-disk artifact (which the worker is
    told to read); the inline prompt copy keeps only metadata and short patch
    excerpts so it stays within the prompt budget and shows all PRs rather than
    being truncated inside the first one.
    """
    if not isinstance(pr_context, list):
        return pr_context
    compact: list[Any] = []
    for entry in pr_context:
        if not isinstance(entry, dict):
            compact.append(entry)
            continue
        slim = {key: value for key, value in entry.items() if key != "files"}
        files = entry.get("files")
        if isinstance(files, list):
            slim["files"] = [
                {key: value for key, value in file_entry.items() if key != "patch"}
                if isinstance(file_entry, dict)
                else file_entry
                for file_entry in files
            ]
        compact.append(slim)
    return compact


def _clip_prompt_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 34)].rstrip()}\n[truncated for worker prompt budget]"


def _bounded_prompt_json(value: Any, limit: int) -> str:
    rendered = json.dumps(value, indent=2, sort_keys=True, default=str)
    return _clip_prompt_text(rendered, limit)


def build_worker_prompt(run_id: str, worker_id: str, subtask: dict[str, Any], task: dict[str, Any], worktree: str) -> str:
    deliverables = _bounded_prompt_json(subtask.get("deliverables") or [], WORKER_JSON_CHAR_LIMIT)
    target_files = [
        str(entry).strip()
        for entry in _as_list(subtask.get("files") or subtask.get("target_files") or [])
        if str(entry).strip()
    ]
    files = json.dumps(target_files)
    existing_files = json.dumps(subtask.get("existing_files") or [])
    substantive_target_files, metadata_only_target_files = _split_substantive_and_support_targets(target_files)
    ignored_target_files = [
        str(entry).strip()
        for entry in _as_list(subtask.get("ignored_target_files"))
        if str(entry).strip()
    ]
    required_test_targets = [path for path in substantive_target_files if _is_test_target_path(path)]
    write_required = bool(subtask.get("write_required", True))
    parent_scope = _clip_prompt_text(_task_scope_block(task), WORKER_PARENT_SCOPE_CHAR_LIMIT)
    subtask_contract_payload = _subtask_contract_for_worker(subtask, target_files)
    subtask_contract = _clip_prompt_text(
        _task_contract_block(subtask_contract_payload),
        WORKER_SUBTASK_CONTRACT_CHAR_LIMIT,
    )
    parent_title = _clip_prompt_text(task.get("title"), 500)
    subtask_title = _clip_prompt_text(subtask.get("title"), 500)
    subtask_goal = _clip_prompt_text(subtask.get("goal"), WORKER_SUBTASK_TEXT_CHAR_LIMIT)
    acceptance_criteria = _bounded_prompt_json(subtask.get("acceptance_criteria") or [], WORKER_JSON_CHAR_LIMIT)
    scope_note = _clip_prompt_text(subtask.get("scope_note"), WORKER_SUBTASK_TEXT_CHAR_LIMIT)
    scope_note_block = f"\nACA scope note: {scope_note}\n" if scope_note else ""
    deterministic_fast_path_block = ""
    if "mechanical slice" in scope_note.lower() and substantive_target_files:
        deterministic_fast_path_block = (
            "\nMechanical deterministic slice fast path:\n"
            f"- First read only the smallest relevant part of {json.dumps(substantive_target_files[:1])}.\n"
            "- Then make the first semantic edit in that target before inspecting unrelated files.\n"
            "- Stay inside the listed target files and acceptance criteria; do not explore the parent task surface until after a real diff exists.\n"
            "- Do not stop after imports, constants, or scaffolding. If the slice wires config/env fields, the first diff must also update the read path or config construction that consumes those fields.\n"
            "- Once the diff exists, run one lightweight readback or syntax check and return the completion note.\n"
        )
    repair_directive_block = _repair_directive_block(subtask, target_files)
    tracked_target_guidance = ""
    write_required_guidance = ""
    if write_required:
        if substantive_target_files:
            support_line = (
                f" Support targets such as {json.dumps(metadata_only_target_files)} may be updated only after "
                "a substantive target has a real diff."
                if metadata_only_target_files
                else ""
            )
            tracked_target_guidance = (
                "\nRequired substantive write targets for this worker: "
                f"{json.dumps(substantive_target_files)}. Briefly read one declared target before editing it, then make the first "
                "substantive edit in one of these files unless a nearby tracked "
                f"source or test file is clearly safer for the same acceptance criterion.{support_line} "
                "A package.json-only or lockfile-only diff fails this worker.\n"
            )
        elif target_files:
            tracked_target_guidance = (
                "\nPreferred tracked write targets for this worker: "
                f"{json.dumps(target_files)}. Briefly read one declared target before editing it, then make the first "
                "substantive edit in one of these files unless a nearby tracked source or test file is clearly safer "
                "for the same acceptance criterion.\n"
            )
        write_required_guidance = (
            "\nThis worker is write-required. Inspect the smallest relevant slice of a declared target file first, "
            "then make a real semantic write/edit against a declared tracked target file or an existing nearby tracked source/test file that directly "
            "satisfies the subtask. Do not create marker files, status files, temporary files, scratch notes, "
            "or placeholder files to prove that writing works; those do not count as work and will fail review. "
            "Do not use no-op patches, comment-only changes, formatting-only churn, or add-then-remove edits to satisfy write-required mode. "
            "If you discover missing coverage or missing behavior, implement the smallest focused improvement now. "
            "Do not stop with an analysis-only blocker unless editing the tracked target files would be unsafe, "
            "and name that concrete safety reason.\n"
        )
        production_followup_targets = [
            str(path).strip()
            for path in _as_list(subtask.get("repair_requires_production_followup"))
            if str(path).strip()
        ]
        if production_followup_targets:
            write_required_guidance += (
                "\nThis repair carries a preserved test-only partial diff. Read the carried test patch for context, "
                "then make the first new semantic edit in the paired production target before adding or changing more tests: "
                f"{json.dumps(production_followup_targets)}. A test-only diff fails this repair unless you report a concrete blocker "
                "explaining why no production edit is safe.\n"
            )
        elif required_test_targets and _repair_requires_test_first(subtask):
            write_required_guidance += (
                "\nThis worker must satisfy required test coverage before production-only continuation. Read and edit at least one required test target first: "
                f"{json.dumps(required_test_targets)}. After adding or tightening the real assertion, make only the "
                "minimal production change needed for that assertion. A production-only diff fails this worker.\n"
            )
        elif required_test_targets and _subtask_mentions_test_work(subtask):
            paired_production_targets = [
                path
                for path in substantive_target_files
                if not _is_test_target_path(path)
            ]
            if paired_production_targets:
                write_required_guidance += (
                    "\nThis worker must keep test coverage paired with production behavior. Read at least one required test target early: "
                    f"{json.dumps(required_test_targets)}, then read and make the first behavioral edit in a paired production target before "
                    f"expanding tests: {json.dumps(paired_production_targets)}. Do not spend the attempt building a test-only diff; "
                    "a test-only diff fails unless you report a concrete blocker explaining why no production edit is safe.\n"
                )
            else:
                write_required_guidance += (
                    "\nThis worker must satisfy required test coverage before production-only continuation. Read and edit at least one required test target first: "
                    f"{json.dumps(required_test_targets)}. After adding or tightening the real assertion, make only the "
                    "minimal production change needed for that assertion. A production-only diff fails this worker.\n"
                )
    no_target_guidance = ""
    if not target_files:
        pr_numbers = _referenced_pr_numbers(task, subtask)
        pr_line = f" Referenced PR candidates: {', '.join('#' + number for number in pr_numbers)}." if pr_numbers else ""
        no_target_guidance = (
            "\nNo target files were declared for this task, so you must discover them from the task context and repository state."
            f"{pr_line}\n"
            "If the task references PRs or branches, inspect them (see the fetched local refs below if present), compare them to latest main, "
            "apply the still-relevant changes into this worktree so a real diff is produced, and leave a clear blocker only if no safe repository diff can be produced.\n"
        )
    ignored_target_guidance = ""
    if ignored_target_files:
        ignored_target_guidance = (
            "\nGit-ignored target files were present in the task metadata: "
            f"{json.dumps(ignored_target_files)}. Do not use those paths as deliverables because Git will ignore them "
            "and ACA cannot create a reviewable diff from them. If tracked target files remain, edit those tracked targets first. "
            "Otherwise choose tracked source, test, or public docs files that satisfy the task, or return a concrete blocker "
            "explaining that the task only names ignored/private files.\n"
        )
    verification_path_guidance = (
        "\nFor smoke, verification, quality-gate, or end-to-end tasks, exercise the existing production path, "
        "server path, control-panel path, or deterministic repository fixture path that the product already uses. "
        "Do not satisfy those tasks by inventing a standalone simulation unless the task explicitly asks for one. "
        "Do not define the quality-gate rules inside the test or smoke script and then assert those same local rules; "
        "drive existing product code, existing server/API behavior, or an existing exported implementation instead. "
        "For regression coverage, each new assertion must exercise existing production functions, structs, API handlers, "
        "fixtures, or exported behavior. A test-only enum, constant, local helper, or string table that merely restates "
        "the expected behavior is not valid coverage, even if it uses realistic names. If the behavior is private, place "
        "the test next to the implementation or make the smallest production helper change needed for the test to call "
        "real code. "
        "Preserve existing live smoke/API behavior such as dry-run modes and endpoint calls unless the task explicitly "
        "requires replacing it. "
        "If a script must export helpers for tests, make sure importing it does not execute its CLI main routine, "
        "perform network calls, append runtime output, or mutate stable fixtures. Runtime smoke output should be "
        "temporary or cleaned up, while tracked fixtures should stay deterministic.\n"
    )
    pr_context_guidance = ""
    pr_context = subtask.get("pr_candidate_context")
    pr_context_artifact = str(subtask.get("pr_candidate_context_artifact") or "").strip()
    pr_refs = [
        ref
        for ref in (subtask.get("pr_candidate_refs") or [])
        if isinstance(ref, dict) and ref.get("ok") and ref.get("ref")
    ]
    if pr_context:
        ref_block = ""
        if pr_refs:
            ref_lines = "\n".join(f"- PR #{ref.get('number')}: `{ref.get('ref')}`" for ref in pr_refs)
            ref_block = (
                "\nACA fetched these candidate PR heads into THIS repository as local git refs, so the real commits are available here:\n"
                f"{ref_lines}\n"
                "Apply them with real git in the worktree: review each with `git show <ref>` or `git diff main...<ref>`, then bring the worthwhile, "
                "still-relevant changes into the working tree (e.g. `git cherry-pick -n <ref>`, `git checkout <ref> -- <path>`, or manual edits). "
                "Resolve conflicts, drop anything already on main or no longer relevant, and leave the working tree with a real, reviewable diff.\n"
            )
        pr_context_guidance = (
            "\nACA already fetched GitHub PR candidate context for this task. "
            f"Full per-file patches are in the artifact `{pr_context_artifact or 'pr_candidate_context.json'}` -- read it for complete diffs.\n"
            f"{ref_block}"
            "This is an edit task, not a report-only task. Do not stop after producing an applicability matrix. "
            "A successful worker turn must either leave a filesystem diff or return a structured blocker that names every inspected PR and explains why no safe code change should be applied.\n"
            "Use this context first, then verify against the repository before editing. "
            "If after applying you genuinely have no safe changes, return a structured blocker that lists the inspected PR numbers.\n"
            f"PR candidate summary:\n{_bounded_prompt_json(_compact_pr_context(pr_context), WORKER_PR_SUMMARY_CHAR_LIMIT)}\n"
        )
    return (
        f"You are ACA worker {worker_id} in run {run_id}.\n"
        "Your isolated worktree is mounted as the current directory.\n"
        "This worktree is owned by this worker/subtask pair only.\n"
        "Only edit files in this worktree.\n"
        "CRITICAL: You MUST use ONLY relative paths (e.g., `package.json` or `src/app.js`) for ALL tool calls.\n"
        "The engine will fail with 'OS_MISMATCH' or 'No such file or directory' if you use absolute paths like `/workspace/...`.\n"
        "You must use tools to inspect the worktree, create or edit the required files, and verify the result.\n"
        "Do not merely describe intended changes. If you did not actually change files, report a blocker instead.\n"
        "Before finishing, verify the changed files with read/glob/grep or bash commands in the worktree.\n"
        "Once a substantive diff exists, stop expanding scope; run one lightweight verification or file readback, retry a narrower readback if a tool is skipped, then return the final completion note.\n"
        "For Python sibling test files under `src/`, prefer `python3 -m unittest <module.path>`; use `python3 -m py_compile <changed files>` as a fallback if dependencies needed by the test command are unavailable.\n"
        "Do not treat missing `pytest` as a blocker when an equivalent `python3 -m unittest ...` command can exercise the changed Python test module.\n"
        "When a subtask needs coverage for private helpers, add real tests inside the source module that defines those helpers; do not add placeholder integration or contract test files.\n"
        "When adding tests, prefer additive test modules or additive cases; do not rewrite existing tests unless the task explicitly requires changing them.\n"
        "If browser tools are available, use them to verify your changes.\n"
        "IMPORTANT: Save any browser screenshots to the `./screenshots/` directory so they can be displayed in the Control Panel.\n"
        "Your final response must describe the real files you changed and the verification you actually performed.\n"
        "Return a concise completion note with changed files, validation performed, and any blockers.\n\n"
        "If the target files already exist and satisfy the subtask, you may finish without editing them, but only after proving that with real tool calls.\n"
        "If you do not need to change a file, say that it was already satisfied and describe the verification you performed.\n\n"
        f"Parent task: {parent_title}\n"
        f"Parent task scope:\n{parent_scope}\n\n"
        f"Subtask title: {subtask_title}\n"
        f"Subtask goal: {subtask_goal}\n"
        f"{scope_note_block}"
        f"{deterministic_fast_path_block}"
        f"{repair_directive_block}"
        f"Subtask contract:\n{subtask_contract}\n\n"
        f"Acceptance criteria: {acceptance_criteria}\n"
        f"Expected deliverables: {deliverables}\n"
        f"Target files: {files}\n"
        f"Existing readable target files in the base repo before this worker: {existing_files}\n"
        f"Write required for this worker: {json.dumps(write_required)}\n"
        f"{tracked_target_guidance}"
        f"{write_required_guidance}"
        f"{verification_path_guidance}"
        f"{no_target_guidance}"
        f"{ignored_target_guidance}"
        f"{pr_context_guidance}"
    )


def build_integration_prompt(run_id: str, task: dict[str, Any], worker_notes: list[dict[str, Any]]) -> str:
    contract_block = _task_contract_block(task)
    return (
        f"You are ACA manager integrating worker output for run {run_id}.\n"
        "Review the worker outputs and the current repository state and reconcile the changes in the base repository.\n"
        "You may edit files in this repository now.\n"
        "Worker changes have already been synchronized into the base repository before this step.\n"
        "CRITICAL: You MUST use ONLY relative paths (e.g., `package.json` or `src/app.js`) for ALL tool calls.\n"
        "The engine will fail with 'OS_MISMATCH' or 'No such file or directory' if you use absolute paths like `/workspace/...`.\n"
        "If the repository state still looks incomplete, report that as a blocker instead of pretending integration succeeded.\n"
        "Reject any out-of-scope edits or edits outside the declared target files.\n"
        "Your summary is advisory only; do not invent missing-file claims if the repository already contains the files.\n"
        "For Python sibling test files under `src/`, prefer `python3 -m unittest <module.path>`; do not use bare `python` unless a task-provided command requires it.\n"
        "Return a short JSON object with summary, risks, and tests.\n\n"
        f"Task title: {task['title']}\n"
        f"Task contract:\n{contract_block}\n\n"
        f"Worker notes: {json.dumps(worker_notes, indent=2)}\n"
    )


def _diff_block(repo_diff: str | None) -> str:
    """Render an uncommitted-changes diff block for review/test prompts."""
    diff_text = (repo_diff or "").strip()
    if not diff_text:
        return (
            "Uncommitted changes (git diff): none detected. "
            "Verify directly against the repository before judging the work.\n"
        )
    return (
        "Uncommitted changes produced by the workers (git diff against HEAD; "
        "new files shown inline). Base your judgement on these actual changes, "
        "not only the worker notes:\n"
        "```diff\n"
        f"{diff_text}\n"
        "```\n"
    )


def build_review_prompt(
    run_id: str,
    task: dict[str, Any],
    worker_notes: list[dict[str, Any]],
    repo_diff: str | None = None,
) -> str:
    contract_block = _task_contract_block(task)
    return (
        f"You are ACA reviewer for run {run_id}.\n"
        "Review the current repository state and the worker outputs.\n"
        "Treat existing readable files in the repository as the source of truth, even if a worker had a noisy tool error.\n"
        "This review happens before final handoff/publish. Do not require a PR branch, PR URL, merge, or branch deletion in this phase; those are finalized later if verification passes.\n"
        "If the task asks for lint/typecheck but the touched package exposes no matching script, accept the available deterministic build/test commands as verification and note the missing script instead of requiring an impossible command.\n"
        "For PR-candidate consolidation tasks, worker applicability notes that name skipped candidates and reasons count as handoff documentation; require extra documentation only if those reasons are missing or unsafe.\n"
        "Review only against the explicit task contract, enumerated acceptance criteria, and declared target files. "
        "Do not expand scope from the title, broad summary wording, or adjacent product ideas. "
        "If broad wording conflicts with a narrower numbered checklist, treat the numbered checklist as controlling. "
        "Set `repair_needed` only for a concrete unmet criterion, regression, unsafe change, or broken verification visible in the actual diff.\n"
        "Return JSON only with keys: next_action, findings, required_fixes, notes.\n"
        "Set next_action to one of `pass`, `repair_needed`, `blocked`, or `human_review_needed`.\n"
        "CRITICAL: You MUST use ONLY relative paths (e.g., `package.json` or `src/app.js`) for ALL tool calls.\n"
        "The engine will fail with 'OS_MISMATCH' or 'No such file or directory' if you use absolute paths like `/workspace/...`.\n"
        "If the review cannot be completed confidently, use `human_review_needed`.\n\n"
        f"Task title: {task['title']}\n"
        f"Task contract:\n{contract_block}\n\n"
        f"{_diff_block(repo_diff)}\n"
        f"Worker notes: {json.dumps(worker_notes, indent=2)}\n"
    )


def build_test_prompt(
    run_id: str,
    task: dict[str, Any],
    repo: dict[str, Any],
    worker_notes: list[dict[str, Any]],
    repo_diff: str | None = None,
    verification_commands: list[str] | None = None,
) -> str:
    contract_block = _task_contract_block(task)
    command_block = ""
    commands = [str(command).strip() for command in (verification_commands or []) if str(command).strip()]
    if commands:
        command_block = (
            "ACA inferred these verification commands from the changed files and task contract. "
            "Run them if the environment allows it, and include exact pass/fail results in JSON:\n"
            f"{json.dumps(commands)}\n\n"
        )
    return (
        f"You are ACA tester for run {run_id}.\n"
        "Run the most relevant validation commands for this repository and task.\n"
        "Prefer the listed verification commands and include them in your answer if they were available.\n"
        "Base your verdict on the actual repository state. Do not fail the run just because a worker had a noisy tool error if the target files exist and are readable.\n"
        "For Python sibling test files under `src/`, prefer `python3 -m unittest <module.path>`; do not use bare `python` unless a task-provided command requires it.\n"
        "Return JSON only with keys: next_action, commands, results, notes.\n"
        "Set next_action to one of `pass`, `repair_needed`, `blocked`, or `human_review_needed`.\n"
        "CRITICAL: You MUST use ONLY relative paths (e.g., `package.json` or `src/app.js`) for ALL tool calls.\n"
        "The engine will fail with 'OS_MISMATCH' or 'No such file or directory' if you use absolute paths like `/workspace/...`.\n"
        "If validation is inconclusive because the environment or command setup is broken, use `blocked`.\n\n"
        "Repository: (mounted as current directory)\n"
        f"Task title: {task['title']}\n"
        f"Task contract:\n{contract_block}\n\n"
        f"{command_block}"
        f"{_diff_block(repo_diff)}\n"
        f"Worker notes: {json.dumps(worker_notes, indent=2)}\n"
    )


def build_qa_prompt(run_id: str, task: dict[str, Any], pr_info: dict[str, Any], diff: str) -> str:
    contract_block = _task_contract_block(task)
    return (
        f"You are the ACA QA Agent for run {run_id}.\n"
        "Your goal is to audit a Pull Request and find potential bugs, security issues, or regressions.\n"
        "You must compare the proposed code changes with the original task description and acceptance criteria.\n\n"
        "If browser tools are available (e.g., `browser_open`), use them to verify UI/UX changes or E2E flows.\n"
        "IMPORTANT: Save any browser screenshots to the `./screenshots/` directory (e.g., `./screenshots/homepage.png`) so they can be displayed in the Control Panel.\n"
        "CRITICAL: You MUST use ONLY relative paths for all tool calls.\n\n"
        f"Task: {task['title']}\n"
        f"Task Description:\n{task.get('description', '')}\n"
        f"Task Contract:\n{contract_block}\n\n"
        f"PR Title: {pr_info.get('title', '')}\n"
        f"PR Body:\n{pr_info.get('body', '')}\n\n"
        "Code Changes (git diff):\n"
        "```diff\n"
        f"{diff[:5000]}\n"
        "```\n\n"
        "Return your audit report in JSON only with keys: findings (list of bugs/issues), verdict (pass/fail), and feedback."
    )
