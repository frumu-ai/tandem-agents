from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from src.tandem_agents.utils.utils import atomic_write_yaml, load_yaml, now_ms, short_id, slugify

BOARD_COLUMNS = ["backlog", "ready", "in_progress", "review", "test", "blocked", "done"]


def default_board(name: str = "ACA Kanban", board_id: str = "aca-main") -> dict[str, Any]:
    return {
        "board": {
            "id": board_id,
            "name": name,
            "columns": list(BOARD_COLUMNS),
            "created_at_ms": now_ms(),
            "updated_at_ms": now_ms(),
        },
        "cards": [],
    }


def ensure_board_template(path: Path, name: str = "ACA Kanban") -> dict[str, Any]:
    board = load_board(path)
    if board.get("cards") is None:
        board["cards"] = []
    if not board.get("board"):
        board["board"] = {}
    board["board"].setdefault("id", "aca-main")
    board["board"].setdefault("name", name)
    board["board"].setdefault("columns", list(BOARD_COLUMNS))
    board["board"].setdefault("created_at_ms", now_ms())
    board["board"]["updated_at_ms"] = now_ms()
    save_board(path, board)
    return board


def load_board(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_board()
    board = load_yaml(path)
    if "board" not in board:
        board = {
            "board": {
                "id": "aca-main",
                "name": "ACA Kanban",
                "columns": list(BOARD_COLUMNS),
                "created_at_ms": now_ms(),
                "updated_at_ms": now_ms(),
            },
            "cards": board if isinstance(board, list) else [],
        }
    board.setdefault("cards", [])
    board.setdefault("board", {})
    board["board"].setdefault("columns", list(BOARD_COLUMNS))
    board["board"].setdefault("created_at_ms", now_ms())
    board["board"].setdefault("updated_at_ms", now_ms())
    return board


def save_board(path: Path, board: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = deepcopy(board)
    payload.setdefault("board", {})
    payload["board"]["updated_at_ms"] = now_ms()
    atomic_write_yaml(path, payload)


def board_lanes(board: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    lanes = {lane: [] for lane in board.get("board", {}).get("columns", BOARD_COLUMNS)}
    for card in board.get("cards", []):
        lane = card.get("lane") or "backlog"
        lanes.setdefault(lane, []).append(card)
    return lanes


def board_summary(board: dict[str, Any]) -> dict[str, int]:
    lanes = board_lanes(board)
    return {lane: len(cards) for lane, cards in lanes.items()}


def board_markdown(board: dict[str, Any]) -> str:
    meta = board.get("board", {})
    lines = [f"# {meta.get('name', 'ACA Kanban')}", ""]
    lines.append(f"- Board ID: `{meta.get('id', 'aca-main')}`")
    lines.append(f"- Updated: `{meta.get('updated_at_ms', now_ms())}`")
    lines.append("")
    lanes = board_lanes(board)
    for lane in board.get("board", {}).get("columns", BOARD_COLUMNS):
        cards = lanes.get(lane, [])
        lines.append(f"## {lane} ({len(cards)})")
        if not cards:
            lines.append("- _empty_")
        else:
            for card in cards:
                lines.append(f"- `{card.get('id', '')}` {card.get('title', '')}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def task_to_card(task: dict[str, Any], lane: str = "ready") -> dict[str, Any]:
    card_id = task.get("card_id") or task.get("task_id") or f"{slugify(task.get('title', 'task'))}-{short_id()}"
    return {
        "id": card_id,
        "title": task.get("title", "Untitled task"),
        "description": task.get("description", ""),
        "project_name": task.get("project_name"),
        "project_column": task.get("project_column"),
        "project_schema": deepcopy(task.get("project_schema") or {}),
        "lane": lane,
        "priority": task.get("priority"),
        "labels": list(task.get("labels") or []),
        "acceptance_criteria": list(task.get("acceptance_criteria") or []),
        "program_goal": task.get("program_goal"),
        "local_goal": task.get("local_goal"),
        "in_scope": list(task.get("in_scope") or []),
        "out_of_scope": list(task.get("out_of_scope") or []),
        "dependencies": list(task.get("dependencies") or []),
        "deliverables": list(task.get("deliverables") or []),
        "target_files": list(task.get("target_files") or []),
        "verification_commands": list(task.get("verification_commands") or []),
        "notes_for_agent": task.get("notes_for_agent"),
        "raw_issue_body": task.get("raw_issue_body"),
        "task_contract": deepcopy(task.get("task_contract") or {}),
        "subtasks": list(task.get("subtasks") or []),
        "source": deepcopy(task.get("source") or {}),
        "repo": deepcopy(task.get("repo") or {}),
        "created_at_ms": now_ms(),
        "updated_at_ms": now_ms(),
        "history": [
            {
                "lane": lane,
                "at_ms": now_ms(),
                "by": "aca",
                "note": "created",
            }
        ],
    }


def card_to_task(card: dict[str, Any], board_path: Path | None = None) -> dict[str, Any]:
    task = {
        "task_id": card.get("id"),
        "title": card.get("title", "Untitled task"),
        "description": card.get("description", ""),
        "project_name": card.get("project_name"),
        "project_column": card.get("project_column"),
        "project_schema": deepcopy(card.get("project_schema") or {}),
        "priority": card.get("priority"),
        "labels": list(card.get("labels") or []),
        "acceptance_criteria": list(card.get("acceptance_criteria") or []),
        "program_goal": card.get("program_goal"),
        "local_goal": card.get("local_goal"),
        "in_scope": list(card.get("in_scope") or []),
        "out_of_scope": list(card.get("out_of_scope") or []),
        "dependencies": list(card.get("dependencies") or []),
        "deliverables": list(card.get("deliverables") or []),
        "target_files": list(card.get("target_files") or []),
        "verification_commands": list(card.get("verification_commands") or []),
        "notes_for_agent": card.get("notes_for_agent"),
        "raw_issue_body": card.get("raw_issue_body"),
        "task_contract": deepcopy(card.get("task_contract") or {}),
        "subtasks": list(card.get("subtasks") or []),
        "source": deepcopy(card.get("source") or {}),
        "repo": deepcopy(card.get("repo") or {}),
        "lane": card.get("lane", "ready"),
    }
    task["source"].setdefault("card_id", card.get("id"))
    if board_path is not None:
        task["source"].setdefault("board_path", str(board_path))
        task["source"].setdefault("type", "kanban_board")
    return task


def find_card(board: dict[str, Any], card_id: str) -> dict[str, Any] | None:
    for card in board.get("cards", []):
        if str(card.get("id")) == str(card_id):
            return card
    return None


def select_card(
    board: dict[str, Any],
    card_id: str | None = None,
    preferred_lanes: Iterable[str] = ("ready", "backlog"),
) -> dict[str, Any] | None:
    if card_id:
        return find_card(board, card_id)
    cards = board.get("cards", [])
    lane_order = list(preferred_lanes)
    for lane in lane_order:
        for card in cards:
            if card.get("lane") == lane:
                return card
    return cards[0] if cards else None


def claim_card(board: dict[str, Any], card_id: str, run_id: str, actor: str = "manager") -> dict[str, Any]:
    card = find_card(board, card_id)
    if card is None:
        raise ValueError(f"Card not found: {card_id}")
    card["lane"] = "in_progress"
    card["updated_at_ms"] = now_ms()
    card["assigned_run_id"] = run_id
    card["claimed_by"] = actor
    card.setdefault("history", []).append(
        {
            "lane": "in_progress",
            "at_ms": now_ms(),
            "by": actor,
            "note": f"claimed by {run_id}",
        }
    )
    return card


def move_card(board: dict[str, Any], card_id: str, lane: str, actor: str, note: str) -> dict[str, Any]:
    card = find_card(board, card_id)
    if card is None:
        raise ValueError(f"Card not found: {card_id}")
    card["lane"] = lane
    card["updated_at_ms"] = now_ms()
    card.setdefault("history", []).append(
        {"lane": lane, "at_ms": now_ms(), "by": actor, "note": note}
    )
    return card


def board_snapshot(board: dict[str, Any], path: Path) -> None:
    save_board(path, board)
