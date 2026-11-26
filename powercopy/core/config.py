from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os

try:  # 允许在没有 python-dotenv 的极简环境里运行
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

if load_dotenv:
    load_dotenv()


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    """进程级配置；同名的环境变量优先于默认值。"""

    env: str = "development"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout: float = 90.0
    tavily_api_key: str = ""
    data_dir: Path = Path("workspace/data")

    @classmethod
    def from_env(cls) -> "Settings":
        timeout_raw = _env("POWERCOPY_LLM_TIMEOUT", "90")
        try:
            timeout = float(timeout_raw)
        except ValueError:
            timeout = 90.0
        return cls(
            env=_env("POWERCOPY_ENV", "development"),
            llm_base_url=_env("POWERCOPY_LLM_BASE_URL"),
            llm_model=_env("POWERCOPY_LLM_MODEL"),
            llm_api_key=_env("POWERCOPY_LLM_API_KEY"),
            llm_timeout=max(5.0, min(timeout, 600.0)),
            tavily_api_key=_env("POWERCOPY_TAVILY_API_KEY"),
            data_dir=Path(_env("POWERCOPY_DATA_DIR", "workspace/data")),
        )

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_base_url and self.llm_model and self.llm_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
