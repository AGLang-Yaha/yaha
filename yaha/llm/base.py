from abc import ABC, abstractmethod

class LLMProvider(ABC):
    def __init__(self, model_config):
        self.config = model_config

    @abstractmethod
    def generate(self, messages, temperature=0.1, context_size=128000):
        """
        messages: list of {"role": "system"/"user"/"assistant", "content": "..."}
        returns: (response_text, token_usage_dict)
        """
        pass
