# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import os
import yaml

def load_skill(name, session=None):
    """Load the specified skill instructions into context."""
    if not session:
        return f"Error: Session context required to load skills."

    # Build list of potential roots using session helper
    skill_roots = session.get_skill_roots()
    
    skill_path = None
    for root in skill_roots:
        temp_path = os.path.join(root, name, "SKILL.md")
        if os.path.exists(temp_path):
            skill_path = temp_path
            break
            
    if not skill_path:
        return f"Error: Skill '{name}' not found in search paths: {skill_roots}."
    
    try:
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        instructions = content
        resources = "None specified."
        
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = yaml.safe_load(parts[1])
                instructions = parts[2].strip()
                if "resources" in frontmatter:
                    resources = "\n".join([f"- {res}" for res in frontmatter["resources"]]) if isinstance(frontmatter["resources"], list) else frontmatter["resources"]

        output = "<activated_skill>\n"
        output += f"<location>{os.path.join(os.path.dirname(skill_path))}</location>\n"
        output += "<instructions>\n"
        output += instructions
        output += "\n</instructions>\n"
        output += "<available_resources>\n"
        output += str(resources)
        output += "\n</available_resources>\n"
        output += "</activated_skill>"
        return output
    except Exception as e:
        return f"Error loading skill '{name}': {str(e)}"
