import json
import os
from ..core.md_loader import load_md_with_includes, MarkdownLoadError

def load_tool_schema(file_path):
    if not os.path.exists(file_path):
        # Check if it's in .yaha/ as well
        alt_path = os.path.join(".yaha", "tool-schema.md")
        if os.path.exists(alt_path):
            file_path = alt_path
        else:
            return []

    try:
        content = load_md_with_includes(file_path)
        
        # The file might be a pure json block or wrapped in ```json
        if content.strip().startswith("```json"):
            content = content.strip().split("```json")[1].split("```")[0].strip()
        elif content.strip().startswith("```"):
            content = content.strip().split("```")[1].split("```")[0].strip()
        
        return json.loads(content)
    except MarkdownLoadError:
        # Load-directive failures are fatal (missing/circular include, macro error)
        # rather than silently producing an empty tool registry.
        raise
    except Exception as e:
        print(f"Error loading or parsing tool-schema.md: {e}")
        return []
