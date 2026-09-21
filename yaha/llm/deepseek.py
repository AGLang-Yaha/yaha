# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

from openai import OpenAI
import httpx
import os
from .base import LLMProvider

# Explicit request timeouts. The OpenAI SDK default is a 600s read: a
# silently stalled stream then freezes the whole run for ten minutes per
# attempt (the "completely blocked" reports), one frozen retry after another.
# With streaming the read timeout applies per socket read (per chunk), so a
# model that keeps emitting tokens never trips it; a dead connection raises
# within two minutes and yaha's own retry machinery takes over.
_REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0)


class DeepSeekProvider(LLMProvider):
    def __init__(self, model_config):
        super().__init__(model_config)
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        base_url = self.config.base_url or "https://api.deepseek.com"
        if api_key:
            # max_retries=0: yaha's own retry loop (with backoff and user
            # wait/break) handles failures; SDK-level retries would retry
            # timed-out requests invisibly behind the spinner.
            self.client = OpenAI(api_key=api_key, base_url=base_url,
                                 max_retries=0, timeout=_REQUEST_TIMEOUT)
        else:
            self.client = None

    def generate(self, messages, temperature=0.1, context_size=128000):
        if not self.client:
            return "Error: DEEPSEEK_API_KEY not set.", {}, None

        try:
            # DeepSeek expects a standard OpenAI message format.
            # Use streaming: v4 reasoning models can have long time-to-first-
            # token, and a non-streaming request looks like a dead connection
            # while the model thinks (APITimeoutError "Request timed out").
            # With streaming the read timeout (_REQUEST_TIMEOUT) applies per
            # chunk, so a thinking model that keeps emitting tokens never
            # trips it; only a genuinely stalled stream does.
            stream = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                temperature=temperature,
                max_tokens=self.config.max_tokens or 4096,
                stream=True,
                stream_options={"include_usage": True},
            )

            content_parts = []
            reasoning_parts = []
            usage = {}
            for chunk in stream:
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    if getattr(delta, 'reasoning_content', None):
                        reasoning_parts.append(delta.reasoning_content)
                    if delta.content:
                        content_parts.append(delta.content)
                if chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "total_tokens": chunk.usage.total_tokens,
                    }

            content = "".join(content_parts)

            # Prepend reasoning content if available (for deepseek-reasoner / v4)
            if reasoning_parts:
                content = f"<reasoning>\n{''.join(reasoning_parts)}\n</reasoning>\n\n{content}"

            return content, usage, None
        except Exception as e:
            return f"Error calling DeepSeek: {str(e)}", {}, None
