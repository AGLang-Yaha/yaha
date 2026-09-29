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
    "description": "Loops the prompt chain with a new task prompt. Resets the chain back to the first stage and executes it with the new task, preserving conversation history.",
    "parameters": {
      "type": "object",
      "properties": {
        "new_task": {
          "type": "string",
          "description": "The new task prompt to execute in the next loop."
        }
      },
      "required": ["new_task"]
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
  }
]
```