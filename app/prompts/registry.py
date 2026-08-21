class PromptRegistry:
    """Registry for managing LLM system and user prompt templates."""
    _prompts = {}

    @classmethod
    def register(cls, name: str, template: str):
        cls._prompts[name] = template

    @classmethod
    def get(cls, name: str) -> str:
        return cls._prompts.get(name, "")
