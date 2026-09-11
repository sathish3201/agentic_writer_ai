# agentic_writer_ai
This is agentic blog writer where we write and publish blog in respecitive blogs


# How to run?
1. Clone your repository to your local machine
```bash
git clone https://github.com/alich32/agentic_writer_ai.git
cd agentic_writer_ai
```
1.1  create a virtual envirolment (optional but recommended)
```bash 
conda create -n agenticwriter python 3.11 -y
``` 
1.2 activate the virtual envirolment 
```bash
conda activate agenticwriter
```

2. Install dependencies
```bash
pip install -r requirements.txt
```

3. Run the application
```bash
python main.py
```

````
# 🤖 Agentic Writer

An **agentic technical writing system** built with **LangGraph, FastAPI, and Qwen2.5 3B**.

The system does more than simply send a prompt to an LLM. It first decides whether research is required, performs web research when necessary, sends the research to an orchestrator, breaks the task into smaller subtasks, delegates them to worker nodes, and finally combines the results using a reducer.

## 🎥 Demo

[Watch the Agentic Writer Demo](https://drive.google.com/file/d/1d94hwAeXpdWq9rHfrKVwWvfx0FBa_ayR/view?usp=sharing)

---

## 🏗️ Architecture

```mermaid
flowchart TD
    START([Start]) --> ROUTER[Router]

    ROUTER -->|Research Required| RESEARCH[Research Node]
    ROUTER -.->|No Research Required| ORCHESTRATOR[Orchestrator]

    RESEARCH --> ORCHESTRATOR

    ORCHESTRATOR --> WORKER[Worker Nodes]

    WORKER --> REDUCER[Reducer]

    REDUCER --> END([Final Output])
````

 ### Workflow

```
User Query
    │
    ▼
┌──────────┐
│  Router  │
└────┬─────┘
     │
     ├──────── Research Required ────────┐
     │                                   │
     ▼                                   │
┌──────────┐                             │
│ Research │                             │
│   Node   │                             │
└────┬─────┘                             │
     │                                   │
     │ Parallel Web Search               │
     │                                   │
     └──────────────┬────────────────────┘
                    ▼
             ┌──────────────┐
             │ Orchestrator │
             └──────┬───────┘
                    │
                    ▼
             ┌──────────────┐
             │ Worker Nodes │
             └──────┬───────┘
                    │
                    ▼
             ┌──────────────┐
             │    Reducer   │
             └──────┬───────┘
                    │
                    ▼
              Final Output
```

---

 ## 🔄 How It Works

 ### 1\. User Query

 The user provides a query or writing request.

 Example:

```
Explain how to build a production-ready RAG
system using PostgreSQL and LangGraph.
```

 The request enters the LangGraph workflow.

 ### 2\. Router

 The **Router** determines whether the query requires external research.

```
                    User Query
                        │
                        ▼
                      Router
                        │
              ┌─────────┴─────────┐
              │                   │
       Research Required      No Research
              │                   │
              ▼                   │
          Research                │
              │                   │
              └─────────┬─────────┘
                        ▼
                  Orchestrator
```

 This prevents unnecessary web searches for queries that can be answered without external information.

 ### 3\. Research Node

 When research is required, the request is sent to the **Research Node**.

 The research node searches the web and collects relevant information.

 A key optimization is **parallel web research**.

 Instead of running searches sequentially:

```
Search 1
   ↓
Search 2
   ↓
Search 3
   ↓
Search 4
```

 the system can perform searches concurrently:

```
              ┌── Search 1 ──┐
              │              │
              ├── Search 2 ──┤
Research ─────┼── Search 3 ──┼──► Research Results
              │              │
              └── Search 4 ──┘
```

 **Threading** is used to execute multiple web-search operations in parallel, reducing unnecessary waiting during the research stage.

 ### 4\. Orchestrator

 The **Orchestrator** receives:

 - User query
- Research information, when available
- Task requirements

 It then decides how the task should be completed.

 Rather than giving the LLM one large task, the orchestrator breaks the problem into smaller subtasks.

 For example:

```
                  Orchestrator
                       │
          ┌────────────┼────────────┐
          │            │            │
          ▼            ▼            ▼
       Analysis      Writing     Examples
          │            │            │
          └────────────┼────────────┘
                       ▼
                  Worker Nodes
```

 ### 5\. Worker Nodes

 The **Worker Nodes** execute the tasks assigned by the orchestrator.

 Different workers can handle different parts of the problem.

 For example:

```
Orchestrator
     │
     ├── Research Analysis
     ├── Technical Explanation
     ├── Content Generation
     └── Example Generation
```

 This makes the system modular and allows different workers to specialize in different tasks.

 ### 6\. Reducer

 Once the workers complete their tasks, their outputs are passed to the **Reducer**.

 The reducer combines the distributed outputs into a coherent final response.

```
Worker 1 ──┐
Worker 2 ──┤
Worker 3 ──┼──► Reducer ──► Final Output
Worker 4 ──┘
```

 The reducer is responsible for synthesizing the worker outputs into the final result.

---

 ## ⚡ Model & Performance Optimization

 One of the main challenges was running the system with a relatively small local model.

 The project uses:

```
Qwen2.5 3B
```

 with approximately **2 GB of model memory**.

 A small model can struggle when a very large amount of raw research information is passed directly into the context.

 Instead of simply switching to a much larger and more expensive model, the architecture was optimized to reduce the amount of unnecessary information reaching the model.

 ### Optimizations

 - 🔎 Parallel web searches
- 🧵 Threading for concurrent research
- 🔀 Conditional routing
- 🧠 Task decomposition
- ⚙️ Worker-based processing
- 📦 Context management
- 🔄 Reducer-based synthesis
- 🎮 GPU optimization
- 🪶 Lightweight local LLM inference

---

 ## 🧠 Why This Architecture?

 A traditional LLM application might look like:

```
User
  │
  ▼
 LLM
  │
  ▼
Answer
```

 This project uses an agentic workflow:

```
User
  │
  ▼
Router
  │
  ├── Research ──► Parallel Web Search
  │                       │
  │                       ▼
  └────────────────► Orchestrator
                          │
                          ▼
                    Worker Nodes
                          │
                          ▼
                       Reducer
                          │
                          ▼
                    Final Output
```

 The system can therefore:

 **Decide → Research → Decompose → Delegate → Process → Reduce → Respond**

---

 ## 🛠️ Technology Stack

 | Technology | Purpose |
| --- | --- |
| Python | Core application |
| LangGraph | Agent workflow orchestration |
| FastAPI | Backend API |
| Qwen2.5 3B | Local language model |
| Web Search | External research |
| Threading | Parallel web research |
| GPU | Local model acceleration |
| HTML/CSS/JavaScript | Web interface |

---

 ## 📊 Core Architecture

```
                    USER QUERY
                        │
                        ▼
                   ┌─────────┐
                   │ ROUTER  │
                   └────┬────┘
                        │
             ┌──────────┴──────────┐
             │                     │
             ▼                     ▼
        ┌──────────┐        ┌──────────────┐
        │ RESEARCH │        │ ORCHESTRATOR │
        └────┬─────┘        └──────┬───────┘
             │                     │
             │ Parallel Search     │
             │                     │
             └──────────┬──────────┘
                        │
                        ▼
                 ┌────────────┐
                 │   WORKER   │
                 │    NODES   │
                 └──────┬─────┘
                        │
                        ▼
                 ┌────────────┐
                 │   REDUCER  │
                 └──────┬─────┘
                        │
                        ▼
                  FINAL OUTPUT
```

---

 ## 🚀 Key Achievement

 The biggest challenge was not simply generating text.

 It was making a **small local model useful as part of a larger agentic workflow**.

 Instead of solving the problem by increasing model size, the system uses architecture and engineering:

```
Small Model
     +
Parallel Research
     +
Smart Routing
     +
Task Decomposition
     +
Worker Nodes
     +
Context Management
     +
Reducer
     +
GPU Optimization
     │
     ▼
More Capable Agentic Workflow
```

 ### Key Takeaway

 > **You don't always need a bigger model. Sometimes you need a better architecture.**

---

 ## 📁 Project Structure

```
project/
│
├── app/
│   ├── agents/
│   │   ├── router.py
│   │   ├── research.py
│   │   ├── orchestrator.py
│   │   ├── worker.py
│   │   └── reducer.py
│   │
│   ├── graph/
│   │   └── workflow.py
│   │
│   └── main.py
│
├── static/
│   ├── css/
│   │   └── style.css
│   │
│   └── js/
│       └── app.js
│
├── templates/
│   └── index.html
│
├── requirements.txt
├── README.md
└── .env
```

---

 ## 🔮 Future Improvements

 - [ ] More specialized worker nodes
- [ ] Better source ranking
- [ ] Research result deduplication
- [ ] Context compression
- [ ] Persistent research memory
- [ ] Streaming agent execution
- [ ] Better article evaluation
- [ ] Improved GPU inference
- [ ] More advanced parallel worker execution
- [ ] Source citation and verification

---

 ## 🎯 Project Goal

 The goal is to build an AI writer that does more than generate text.

 It should be able to:

```
Understand the request
        ↓
Decide if research is needed
        ↓
Research the web
        ↓
Process research information
        ↓
Break the task into smaller tasks
        ↓
Delegate to workers
        ↓
Combine the results
        ↓
Generate the final response
```

---

 ## 🏁 Summary

 This project demonstrates how **agentic orchestration, parallel research, task decomposition, local LLMs, and GPU optimization** can be combined to build a practical AI writing system.

 **Built with ❤️ using LangGraph, FastAPI, Qwen2.5 3B, Python, threading, web search, and GPU optimization.**

```

```
