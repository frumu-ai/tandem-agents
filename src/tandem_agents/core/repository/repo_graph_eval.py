from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.tandem_agents.core.repository.repo_context import repo_context_hints_for_task


@dataclass(frozen=True)
class RepoGraphEvalCase:
    name: str
    task: dict[str, Any]
    expected_path_scope: str
    expected_terms: tuple[str, ...]


EVAL_CASES: tuple[RepoGraphEvalCase, ...] = (
    RepoGraphEvalCase(
        name="meta_harness_eval",
        task={
            "task_id": "TAN-106",
            "identifier": "TAN-106",
            "title": "MH-04 Add prompt-injection exfiltration and blast-radius eval suite",
            "description": (
                "Add fixture coverage and scoring for Meta-Harness work in "
                "crates/tandem-meta-harness-eval without drifting into desktop memory context files."
            ),
            "labels": ["Meta Harness", "Eval Coverage"],
        },
        expected_path_scope="crates/tandem-meta-harness-eval",
        expected_terms=("TAN-106", "Meta Harness", "crates/tandem-meta-harness-eval"),
    ),
    RepoGraphEvalCase(
        name="control_panel",
        task={
            "title": "Control Panel: Show ACA retry and graph usage in run detail",
            "description": "Update the Tandem control panel timeline.",
            "task_contract": {"target_files": ["packages/tandem-control-panel/src/App.tsx"]},
            "labels": ["Coder Runtime", "observability"],
        },
        expected_path_scope="packages/tandem-control-panel",
        expected_terms=("packages/tandem-control-panel/src/App.tsx", "Control Panel"),
    ),
    RepoGraphEvalCase(
        name="runtime_security",
        task={
            "title": "Runtime security: tighten token handling around ACA API auth",
            "description": "Audit auth helpers and avoid leaking tokens in runtime events.",
            "task_contract": {"target_files": ["crates/tandem-server/src/security/auth.rs"]},
            "labels": ["security", "runtime"],
        },
        expected_path_scope="crates/tandem-server",
        expected_terms=("crates/tandem-server/src/security/auth.rs", "security"),
    ),
    RepoGraphEvalCase(
        name="docs_only",
        task={
            "title": "Docs: update engine management runbook",
            "description": "Clarify restart and health-check commands.",
            "task_contract": {"target_files": ["docs/ENGINE_MANAGEMENT.md"]},
            "labels": ["documentation"],
        },
        expected_path_scope="docs",
        expected_terms=("docs/ENGINE_MANAGEMENT.md", "engine management"),
    ),
    RepoGraphEvalCase(
        name="github_projects_coder_intake",
        task={
            "task_id": "TAN-57",
            "title": "CRI-02 Add GitHub Projects schema drift and divergence regression coverage",
            "description": (
                "Harden GitHub Projects intake against schema drift and remote state changes. "
                "Regression output identifies degraded read/write readiness clearly."
            ),
            "labels": ["Coder Runtime", "Improvement"],
        },
        expected_path_scope="crates/tandem-server/src/http",
        expected_terms=("CoderGithubProjectBinding", "schema_drift", "crates/tandem-server/src/http/coder_parts/part09.rs"),
    ),
)


def run_repo_graph_eval() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    passed = 0
    for case in EVAL_CASES:
        hints = repo_context_hints_for_task(case.task)
        task_query = str(hints.get("task") or "")
        path_scope = str(hints.get("path_scope") or "")
        missing_terms = [term for term in case.expected_terms if term.lower() not in task_query.lower()]
        ok = path_scope == case.expected_path_scope and not missing_terms
        if ok:
            passed += 1
        cases.append(
            {
                "name": case.name,
                "passed": ok,
                "expected_path_scope": case.expected_path_scope,
                "path_scope": path_scope,
                "missing_terms": missing_terms,
                "required_files": hints.get("required_files") or [],
            }
        )
    total = len(cases)
    return {
        "passed": passed == total,
        "score": passed,
        "total": total,
        "cases": cases,
    }
