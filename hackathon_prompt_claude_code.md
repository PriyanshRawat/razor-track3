# Hackathon Strategy & Build Plan Generator — AI Revenue Recovery
### (Claude Code edition)

## Session scope — read this first

You are operating inside **Claude Code** for this task. This is a **strategy and architecture exercise, not an implementation task**.

- Do **not** run bash commands, do **not** create, edit, or scaffold any code files, and do **not** initialize a project structure.
- The only tool use permitted: up to **3 web searches** (plus matching fetches), and only if they materially sharpen "Why This Problem/Angle Wins" or "Competitive/Differentiation Analysis" with real, current products — e.g. checking how Stripe's Smart Retries, Chargebee Retention, Recurly, HighRadius, Tesorio, or Chaser actually approach revenue recovery, so the differentiation claims are accurate rather than invented. Do not exceed this budget, and do not use any other tools.
- Once the full plan below is finalized, write it to a single file named `HACKATHON_PLAN.md` in the project root, so it persists as reference for the implementation phase that comes next in this session. Still present the complete plan in your response — the file is in addition to that, not instead of it.

---

You are an expert **AI/ML engineer, agentic AI architect, technical product manager, startup CTO, hackathon strategist, and hackathon judge**.

Your task is to create a **winning technical and product plan** for the problem statement below.

The goal is **not merely to satisfy the problem statement**. The goal is to design a project that convincingly demonstrates strong understanding of **AI, ML, LLMs, AI agents, reasoning, tool use, orchestration, evaluation, reliability, and real-world system design**, while solving a meaningful business problem.

You must think independently and critically. Do not assume that the most obvious interpretation of the problem statement is the best one. Explore the solution space yourself, identify the strongest opportunity, challenge your own assumptions, and then converge on the approach that has the highest probability of producing an exceptional hackathon submission.

## Problem Statement

### Track 03 — AI Revenue Recovery

**Find revenue that's slipping away and win it back**

Build an agent that detects revenue at risk, determines the right intervention, and executes a bounded recovery workflow: from payment failures and checkout abandonment to overdue receivables.

### Why now

Revenue loss rarely happens in one clean step. A payment degrades, a checkout gets abandoned, a subscription fails, or an invoice goes overdue. AI can now close the loop from detecting the problem to diagnosing it, choosing the right intervention, and recovering the money.

### Example directions

* Payment degradation → root cause → recovery action
* Checkout drop-off recovery
* Failed-subscription recovery
* B2B receivables chaser
* Mandate retry sequencer
* Hinglish voice recovery
* Promise-to-pay tracker

### The bar

Don't just identify the problem. Show measured money recovered across a batch, with compliant escalation, stopping rules, and an audit trail.

## Your Objective

Design the strongest possible **hackathon project plan** for this problem.

The final plan should optimize simultaneously for:

* meaningful real-world business value
* genuine and defensible use of AI
* strong agentic behavior rather than superficial LLM usage
* intelligent use of reasoning and tool interaction
* bounded and reliable autonomy
* measurable outcomes
* rigorous evaluation
* explainability and auditability
* technical depth
* demo impact
* feasibility within hackathon constraints
* clarity of the overall product story
* the ability to withstand aggressive technical questioning from expert judges

Do not optimize for novelty alone. Do not optimize for implementation simplicity alone. Find the strongest balance between **ambition, technical credibility, measurable value, and execution feasibility**.

## Critical Instructions

Do not blindly follow the example directions in the problem statement. Treat them as possibilities, not recommendations.

Do not assume that a particular model, framework, architecture, workflow, interface, or technology is required.

Do not force an LLM into parts of the system where deterministic logic, conventional ML, optimization, retrieval, or other techniques would be better.

Likewise, do not reduce the project to a conventional ML model merely because that would be easier to implement.

Determine independently:

* where AI genuinely creates additional value
* where an agent is actually justified
* what should remain deterministic
* where humans should remain involved
* what the agent should be allowed to do
* what the agent should never be allowed to do
* what information the agent needs
* what tools or external systems it should interact with
* how the system should respond to uncertainty and failure
* how success should be measured
* how the claim of "money recovered" can be made honestly and defensibly

Avoid generic statements such as "use AI to automate the workflow." Explain the actual reasoning behind the proposed system.

## Required Reasoning Process

Before presenting the final recommendation, reason through the problem systematically.

### 1. Understand the real problem

Identify the underlying revenue-recovery problem rather than simply repeating the wording of the prompt.

Determine: who the ideal user is; what operational pain they experience; where money is actually being lost; why existing approaches are insufficient; what part of the workflow is most suitable for AI.

### 2. Explore the solution space

Consider multiple plausible solution directions inspired by the problem statement, including directions that are not explicitly listed. Compare them objectively instead of committing to the first attractive idea.

For each major candidate, assess: business value; agentic depth; AI necessity; technical complexity; data requirements; measurability; demo potential; reliability; implementation risk; differentiation; scalability; judge appeal.

Then select the strongest opportunity.

### 3. Define the product

Turn the selected opportunity into a precise product concept. Clearly define: target user; problem; product promise; core workflow; primary user interaction; system boundaries; what the agent actually does; what remains deterministic; what requires human approval.

The final product should feel like a coherent product, not a collection of AI features.

### 4. Design the AI/agent architecture

Design an architecture that demonstrates genuine AI engineering. Think carefully about: perception/input; context gathering; reasoning; planning; decision making; tool use; execution; feedback; state; memory where justified; recovery from failure; uncertainty; human escalation; policy enforcement; observability; auditability.

Do not use multi-agent architecture, agent frameworks, RAG, vector databases, memory systems, or other fashionable components unless they materially improve the solution.

### 5. Separate intelligence from control

Explicitly distinguish between what the AI is responsible for and what deterministic systems are responsible for. The system should be architected so that AI mistakes do not automatically become irreversible business actions. Design appropriate guardrails, constraints, stopping conditions, escalation paths, and approval boundaries.

### 6. Design the data and evaluation strategy

Determine what data is required and how realistic development/testing data can be generated or obtained within a hackathon. Define a rigorous evaluation framework. Do not rely only on generic model metrics.

Establish metrics that demonstrate: detection quality; decision quality; intervention quality; agent reliability; policy adherence; false actions; successful recovery; money at risk; money recovered; recovery rate; cost of incorrect interventions; unresolved cases; human escalations; other metrics that are genuinely relevant to the selected product.

Ensure the evaluation cannot be easily dismissed as cherry-picked.

### 7. Make "money recovered" defensible

The problem explicitly requires measured money recovered. Design a method that lets the project make this claim honestly. Clearly distinguish between: observed recovery; simulated recovery; estimated recovery; projected business impact.

Do not manufacture impressive numbers. Explain what ground truth exists, what assumptions are required, and how a judge could reproduce the evaluation.

### 8. Design the demo

Create a high-impact demo narrative that demonstrates the system rather than merely describing it. Determine: what the audience sees first; what happens during the live workflow; where the AI reasons; where tools are used; where policy constraints matter; how a failure is handled; how the system demonstrates measurable business impact; what evidence proves that the system works.

### 9. Design for adversarial judge questioning

Anticipate the strongest questions a technical judge, ML engineer, AI engineer, product manager, or finance/revenue expert could ask. Identify weaknesses in the proposed solution before the judges do.

For every important design choice, be able to explain: why it exists; why it is AI-driven; why a simpler approach was insufficient; how it is evaluated; what can go wrong; how the system prevents or handles that failure.

### 10. Define the MVP ruthlessly

Separate **Must build** from **Should build** from **Stretch** from **Do not build**. Prioritize the components that contribute most to the judging outcome. Do not allow scope creep to destroy the core product.

## Required Final Output

Produce the final answer as a detailed execution plan with the following structure:

1. Winning Concept
2. Why This Problem/Angle Wins
3. Target User & Real-World Pain
4. Core Product Workflow
5. Why AI Is Actually Necessary
6. Why This Qualifies as an Agent
7. AI vs Deterministic Responsibilities
8. System Architecture
9. Agent State / Decision Flow
10. Tools and External Actions
11. Data Strategy
12. Evaluation & Ground Truth
13. Business Metrics
14. Safety, Guardrails & Stopping Rules
15. Auditability & Observability
16. Failure Handling
17. Demo Story
18. MVP Scope
19. Stretch Features
20. Recommended Tech Stack
21. Implementation Plan
22. Testing Strategy
23. Likely Judge Questions & Strong Answers
24. Major Risks & Mitigations
25. What Not to Build
26. Competitive/Differentiation Analysis
27. Final Winning Pitch
28. One-Sentence Product Definition

## Quality Standard

Be highly critical. Do not praise weak ideas merely because they sound innovative. Reject unnecessary complexity. Reject superficial "AI" features. Reject arbitrary use of agents. Reject unmeasurable business claims. Reject architecture whose components exist only to look sophisticated. Prefer a smaller system that demonstrates deep understanding over a large system that demonstrates shallow understanding.

The final recommendation should make a technically sophisticated judge think:

> "They understand not only how to use an LLM, but when to use AI, when not to use AI, how to give an agent useful autonomy, how to constrain it, how to evaluate it, and how to turn it into a trustworthy business system."

Do **not** write code. Do **not** implement anything. Do **not** provide setup instructions. Do **not** start building the project. Do **not** make API calls or assume that a particular external service must be used.

Your output at this stage must be **strategy, architecture, product reasoning, evaluation design, and an execution plan only**. Once finished, save it to `HACKATHON_PLAN.md` as instructed at the top of this prompt.
