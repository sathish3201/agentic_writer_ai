
from __future__ import annotations

import json
import operator
import os
import re
from pathlib import Path
from typing import Annotated, List, Literal, Optional, TypedDict

import psycopg
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_groq import ChatGroq
from langchain_tavily import TavilySearch

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from psycopg.rows import dict_row

import threading
import time
from groq import RateLimitError

# ============================================================
# 0. Environment
# ============================================================

load_dotenv()


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise ValueError(
            "DATABASE_URL is missing. Add your PostgreSQL URL to .env"
        )

    if "sslmode=" not in database_url:
        separator = "&" if "?" in database_url else "?"
        database_url = f"{database_url}{separator}sslmode=require"

    return database_url


# ============================================================
# 1. Pydantic schemas
# ============================================================

class Task(BaseModel):
    id: int
    title: str

    goal: str = Field(
        ...,
        description=(
            "One sentence describing what the reader should "
            "understand after this section."
        ),
    )

    bullets: List[str] = Field(
        ...,
        min_length=3,
        max_length=6,
        description=(
            "3-6 concrete, specific, non-overlapping subpoints."
        ),
    )

    target_words: int = Field(
        ...,
        ge=120,
        le=550,
        description="Target word count for this section.",
    )


    tags: List[str] = Field(default_factory=list)
    requires_research: bool = False
    requires_citations: bool = False
    requires_code: bool = False


class Plan(BaseModel):
    blog_title: str
    audience: str
    tone: str

    blog_kind: Literal[
        "explainer",
        "tutorial",
        "news_roundup",
        "comparison",
        "system_design",
    ] = "explainer"

    constraints: List[str] = Field(default_factory=list)
    tasks: List[Task]


class EvidenceItem(BaseModel):
    title: str
    url: str
    published_at: Optional[str] = None
    snippet: Optional[str] = None
    source: Optional[str] = None


class RouterDecision(BaseModel):
    needs_research: bool
    mode: Literal["closed_book", "hybrid", "open_book"]
    queries: List[str] = Field(default_factory=list)


class ImageSpec(BaseModel):
    # Heading after which the image should be inserted.
    after_heading: str

    placeholder: str = Field(
        ...,
        description="Placeholder such as [[IMAGE_1]].",
    )

    filename: str = Field(
        ...,
        description="Filename such as architecture_flow.png.",
    )

    alt: str
    caption: str

    prompt: str = Field(
        ...,
        description="Detailed prompt for the image generation model.",
    )

    size: Literal[
        "1024x1024",
        "1024x1536",
        "1536x1024",
    ] = "1024x1024"

    quality: Literal["low", "medium", "high"] = "medium"


class GlobalImagePlan(BaseModel):
    images: List[ImageSpec] = Field(default_factory=list)


# ============================================================
# 2. LangGraph state
# ============================================================

class State(TypedDict):
    topic: str

    # Router / research
    mode: str
    needs_research: bool
    queries: List[str]
    evidence: List[EvidenceItem]
    plan: Optional[Plan]

    # Parallel workers
    sections: Annotated[
        List[tuple[int, str]],
        operator.add,
    ]

    # Reducer / image generation
    merged_md: str
    md_with_placeholders: str
    image_specs: List[dict]

    # Final article
    final: str


# ============================================================
# 3. Groq LLM
# ============================================================

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY is missing from .env")


# llm = ChatGroq(
#     model="openai/gpt-oss-20b",
#     temperature=0.2,
#     max_tokens=2000,
#     max_retries=2,
#     api_key=GROQ_API_KEY,
# )

llm = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0.2,
    max_tokens=1400,
    max_retries=0,
    reasoning_effort="low",
    reasoning_format="hidden",
    api_key=GROQ_API_KEY,
)
# Only one Groq request at a time.
GROQ_SEMAPHORE = threading.Semaphore(1)


def call_groq(messages, max_tokens: int):
    """
    Centralized Groq invocation.

    - Limits concurrent requests.
    - Retries TPM rate-limit errors.
    - Uses exponential backoff.
    """

    for attempt in range(5):

        try:
            with GROQ_SEMAPHORE:
                return llm.bind(
                    max_tokens=max_tokens
                ).invoke(messages)

        except RateLimitError as exc:

            if attempt == 4:
                raise

            # Use Groq's suggested retry duration when available.
            match = re.search(
                r"try again in ([\d.]+)s",
                str(exc),
                re.IGNORECASE,
            )

            wait_seconds = (
                float(match.group(1)) + 1
                if match
                else min(8 * (2 ** attempt), 60)
            )

            print(
                f"Groq TPM limit reached. "
                f"Retrying in {wait_seconds:.1f} seconds..."
            )

            time.sleep(wait_seconds)

# ============================================================
# 4. Reusable structured JSON generation
# ============================================================

def invoke_structured(
    schema: type[BaseModel],
    messages,
    max_tokens: int = 3000,
    max_attempts: int = 3,
) -> BaseModel:
    """
    Generate JSON using Groq JSON mode.

    Important:
    - Does NOT use with_structured_output().
    - Parses the response using json.loads().
    - Validates the result using Pydantic.
    - Retries when JSON or schema validation fails.
    """

    schema_json = json.dumps(
        schema.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )

    system_message = SystemMessage(
        content=f"""
You are a strict JSON generator.

Return exactly one valid JSON object.

Follow this JSON schema:

{schema_json}

Rules:
- Every property must be a separate JSON key.
- Never put property names inside arrays.
- Arrays must contain only their declared value types.
- Include every required property.
- Use valid JSON booleans and numbers.
- Do not use Markdown fences.
- Do not add explanations.
"""
    )

    conversation = [system_message, *messages]

    # json_llm = llm.bind(
    #     max_tokens=max_tokens,
    #     response_format={"type": "json_object"},
    # )
    
    last_error = None

    for attempt in range(1, max_attempts + 1):

        try:
            # response = llm.bind(max_tokens=max_tokens).invoke(conversation)
            response = call_groq(
                conversation,
                max_tokens=max_tokens,
            )

        except Exception as exc:
            raise RuntimeError(
                f"Groq request failed while generating "
                f"{schema.__name__}: {exc}"
            ) from exc

        raw_content = response.content

        # Handle normal string responses.
        if isinstance(raw_content, str):
            content = raw_content.strip()

        # Handle content-block responses, if returned.
        elif isinstance(raw_content, list):
            text_parts = []

            for part in raw_content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        text_parts.append(part.get("text", ""))

            content = "\n".join(text_parts).strip()

        else:
            raise ValueError(
                f"Unexpected response content type: "
                f"{type(raw_content)}"
            )

        # Remove optional Markdown fences.
        content = re.sub(
            r"^\s*```(?:json)?\s*|\s*```\s*$",
            "",
            content,
            flags=re.IGNORECASE,
        ).strip()

        try:
            data = json.loads(content)

            validated = schema.model_validate(data)

            print(
                f"{schema.__name__} JSON validated successfully."
            )

            return validated

        except (json.JSONDecodeError, ValidationError) as exc:

            last_error = exc

            print(
                f"{schema.__name__} validation failed "
                f"(attempt {attempt}/{max_attempts}): {exc}"
            )

            if attempt == max_attempts:
                break

            conversation.extend([
                AIMessage(content=content),
                HumanMessage(
                    content=f"""
Your previous JSON response failed validation.

Validation error:
{str(exc)}

Return the complete corrected JSON object.

Follow the original schema exactly.
Every property must be a separate JSON key.
Do not put field names inside arrays.
Return JSON only.
"""
                ),
            ])

    raise ValueError(
        f"Could not generate valid {schema.__name__} "
        f"after {max_attempts} attempts. "
        f"Last error: {last_error}"
    )


# ============================================================
# 5. Router
# ============================================================

ROUTER_SYSTEM = """
You are a routing module for a technical blog planner.

Decide whether web research is needed BEFORE planning.

Modes:

closed_book:
- needs_research=false
- Use for evergreen concepts and fundamentals.

hybrid:
- needs_research=true
- Use for mostly evergreen topics that need current tools,
  models, examples, or releases.

open_book:
- needs_research=true
- Use for volatile topics such as latest news, weekly roundups,
  rankings, pricing, policies, and regulations.

If needs_research=true:
- Generate 3-10 high-signal search queries.
- Queries must be scoped and specific.
- Avoid generic queries such as only "AI" or "LLM".
- Reflect requested dates or time periods in the queries.
"""


def router_node(state: State) -> dict:

    topic = state["topic"]

    decision = invoke_structured(
        RouterDecision,
        [
            SystemMessage(content=ROUTER_SYSTEM),
            HumanMessage(content=f"Topic: {topic}"),
        ],
        max_tokens=1000,
    )

    print(
        f"Router decision: mode={decision.mode}, "
        f"research={decision.needs_research}"
    )

    return {
        "needs_research": decision.needs_research,
        "mode": decision.mode,
        "queries": decision.queries,
    }


def route_next(state: State) -> str:
    return (
        "research"
        if state["needs_research"]
        else "orchestrator"
    )


# ============================================================
# 6. Tavily research
# ============================================================

def _tavily_search(
    query: str,
    max_results: int = 5,
) -> List[dict]:

    tool = TavilySearch(
        max_results=max_results,
    )

    response = tool.invoke({
        "query": query,
    })

    if isinstance(response, dict):
        results = response.get("results", [])

    elif isinstance(response, list):
        results = response

    else:
        results = []

    normalized = []

    for result in results or []:

        if not isinstance(result, dict):
            continue

        normalized.append({
            "title": result.get("title") or "",
            "url": result.get("url") or "",
            "snippet": (
                result.get("content")
                or result.get("snippet")
                or ""
            ),
            "published_at": (
                result.get("published_date")
                or result.get("published_at")
            ),
            "source": result.get("source"),
        })

    return normalized


def parallel_tavily_search(
    queries: List[str],
    max_workers: int = 5,
) -> List[dict]:
    """
    Optional parallel search helper.

    The research_node below uses sequential searches for
    simpler rate-limit handling.
    """

    from concurrent.futures import (
        ThreadPoolExecutor,
        as_completed,
    )

    results = []

    with ThreadPoolExecutor(
        max_workers=max_workers,
    ) as executor:

        futures = {
            executor.submit(
                _tavily_search,
                query,
                2,
            ): query
            for query in queries
        }

        for future in as_completed(futures):

            query = futures[future]

            try:
                results.append({
                    "query": query,
                    "results": future.result(),
                })

            except Exception as exc:
                results.append({
                    "query": query,
                    "results": [],
                    "error": str(exc),
                })

    return results


def research_node(state: State) -> dict:
    """
    Search Tavily and convert results directly into EvidenceItem.

    No LLM call is needed here because Tavily already returns
    the fields required by EvidenceItem.
    """

    queries = state.get("queries", []) or []

    max_results = 2

    raw_results: List[dict] = []

    for query in queries[:10]:

        try:
            results = _tavily_search(
                query,
                max_results=max_results,
            )

            raw_results.extend(results)

            print(
                f"Tavily returned {len(results)} results "
                f"for query: {query}"
            )

        except Exception as exc:
            print(
                f"Tavily search failed for '{query}': {exc}"
            )

    if not raw_results:
        print("No research results found.")

        return {
            "evidence": [],
        }

    dedup = {}

    for item in raw_results:

        url = (item.get("url") or "").strip()

        if not url:
            continue

        try:
            evidence = EvidenceItem(
                title=item.get("title") or "Untitled source",
                url=url,
                published_at=item.get("published_at"),
                snippet=item.get("snippet"),
                source=item.get("source"),
            )

            dedup[evidence.url] = evidence

        except ValidationError as exc:
            print(
                f"Skipping invalid evidence item: {exc}"
            )

    evidence_list = list(dedup.values())

    print(
        f"Research completed: "
        f"{len(evidence_list)} unique sources collected."
    )

    return {
        "evidence": evidence_list,
    }


# ============================================================
# 7. Orchestrator: create article plan
# ============================================================

ORCH_SYSTEM = """
You are a senior technical writer and developer advocate.

Your job is to produce a highly actionable outline
for a technical blog post.

Hard requirements:
- Create exactly 5 sections.
- Set target_words between 150 and 220 for each section.
- Every section must include:
  1. id
  2. title
  3. goal
  4. 3-6 concrete bullets
  5. target_words between 120 and 550
  6. tags
  7. requires_research
  8. requires_citations
  9. requires_code

Quality requirements:
- Assume the reader is a developer.
- Use correct technical terminology.
- Bullets must be actionable.
- Avoid overlapping sections.
- Include at least two of:
  - Minimal code example
  - Edge cases and failure modes
  - Performance and cost considerations
  - Security and privacy
  - Debugging and observability

Grounding rules:

closed_book:
- Keep content evergreen.
- Do not depend on current external facts.

hybrid:
- Use evidence for current tools, models, and releases.
- Mark sections using fresh information as requiring research
  and citations.

open_book:
- Set blog_kind to news_roundup.
- Focus on events and implications.
- Do not create tutorial sections unless explicitly requested.
- If evidence is insufficient, state that transparently.

Return a JSON object matching the Plan schema.
"""


def orchestrator_node(state: State) -> dict:

    evidence = state.get("evidence", [])

    mode = state.get(
        "mode",
        "closed_book",
    )

    evidence_data = [
        item.model_dump()
        for item in evidence[:16]
    ]

    plan = invoke_structured(
        Plan,
        [
            SystemMessage(content=ORCH_SYSTEM),
            HumanMessage(
                content=(
                    f"Topic: {state['topic']}\n"
                    f"Mode: {mode}\n\n"
                    "Evidence (use only for fresh claims):\n"
                    f"{json.dumps(evidence_data, ensure_ascii=False)}"
                )
            ),
        ],
        max_tokens=3000,
    )

    print("\nARTICLE PLAN VALIDATED SUCCESSFULLY")
    print(plan.model_dump_json(indent=2))

    return {
        "plan": plan,
    }


# ============================================================
# 8. Fan-out: one worker per task
# ============================================================
def fanout(state: State):

    plan = state["plan"]

    if plan is None:
        raise ValueError("Plan is missing before worker fan-out.")

    return [
        Send(
            "worker",
            {
                "task": task.model_dump(),
                "topic": state["topic"],
                "mode": state["mode"],
                "blog_title": plan.blog_title,
                "audience": plan.audience,
                "tone": plan.tone,
                "blog_kind": plan.blog_kind,
                "evidence": [
                    item.model_dump()
                    for item in state.get("evidence", [])[:4]
                ],
            },
        )
        for task in plan.tasks
    ]

# ============================================================
# 9. Worker: write one article section
# ============================================================

WORKER_SYSTEM = """
You are a senior technical writer and developer advocate.

Write ONE section of a technical blog post in Markdown.

Hard constraints:
- Follow the provided Goal.
- Cover ALL bullets in the supplied order.
- Do not skip or merge bullets.
- Stay close to the target word count.
- Output only the section content.
- Do not include the blog title as an H1.
- Start with a level-two heading: ## Section Title.

Scope guard:
- If blog_kind is news_roundup, do not turn the section
  into a tutorial or how-to guide.
- Focus on events, facts, and implications.

Grounding policy:
- If mode is open_book, do not introduce specific
  event/company/model/policy claims unless supported
  by the provided evidence.
- Only use URLs provided in Evidence.
- Cite supported claims using Markdown links.
- If a claim cannot be supported, say:
  "Not found in provided sources."
- If requires_citations is true, cite outside-world claims.

Code:
- If requires_code is true, include at least one
  minimal and correct code example.

Style:
- Short paragraphs.
- Use bullets where helpful.
- Use code fences for code.
- Avoid marketing language and fluff.
- Be precise and implementation-oriented.
"""


def worker_node(payload: dict) -> dict:

    task = Task(**payload["task"])

    # plan = Plan(**payload["plan"])
    blog_title = payload["blog_title"]
    audience = payload["audience"]
    tone = payload["tone"]
    blog_kind = payload["blog_kind"]

    evidence = [
        EvidenceItem(**item)
        for item in payload.get("evidence", [])
    ]

    topic = payload["topic"]

    mode = payload.get(
        "mode",
        "closed_book",
    )

    bullets_text = "\n".join(
        f"- {bullet}"
        for bullet in task.bullets
    )

    evidence_text = "\n".join(
        (
            f"- {item.title} | "
            f"{item.url} | "
            f"{item.published_at or 'date:unknown'}"
        )
        for item in evidence[:4]
    )

    human_message = HumanMessage(
        content=(
            f"Blog title: {blog_title}\n"
            f"Audience: {audience}\n"
            f"Tone: {tone}\n"
            f"Blog kind: {blog_kind}\n"
            # f"Constraints: {constraints}\n"
            f"Topic: {topic}\n"
            f"Mode: {mode}\n\n"
            f"Section title: {task.title}\n"
            f"Goal: {task.goal}\n"
            f"Target words: {task.target_words}\n"
            f"Tags: {task.tags}\n"
            f"Requires research: {task.requires_research}\n"
            f"Requires citations: {task.requires_citations}\n"
            f"Requires code: {task.requires_code}\n\n"
            f"Bullets:\n{bullets_text}\n\n"
            f"Evidence (only use these URLs when citing):\n"
            f"{evidence_text}"
        )
    )

    response = call_groq(
        [
            SystemMessage(content=WORKER_SYSTEM),
            human_message,
        ],
    max_tokens=700,
    )
   

    print("RESPONSE CONTENT:", repr(response.content))
    print("RESPONSE METADATA:", response.response_metadata)
    print("USAGE:", response.usage_metadata)
    print("ADDITIONAL KWARGS:", response.additional_kwargs)
    # section_md = response.content.strip()
    section_md = (
        response.content.strip()
        if isinstance(response.content, str)
        else str(response.content)
    )

    if not section_md:
        raise RuntimeError(
            f"Worker {task.id} generated no final content. "
            f"Finish reason: "
            f"{response.response_metadata.get('finish_reason')}. "
            f"Metadata: {response.response_metadata}"
        )
    
    print(f"\n===== WORKER {task.id}: {task.title} =====")
    print(section_md)
    print(f"===== END WORKER {task.id} =====\n")

    print(
        f"Worker completed task {task.id}: {task.title}"
    )

    return {
        "sections": [
            (task.id, section_md)
        ],
    }


# ============================================================
# 10. Reducer: merge article sections
# ============================================================

def merge_content(state: State) -> dict:

    plan = state["plan"]

    if plan is None:
        raise ValueError(
            "Plan is missing during content merge."
        )

    ordered_sections = [
        markdown
        for _, markdown in sorted(
            state["sections"],
            key=lambda item: item[0],
        )
    ]

    body = "\n\n".join(
        ordered_sections
    ).strip()

    merged_md = (
        f"# {plan.blog_title}\n\n"
        f"{body}\n"
    )

    return {
        "merged_md": merged_md,
    }


from pathlib import Path
import re

def save_article(title: str, markdown: str):
    safe_title = re.sub(r'[<>:"/\\|?*]', "_", title).strip()
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)

    output_path = output_dir / f"{safe_title}.md"
    output_path.write_text(markdown, encoding="utf-8")

    print(f"Article saved: {output_path}")
    return output_path

# ============================================================
# 11. Image planning
# ============================================================

DECIDE_IMAGES_SYSTEM = """
You are an expert technical editor.

Analyze the supplied technical blog article.

Decide whether diagrams or technical images would materially
improve the reader's understanding.

Rules:
- Maximum 3 images.
- Prefer architecture diagrams, workflow diagrams,
  sequence diagrams, and technical concept diagrams.
- Do not suggest decorative images.
- Do not reproduce the article.
- Return only image specifications.
- Each image must identify an existing section heading
  using after_heading.
- after_heading must match a heading from the supplied article.
- Use placeholders exactly as [[IMAGE_1]], [[IMAGE_2]],
  or [[IMAGE_3]].
- Filenames must be simple PNG filenames.
- Prompts should describe clear technical diagrams
  with readable, short labels.
- If no image is useful, return {"images": []}.
"""


def insert_image_placeholders(
    markdown: str,
    images: List[ImageSpec],
) -> str:
    """
    Insert image placeholders after their selected headings.

    This avoids sending the entire article back through the
    model as a large JSON string.
    """

    updated_markdown = markdown

    for image in images:

        heading = image.after_heading.strip()

        if not heading:
            continue

        # Find the exact heading line.
        pattern = re.compile(
            rf"(?m)^(###+\s+{re.escape(heading.lstrip('#').strip())}\s*)$"
        )

        match = pattern.search(updated_markdown)

        if not match:
            print(
                f"Image heading not found: {heading}"
            )
            continue

        insertion = (
            match.group(1)
            + "\n\n"
            + image.placeholder
        )

        updated_markdown = (
            updated_markdown[:match.start()]
            + insertion
            + updated_markdown[match.end():]
        )

    return updated_markdown


def decide_images(state: State) -> dict:

    merged_md = state["merged_md"]

    plan = state["plan"]

    if plan is None:
        raise ValueError(
            "Plan is missing during image planning."
        )
    
    article_outline = "\n".join(
        f"## {task.title}\nGoal: {task.goal}"
        for task in plan.tasks
    )

    image_plan = invoke_structured(
        GlobalImagePlan,
        [
            SystemMessage(
                content=DECIDE_IMAGES_SYSTEM
            ),
            HumanMessage(
                content=(
                    f"Blog kind: {plan.blog_kind}\n"
                    f"Topic: {state['topic']}\n\n"
                    "Article outline:\n"
                    f"{article_outline}\n\n"
                    "Choose up to 3 useful technical diagrams. "
                    "Return image specifications only."
                )
            ),
        ],
        max_tokens=1200,
    )
    # image_plan = invoke_structured(
    #     GlobalImagePlan,
    #     [
    #         SystemMessage(
    #             content=DECIDE_IMAGES_SYSTEM
    #         ),
    #         HumanMessage(
    #             content=(
    #                 f"Blog kind: {plan.blog_kind}\n"
    #                 f"Topic: {state['topic']}\n\n"
    #                 "Select useful images for these article sections. "
    #                 "Return image specifications only.\n\n"
    #                 f"{merged_md}"
    #             )
    #         ),
    #     ],
    #     max_tokens=2500,
    # )
    

    # Enforce the maximum image count in application code.
    images = image_plan.images[:3]

    # Insert placeholders in Python, not in the LLM response.
    md_with_placeholders = insert_image_placeholders(
        merged_md,
        images,
    )

    print(
        f"Image planning completed: {len(images)} images selected."
    )

    return {
        "md_with_placeholders": md_with_placeholders,
        "image_specs": [
            image.model_dump()
            for image in images
        ],
    }



# ============================================================
# 12. Gemini image generation
# ============================================================

def _gemini_generate_image_bytes(
    prompt: str,
) -> bytes:
    """
    Generate an image using Gemini.

    Requires:
        pip install google-genai

    Environment:
        GOOGLE_API_KEY
    """

    from google import genai
    from google.genai import types

    api_key = os.getenv("GOOGLE_API_KEY")

    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY is not set."
        )

    client = genai.Client(
        api_key=api_key,
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash-image",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            safety_settings=[
                types.SafetySetting(
                    category="HARM_CATEGORY_DANGEROUS_CONTENT",
                    threshold="BLOCK_ONLY_HIGH",
                )
            ],
        ),
    )

    parts = getattr(
        response,
        "parts",
        None,
    )

    if not parts and getattr(
        response,
        "candidates",
        None,
    ):
        try:
            parts = response.candidates[0].content.parts
        except Exception:
            parts = None

    if not parts:
        raise RuntimeError(
            "No image content returned. "
            "Check Gemini quota, safety filters, and SDK version."
        )

    for part in parts:

        inline = getattr(
            part,
            "inline_data",
            None,
        )

        if inline and getattr(
            inline,
            "data",
            None,
        ):
            return inline.data

    raise RuntimeError(
        "No inline image bytes found in Gemini response."
    )


# ============================================================
# 13. Generate images and place them in Markdown
# ============================================================

def generate_and_place_images(
    state: State,
) -> dict:

    plan = state["plan"]

    if plan is None:
        raise ValueError(
            "Plan is missing during image generation."
        )

    markdown = (
        state.get("md_with_placeholders")
        or state["merged_md"]
    )

    image_specs = (
        state.get("image_specs")
        or []
    )

    # No images requested.
    if not image_specs:

        output_path = Path(
            f"{plan.blog_title}.md"
        )

        output_path.write_text(
            markdown,
            encoding="utf-8",
        )

        print(
            f"Article saved: {output_path}"
        )

        return {
            "final": markdown,
        }

    images_dir = Path("images")

    images_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    for spec in image_specs:

        placeholder = spec["placeholder"]

        filename = Path(
            spec["filename"]
        ).name

        # Ensure image files use PNG.
        if not filename.lower().endswith(".png"):
            filename = f"{Path(filename).stem}.png"

        output_path = images_dir / filename

        if not output_path.exists():

            try:
                image_bytes = _gemini_generate_image_bytes(
                    spec["prompt"]
                )

                output_path.write_bytes(
                    image_bytes
                )

                print(
                    f"Generated image: {output_path}"
                )

            except Exception as exc:

                fallback = (
                    f"> **[IMAGE GENERATION FAILED]** "
                    f"{spec.get('caption', '')}\n>\n"
                    f"> **Alt:** {spec.get('alt', '')}\n>\n"
                    f"> **Prompt:** {spec.get('prompt', '')}\n>\n"
                    f"> **Error:** {exc}\n"
                )

                markdown = markdown.replace(
                    placeholder,
                    fallback,
                )

                print(
                    f"Image generation failed: {exc}"
                )

                continue

        image_markdown = (
            f"![{spec['alt']}](images/{filename})\n\n"
            f"*{spec['caption']}*"
        )

        markdown = markdown.replace(
            placeholder,
            image_markdown,
        )

    # Remove any placeholders that were not inserted.
    markdown = re.sub(
        r"\[\[IMAGE_\d+\]\]",
        "",
        markdown,
    )

    output_path = Path(
        f"{plan.blog_title}.md"
    )

    output_path.write_text(
        markdown,
        encoding="utf-8",
    )

    print(
        f"Final article saved: {output_path}"
    )

    return {
        "final": markdown,
    }


# ============================================================
# 14. Build reducer subgraph
# ============================================================

reducer_graph = StateGraph(State)

reducer_graph.add_node(
    "merge_content",
    merge_content,
)

reducer_graph.add_node(
    "decide_images",
    decide_images,
)

reducer_graph.add_node(
    "generate_and_place_images",
    generate_and_place_images,
)

reducer_graph.add_edge(
    START,
    "merge_content",
)

reducer_graph.add_edge(
    "merge_content",
    "decide_images",
)

reducer_graph.add_edge(
    "decide_images",
    "generate_and_place_images",
)

reducer_graph.add_edge(
    "generate_and_place_images",
    END,
)

reducer_subgraph = reducer_graph.compile()


# ============================================================
# 15. Build main LangGraph workflow
# ============================================================

workflow = StateGraph(State)

workflow.add_node(
    "router",
    router_node,
)

workflow.add_node(
    "research",
    research_node,
)

workflow.add_node(
    "orchestrator",
    orchestrator_node,
)

workflow.add_node(
    "worker",
    worker_node,
)

workflow.add_node(
    "reducer",
    reducer_subgraph,
)


workflow.add_edge(
    START,
    "router",
)

workflow.add_conditional_edges(
    "router",
    route_next,
    {
        "research": "research",
        "orchestrator": "orchestrator",
    },
)

workflow.add_edge(
    "research",
    "orchestrator",
)

workflow.add_conditional_edges(
    "orchestrator",
    fanout,
    ["worker"],
)

workflow.add_edge(
    "worker",
    "reducer",
)

workflow.add_edge(
    "reducer",
    END,
)


# ============================================================
# 16. PostgreSQL checkpointer
# ============================================================

DATABASE_URL = get_database_url()

_conn = psycopg.connect(
    DATABASE_URL,
    autocommit=True,
    row_factory=dict_row,
)

checkpointer = PostgresSaver(_conn)

# Create required checkpoint tables if they do not exist.
checkpointer.setup()


# ============================================================
# 17. Compile application
# ============================================================

app = workflow.compile(
    checkpointer=checkpointer,
)
