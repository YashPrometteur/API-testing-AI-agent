from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB_NAME: str = "api_testing_agent"
    REDIS_URL: str = "redis://localhost:6379/0"
    LLM_PROVIDER: str = "openai"
    OPENAI_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""
    COHERE_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"
    OPENROUTER_MODEL: str = "meta-llama/llama-3.3-70b-instruct"
    CHROMA_PERSIST_DIR: str = "./chroma_db"
    MAX_REFLECTION_LOOPS: int = 2
    MIN_CONFIDENCE_THRESHOLD: float = 0.6

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
