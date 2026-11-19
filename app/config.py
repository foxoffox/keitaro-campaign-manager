from functools import lru_cache

from pydantic import AnyHttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./keitaro.sqlite3"
    keitaro_base_url: AnyHttpUrl | None = None
    keitaro_api_key: SecretStr | None = None

    @field_validator("keitaro_base_url")
    @classmethod
    def validate_keitaro_base_url(cls, url: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if url is not None and (
            url.scheme != "https"
            or url.username is not None
            or url.password is not None
            or url.query is not None
            or url.fragment is not None
            or url.path not in (None, "", "/")
        ):
            raise ValueError("Use the HTTPS origin of the Keitaro tracker")
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
