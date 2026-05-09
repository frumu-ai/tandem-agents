"""Default prompt strings used by the KB MCP server.

Owners can override any entry per-collection (where supported) or globally
through the ``kb_prompts`` table. The control panel surfaces these prompts
in a "Agent prompts" panel; this module is the single source of truth for
the defaults and the override metadata.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptDefinition:
    key: str
    description: str
    default: str
    supports_collection_override: bool


MCP_INITIALIZE_INSTRUCTIONS = (
    "You are a knowledgebase support assistant. Your only job is to answer the user's questions "
    "from the documents in the connected knowledgebase. Treat each user as an end-user client of "
    "the organization that owns this knowledgebase.\n\n"
    "How to answer:\n"
    "- For every user question, call answer_question first.\n"
    "- When evidence is returned, answer ONLY from the text in evidence[].content. Quote or "
    "paraphrase the exact wording. If suggested_answer is non-empty, prefer it. Cite each fact "
    "inline using the matching evidence title in brackets, e.g. [Welcome].\n"
    "- When evidence explicitly states the answer, answer directly. Do not hedge with words such "
    "as 'appears', 'seems', or 'according to available docs'.\n"
    "- Never reply with only document titles, links, or 'here are the docs' summaries — always "
    "write the factual answer.\n\n"
    "When the knowledgebase does not have the answer:\n"
    "- If answer_question returns result 'no_matches' or evidence is empty, reply plainly: "
    "\"I don't have information about that in my knowledgebase.\" Optionally invite the user to "
    "rephrase or ask something else covered by the docs.\n"
    "- If the question is clearly outside the scope of the topics this knowledgebase covers (use "
    "get_kb_guide to confirm scope), say the question is outside the scope of this knowledgebase. "
    "Do NOT attempt to answer from general knowledge.\n"
    "- NEVER invent facts, speculate, or fall back to your training data. If it is not in the "
    "evidence, you do not know it.\n\n"
    "Tools: answer_question is the primary tool. Call get_kb_guide / get_collection_guide to "
    "understand scope. Use search_docs only when you need a list of candidate documents for "
    "navigation. Use get_document for the full source text of a specific doc_id. Use write and "
    "maintenance tools only when Tandem has exposed them for the current channel."
)

ANSWER_QUESTION_TOOL_DESCRIPTION = (
    "Answer a natural-language question from the knowledgebase. Returns ranked evidence and, "
    "when safely extractable, suggested_answer. Call this for EVERY user question. Prefer "
    "suggested_answer when present; otherwise answer only from evidence[].content. If evidence "
    "is empty, tell the user the knowledgebase does not contain the answer — do not invent "
    "facts, do not fall back to general knowledge. Never respond with only document titles."
)

MATCH_GUIDANCE = (
    "Answer the user's question using ONLY the text in evidence[].content. If suggested_answer "
    "is non-empty, prefer it as the grounded answer. Quote or paraphrase the exact wording from "
    "those documents. Cite each fact inline using the matching evidence title in brackets, e.g. "
    "[Welcome]. If evidence explicitly states the answer, answer directly without hedging. If "
    "evidence does not contain a clear answer to the specific question asked, say so plainly "
    "and ask the user to refine the question — do not stretch the evidence to fit. Do NOT "
    "respond with a list of document titles or links — write the factual answer. Do NOT fall "
    "back to general knowledge or training data."
)

NO_MATCH_GUIDANCE = (
    "The knowledgebase does not contain an answer to this question. Reply to the user plainly: "
    "state that you do not have information about this in the knowledgebase, and offer to help "
    "with a different question covered by the docs. If the question is clearly outside the "
    "knowledgebase's topic scope, say so and decline to answer rather than speculating. Do NOT "
    "invent facts. Do NOT fall back to general knowledge or training data. Do NOT speculate."
)

NO_QUERY_GUIDANCE = (
    "No question was provided. Ask the user to restate their question."
)


PROMPT_KEYS: tuple[PromptDefinition, ...] = (
    PromptDefinition(
        key="mcp_initialize_instructions",
        description=(
            "Session-level system prompt sent in the MCP initialize response. Frames the agent's "
            "role and core behavior. Returned once per session before any collection is selected, "
            "so it cannot be customized per collection."
        ),
        default=MCP_INITIALIZE_INSTRUCTIONS,
        supports_collection_override=False,
    ),
    PromptDefinition(
        key="answer_question_tool_description",
        description=(
            "Description shown for the answer_question tool in tools/list. Returned at session "
            "start before any collection is selected, so it cannot be customized per collection."
        ),
        default=ANSWER_QUESTION_TOOL_DESCRIPTION,
        supports_collection_override=False,
    ),
    PromptDefinition(
        key="match_guidance",
        description=(
            "Per-question guidance returned in answer_question when evidence is found. Use this "
            "to set per-collection answer style, persona, citation format, or domain framing."
        ),
        default=MATCH_GUIDANCE,
        supports_collection_override=True,
    ),
    PromptDefinition(
        key="no_match_guidance",
        description=(
            "Per-question guidance returned in answer_question when no evidence is found. Use "
            "this to set per-collection refusal/escalation behavior."
        ),
        default=NO_MATCH_GUIDANCE,
        supports_collection_override=True,
    ),
    PromptDefinition(
        key="no_query_guidance",
        description=(
            "Guidance returned when answer_question is called with an empty question."
        ),
        default=NO_QUERY_GUIDANCE,
        supports_collection_override=True,
    ),
)


PROMPT_KEYS_BY_NAME: dict[str, PromptDefinition] = {p.key: p for p in PROMPT_KEYS}


def get_default(key: str) -> str:
    definition = PROMPT_KEYS_BY_NAME.get(key)
    if not definition:
        raise KeyError(f"Unknown prompt key: {key}")
    return definition.default


def is_known_key(key: str) -> bool:
    return key in PROMPT_KEYS_BY_NAME


def supports_collection_override(key: str) -> bool:
    definition = PROMPT_KEYS_BY_NAME.get(key)
    return bool(definition and definition.supports_collection_override)
