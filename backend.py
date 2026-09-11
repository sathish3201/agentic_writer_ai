from __future__ import annotations 

import operator 
from pathlib import Path 
from typing import TypedDict, List, Optional, Literal, Annotated

from pydantic import BaseModel, Field

from langgraph.graph import StateGraph, START, END 
from langgraph.types import Send 

from langchain_core.messages import SystemMessage, HumanMessage
# from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_tavily import TavilySearch

from langchain_groq import ChatGroq 
from langchain_ollama import ChatOllama 

import psycopg 
from psycopg.rows import dict_row 
from langgraph.checkpoint.postgres import PostgresSaver 

from dotenv import load_dotenv

import os


def get_database_url():
    database_url = os.getenv("DATABASE_URL")
    # print(database_url)
    if not database_url:
        raise ValueError(
            "Database url is missing. Please Add your Render PostgreSQL External Database URL to .env"
        )
    
    if "sslmode" not in database_url:
        seperator= "&" if "?" in database_url else "?"
        database_url = f"{database_url}{seperator}sslmode=require"
    return database_url

# get_database_url() 


# // upgrade schema -state
from typing import Literal


# generating state
class Task(BaseModel):
    id: int
    title: str 
    goal: str = Field(
        ...,
       description = "One sentence describing what reader should be able to  understand after" 
    )
    bullets: List[str] = Field(..., 
        min_length=3, max_length= 5,
        description = "3-5 concrete, non-overlapping subpoints to cover in this seciton " )
    target_words: int  = Field(..., 
        description = "Target word count for this sections (120-450)")

    section_type: Literal["intro", "core","examples", "checklist", "common_mistakes", "conclusion"]= Field(
        ...,
        description = "use 'common_mistakes' exactly once in the plain"
    )

    tags: List[str] = Field(default_factory= list)
    requires_research: bool = False
    requires_citation: bool = False 
    requires_code: bool = False

    
    # brief:str = Field(..., description="what to cover")

class Plan(BaseModel):
    blog_title : str
    audience: str = Field(..., description= "who this blog is for")
    tone: str = Field(..., description= "writing tone (e.g. , Practical, critical)")
    tasks: List[Task]
    blog_kind: Literal["explainer", "tutorial", "next_roundup", "comparison", "system_design"] = "explainer"
    constraints: List[str] = Field(default_factory= list)
    

class RouterDecision(BaseModel):
    needs_research: bool
    mode: Literal['closed_book', 'hybrid','open_book']
    queries: List[str] = Field(default_factory= list)

class EvidenceItem(BaseModel):
    title: str 
    url: str 
    published_at: Optional[str] = None 
    snippet: Optional[str] = None 
    source: Optional[str] = None


class EvidencePack(BaseModel):
    evidence: List[EvidenceItem ] = Field(default_factory= list)



class State(TypedDict):
    topic:str
    # routing/reserach 
    mode:str
    needs_research: bool 
    queries : list[str]
    evidence: List[EvidenceItem]
    plan: Optional[Plan]

    # workers/reducer
    sections: Annotated[List[tuple[int, str]], operator.add] #(task_id, section_md)
    final: str

llm = ChatOllama(
    model = 'qwen2.5:3b',
    base_url = 'http://localhost:11434',
    temperature = 0.3,
    num_ctx= 8192, # lowering save huge vRam
    num_gpu = 89 #off loads 100% of model to gpu ram
)



    ##
ROUTER_SYSTEM = """ You are a routing module for a technical blog planner
     
    Decide whether websearch is needed Before planning 

modes: 
- closed_book (need_research=False)
    Evergreen topic where correction doesnot depend on recent facts (concepts, fundamentals)
- hybrid (need_research=True)
    Most Evergreen but need up-to-date examples/tools/models to be usefull
- open_book (needs_research=True)
    Mostly volatile: weekly roundups, "this week", latest, "ranking","pricing", policy/regulations

    if needs_research=True
    - Ouput 3- 10 high-signal queries
    - Queries should be scoped and specific (avoid generic like just "AI" OR "LLM" )
    - if user ask for  "last week/this week/latest", reflect the constraints in the queries
    
 """

def router_node(state:State):
    topic = state['topic']
    decider = llm.with_structured_output(RouterDecision)
    decision = decider.invoke([
        SystemMessage(content= ROUTER_SYSTEM),
        HumanMessage(content=f"Topic:{topic}")
    ])
    return {
        "needs_research" : decision.needs_research,
        "mode" : decision.mode,
        "queries": decision.queries

    }

def route_next(state:State):
    return "research" if state["needs_research"] else "orchestrator"

from concurrent.futures import ThreadPoolExecutor


# 1. Initialize tool ONCE outside the function (saves connection overhead)
tavily_tool = TavilySearch(max_results=2)

def _single_search(query: str) -> List[dict]:
    try:
        results = tavily_tool.invoke({"query": query})
        normalized: List[dict] = []
        for r in results or []:
            normalized.append({
                "title": r.get("title") or "",
                "url": r.get("url") or "",
                "snippet": r.get("content") or r.get("snippet") or "",
                "published_at": r.get("published_date") or r.get("published_at") or "",
                "source": r.get("source") or ""
            })
        return normalized
    except Exception as e:
        print(f"Search failed for '{query}': {e}")
        return []

RESEARCH_SYSTEM = """ You are a research synthesizer for technical writing.
Given raw web search results, produce a deduplicated list of EvidenceItem objects.

Rules:
- Only include items with a non-empty url
- Prefer relevant + authoritative sources
- Deduplicate by URL
"""

def research_node(state: State):
    # 2. Cap to max 2 queries (more than enough for a blog)
    queries = (state.get("queries", []) or [])[:2]
    
    if not queries:
        return {"evidence": []}
    
    print(f"🔎 Running {len(queries)} Tavily searches in parallel...")
    
    # 3. Search all queries at the exact same time in parallel threads
    raw_results: List[dict] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        for res in executor.map(_single_search, queries):
            raw_results.extend(res)
            
    if not raw_results:
        return {"evidence": []}
    
    # 4. Fast Python deduplication (Instant! Skips the slow 30-sec LLM call)
    dedup = {}
    for r in raw_results:
        url = r.get("url")
        if url and url not in dedup:
            dedup[url] = EvidenceItem(
                title=r.get("title", ""),
                url=url,
                snippet=r.get("snippet", "")[:250],
                published_at=r.get("published_at")
            )
            
    print(f"✅ Found {len(dedup)} unique sources in ~2 seconds!")
    return {"evidence": list(dedup.values())[:6]}


# 5.orchestrator
def orchestrator_node(state: State) -> dict:
    plan = llm.with_structured_output(Plan).invoke(
        [
            SystemMessage(content=
            ("""You are an expert content strategist and senior blog editor.

Your responsibility is to take a user's blog topic or headline and create a structured, executable writing plan for a team of parallel blog-writing workers.

You do NOT write the final blog article yourself.

Instead, analyze the topic and divide the article into independent writing tasks. Each task will later be sent to a separate worker using a Send operation.

Your goal is to create a plan where:
- Every important aspect of the topic is covered.
- Tasks do not unnecessarily overlap.
- Each task can be written independently by a worker.
- The tasks together form a coherent and complete blog article.
- The final article has a natural beginning, middle, and conclusion.

## Planning Process

For the given topic:

1. Understand the likely search intent.
2. Identify the target audience.
3. Decide the appropriate writing tone.
4. Create a strong blog title.
5. Break the article into logical sections.
6. Give each section a clear goal.
7. Give each section 3-5 concrete points to cover.
8. Assign an appropriate target word count.
9. Provide a detailed brief for the worker.
10. Ensure the tasks collectively produce a complete article.

## Task Design Rules

Each task must contain:

- id: unique integer
- title: clear section heading
- goal: one sentence explaining what the reader should understand after reading the section
- bullets: 3-5 concrete, non-overlapping points
- target_words: between 120 and 450
- section_type: appropriate section classification
- brief: detailed instructions for the worker

Use these section types:

- intro
- core
- examples
- checklist
- common_mistakes
- conclusion

Use "common_mistakes" exactly once when that section is appropriate.

The introduction should establish the problem and promise value.

Core sections should explain the important concepts.

Examples sections should make concepts practical.

Checklist sections should provide actionable steps.

The common_mistakes section should explain mistakes readers should avoid.

The conclusion should summarize the key ideas and provide a useful final takeaway.

## Important

Do not create tasks merely by splitting the topic into arbitrary pieces.

Think like a professional editor assigning work to multiple writers.

Every task must have a distinct purpose.

Avoid overlapping tasks such as:
- "Benefits"
- "Advantages"
- "Why it is useful"

when they would contain essentially the same information.

Instead, make each section meaningfully different.

The workers will write the sections independently, so make each brief sufficiently detailed that a worker can produce its section without needing the full conversation.

## Output

Return ONLY the structured Plan object requested by the application schema.

Do not write the blog article.

Do not include explanations outside the structured plan.

            """)
           ),
           HumanMessage(content=f"Topic: {state['topic']}")
        ]
    )
    return {'plan': plan}




# 6.fanout 

def fanout(state: State):
    return [
        Send('worker', 
        {
            "task" : task.model_dump(),
            "topic": state['topic'],
            "mode": state["mode"],
            "plan": state['plan'].model_dump(),
            "evidence": [e.model_dump() for e in state.get("evidence", [])]

        })

        for task in state['plan'].tasks
    ]



# 7. worker 
#----------------------
WORKER_SYSTEM = """ You are senior techincal writer and developer advocate 

Write one section of techinical blog post in markdown 

Hard constraints: 
    -  follow the provided goal and cover all bullets in order(don't skip and merge bullets)
    - stay close to the target words (+-15%)
    - output only the section content in markdown ( no blog title H1, no extra commentry)
    - Start with a '## <Section Title>' heading 

scope guard: 
    - if blog_kind == "news_round_up"; do not turn this into tutorial/ how-to guide
      Do not teach web scrapping, RSS, AUTOMATION OR HOW TO FETCH NEWS" unless bullets explicitly ask for it 
      focus on summerising events and implication 

Grounding Policy:
- if mode == open_book:
    - Do not introduce any specific event/company/model/funding/policy claim unless it supported
    - For each event claim, attach a source as a markdown link: ([source][url])
    - only use urls provided in evidence. if not supported, write: "Not found in provided source" 
    - if require_citation = true:
    for outside-word claims, cite evidence urls the same way
    - Evergreen reasoning is OK without citation unless requires_citation is true

Code: 
    - if require_code = true,
        include atleast one minimal, correct code snippet, relavant to the bullets 

Style:
    - Short paragraph, bullets where helpful, code fences for code 
    - Avoid fluff/marketing. Be precise and implementation oriented

"""

def worker_node(payload: dict) -> dict:

    task = Task(**payload['task'])
    plan = Plan(**payload['plan'])

    evidence = [EvidenceItem(**e) for e in payload.get('evidence',[])]

    bullets_text = "\n- " + "\n- ".join(task.bullets)
    evidence_text = ""
    if evidence:
        evidence_text = '\n'.join(
            f"- {e.title} | {e.url} | {e.published_at or 'date: unknown'}".strip()
            for e in evidence[:20]
        )
    
    section_md = llm.invoke([
        SystemMessage(content=WORKER_SYSTEM),
        HumanMessage (content = (
            f"Blog title : {plan.blog_title}\n"
            f"Audience : {plan.audience}\n"
            f"Tone : {plan.tone}\n"
            f"Blog kind : {plan.blog_kind}\n"
            f"Constraints : {plan.constraints}\n"

            f"Topic : {payload['topic']}\n"
            f"Mode : {payload['mode']}\n"

            f"Section title : {task.title}\n"
            f"Goal : {task.goal}\n"
            f"Target words: {task. target_words}\n"

            f"Tags : {task.tags}\n"
            f"requires_research : {task.requires_research}\n"
            f"requires_citations : {task.requires_citation}\n"
            f"requires_code : {task.requires_code}\n"
            f"Bullets : {bullets_text}\n"
            f"Evidence (only use this urls when citing) :\n {evidence_text}\n"
            
    ))

    ]).content.strip()

    return {'sections': [(task.id, section_md)]}



from pathlib import Path 
import re

def reducer_node(state: State) -> dict:
    plan = state['plan']

    ordered_sections = [md for _, md in sorted(state['sections'], key=lambda x: x[0])]
    body = '\n\n'.join(ordered_sections).strip()
    final_md = f'# {plan.blog_title}\n\n{body}'

    # Remove invalid Windows filename characters: \ / : * ? " < > |
    safe_title = re.sub(r'[\\/*?:"<>|]', "", plan.blog_title).strip()
    safe_title = re.sub(r'\s+', '_', safe_title)
    file_name = f"{safe_title}.md"

    Path(file_name).write_text(final_md, encoding="utf-8")
    print(f"✅ Saved final article ({len(final_md)} chars) to: {file_name}")

    return {'final': final_md}



g = StateGraph(State)
g.add_node("router", router_node)
g.add_node("research", research_node)
g.add_node("orchestrator", orchestrator_node)
g.add_node("worker", worker_node)
g.add_node("reducer", reducer_node)

g.add_edge(START, "router")
g.add_conditional_edges("router", route_next, {"research": "research", "orchestrator": "orchestrator"})
g.add_edge("research", "orchestrator")


g.add_conditional_edges("orchestrator", fanout, ["worker"])
g.add_edge("worker", "reducer")
g.add_edge("reducer", END)




# postgres check pointer

DATABASE_URL = get_database_url()
_conn = psycopg.connect(
    DATABASE_URL,
    autocommit= True,
    row_factory = dict_row
)

checkpointer =  PostgresSaver(_conn)
checkpointer.setup()


app = g.compile(checkpointer= checkpointer)


app


config = {"configurable": {"thread_id": "thread_002"}}


def run(topic: str):
    inputs = {
        "topic": topic,
        "mode": "",
        "needs_research": False,
        "queries": [],
        "evidence": [],
        "plan": None,
        "sections": [],
        "final": ""
    }
    
    # Use app.stream to see live step-by-step progress
    print(f"🚀 Starting Blog Agent for: '{topic}'\n" + "="*45)
    for event in app.stream(inputs, config=config):
        for node_name, output in event.items():
            if node_name == "router":
                print(f"✅ [Router] Done -> Mode: {output.get('mode')}, Needs Research: {output.get('needs_research')}")
            elif node_name == "research":
                print(f"✅ [Research] Found {len(output.get('evidence', []))} evidence sources")
            elif node_name == "orchestrator":
                print(f"✅ [Orchestrator] Created plan with {len(output['plan'].tasks)} section tasks")
            elif node_name == "worker":
                task_id, _ = output['sections'][0]
                print(f"✍️  [Worker] Completed section (Task #{task_id})")
            elif node_name == "reducer":
                print("🎉 [Reducer] Assembled final article and saved to disk!")