import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# ---------------------------------------------------------------------------
# Data directory – shared between the webapp and the sync service.
# All persistent files (SQLite DB, session cookies, config) live here.
# ---------------------------------------------------------------------------
DATA_DIR: str = os.environ.get("DATA_DIR", "/data")
_CONFIG_PATH: str = os.environ.get("CONFIG_PATH", os.path.join(DATA_DIR, "config.json"))


@dataclass
class ActualBudgetConfig:
    budget_sync_id: str = ""
    account_id: str = ""
    dry_run: bool = True
    server_url: str = "http://localhost:5006"
    server_password: str = ""
    actual_sync_start_date: Optional[str] = None  # ISO date string YYYY-MM-DD
    amazon_category_name: str = ""  # If set, sync only transactions with this category; if empty, sync only uncategorized


@dataclass
class AccountConfig:
    username: str
    password: str
    otp_secret_key: Optional[str] = None
    label: Optional[str] = None
    domain: Optional[str] = None
    tags: List[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.label or self.username


@dataclass
class AppConfig:
    accounts: List[AccountConfig]
    sync_schedule: str
    db_path: str
    lookback_days: int
    log_level: str
    actual_budget: Optional[ActualBudgetConfig] = None

    @classmethod
    def load(cls) -> "AppConfig":
        """Load from config file, or return defaults if the file doesn't exist yet."""
        try:
            with open(_CONFIG_PATH) as f:
                return cls._from_dict(json.load(f))
        except FileNotFoundError:
            return cls._defaults()

    @classmethod
    def _defaults(cls) -> "AppConfig":
        return cls(
            accounts=[],
            sync_schedule="0 2 * * *",
            db_path=os.path.join(DATA_DIR, "orders.db"),
            lookback_days=30,
            log_level="INFO",
            actual_budget=None,
        )

    @classmethod
    def _from_dict(cls, data: dict) -> "AppConfig":
        accounts: List[AccountConfig] = [
            AccountConfig(
                username=a["username"],
                password=a["password"],
                otp_secret_key=a.get("otp_secret_key").replace(" ", "").strip() if a.get("otp_secret_key") else None,
                label=a.get("label") or None,
                domain=a.get("domain") or None,
                tags=a.get("tags") or [],
            )
            for a in data.get("accounts", [])
        ]
        ab = data.get("actual_budget") or {}
        actual_budget: Optional[ActualBudgetConfig] = (
            ActualBudgetConfig(
                budget_sync_id=ab.get("budget_sync_id", ""),
                account_id=ab.get("account_id", ""),
                dry_run=bool(ab.get("dry_run", True)),
                server_url=ab.get("server_url", "http://localhost:5006"),
                server_password=ab.get("server_password", ""),
                actual_sync_start_date=ab.get("actual_sync_start_date") or None,
                amazon_category_name=ab.get("amazon_category_name", ""),
            )
            if ab
            else None
        )
        return cls(
            accounts=accounts,
            sync_schedule=data.get("sync_schedule", "0 2 * * *"),
            db_path=data.get("db_path", os.path.join(DATA_DIR, "orders.db")),
            lookback_days=max(1, int(data.get("lookback_days", 30))),
            log_level=str(data.get("log_level", "INFO")).upper(),
            actual_budget=actual_budget,
        )

    def to_dict(self) -> dict:
        ab = (
            {
                "budget_sync_id": self.actual_budget.budget_sync_id,
                "account_id": self.actual_budget.account_id,
                "dry_run": self.actual_budget.dry_run,
                "server_url": self.actual_budget.server_url,
                "server_password": self.actual_budget.server_password,
                "actual_sync_start_date": self.actual_budget.actual_sync_start_date,
                "amazon_category_name": self.actual_budget.amazon_category_name,
            }
            if self.actual_budget is not None
            else None
        )
        return {
            "sync_schedule": self.sync_schedule,
            "db_path": self.db_path,
            "lookback_days": self.lookback_days,
            "log_level": self.log_level,
            "actual_budget": ab,
            "accounts": [
                {
                    "username": a.username,
                    "password": a.password,
                    "otp_secret_key": a.otp_secret_key,
                    "label": a.label,
                    "domain": a.domain,
                    "tags": a.tags,
                }
                for a in self.accounts
            ],
        }

    def save(self) -> None:
        Path(_CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(_CONFIG_PATH, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
