import requests
import json
from .base import LLMProvider

class OllamaProvider(LLMProvider):
    def generate(self, messages, temperature=0.1, context_size=128000):
        url = f"{self.config.base_url or 'http://localhost:11434'}/api/chat"
        
        payload = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": context_size
            }
        }
        
        try:
            response = requests.post(url, json=payload, timeout=300)
            response.raise_for_status()
            data = response.json()
            
            content = data.get("message", {}).get("content", "")
            usage = {
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
                "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
            }
            return content, usage, None
        except Exception as e:
            return f"Error calling Ollama: {str(e)}", {}, None
