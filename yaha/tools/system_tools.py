# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import os
import subprocess
import httpx
from bs4 import BeautifulSoup

from .output_guard import guard_output

def bash_tool(cmdline, access_controller=None, agent_id="Unknown", ui=None,
              max_output_bytes=None):
    """Run a shell command, capped to the caller's remaining-context budget.

    ``max_output_bytes`` is the byte cap computed by the agent tool dispatch
    (see tools/output_guard.py); it is applied to stdout and stderr together,
    since both reach the model as one tool result. ``None`` means the caller
    opted out (direct callers keep the exact legacy behaviour).
    """
    if access_controller:
        authorized, msg = access_controller.authorize_bash(agent_id, cmdline, ui=ui)
        if not authorized:
            return msg
        
        # Apply sandboxing (if supported by OS)
        cmdline = access_controller.get_sandbox_command(cmdline)
            
    try:
        # 300-second timeout per tool as per design
        result = subprocess.run(
            cmdline, shell=True, capture_output=True, text=True, timeout=300
        )
        
        output = result.stdout
        if result.stderr:
            output += "\nError Output:\n" + result.stderr
        
        if not output:
            return "(empty output)"
            
        return guard_output(output, max_output_bytes)
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 300 seconds."
    except Exception as e:
        return f"Error executing bash command: {str(e)}"

def web_fetch_tool(url, prompt=None):
    try:
        # Proxy support can be handled here if needed, 
        # but httpx usually respects environment variables.
        response = httpx.get(url, timeout=30, follow_redirects=True)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Remove script and style elements
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()
            
        text = soup.get_text(separator="\n")
        # Basic cleaning: remove extra whitespace
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = "\n".join(chunk for chunk in chunks if chunk)
        return text
    except Exception as e:
        return f"Error fetching URL: {str(e)}"

def tavily_search_tool(query, max_results=5):
    """Search the web using the Tavily API and return a summary of results."""
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        return "Error: TAVILY_API_KEY environment variable is not set."

    try:
        response = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        results = data.get("results", [])
        if not results:
            return "No results found."

        lines = [f"Tavily search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "No title")
            url = r.get("url", "")
            content = r.get("content", "")
            lines.append(f"{i}. {title}\n   URL: {url}\n   Summary: {content}\n")

        return "\n".join(lines)
    except Exception as e:
        return f"Error calling Tavily API: {str(e)}"
