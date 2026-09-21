# Agent-oriented Programming: a White Paper

TAN, Kun (kuntan@gmail.com)
Independent Researcher
## Introduction

Large language model (LLM)-based agents that can autonomously perceive environments, reason over goals, and execute multi-step actions are increasingly becoming indispensable digital assistants in our daily work, spanning from passive question answering to active task completion. It is widely recognized that an agent's capability depends not only on the underlying models but also on the harness system built around them. Consequently, **harness engineering** is emerging as an important and rapidly evolving area of research.

However, building a good and robust harness is not easy. Most existing production agents, such as Claude Code, Codex, OpenCode, and Hermes Agent, rely on hand-built harnesses tailored to specific domains, e.g., Claude Code and Codex for coding, and Hermes for document handling. Although these specialized harnesses work well within their own domains, they are difficult to extend to other areas where the workflows and coordination models may differ significantly.

Admittedly, quite a few frameworks exist to help build harnesses, such as LangChain, AutoGen, LlamaIndex, and Dify. However, these frameworks treat agents as extensions of their own host programming languages (TypeScript, Python, etc.), and they usually come with misplaced abstractions and complicated wiring to connect the various components of an agent. Moreover, a harness implementation is tightly coupled to the particular framework it uses, making it very difficult and tedious to port a harness from one framework to another. We believe that a truly agent-centric programming paradigm cannot be realized with these frameworks; instead, it must be baked into the language itself.

In this white paper, we present **AGLang**, an agent-oriented language for modern LLM-based agents. In one sentence, AGLang is a simple, agent-oriented, multi-stage, multi-agent-native, distributed, extensible, robust, observable, interpreted, and portable programming language. In what follows, we explain what we mean by these buzzwords and the problems we are trying to solve.

## Simple

We set out to build AGLang so that it could be programmed easily, without requiring esoteric training in conventional computer programming. Of course, a good knowledge of computer programming remains a plus -- an enabler, even, for advanced integration and extension of AGLang with existing software systems.

AGLang programs are essentially Markdown documents that describe agent behavior in structured natural language. AGLang treats the agent as a first-class citizen and adopts high-level abstractions for agentic processing; the complex, low-level concepts of conventional programming languages are all hidden away.

Simplicity is a virtue: it widens the pool of agent developers beyond conventional programmers. Empowering domain experts to write agents themselves will be critical to accelerating AI adoption across society.

Simplicity also buys expressiveness, so that fewer lines of code are needed. For example, a full-fledged coding agent crew (a team of planner, coder, reviewer, and committer) takes only 500 lines.

## Agent-oriented

AGLang is agent-oriented. An agent in AGLang is represented as an **agent class**, which encapsulates prompts (system prompt and instructions), workflow, tools, and context memory into a single holistic abstraction. This clean abstraction allows maximally component reuse but remains highly flexible to extend.  

Agent classes are inheritable. A subclass can reuse its parent's properties and need only modify the aspects that change its behavior. Agents are also stateful: AGLang reserves a shared memory folder for each workspace, fully managed by the agents themselves. Indexes of the memory files are loaded automatically when an agent starts, so agents can go directly to the relevant memory at runtime as needed. Over time, however, the memory folder may inflate and become inconsistent — for example, an earlier experience may go stale as the environment improves. To address this, a utility class called **SleepAgent** can compress, verify, and reorganize all memory files. Customized classes can also be derived from SleepAgent, allowing users to define their own sleeping procedures.

The AGLang runtime monitors context-window usage in real time. Since compression is essential to enabling long-horizon tasks, AGLang treats it as a customizable stage rather than a fixed process at the runtime layer. By allowing agent developers to write their own compression procedures, it gives them far greater flexibility and control over engineering their agent's context.

Agent-oriented programming is largely analogous to object-oriented programming in conventional programming languages, but it is considerably simpler and tailored specifically to agent development.

## Multi-stage and multi-agent

AGLang natively supports multi-stage workflows through prompt chaining. Each stage carries its own instructions and can modify both the available tool set and the gating conditions for entering and exiting. Control tools allow agents to traverse the prompt chain flexibly, making it possible to implement arbitrary workflows over a shared conversation history.

Alternatively, AGLang allows a parent agent to spawn multiple sub-agents, which may belong to the same class or to different classes. It is therefore natural for AGLang to orchestrate an agent crew with distinct roles and backend LLMs. Sub-agent invocation can be sequential or concurrent. A parent agent can synchronously spawn a sub-agent, hand off a sub-task, and wait for that sub-agent to finish and return its result. Alternatively, the parent can spawn a sub-agent that runs in parallel, with the runtime responsible for scheduling such parallel agents concurrently. AGLang provides a built-in signaling and locking mechanism to coordinate parallel agents. Notably, although parent and child agents have isolated context windows, they share a common workspace and persistent memory files, which makes it easy for multiple collaborative agents to share data and artifacts.

## Distributed

AGLang provides native primitives that let agents communicate across machine boundaries. Unlike the Agent-to-Agent (A2A) protocol, which defines a rigid protocol for task completion, AGLang supports much finer-grained and extensible messaging to coordinate agents or stages within distributed agent crews. Even so, AGLang's communication primitives still build on the widely used HTTP(S) and JSON message framing, so agents can open and reach remote agents over the Internet via URLs.
## Extensible and integration 

While AGLang aims to be simple, it must nonetheless remain extensible. Users can register new tools, or enable and disable existing ones, by editing configuration files. New capabilities can also be added through the industry-standard **Skills** interface. Moreover, advanced programmers can leverage stage hooks to embed Python or Bash scripts within AGLang, tightly integrating agents with existing software systems.

## Robust and observability

**AGLang is designed to embrace the inherent uncertainty of LLMs.** The runtime dynamically validates model responses, detecting malformed outputs and retrying them automatically. Users can place gates at stage boundaries to reject unexpected or undesirable behaviors. In addition, AGLang employs a structured exception-handling mechanism that catches exceptions, invokes a recovery process, and then resumes normal execution if the recovery succeeds.

All agent history is faithfully logged, along with runtime logging information. Together, these logs provide valuable first-hand data for diagnosis and self-learning.
## Interpreted and portable

AGLang is a high-level language executed by interpreters, which makes it inherently architecture-neutral and portable. As long as an interpreter is available, AGLang can run anywhere, regardless of the underlying computer hardware, the operating system, or the host language in which the interpreter is implemented.

## Summary

AGLang is a simple yet powerful addition to agent developers. By embracing an agent-oriented programming paradigm, AGLang makes it significantly easier to build LLM-based agents, while offering strong extensibility, robustness, and portability. An open-source AGLang programming environment, named YAHA, is available at https://github.com/AGLang-Yaha/yaha. Download it today and try your first agent program. Enjoy!
