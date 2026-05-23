"""Configuration management via pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Literal


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    debug: bool = False

    # Security
    api_key: str = ""  # Required for all /api/v1/ endpoints

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Database
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "trading_db"
    postgres_user: str = "trader"
    postgres_password: str = "changeme"

    @property
    def database_url(self) -> str:
        return f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 0

    @property
    def redis_url(self) -> str:
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    # Exchange
    binance_testnet: bool = True
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet_url: str = "https://testnet.binance.vision"
    default_symbol: str = "BTC/USDT"

    # AI
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    ai_model: str = "claude-sonnet-4-20250514"
    ai_provider: Literal["anthropic", "openai"] = "anthropic"

    # Trading Defaults
    default_position_size: float = 0.001
    max_daily_loss_pct: float = 5.0
    max_position_size_pct: float = 10.0
    cooldown_minutes_after_loss: int = 60

    # Risk
    max_open_positions: int = 3
    emergency_kill_switch: bool = False

    # Paper Trading
    paper_trading_mode: bool = True  # Swap to False to use real exchange
    paper_initial_capital: float = 10_000.0  # Starting USDT balance
    paper_maker_fee_bps: float = 5.0  # 0.05%
    paper_taker_fee_bps: float = 10.0  # 0.10%
    paper_base_latency_ms: float = 50.0  # Simulated network latency
    paper_adv: float = 1_000_000.0  # Average daily volume (quote currency)
    paper_volatility: float = 1.5  # Initial volatility % for slippage
    paper_enable_downtime: bool = False  # Simulate exchange downtime
    paper_downtime_prob: float = 0.001  # Probability per call
    paper_latency_spike_prob: float = 0.05  # Probability of latency spike

    # Monitoring
    sentry_dsn: str = ""
    prometheus_port: int = 9090


settings = Settings()
