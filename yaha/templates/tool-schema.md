```json
[
  {
    "name": "create_file",
    "description": "Creates a new file with the given content.",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {
          "type": "string",
          "description": "The path where the file should be created."
        },
        "content": {
          "type": "string",
          "description": "The text content to write into the new file."
        }
      },
      "required": ["path", "content"]
    }
  },
  {
    "name": "edit_file",
    "description": "Robust file editing. Supports Search/Replace (search_text, replace_text) or Range Replace (start_line, end_line, replace_text).",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {
          "type": "string",
          "description": "The path of the file to edit."
        },
        "search_text": {
          "type": "string",
          "description": "The exact text to search for within the file for a Search/Replace operation."
        },
        "replace_text": {
          "type": "string",
          "description": "The text that will replace the search_text or the specified line range."
        },
        "start_line": {
          "type": "integer",
          "description": "The starting line number (inclusive) for a Range Replace operation."
        },
        "end_line": {
          "type": "integer",
          "description": "The ending line number (inclusive) for a Range Replace operation."
        }
      },
      "required": ["path"]
    }
  },
  {
    "name": "read_file",
    "description": "Reads specified lines from a file. Returns the content prepended with the actual number of lines read.",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {
          "type": "string",
          "description": "The path of the file to read."
        },
        "start_line": {
          "type": "integer",
          "description": "The line number to start reading from."
        },
        "end_line": {
          "type": "integer",
          "description": "The line number to stop reading at."
        }
      },
      "required": ["path", "start_line", "end_line"]
    }
  },
  {
    "name": "write_file",
    "description": "Inserts content into a file starting from the given line.",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {
          "type": "string",
          "description": "The path of the file to modify."
        },
        "content": {
          "type": "string",
          "description": "The content to insert into the file."
        },
        "start_line": {
          "type": "integer",
          "description": "The line number where the content insertion should begin."
        }
      },
      "required": ["path", "content", "start_line"]
    }
  },
  {
    "name": "search_files",
    "description": "Returns the lines that contain the search text (regular expression) in the specified file.",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {
          "type": "string",
          "description": "The path of the file to search within."
        },
        "search_text": {
          "type": "string",
          "description": "The regular expression pattern to find within the file."
        }
      },
      "required": [
        "path",
        "search_text"
      ]
    }
  },
  {
    "name": "grep_files",
    "description": "Recursively searches files for lines matching a regular expression. Returns matching lines with file path and line number. Optionally filters by glob pattern and limits the result count.",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {
          "type": "string",
          "description": "The file or directory path to search."
        },
        "search_text": {
          "type": "string",
          "description": "The regular expression pattern to search for."
        },
        "recursive": {
          "type": "boolean",
          "description": "If true, search directories recursively. Default: true."
        },
        "glob_pattern": {
          "type": "string",
          "description": "Optional glob pattern to filter files, e.g., '*.py'."
        },
        "max_results": {
          "type": "integer",
          "description": "Maximum number of matching lines to return. Default: 100."
        }
      },
      "required": [
        "path",
        "search_text"
      ]
    }
  },

  {
    "name": "append_file",
    "description": "Appends the given content to the end of the specified file.",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {
          "type": "string",
          "description": "The path of the file to append to."
        },
        "content": {
          "type": "string",
          "description": "The content to add to the end of the file."
        }
      },
      "required": ["path", "content"]
    }
  },
  {
    "name": "bash_tool",
    "description": "Executes a shell command and returns the output.",
    "parameters": {
      "type": "object",
      "properties": {
        "cmdline": {
          "type": "string",
          "description": "The shell command to execute."
        }
      },
      "required": ["cmdline"]
    }
  },
  {
    "name": "async_bash",
    "description": "Launch a shell command in the BACKGROUND and return immediately with a waitable job id. Use it for long-running commands so work can continue in parallel. The id (async-N) is a waitable EVENT name (declare it as a stage Wait Event, or bind a @placeholder to it); check progress with poll_async; the finished result is also attached automatically to your next message.",
    "parameters": {
      "type": "object",
      "properties": {
        "cmdline": {
          "type": "string",
          "description": "The shell command to execute in the background."
        }
      },
      "required": ["cmdline"]
    }
  },
  {
    "name": "poll_async",
    "description": "Check the status or result of a background async_bash job by its id, without blocking.",
    "parameters": {
      "type": "object",
      "properties": {
        "job_id": {
          "type": "string",
          "description": "The job id returned by async_bash, e.g. async-1."
        }
      },
      "required": ["job_id"]
    }
  },
  {
    "name": "web_fetch_tool",
    "description": "Fetches and cleans content from a URL.",
    "parameters": {
      "type": "object",
      "properties": {
        "url": {
          "type": "string",
          "description": "The URL of the webpage to fetch and extract content from."
        },
        "prompt": {
          "type": "string",
          "description": "Optional instructions or context on what specific information to clean or extract from the page."
        }
      },
      "required": ["url", "prompt"]
    }
  },
  {
    "name": "yaha_spawn_agent",
    "description": "Spawns a recursive sub-agent with a specific class, system prompt and task instruction. Returns the sub-agent's final answer.",
    "parameters": {
      "type": "object",
      "properties": {
        "agent_class": {
		  "type": "string",
		  "description": "The class of the sub-agent to spawn (e.g., 'ChatAgent', 'Cha', 'BaseAgent'). Use the full name or short code."
        },
        "system": {
          "type": "string",
          "description": "The system prompt defining the persona, constraints, and operational guidelines for the sub-agent."
        },
        "instruction": {
          "type": "string",
          "description": "The specific task or objective the sub-agent needs to execute and resolve."
        }
      },
      "required": ["agent_class", "system", "instruction"]
    }
  },
  {
    "name": "google_search_tool",
    "description": "Performs a Google Search using Gemini's native grounding feature. Returns grounded text with citations and sources.",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "The search query to perform."
        }
      },
      "required": ["query"]
    }
  },
  {
    "name": "tavily_search_tool",
    "description": "Searches the web using the Tavily API. Returns a list of result titles, URLs, and content summaries. Requires TAVILY_API_KEY environment variable.",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "The search query to perform."
        },
        "max_results": {
          "type": "integer",
          "description": "Maximum number of results to return (default 5).",
          "default": 5
        }
      },
      "required": ["query"]
    }
  },
  {
    "name": "load_skill",
    "description": "Load the specified skill instructions into context.",
    "parameters": {
      "type": "object",
      "properties": {
        "name": {
          "type": "string",
          "description": "The name of the skill."
        }
      },
      "required": ["name"]
    }
  },
  {
    "name": "list_files",
    "description": "Lists files and directories in the specified path. Returns entries with type indicator and size.",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {
          "type": "string",
          "description": "The directory path to list. Defaults to current directory."
        },
        "pattern": {
          "type": "string",
          "description": "Optional regex pattern to filter entries."
        }
      },
      "required": []
    }
  },
  {
    "name": "ask_user",
    "description": "Displays a question to the user and waits for their feedback. Use this when you need clarification or additional input from the human user.",
    "parameters": {
      "type": "object",
      "properties": {
        "question": {
          "type": "string",
          "description": "The question or prompt to display to the user."
        }
      },
      "required": ["question"]
    }
  },
  {
    "name": "restart",
    "description": "Restarts the prompt chain from the beginning (Preamble + Task). Use this if you realize the current trajectory is fundamentally flawed or if the environment state has changed in a way that requires a full reset.",
    "parameters": {
      "type": "object",
      "properties": {},
      "required": []
    }
  },
  {
    "name": "loop_tool",
    "description": "Loops the CURRENT stage: the stage ends and is immediately re-entered (its Wait Event gate is re-checked, its prompt rebuilt), preserving conversation history. To move to a DIFFERENT stage use exit_to instead.",
    "parameters": {
      "type": "object",
      "properties": {
        "summary": {
          "type": "string",
          "description": "Optional concise summary of this stage; becomes the stage's final response."
        },
        "modifier": {
          "type": "string",
          "description": "Optional one-shot note appended to the re-entered stage's prompt as a [TASK MODIFIER] section."
        }
      },
      "required": []
    }
  },
  {
    "name": "success_tool",
    "description": "Signals successful completion of the current stage and advances to the next stage. Only use this when the stage instructions explicitly require an exit tool.",
    "parameters": {
      "type": "object",
      "properties": {
        "summary": {
          "type": "string",
          "description": "Optional concise summary of what was accomplished in this stage."
        }
      },
      "required": []
    }
  },
  {
    "name": "fail_tool",
    "description": "Signals that the current stage has failed and routes to the designated failure-handling stage. Use this when the stage cannot complete successfully.",
    "parameters": {
      "type": "object",
      "properties": {
        "reason": {
          "type": "string",
          "description": "Optional reason for the failure."
        }
      },
      "required": []
    }
  },
  {
    "name": "set_event_success",
    "description": "Set one or more named multi-agent events, then complete the current stage (set_event + success_tool). Peers waiting on any of those events at a stage boundary are woken. The FIRST argument is a summary of this stage and becomes that stage's final response; nothing is injected into any later stage. Only use this when the stage instructions explicitly require it.",
    "parameters": {
      "type": "object",
      "properties": {
        "summary": {
          "type": "string",
          "description": "Optional concise summary of what this stage accomplished. It becomes the stage's final response."
        },
        "names": {
          "type": "array",
          "items": {"type": "string"},
          "description": "The event name(s) to set before leaving the stage. At least one is required."
        }
      },
      "required": ["names"]
    }
  },
  {
    "name": "set_event_jump",
    "description": "Set one or more named multi-agent events, then move the chain to the target stage (set_event + exit_to). Peers waiting on any of those events at a stage boundary are woken. The target obeys the exit_to rules: a chain stage is a positional jump (forward or backward; a JUMP naming the CURRENT chain stage is refused - use set_event_loop to re-enter it), and a declared but unattached Detached stage is a CALL (runs as a full stage, then the chain resumes at the caller's next stage; a call may name the current Detached stage - that is recursion, still capped by call depth). Only use this when the stage instructions explicitly require it.",
    "parameters": {
      "type": "object",
      "properties": {
        "summary": {
          "type": "string",
          "description": "Optional concise summary of what this stage accomplished. It becomes the stage's final response."
        },
        "target": {
          "type": "string",
          "description": "The stage to move to: a chain stage (jump) or a declared Detached stage (call)."
        },
        "names": {
          "type": "array",
          "items": {"type": "string"},
          "description": "The event name(s) to set before leaving the stage. At least one is required."
        }
      },
      "required": ["target", "names"]
    }
  },
  {
    "name": "exit_to",
    "description": "Leave the current stage and continue the chain from the named stage. A chain-stage name is a positional jump (forward or backward; a JUMP naming the CURRENT chain stage is refused - use loop_tool to re-enter it). A name that is a declared but unattached Detached stage is a CALL: it runs as a full stage, then the chain resumes at the caller's next stage (calls nest, capped at 16; naming the current Detached stage is recursion, still capped). A blank or unknown name is a hard error and the stage is NOT left. Costs one chain operation against Max Loops.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The stage to move to (a chain stage or a declared Detached stage)."},
            "summary": {"type": "string", "description": "Optional concise summary of what this stage accomplished. It becomes the stage's final response."}
        },
        "required": ["name"]
    }
  },
  {
    "name": "set_event_loop",
    "description": "Set one or more named multi-agent events, then loop the CURRENT stage (set_event + loop_tool): the stage ends and is immediately re-entered (its Wait Event gate is re-checked, its prompt rebuilt). Peers waiting on any of those events are woken. The summary becomes the stage's final response; nothing is injected into any later stage.",
    "parameters": {
      "type": "object",
      "properties": {
        "summary": {
          "type": "string",
          "description": "Optional concise summary of what this stage accomplished. It becomes the stage's final response."
        },
        "modifier": {
          "type": "string",
          "description": "Optional one-shot note appended to the re-entered stage's prompt as a [TASK MODIFIER] section."
        },
        "names": {
          "type": "array",
          "items": {"type": "string"},
          "description": "The event name(s) to set before looping the stage. At least one is required."
        }
      },
      "required": ["names"]
    }
  },
  {
    "name": "spawn_parallel_agent",
    "description": "Start a parallel peer agent that owns its own context window (non-blocking). The peer runs when the caller blocks at a stage boundary. Returns the peer handle name; coordinate with it through named events (a peer sets NO event automatically when it finishes). Fan-out is intentionally unbounded - it is limited only by Agentsets and the toolset privilege cap.",
    "parameters": {
      "type": "object",
      "properties": {
        "agent_class": {
          "type": "string",
          "description": "The peer agent class (name or code); must be in this agent's Agentset."
        },
        "clone_context": {
          "type": "string",
          "description": "Context mode: 'new' for a fresh context window, 'clone' for a deep copy of the caller's current context."
        },
        "prompt": {
          "type": "string",
          "description": "The task prompt for the peer agent."
        }
      },
      "required": ["agent_class"]
    }
  },
  {
    "name": "bind_tool",
    "description": "Late-bind an event placeholder (a name starting with '@') to a REAL event name for this run. The binding is run-global (every agent sees it) and strictly 1:1; re-binding is allowed and the LAST bind wins. The second argument must be a REAL event name - placeholder chaining is not supported. Once bound, the placeholder may be used both in a stage's Wait Event and in the set_event_success / set_event_loop / set_event_jump names. Using an UNBOUND placeholder is a stage FAILURE routed to that stage's Fail Target, so bind it before the wait is reached. Bind a placeholder BEFORE any actor waits on or signals it; re-binding a placeholder that is already in use is unsupported, because a signal already latched under the old target is not moved by a rebind.",
    "parameters": {
      "type": "object",
      "properties": {
        "placeholder": {
          "type": "string",
          "description": "The placeholder name, starting with '@' (for example '@gate')."
        },
        "real_event": {
          "type": "string",
          "description": "The REAL event name the placeholder resolves to."
        }
      },
      "required": ["placeholder", "real_event"]
    }
  },
  {
    "name": "connect_tool",
    "description": "Open a conversation with a remote YAHA agent's SERVICE endpoint. The argument is an endpoint URL of the form http(s)://host[:port]/path/version (the trailing version segment is required, e.g. /crew/reviewer/v1). On success the reply carries the CONVERSATION endpoint (CEP) for that conversation: bind it with bind_tool and wait on the placeholder to receive, and answer with send_tool. An optional shared token may be given as ?token= in the URL.",
    "parameters": {
      "type": "object",
      "properties": {
        "service_ep": {
          "type": "string",
          "description": "The remote service endpoint URL, e.g. http://127.0.0.1:8123/crew/reviewer/v1."
        }
      },
      "required": ["service_ep"]
    }
  },
  {
    "name": "send_tool",
    "description": "Send ONE message on a conversation. The first argument is a conversation endpoint (CEP): either the URL returned by connect_tool or by a REMOTE CONNECTION notice, or a bound placeholder such as '@channel'. Replies are EXPLICIT: a stage that was prompted with a message answers by calling send_tool itself, which is what lets an operator gate replying per stage or toolset.",
    "parameters": {
      "type": "object",
      "properties": {
        "cep": {
          "type": "string",
          "description": "A conversation endpoint URL, or a bound placeholder like '@channel'."
        },
        "message": {
          "type": "string",
          "description": "The message text to deliver to the peer."
        }
      },
      "required": ["cep", "message"]
    }
  },
  {
    "name": "close_tool",
    "description": "Close a conversation politely. The peer is told, and the conversation endpoint is released. Closing is a courtesy rather than a requirement: YAHA reclaims every listener and conversation when the run ends.",
    "parameters": {
      "type": "object",
      "properties": {
        "cep": {
          "type": "string",
          "description": "A conversation endpoint URL, or a bound placeholder like '@channel'."
        }
      },
      "required": ["cep"]
    }
  }
]
```