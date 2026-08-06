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

    discovery_queries: str = "AI agent development agency,custom chatbot development agency"
    discovery_manual_daily_limit: int = 5

    # Рынки, которые нам не интересны: цены оттуда не собираем и сайты не берём
    # под наблюдение. Ориентир — запад и Украина.
    ignored_currencies: str = "RUB"
    ignored_domain_zones: str = ".ru,.рф,.by,.su"

    # Сколько единиц валюты дают за один доллар. Курс задаём руками и правим
    # когда нужно: тянуть его из сети ради витрины, где важен порядок цифр,
    # а не копейки, — лишняя зависимость, которая однажды отвалится молча.
    usd_rates: str = "UAH=41.5,EUR=0.92,GBP=0.79,PLN=3.65"

    @property
    def usd_rate_map(self) -> dict[str, float]:
        rates = {"USD": 1.0}
        for pair in self.usd_rates.split(","):
            if "=" not in pair:
                continue
            code, _, value = pair.partition("=")
            try:
                rates[code.strip().upper()] = float(value)
            except ValueError:
                continue
        return rates

    @property
    def ignored_currency_list(self) -> list[str]:
        return [c.strip().upper() for c in self.ignored_currencies.split(",") if c.strip()]

    @property
    def ignored_zone_list(self) -> list[str]:
        return [z.strip().lower() for z in self.ignored_domain_zones.split(",") if z.strip()]

    # Сколько дней акция может не попадаться на сайте, прежде чем считаем её снятой.
    # Модель переформулирует акции от скана к скану, поэтому мгновенное снятие даёт ложные сигналы.
    promotion_stale_days: int = 7

    @property
    def discovery_query_list(self) -> list[str]:
        return [q.strip() for q in self.discovery_queries.split(",") if q.strip()]


settings = Settings()
