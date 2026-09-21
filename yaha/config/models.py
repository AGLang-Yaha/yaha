# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import os
import re
from ..core.md_loader import load_md_with_includes

class ModelConfig:
    def __init__(self, name, provider, model, base_url=None, max_tokens=None, context_size=None):
        self.name = name
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.max_tokens = int(max_tokens) if max_tokens else None
        self.context_size = int(context_size) if context_size else None

def parse_model_md(file_path):
    if not os.path.exists(file_path):
        return {}

    content = load_md_with_includes(file_path)

    models = {}
    # Match ## name followed by bullets
    sections = re.split(r'^##\s+', content, flags=re.MULTILINE)
    for section in sections[1:]:
        lines = section.strip().split('\n')
        name = lines[0].strip()
        config = {"name": name}
        for line in lines[1:]:
            match = re.match(r'^- \*\*(\w+):\*\*?\s*(.*)', line.strip())
            if match:
                key, value = match.groups()
                config[key.lower()] = value.strip()
        
        models[name] = ModelConfig(
            name=name,
            provider=config.get("provider"),
            model=config.get("model"),
            base_url=config.get("base_url"),
            max_tokens=config.get("max_tokens"),
            context_size=config.get("context_size")
        )
    return models
