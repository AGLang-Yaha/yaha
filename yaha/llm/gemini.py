from google import genai
from google.genai import types
import os
from .base import LLMProvider

class GeminiProvider(LLMProvider):
    def __init__(self, model_config):
        super().__init__(model_config)
        api_key = os.environ.get("GEMINI_API_KEY")
        if api_key:
            # Set default 300s timeout (300,000 ms)
            self.client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=300_000)
            )
        else:
            # If no API key, initialization might fail or need explicit error later
            self.client = None

    def generate(self, messages, temperature=0.1, context_size=128000):
        if not self.client:
            return "Error: GEMINI_API_KEY not set.", {}

        # Convert messages to Gemini format
        # Gemini-genai uses 'role': 'user'/'model'
        # The first message can be a system instruction in the config
        gemini_messages = []
        system_instruction = ""
        
        for msg in messages:
            if msg["role"] == "system":
                system_instruction += msg["content"] + "\n"
            else:
                role = "user" if msg["role"] == "user" else "model"
                gemini_messages.append(
                    types.Content(
                        role=role,
                        parts=[types.Part(text=msg["content"])]
                    )
                )
        
        try:
            tools = []
            # Use a real model name for the API call, even if the config says 'web-search'
            real_model = self.config.model
            if self.config.model == "web-search" or self.config.name.lower() == "gemini_search":
                tools.append(types.Tool(google_search=types.GoogleSearch()))
                if real_model == "web-search":
                    real_model = "gemini-3.5-flash" # Fallback to a supported model for search
            
            config = types.GenerateContentConfig(
                system_instruction=system_instruction if system_instruction else None,
                temperature=temperature,
                tools=tools if tools else None
            )
            
            # Use generate_content. For multi-turn, we can pass the whole history in contents.
            # In the new SDK, contents is a list of Content objects.
            response = self.client.models.generate_content(
                model=real_model,
                contents=gemini_messages,
                config=config
            )
            
            content = response.text
            
            # Extract grounding metadata if available
            grounding_metadata = None
            if response.candidates and response.candidates[0].grounding_metadata:
                gm = response.candidates[0].grounding_metadata
                grounding_metadata = {
                    "chunks": [],
                    "supports": []
                }
                if gm.grounding_chunks:
                    for chunk in gm.grounding_chunks:
                        if chunk.web:
                            grounding_metadata["chunks"].append({
                                "uri": chunk.web.uri,
                                "title": chunk.web.title
                            })
                if gm.grounding_supports:
                    for support in gm.grounding_supports:
                         grounding_metadata["supports"].append({
                             "segment": support.segment.text if support.segment else "",
                             "indices": support.grounding_chunk_indices
                         })

            # Token usage info is in response.usage_metadata
            usage = {
                "prompt_tokens": response.usage_metadata.prompt_token_count if response.usage_metadata else 0,
                "completion_tokens": response.usage_metadata.candidates_token_count if response.usage_metadata else 0,
                "total_tokens": response.usage_metadata.total_token_count if response.usage_metadata else 0
            }
            return content, usage, grounding_metadata
        except Exception as e:
            return f"Error calling Gemini: {str(e)}", {}, None
