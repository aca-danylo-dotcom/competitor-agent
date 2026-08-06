from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    tavily_api_key: str = ""

    database_url: str = "sqlite:///./competitor_agent.db"

    web_host: str = "127.0.0.1"
    web_port: int = 8090

    discovery_queries: str = "AI agent development agency,разработка ИИ ботов на заказ"
    discovery_manual_daily_limit: int = 5

    # Сколько дней акция может не попадаться на сайте, прежде чем считаем её снятой.
    # Модель переформулирует акции от скана к скану, поэтому мгновенное снятие даёт ложные сигналы.
    promotion_stale_days: int = 7

    @property
    def discovery_query_list(self) -> list[str]:
        return [q.strip() for q in self.discovery_queries.split(",") if q.strip()]


settings = Settings()
