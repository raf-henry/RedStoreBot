"""RedStore Discord bridge.

Runs the web API/OAuth2 flow and the Discord bot in the same process.
The website should use the OAuth endpoints for user login and the /api/v1
endpoints for server-to-server operations.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Awaitable, Callable, Literal, Sequence
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import discord
import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from discord.ext import commands
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from pydantic import BaseModel, Field

load_dotenv()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("redstore")
DEFAULT_DISCORD_GUILD_ID = "1535813395394330814"
DEFAULT_PUBLIC_SITE_URL = "https://redbuxx.com.br"
PUBLIC_SITE_HOSTS = {"redbuxx.com.br", "www.redbuxx.com.br"}
LIVE_COMMAND_OWNER_ID = 385106984522743819
LIVE_ANNOUNCEMENT_CHANNEL_ID = 1541592129644535828
TIKTOK_PROFILE_URL = "https://www.tiktok.com/@.redlocker"
EXCLUDED_DEPOSIT_RANKING_IDS = frozenset(
    {
        "385106984522743819",
        "418446977349451777",
    }
)


def configured_site_url() -> str:
    """Return the public site URL without trusting a stale production host."""
    configured = os.getenv("SITE_URL", "").strip().rstrip("/")
    is_production = os.getenv("ENVIRONMENT", "development").lower() in {"production", "prod"}

    if not configured:
        return DEFAULT_PUBLIC_SITE_URL if is_production else "http://localhost:3000"

    if is_production:
        parsed = urlsplit(configured)
        if parsed.scheme != "https" or parsed.hostname not in PUBLIC_SITE_HOSTS:
            logger.warning(
                "SITE_URL de produção inválida (%s); usando %s.",
                configured,
                DEFAULT_PUBLIC_SITE_URL,
            )
            return DEFAULT_PUBLIC_SITE_URL

    return configured


def parse_id_list(value: str) -> tuple[int, ...]:
    ids: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if item.isdigit() and int(item) > 0:
            ids.append(int(item))
    return tuple(dict.fromkeys(ids))


def parse_amount(value: str | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        amount = value
    else:
        normalized = value.strip().upper().replace("R$", "").replace(" ", "")
        if "," in normalized and "." in normalized:
            normalized = normalized.replace(".", "").replace(",", ".")
        else:
            normalized = normalized.replace(",", ".")
        amount = Decimal(normalized)
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Os valores precisam ser maiores que zero.")
    return amount


def calculate_robux(
    price_per_thousand: str,
    available_money: str,
) -> tuple[int, Decimal, Decimal, Decimal, Decimal]:
    """Calcula Robux proporcionalmente ao preço informado para cada 1.000."""
    price = parse_amount(price_per_thousand)
    money = parse_amount(available_money)
    robux = int((money / price * Decimal("1000")).to_integral_value(rounding="ROUND_DOWN"))
    spent = (Decimal(robux) * price / Decimal("1000")).quantize(
        Decimal("0.01"), rounding="ROUND_DOWN"
    )
    return robux, price, money, spent, money - spent


def normalize_currency(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"real", "reais", "brl", "r$"}:
        return "BRL"
    if normalized in {"dolar", "dólar", "dólares", "usd", "$"}:
        return "USD"
    raise ValueError("Moeda inválida.")


def format_currency(value: Decimal, currency: str) -> str:
    if currency == "USD":
        return f"US$ {value:,.2f}"
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def convert_currency(
    amount: Decimal,
    from_currency: str,
    to_currency: str,
    buy_rate: Decimal,
    sell_rate: Decimal,
) -> Decimal:
    if from_currency == to_currency:
        return amount
    if from_currency == "BRL" and to_currency == "USD":
        return amount / sell_rate
    return amount * buy_rate


def format_robux(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def read_role_id(name: str, default: str, *legacy_names: str) -> int:
    """Reads a role ID, keeping compatibility with the previous tier names."""
    for env_name in (name, *legacy_names):
        value = os.getenv(env_name)
        if value is not None:
            return int(value or 0)
    return int(default)


@dataclass(frozen=True)
class Settings:
    environment: str = os.getenv("ENVIRONMENT", "development").lower()
    discord_client_id: str = os.getenv("DISCORD_CLIENT_ID", "")
    discord_client_secret: str = os.getenv("DISCORD_CLIENT_SECRET", "")
    discord_bot_token: str = os.getenv("DISCORD_BOT_TOKEN", "")
    discord_guild_id: int = int(os.getenv("DISCORD_GUILD_ID", DEFAULT_DISCORD_GUILD_ID) or 0)
    discord_command_guild_id: int = int(
        os.getenv("DISCORD_COMMAND_GUILD_ID", DEFAULT_DISCORD_GUILD_ID) or 0
    )
    oauth_redirect_uri: str = os.getenv(
        "OAUTH_REDIRECT_URI",
        "http://localhost:8000/auth/discord/callback",
    )
    site_url: str = configured_site_url()
    redstore_api_url: str = os.getenv("REDSTORE_API_URL", "http://localhost:8080")
    redstore_bridge_api_key: str = os.getenv(
        "REDSTORE_BRIDGE_API_KEY",
        os.getenv("INTERNAL_API_KEY", ""),
    )
    deposit_notification_discord_id: int = int(
        os.getenv("DEPOSIT_NOTIFICATION_DISCORD_ID", "0") or 0
    )
    session_secret: str = os.getenv("SESSION_SECRET", "dev-only-change-me")
    internal_api_key: str = os.getenv("INTERNAL_API_KEY", "dev-only-change-me")
    database_path: str = os.getenv("DATABASE_PATH", "redstore.db")
    cookie_secure: bool = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    # O login não deve exigir que o usuário esteja em um servidor do Discord.
    # Mantido fixo em False para evitar que o Render reative essa restrição por ambiente.
    require_guild_membership: bool = False
    ticket_enabled: bool = os.getenv("TICKET_ENABLED", "true").lower() != "false"
    ticket_category_id: int = int(os.getenv("TICKET_CATEGORY_ID", "0") or 0)
    ticket_support_role_ids: tuple[int, ...] = parse_id_list(
        os.getenv("TICKET_SUPPORT_ROLE_IDS", "")
    )
    ticket_master_role_id: int = int(os.getenv("TICKET_MASTER_ROLE_ID", "0") or 0)
    ticket_log_channel_id: int = int(os.getenv("TICKET_LOG_CHANNEL_ID", "0") or 0)
    ticket_close_delay_seconds: int = max(
        0, int(os.getenv("TICKET_CLOSE_DELAY_SECONDS", "10") or 10)
    )
    deliverer_role_id: int = int(os.getenv("DELIVERER_ROLE_ID", "0") or 0)
    deliverer_role_name: str = os.getenv("DELIVERER_ROLE_NAME", "Entregador")
    proof_channel_id: int = int(os.getenv("PROOF_CHANNEL_ID", "0") or 0)
    order_notification_channel_id: int = int(
        (os.getenv("ORDER_NOTIFICATION_CHANNEL_ID") or os.getenv("PROOF_CHANNEL_ID", "0")) or 0
    )
    deposit_plebeu_role_id: int = read_role_id(
        "DEPOSIT_PLEBEU_ROLE_ID", "1540196431875276820", "DEPOSIT_BRONZE_ROLE_ID"
    )
    deposit_campones_role_id: int = read_role_id(
        "DEPOSIT_CAMPONES_ROLE_ID", "1540196669801635910", "DEPOSIT_PRATA_ROLE_ID"
    )
    deposit_artesao_role_id: int = read_role_id(
        "DEPOSIT_ARTESAO_ROLE_ID", "1540196877193060372", "DEPOSIT_OURO_ROLE_ID"
    )
    deposit_mercador_role_id: int = read_role_id(
        "DEPOSIT_MERCADOR_ROLE_ID", "1540349040246525962", "DEPOSIT_ESMERALDA_ROLE_ID"
    )
    deposit_nobre_role_id: int = read_role_id(
        "DEPOSIT_NOBRE_ROLE_ID", "1540348193076682802", "DEPOSIT_DIAMANTE_ROLE_ID"
    )
    deposit_escudeiro_role_id: int = read_role_id(
        "DEPOSIT_ESCUDEIRO_ROLE_ID", "1540351414377779260"
    )
    deposit_cavaleiro_role_id: int = read_role_id(
        "DEPOSIT_CAVALEIRO_ROLE_ID", "1540351342512705587"
    )
    deposit_barao_role_id: int = read_role_id(
        "DEPOSIT_BARAO_ROLE_ID", "1540351462918463508"
    )
    deposit_visconde_role_id: int = read_role_id(
        "DEPOSIT_VISCONDE_ROLE_ID", "1540353613145051187"
    )
    deposit_conde_role_id: int = read_role_id(
        "DEPOSIT_CONDE_ROLE_ID", "1540353652827357264"
    )
    deposit_marques_role_id: int = read_role_id(
        "DEPOSIT_MARQUES_ROLE_ID", "1540353694707744848"
    )
    deposit_duque_role_id: int = read_role_id(
        "DEPOSIT_DUQUE_ROLE_ID", "1540353747107188756"
    )
    deposit_grao_duque_role_id: int = read_role_id(
        "DEPOSIT_GRAO_DUQUE_ROLE_ID", "1540353832179990649"
    )
    deposit_principe_role_id: int = read_role_id(
        "DEPOSIT_PRINCIPE_ROLE_ID", "1540196429950091396"
    )
    deposit_rei_role_id: int = read_role_id(
        "DEPOSIT_REI_ROLE_ID", "1540353925302059028"
    )
    deposit_arquiduque_role_id: int = read_role_id(
        "DEPOSIT_ARQUIDUQUE_ROLE_ID", "1540353961863811184"
    )
    deposit_imperador_role_id: int = read_role_id(
        "DEPOSIT_IMPERADOR_ROLE_ID", "1540354008919703613"
    )
    deposit_soberano_imperial_role_id: int = read_role_id(
        "DEPOSIT_SOBERANO_IMPERIAL_ROLE_ID", "1540354059830042625"
    )
    deposit_imperador_supremo_role_id: int = read_role_id(
        "DEPOSIT_IMPERADOR_SUPREMO_ROLE_ID", "1540354059838431242"
    )
    deposit_lenda_da_coroa_role_id: int = read_role_id(
        "DEPOSIT_LENDA_DA_COROA_ROLE_ID", "1540354156785700975"
    )
    deposit_monarca_eterno_role_id: int = read_role_id(
        "DEPOSIT_MONARCA_ETERNO_ROLE_ID", "1540354251199479839"
    )
    deposit_role_sync_interval_seconds: int = max(
        60, int(os.getenv("DEPOSIT_ROLE_SYNC_INTERVAL_SECONDS", "300") or 300)
    )
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
        if origin.strip()
    )


settings = Settings()


DEPOSIT_TIERS: tuple[tuple[str, Decimal, int], ...] = (
    ("Plebeu", Decimal("1.00"), settings.deposit_plebeu_role_id),
    ("Camponês", Decimal("20.00"), settings.deposit_campones_role_id),
    ("Artesão", Decimal("50.00"), settings.deposit_artesao_role_id),
    ("Mercador", Decimal("80.00"), settings.deposit_mercador_role_id),
    ("Nobre", Decimal("120.00"), settings.deposit_nobre_role_id),
    ("Escudeiro", Decimal("160.00"), settings.deposit_escudeiro_role_id),
    ("Cavaleiro", Decimal("210.00"), settings.deposit_cavaleiro_role_id),
    ("Barão", Decimal("260.00"), settings.deposit_barao_role_id),
    ("Visconde", Decimal("320.00"), settings.deposit_visconde_role_id),
    ("Conde", Decimal("380.00"), settings.deposit_conde_role_id),
    ("Marquês", Decimal("450.00"), settings.deposit_marques_role_id),
    ("Duque", Decimal("550.00"), settings.deposit_duque_role_id),
    ("Grão-Duque", Decimal("650.00"), settings.deposit_grao_duque_role_id),
    ("Príncipe", Decimal("800.00"), settings.deposit_principe_role_id),
    ("Rei", Decimal("1000.00"), settings.deposit_rei_role_id),
    ("Arquiduque", Decimal("1250.00"), settings.deposit_arquiduque_role_id),
    ("Imperador", Decimal("1500.00"), settings.deposit_imperador_role_id),
    ("Soberano Imperial", Decimal("2000.00"), settings.deposit_soberano_imperial_role_id),
    ("Imperador Supremo", Decimal("2500.00"), settings.deposit_imperador_supremo_role_id),
    ("Lenda da Coroa", Decimal("3500.00"), settings.deposit_lenda_da_coroa_role_id),
    ("Monarca Eterno", Decimal("5000.00"), settings.deposit_monarca_eterno_role_id),
)


def deposit_tier_for_amount(amount: Decimal) -> tuple[str | None, Decimal | None, int | None]:
    """Returns the highest deposit tier reached by the confirmed total."""
    for tier_name, minimum_amount, role_id in reversed(DEPOSIT_TIERS):
        if amount >= minimum_amount:
            return tier_name, minimum_amount, role_id
    return None, None, None


def validate_configuration() -> None:
    """Fail fast for unsafe/incomplete production configuration."""
    if settings.environment not in {"production", "prod"}:
        return
    required = {
        "DISCORD_CLIENT_ID": settings.discord_client_id,
        "DISCORD_CLIENT_SECRET": settings.discord_client_secret,
        "DISCORD_BOT_TOKEN": settings.discord_bot_token,
        "DISCORD_GUILD_ID": settings.discord_guild_id,
        "REDSTORE_BRIDGE_API_KEY": settings.redstore_bridge_api_key,
        "DEPOSIT_NOTIFICATION_DISCORD_ID": settings.deposit_notification_discord_id,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Configuração de produção incompleta: {', '.join(missing)}")
    if settings.session_secret in {"", "dev-only-change-me"} or len(settings.session_secret) < 32:
        raise RuntimeError("SESSION_SECRET precisa ter pelo menos 32 caracteres em produção")
    if settings.internal_api_key in {"", "dev-only-change-me"} or len(settings.internal_api_key) < 32:
        raise RuntimeError("INTERNAL_API_KEY precisa ter pelo menos 32 caracteres em produção")
    if not settings.cookie_secure:
        raise RuntimeError("COOKIE_SECURE=true é obrigatório em produção")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class UserStore:
    """Small SQLite store for the Discord account linked to a RedStore user."""

    def __init__(self, path: str) -> None:
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_users (
                discord_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                global_name TEXT,
                avatar_hash TEXT,
                email TEXT,
                last_login_at TEXT NOT NULL
            )
            """
        )
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(discord_users)")
        }
        if "email" not in columns:
            self.connection.execute("ALTER TABLE discord_users ADD COLUMN email TEXT")
        self.connection.commit()

    def upsert(self, user: dict[str, Any]) -> dict[str, Any]:
        self.connection.execute(
            """
            INSERT INTO discord_users
                (discord_id, username, global_name, avatar_hash, email, last_login_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                username = excluded.username,
                global_name = excluded.global_name,
                avatar_hash = excluded.avatar_hash,
                email = excluded.email,
                last_login_at = excluded.last_login_at
            """,
            (
                str(user["id"]),
                user.get("username", ""),
                user.get("global_name"),
                user.get("avatar"),
                user.get("email"),
                utc_now(),
            ),
        )
        self.connection.commit()
        return self.get(str(user["id"])) or {}

    def get(self, discord_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM discord_users WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        self.connection.close()


class TicketStore:
    """Persists ticket metadata without exposing it in the Discord channel topic."""

    def __init__(self, path: str) -> None:
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_tickets (
                channel_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                claimed_by TEXT,
                claimed_at TEXT,
                mentions INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'open'
            )
            """
        )
        self.connection.commit()

    def get(self, channel_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM discord_tickets WHERE channel_id = ?",
            (str(channel_id),),
        ).fetchone()
        if not row:
            return None
        return self._row_to_data(row)

    def find_open_by_user(self, user_id: int) -> int | None:
        row = self.connection.execute(
            """
            SELECT channel_id
            FROM discord_tickets
            WHERE user_id = ? AND state = 'open'
            ORDER BY opened_at DESC
            LIMIT 1
            """,
            (str(user_id),),
        ).fetchone()
        return int(row["channel_id"]) if row else None

    def save(self, channel_id: int, data: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO discord_tickets
                (channel_id, user_id, opened_at, claimed_by, claimed_at, mentions, state)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                user_id = excluded.user_id,
                opened_at = excluded.opened_at,
                claimed_by = excluded.claimed_by,
                claimed_at = excluded.claimed_at,
                mentions = excluded.mentions,
                state = excluded.state
            """,
            (
                str(channel_id),
                str(data["user_id"]),
                data["opened_at"],
                str(data["claimed_by"]) if data.get("claimed_by") else None,
                data.get("claimed_at"),
                int(data.get("mentions", 0)),
                data.get("state", "open"),
            ),
        )
        self.connection.commit()

    def delete(self, channel_id: int) -> None:
        self.connection.execute(
            "DELETE FROM discord_tickets WHERE channel_id = ?", (str(channel_id),)
        )
        self.connection.commit()

    @staticmethod
    def _row_to_data(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "user_id": int(row["user_id"]),
            "opened_at": row["opened_at"],
            "claimed_by": int(row["claimed_by"]) if row["claimed_by"] else None,
            "claimed_at": row["claimed_at"],
            "mentions": int(row["mentions"] or 0),
            "state": row["state"],
        }

    def close(self) -> None:
        self.connection.close()


class ProofStore:
    """Persists the automatic sale number used by delivery proofs."""

    def __init__(self, path: str) -> None:
        self.connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_proof_sequence (
                sequence_name TEXT PRIMARY KEY,
                next_number INTEGER NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO discord_proof_sequence (sequence_name, next_number)
            VALUES ('delivery_proof', 1)
            """
        )
        self.connection.commit()
        self._lock = threading.Lock()

    def next_sale_number(self) -> int:
        """Reserve and return the next sale number without duplicates."""
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.connection.execute(
                    """
                    SELECT next_number
                    FROM discord_proof_sequence
                    WHERE sequence_name = 'delivery_proof'
                    """
                ).fetchone()
                number = int(row["next_number"]) if row else 1
                self.connection.execute(
                    """
                    INSERT INTO discord_proof_sequence (sequence_name, next_number)
                    VALUES ('delivery_proof', ?)
                    ON CONFLICT(sequence_name) DO UPDATE SET next_number = excluded.next_number
                    """,
                    (number + 1,),
                )
                self.connection.commit()
                return number
            except Exception:
                self.connection.rollback()
                raise

    def close(self) -> None:
        self.connection.close()


class OAuthClient:
    DISCORD_API = "https://discord.com/api/v10"

    @staticmethod
    def authorization_url(state: str) -> str:
        query = urlencode(
            {
                "client_id": settings.discord_client_id,
                "redirect_uri": settings.oauth_redirect_uri,
                "response_type": "code",
                "scope": "identify email",
                "state": state,
                "prompt": "consent",
            }
        )
        return f"https://discord.com/oauth2/authorize?{query}"

    async def exchange_code(self, code: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15) as client:
            token_response = await client.post(
                f"{self.DISCORD_API}/oauth2/token",
                data={
                    "client_id": settings.discord_client_id,
                    "client_secret": settings.discord_client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.oauth_redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if token_response.is_error:
                logger.warning("Discord OAuth token exchange failed: %s", token_response.text)
                raise HTTPException(status_code=400, detail="Código OAuth inválido ou expirado")

            access_token = token_response.json().get("access_token")
            if not access_token:
                raise HTTPException(status_code=400, detail="Discord não retornou um token")

            user_response = await client.get(
                f"{self.DISCORD_API}/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if user_response.is_error:
                raise HTTPException(status_code=400, detail="Não foi possível obter a conta Discord")
            user = user_response.json()
            if not user.get("email"):
                raise HTTPException(
                    status_code=400,
                    detail="Não foi possível obter o e-mail da conta Discord",
                )
            return user

    async def provision_redstore_user(
        self,
        discord_user: dict[str, Any],
        consent: dict[str, Any],
    ) -> dict[str, Any]:
        if not settings.redstore_bridge_api_key:
            raise HTTPException(status_code=503, detail="Chave do bridge com o RedStore não configurada")
        if not discord_user.get("verified"):
            raise HTTPException(status_code=400, detail="A conta Discord precisa ter um e-mail verificado")

        avatar_url = (
            f"https://cdn.discordapp.com/avatars/{discord_user['id']}/{discord_user['avatar']}.png?size=256"
            if discord_user.get("avatar")
            else None
        )
        flow = consent.get("flow", "register")
        payload = {
            "discordId": str(discord_user["id"]),
            "discordUsername": discord_user.get("username", "discord_user"),
            "globalName": discord_user.get("global_name"),
            "email": discord_user["email"],
            "emailVerified": bool(discord_user.get("verified")),
            "avatarUrl": avatar_url,
        }
        endpoint = "/api/auth/discord"
        if flow == "link":
            link_token = consent.get("linkToken")
            if not link_token:
                raise HTTPException(status_code=400, detail="Código para vincular o Discord não informado")
            endpoint = "/api/auth/discord/link"
            payload["linkToken"] = link_token
        else:
            payload.update(consent)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    f"{settings.redstore_api_url.rstrip('/')}{endpoint}",
                    json=payload,
                    headers={"X-Discord-Bridge-Key": settings.redstore_bridge_api_key},
                )
        except httpx.RequestError as exc:
            logger.exception("Não foi possível acessar a API principal do RedStore")
            raise HTTPException(
                status_code=502,
                detail="Aguarde um instante, o sistema está processando sua solicitação.",
            ) from exc
        if response.is_error:
            backend_message = "Não foi possível registrar a conta no RedStore"
            try:
                backend_message = response.json().get("message") or response.json().get("detail") or backend_message
            except (ValueError, TypeError):
                pass
            logger.warning("Provisionamento Discord recusado pelo RedStore: %s", response.text)
            raise HTTPException(
                status_code=response.status_code if 400 <= response.status_code < 500 else 502,
                detail=backend_message,
            )
        return response.json()


class TicketPanelView(discord.ui.View):
    def __init__(self, bridge: "DiscordBridge") -> None:
        super().__init__(timeout=None)
        self.bridge = bridge

    @discord.ui.button(
        label="Abrir Ticket",
        style=discord.ButtonStyle.green,
        emoji="🎫",
        custom_id="redstore:tickets:open",
    )
    async def open_ticket(
        self,
        button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        await self.bridge.open_ticket(interaction)


class TicketView(discord.ui.View):
    def __init__(self, bridge: "DiscordBridge") -> None:
        super().__init__(timeout=None)
        self.bridge = bridge

    @discord.ui.button(
        label="Assumir",
        style=discord.ButtonStyle.green,
        emoji="✅",
        custom_id="redstore:tickets:claim",
    )
    async def claim_ticket(
        self,
        button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        await self.bridge.claim_ticket(interaction)

    @discord.ui.button(
        label="Mencionar",
        style=discord.ButtonStyle.blurple,
        emoji="📢",
        custom_id="redstore:tickets:mention",
    )
    async def mention_ticket(
        self,
        button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        await self.bridge.mention_ticket(interaction)

    @discord.ui.button(
        label="Renomear",
        style=discord.ButtonStyle.gray,
        emoji="✏️",
        custom_id="redstore:tickets:rename",
    )
    async def rename_ticket(
        self,
        button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        await self.bridge.show_rename_ticket_modal(interaction)

    @discord.ui.button(
        label="Fechar",
        style=discord.ButtonStyle.red,
        emoji="🔒",
        custom_id="redstore:tickets:close",
    )
    async def close_ticket(
        self,
        button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        await self.bridge.close_ticket(interaction)


class TicketRenameModal(discord.ui.Modal):
    def __init__(self, bridge: "DiscordBridge") -> None:
        super().__init__(title="Renomear ticket")
        self.bridge = bridge
        self.name_input = discord.ui.InputText(
            label="Novo nome do canal",
            placeholder="ex.: suporte-pedido",
            min_length=2,
            max_length=70,
        )
        self.add_item(self.name_input)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.bridge.rename_ticket(interaction, self.name_input.value)


class DepositReviewView(discord.ui.View):
    def __init__(self, bridge: "DiscordBridge", deposit_id: int) -> None:
        super().__init__(timeout=None)
        self.bridge = bridge
        self.deposit_id = deposit_id
        approve = discord.ui.Button(
            label="Aprovar saldo", style=discord.ButtonStyle.green,
            custom_id=f"redstore:deposit:{deposit_id}:approve",
        )
        reject = discord.ui.Button(
            label="Rejeitar", style=discord.ButtonStyle.red,
            custom_id=f"redstore:deposit:{deposit_id}:reject",
        )
        approve.callback = self.approve
        reject.callback = self.reject
        self.add_item(approve)
        self.add_item(reject)

    async def approve(self, interaction: discord.Interaction) -> None:
        await self.bridge.review_deposit(interaction, self.deposit_id, "approve", self)

    async def reject(self, interaction: discord.Interaction) -> None:
        await self.bridge.review_deposit(interaction, self.deposit_id, "reject", self)


class DiscordBridge:
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        self.bot = commands.Bot(command_prefix="!", intents=intents)
        self.ready = threading.Event()
        self._bot_loop: asyncio.AbstractEventLoop | None = None
        self._bot_thread: threading.Thread | None = None
        self._bot_started = threading.Event()
        self._bot_stopped = threading.Event()
        self._connected_guild_id: int | None = None
        self._ticket_closing: set[int] = set()
        self._ticket_views_registered = False
        self._legacy_ticket_topics_migrated = False
        self._proof_number_lock = asyncio.Lock()
        self._deposit_review_in_flight: set[int] = set()
        self._deposit_role_sync_task: asyncio.Task[None] | None = None
        self._application_commands_synced = False
        self._ptax_cache: tuple[float, Decimal, Decimal, str] | None = None
        self._register_commands()

    async def _get_ptax_usd_brl(self) -> tuple[Decimal, Decimal, str]:
        """Busca a PTAX mais recente: compra, venda e data da cotação."""
        now_monotonic = time.monotonic()
        if self._ptax_cache and now_monotonic - self._ptax_cache[0] < 900:
            return self._ptax_cache[1:]

        today = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
        start_date = today - timedelta(days=7)
        endpoint = (
            "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
            "CotacaoMoedaPeriodo"
            f"(moeda='USD',dataInicial='{start_date:%m-%d-%Y}',dataFinal='{today:%m-%d-%Y}')"
            "?$format=json"
        )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(endpoint)
                response.raise_for_status()
                rows = response.json().get("value", [])
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Não foi possível consultar a cotação PTAX agora.") from exc

        if not rows:
            raise ValueError("A cotação PTAX ainda não está disponível.")
        closing_rows = [
            row for row in rows
            if "fechamento" in str(row.get("tipoBoletim", "")).lower()
        ]
        selected = max(
            closing_rows or rows,
            key=lambda row: row.get("dataHoraCotacao", ""),
        )
        try:
            buy_rate = Decimal(str(selected["cotacaoCompra"]))
            sell_rate = Decimal(str(selected["cotacaoVenda"]))
            quote_date = str(selected["dataHoraCotacao"])[:10]
        except (KeyError, ArithmeticError, TypeError) as exc:
            raise ValueError("A resposta da cotação PTAX é inválida.") from exc
        if buy_rate <= 0 or sell_rate <= 0:
            raise ValueError("A cotação PTAX retornou um valor inválido.")

        self._ptax_cache = (time.monotonic(), buy_rate, sell_rate, quote_date)
        return buy_rate, sell_rate, quote_date

    async def _calculate_robux_quote(
        self,
        price_per_thousand: str,
        available_money: str,
        price_currency: str,
        money_currency: str,
    ) -> dict[str, Any]:
        price_currency = normalize_currency(price_currency)
        money_currency = normalize_currency(money_currency)
        price = parse_amount(price_per_thousand)
        available = parse_amount(available_money)
        calculation_budget = available
        ptax: tuple[Decimal, str, str] | None = None

        if price_currency != money_currency:
            buy_rate, sell_rate, quote_date = await self._get_ptax_usd_brl()
            calculation_budget = convert_currency(
                available,
                money_currency,
                price_currency,
                buy_rate,
                sell_rate,
            )
            rate = sell_rate if money_currency == "BRL" else buy_rate
            rate_type = "venda" if money_currency == "BRL" else "compra"
            ptax = rate, rate_type, quote_date

        robux, _, _, spent, remainder = calculate_robux(price, calculation_budget)
        return {
            "robux": robux,
            "price": price,
            "available": available,
            "calculation_budget": calculation_budget,
            "spent": spent,
            "remainder": remainder,
            "price_currency": price_currency,
            "money_currency": money_currency,
            "ptax": ptax,
        }

    async def _fetch_confirmed_deposit_amount(self, discord_id: str) -> Decimal:
        if not settings.redstore_bridge_api_key:
            raise RuntimeError("REDSTORE_BRIDGE_API_KEY não está configurada")
        endpoint = (
            f"{settings.redstore_api_url.rstrip('/')}/api/internal/discord/users/"
            f"{discord_id}/deposit-summary"
        )
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                endpoint,
                headers={"X-Discord-Bridge-Key": settings.redstore_bridge_api_key},
            )
            response.raise_for_status()
            payload = response.json()
        try:
            amount = Decimal(str(payload.get("confirmedAmount", "0")))
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise RuntimeError("O resumo de depósitos retornado pelo RedStore é inválido") from exc
        if not amount.is_finite() or amount < 0:
            raise RuntimeError("O resumo de depósitos retornado pelo RedStore é inválido")
        return amount

    async def _fetch_deposit_summary(self, deposit_id: int) -> tuple[str | None, Decimal]:
        if not settings.redstore_bridge_api_key:
            raise RuntimeError("REDSTORE_BRIDGE_API_KEY não está configurada")
        endpoint = (
            f"{settings.redstore_api_url.rstrip('/')}/api/internal/discord/deposits/"
            f"{deposit_id}/deposit-summary"
        )
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                endpoint,
                headers={"X-Discord-Bridge-Key": settings.redstore_bridge_api_key},
            )
            response.raise_for_status()
            payload = response.json()
        try:
            amount = Decimal(str(payload.get("confirmedAmount", "0")))
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise RuntimeError("O resumo de depósitos retornado pelo RedStore é inválido") from exc
        if not amount.is_finite() or amount < 0:
            raise RuntimeError("O resumo de depósitos retornado pelo RedStore é inválido")
        discord_id = payload.get("discordId")
        return (str(discord_id) if discord_id else None), amount

    async def _fetch_all_deposit_summaries(self) -> list[tuple[str, Decimal]]:
        if not settings.redstore_bridge_api_key:
            raise RuntimeError("REDSTORE_BRIDGE_API_KEY não está configurada")
        endpoint = f"{settings.redstore_api_url.rstrip('/')}/api/internal/discord/deposit-summaries"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                endpoint,
                headers={"X-Discord-Bridge-Key": settings.redstore_bridge_api_key},
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("A lista de depósitos retornada pelo RedStore é inválida")

        summaries: list[tuple[str, Decimal]] = []
        for item in payload:
            if not isinstance(item, dict) or not item.get("discordId"):
                continue
            try:
                amount = Decimal(str(item.get("confirmedAmount", "0")))
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise RuntimeError("A lista de depósitos retornada pelo RedStore é inválida") from exc
            if not amount.is_finite() or amount < 0:
                raise RuntimeError("A lista de depósitos retornada pelo RedStore é inválida")
            summaries.append((str(item["discordId"]), amount))
        return summaries

    async def _sync_deposit_roles_on_bot_loop(
        self,
        discord_id: str,
        confirmed_amount: Decimal | None = None,
        member: discord.Member | None = None,
        allow_member_fetch: bool = True,
    ) -> dict[str, Any]:
        try:
            numeric_discord_id = int(discord_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("ID do Discord inválido") from exc
        if numeric_discord_id <= 0:
            raise RuntimeError("ID do Discord inválido")

        guild = self._guild_on_bot_loop()
        if member is not None and member.guild.id != guild.id:
            member = None
        if member is None:
            member = guild.get_member(numeric_discord_id)
        if member is None and allow_member_fetch:
            try:
                member = await asyncio.wait_for(
                    guild.fetch_member(numeric_discord_id),
                    timeout=8,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError("Discord demorou ao consultar o membro") from exc
            except discord.NotFound:
                member = None
            except discord.HTTPException as exc:
                raise RuntimeError("Discord não respondeu ao consultar o membro") from exc
        if member is None:
            return {
                "discord_id": str(numeric_discord_id),
                "is_member": False,
                "confirmed_amount": confirmed_amount or Decimal("0"),
                "tier": None,
            }

        amount = confirmed_amount
        if amount is None:
            amount = await self._fetch_confirmed_deposit_amount(str(numeric_discord_id))
        tier_name, _, target_role_id = deposit_tier_for_amount(amount)
        configured_role_ids = tuple(role_id for _, _, role_id in DEPOSIT_TIERS)

        for role_id in dict.fromkeys(role_id for role_id in configured_role_ids if role_id > 0):
            role = guild.get_role(role_id)
            if role is None:
                logger.warning("Cargo de depósito %s não foi encontrado no servidor", role_id)
                continue
            if not guild.me or role >= guild.me.top_role:
                logger.error("O bot não pode gerenciar o cargo de depósito %s", role_id)
                continue
            should_have_role = role_id == target_role_id
            has_role = role in member.roles
            try:
                if should_have_role and not has_role:
                    await asyncio.wait_for(
                        member.add_roles(role, reason="Sincronização do ranking de depósitos confirmados"),
                        timeout=8,
                    )
                elif not should_have_role and has_role:
                    await asyncio.wait_for(
                        member.remove_roles(role, reason="Atualização do ranking de depósitos confirmados"),
                        timeout=8,
                    )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(f"Discord demorou ao alterar o cargo de depósito {role_id}") from exc
            except discord.Forbidden as exc:
                raise RuntimeError(f"Discord recusou a alteração do cargo de depósito {role_id}") from exc
            except discord.HTTPException as exc:
                raise RuntimeError("Discord não respondeu à alteração do cargo de depósito") from exc

        return {
            "discord_id": str(numeric_discord_id),
            "is_member": True,
            "confirmed_amount": amount,
            "tier": tier_name,
            "role_id": target_role_id,
        }

    async def _deposit_ranking_message(self, member: discord.Member) -> str:
        result = await self._sync_deposit_roles_on_bot_loop(
            str(member.id),
            member=member,
        )
        amount = result["confirmed_amount"]
        tier_name = result.get("tier")
        if tier_name:
            tier_text = f"**{tier_name}**"
        else:
            tier_text = "**Sem cargo**"

        for next_tier_name, next_minimum_amount, _ in DEPOSIT_TIERS:
            if amount < next_minimum_amount:
                next_text = (
                    f"Faltam {format_currency(next_minimum_amount - amount, 'BRL')} "
                    f"para {next_tier_name}."
                )
                break
        else:
            next_text = "Você alcançou o nível máximo."

        return (
            "🏆 **Ranking de depósitos**\n"
            f"Depósitos confirmados: **{format_currency(amount, 'BRL')}**\n"
            f"Cargo atual: {tier_text}\n"
            f"{next_text}"
        )

    def _current_deposit_role_name(self, member: discord.Member | None) -> str:
        if member is None:
            return "Fora do servidor"

        member_role_ids = {role.id for role in member.roles}
        matching_tiers = [
            (minimum_amount, tier_name)
            for tier_name, minimum_amount, role_id in DEPOSIT_TIERS
            if role_id > 0 and role_id in member_role_ids
        ]
        if not matching_tiers:
            return "Sem cargo"
        return max(matching_tiers, key=lambda tier: tier[0])[1]

    async def _deposit_leaderboard_profile(
        self,
        guild: discord.Guild,
        discord_id: str,
    ) -> dict[str, Any] | None:
        try:
            numeric_discord_id = int(discord_id)
        except (TypeError, ValueError):
            return None
        if numeric_discord_id <= 0:
            return None

        member = guild.get_member(numeric_discord_id)
        if member is None:
            try:
                member = await asyncio.wait_for(
                    guild.fetch_member(numeric_discord_id),
                    timeout=8,
                )
            except asyncio.TimeoutError:
                logger.warning("Discord demorou ao consultar o usuário %s do ranking", discord_id)
            except discord.NotFound:
                pass
            except discord.HTTPException as exc:
                logger.warning("Não foi possível consultar o membro %s do ranking: %s", discord_id, exc)

        profile: discord.Member | discord.User | None = member
        if profile is None:
            try:
                profile = await asyncio.wait_for(
                    self.bot.fetch_user(numeric_discord_id),
                    timeout=8,
                )
            except asyncio.TimeoutError:
                logger.warning("Discord demorou ao consultar o perfil %s do ranking", discord_id)
            except discord.NotFound:
                pass
            except discord.HTTPException as exc:
                logger.warning("Não foi possível consultar o perfil %s do ranking: %s", discord_id, exc)

        display_name = "Usuário não encontrado"
        username = "indisponível"
        if profile is not None:
            display_name = (
                getattr(profile, "display_name", None)
                or getattr(profile, "global_name", None)
                or profile.name
            )
            username = profile.name

        return {
            "discord_id": str(numeric_discord_id),
            "display_name": display_name,
            "username": username,
            "deposit_role": self._current_deposit_role_name(member),
        }

    async def _deposit_leaderboard_embeds(self) -> list[discord.Embed]:
        summaries = await self._fetch_all_deposit_summaries()
        top_summaries = sorted(
            (
                summary
                for summary in summaries
                if str(summary[0]) not in EXCLUDED_DEPOSIT_RANKING_IDS
            ),
            key=lambda summary: (summary[1], summary[0]),
            reverse=True,
        )[:10]
        if not top_summaries:
            return []

        guild = self._guild_on_bot_loop()
        profiles = await asyncio.gather(
            *(
                self._deposit_leaderboard_profile(guild, discord_id)
                for discord_id, _ in top_summaries
            )
        )

        ranking_lines: list[str] = []
        position = 0
        for (discord_id, amount), profile in zip(top_summaries, profiles):
            if profile is None:
                continue
            position += 1
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, "🏅")
            user_label = (
                f"<@{discord_id}>"
                if profile["username"] != "indisponível"
                else profile["display_name"]
            )
            ranking_lines.append(
                f"{medal} **#{position}** {user_label} — **{profile['deposit_role']}**\n"
                f"💰 **{format_currency(amount, 'BRL')}**"
            )

        if not ranking_lines:
            return []
        return [
            discord.Embed(
                title="🏆 Rank Geral — TOP 10 Compradores",
                description="\n\n".join(ranking_lines),
                color=discord.Color.gold(),
            )
        ]

    async def _reconcile_deposit_roles_loop(self) -> None:
        while True:
            await asyncio.sleep(settings.deposit_role_sync_interval_seconds)
            try:
                for discord_id, amount in await self._fetch_all_deposit_summaries():
                    try:
                        await self._sync_deposit_roles_on_bot_loop(
                            discord_id,
                            amount,
                            allow_member_fetch=False,
                        )
                    except (RuntimeError, httpx.HTTPError) as exc:
                        logger.warning("Não foi possível sincronizar o ranking de %s: %s", discord_id, exc)
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Falha na reconciliação do ranking de depósitos")

    def _register_commands(self) -> None:
        command_guild_id = settings.discord_guild_id or settings.discord_command_guild_id
        command_guild_ids = [command_guild_id] if command_guild_id else None

        @self.bot.event
        async def on_ready() -> None:
            logger.info("Bot conectado como %s", self.bot.user)
            self._connected_guild_id = settings.discord_guild_id or None
            if not self._application_commands_synced:
                try:
                    await self.bot.sync_commands(force=True)
                    self._application_commands_synced = True
                    logger.info(
                        "Comandos slash sincronizados para o servidor %s",
                        settings.discord_guild_id or settings.discord_command_guild_id or "global",
                    )
                except Exception:
                    logger.exception("Falha ao sincronizar os comandos slash")
            if settings.ticket_enabled and not self._ticket_views_registered:
                self.bot.add_view(TicketPanelView(self))
                self.bot.add_view(TicketView(self))
                self._ticket_views_registered = True
            if settings.ticket_enabled and not self._legacy_ticket_topics_migrated:
                await self._migrate_legacy_ticket_topics()
                self._legacy_ticket_topics_migrated = True
            if self._deposit_role_sync_task is None or self._deposit_role_sync_task.done():
                self._deposit_role_sync_task = asyncio.create_task(
                    self._reconcile_deposit_roles_loop(),
                    name="deposit-role-reconciliation",
                )
            self.ready.set()

        @self.bot.slash_command(
            name="ping",
            description="Verifica se o RedStore está online",
            guild_ids=command_guild_ids,
        )
        async def ping(ctx: discord.ApplicationContext) -> None:
            await ctx.respond(f"Pong! {round(self.bot.latency * 1000)}ms")

        @self.bot.slash_command(
            name="live",
            description="Divulga a live no TikTok",
            guild_ids=command_guild_ids,
        )
        async def live(ctx: discord.ApplicationContext) -> None:
            if ctx.author.id != LIVE_COMMAND_OWNER_ID:
                await ctx.respond(
                    "Apenas o dono da live pode usar este comando.",
                    ephemeral=True,
                )
                return

            await ctx.defer(ephemeral=True)
            live_channel = self.bot.get_channel(LIVE_ANNOUNCEMENT_CHANNEL_ID)
            if live_channel is None:
                try:
                    live_channel = await self.bot.fetch_channel(LIVE_ANNOUNCEMENT_CHANNEL_ID)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    live_channel = None

            if not isinstance(live_channel, (discord.TextChannel, discord.Thread)):
                await ctx.followup.send(
                    "Não encontrei um canal de texto válido para publicar a live.",
                    ephemeral=True,
                )
                return

            try:
                await live_channel.send(
                    "🔴 **Estou ao vivo no TikTok!**\n\n"
                    f"Vem acompanhar a live: {TIKTOK_PROFILE_URL}"
                )
            except (discord.Forbidden, discord.HTTPException):
                await ctx.followup.send(
                    "Não consegui publicar a divulgação nesse canal. Verifique as permissões do bot.",
                    ephemeral=True,
                )
                return

            await ctx.followup.send(
                f"Divulgação enviada em {live_channel.mention}.",
                ephemeral=True,
            )

        @self.bot.slash_command(
            name="site",
            description="Envia o link do RedStore",
            guild_ids=command_guild_ids,
        )
        async def site(ctx: discord.ApplicationContext) -> None:
            await ctx.respond(f"Acesse o RedStore: {settings.site_url}", ephemeral=True)

        @self.bot.slash_command(
            name="verificar",
            description="Mostra seus cargos no servidor configurado",
            guild_ids=command_guild_ids,
        )
        async def verificar(ctx: discord.ApplicationContext) -> None:
            if not isinstance(ctx.author, discord.Member):
                await ctx.respond("Este comando precisa ser usado dentro de um servidor.", ephemeral=True)
                return
            linked_user = store.get(str(ctx.author.id))
            if not linked_user:
                await ctx.respond(
                    f"Sua conta ainda não está vinculada ao RedStore. Acesse {settings.site_url} para entrar.",
                    ephemeral=True,
                )
                return
            roles = ", ".join(role.name for role in ctx.author.roles if role.name != "@everyone") or "nenhum"
            await ctx.respond(f"Sua conta está vinculada ao RedStore. Cargos: {roles}", ephemeral=True)

        @self.bot.slash_command(
            name="rank",
            description="Mostra seus depósitos confirmados e atualiza seu cargo",
            guild_ids=command_guild_ids,
        )
        async def rank(ctx: discord.ApplicationContext) -> None:
            await ctx.defer(ephemeral=True)
            try:
                if not isinstance(ctx.author, discord.Member):
                    await ctx.followup.send(
                        "Este comando precisa ser usado dentro de um servidor.",
                        ephemeral=True,
                    )
                    return
                message = await asyncio.wait_for(
                    self._deposit_ranking_message(ctx.author),
                    timeout=20,
                )
            except asyncio.TimeoutError:
                logger.error("A consulta do ranking de %s excedeu 20 segundos", ctx.author.id)
                await ctx.followup.send(
                    "A consulta demorou mais que o esperado. Tente novamente em instantes.",
                    ephemeral=True,
                )
                return
            except Exception:
                logger.exception("Falha ao processar o ranking de %s", ctx.author.id)
                await ctx.followup.send(
                    "Não foi possível consultar seus depósitos agora. Tente novamente em instantes.",
                    ephemeral=True,
                )
                return
            await ctx.followup.send(message, ephemeral=True)

        @self.bot.command(name="rank")
        async def rank_prefix(ctx: commands.Context) -> None:
            if not isinstance(ctx.author, discord.Member):
                await ctx.reply("Este comando precisa ser usado dentro de um servidor.")
                return
            try:
                message = await self._deposit_ranking_message(ctx.author)
            except (RuntimeError, httpx.HTTPError, ValueError) as exc:
                logger.warning("Não foi possível consultar o ranking de %s: %s", ctx.author.id, exc)
                await ctx.reply("Não foi possível consultar seus depósitos agora. Tente novamente em instantes.")
                return
            await ctx.reply(message)

        @self.bot.slash_command(
            name="ranking",
            description="Mostra os 10 usuários com maior gasto no RedStore",
            guild_ids=command_guild_ids,
        )
        async def ranking(ctx: discord.ApplicationContext) -> None:
            await ctx.defer()
            try:
                embeds = await asyncio.wait_for(
                    self._deposit_leaderboard_embeds(),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                logger.error("A consulta do top 10 do ranking excedeu 30 segundos")
                await ctx.followup.send(
                    "A consulta demorou mais que o esperado. Tente novamente em instantes."
                )
                return
            except Exception:
                logger.exception("Falha ao processar o top 10 do ranking")
                await ctx.followup.send(
                    "Não foi possível consultar o ranking agora. Tente novamente em instantes."
                )
                return
            if not embeds:
                await ctx.followup.send("Ainda não há depósitos confirmados para exibir no ranking.")
                return
            await ctx.followup.send(embeds=embeds)

        @self.bot.command(name="ranking")
        async def ranking_prefix(ctx: commands.Context) -> None:
            try:
                embeds = await asyncio.wait_for(
                    self._deposit_leaderboard_embeds(),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                logger.error("A consulta do top 10 do ranking excedeu 30 segundos")
                await ctx.reply("A consulta demorou mais que o esperado. Tente novamente em instantes.")
                return
            except Exception:
                logger.exception("Falha ao processar o top 10 do ranking")
                await ctx.reply("Não foi possível consultar o ranking agora. Tente novamente em instantes.")
                return
            if not embeds:
                await ctx.reply("Ainda não há depósitos confirmados para exibir no ranking.")
                return
            await ctx.reply(embeds=embeds)

        @self.bot.slash_command(
            name="robux",
            description="Calcula quantos Robux cabem no seu orçamento",
            guild_ids=command_guild_ids,
        )
        async def robux_slash(
            ctx: discord.ApplicationContext,
            valor_k: str = discord.Option(
                str,
                "Preço de 1.000 Robux (ex.: 5,00)",
            ),
            dinheiro: str = discord.Option(
                str,
                "Quanto você tem disponível (ex.: 20,00)",
            ),
            moeda_k: str = discord.Option(
                str,
                "Moeda do preço de 1k",
                choices=["real", "dolar"],
            ),
            moeda_dinheiro: str = discord.Option(
                str,
                "Moeda do seu dinheiro",
                choices=["real", "dolar"],
            ),
        ) -> None:
            try:
                quote = await self._calculate_robux_quote(
                    valor_k,
                    dinheiro,
                    moeda_k,
                    moeda_dinheiro,
                )
            except (ArithmeticError, ValueError):
                await ctx.respond(
                    "Informe valores válidos, escolha as moedas e tente novamente. "
                    "Exemplo: `/robux valor_k:5,00 dinheiro:20,00 moeda_k:real moeda_dinheiro:real`.",
                    ephemeral=True,
                )
                return

            price_currency = quote["price_currency"]
            money_currency = quote["money_currency"]
            conversion_text = ""
            if quote["ptax"]:
                rate, rate_type, quote_date = quote["ptax"]
                conversion_text = (
                    f"💱 Convertido para {format_currency(quote['calculation_budget'], price_currency)} "
                    f"pela PTAX ({rate_type}) de {quote_date}: "
                    f"US$ 1 = {format_currency(rate, 'BRL')}\n"
                )

            await ctx.respond(
                "🧮 **Calculadora de Robux**\n"
                f"Preço do 1k: **{format_currency(quote['price'], price_currency)}**\n"
                f"Orçamento: **{format_currency(quote['available'], money_currency)}**\n"
                f"{conversion_text}\n"
                f"Você pode comprar aproximadamente **{format_robux(quote['robux'])} Robux**.\n"
                f"Valor usado: **{format_currency(quote['spent'], price_currency)}** • "
                f"Sobra: **{format_currency(quote['remainder'], price_currency)}**"
            )

        @self.bot.command(name="robux")
        async def robux_prefix(ctx: commands.Context, *, argumentos: str = "") -> None:
            valores = argumentos.split()
            if len(valores) not in {2, 3, 4}:
                await ctx.reply(
                    "Uso: `!robux <valor do 1k> <seu dinheiro> [moeda do k] [moeda do dinheiro]`. "
                    "Exemplo: `!robux 5,00 20,00 real real`."
                )
                return
            try:
                price_currency = valores[2] if len(valores) >= 3 else "real"
                money_currency = valores[3] if len(valores) == 4 else price_currency
                quote = await self._calculate_robux_quote(
                    valores[0],
                    valores[1],
                    price_currency,
                    money_currency,
                )
            except (ArithmeticError, ValueError):
                await ctx.reply(
                    "Informe valores válidos e escolha `real` ou `dolar`. "
                    "Exemplo: `!robux 5,00 20,00 real real`."
                )
                return

            conversion_text = ""
            if quote["ptax"]:
                rate, rate_type, quote_date = quote["ptax"]
                conversion_text = (
                    f" Conversão PTAX ({rate_type}, {quote_date}): "
                    f"US$ 1 = {format_currency(rate, 'BRL')}."
                )
            await ctx.reply(
                f"🧮 Com {format_currency(quote['available'], quote['money_currency'])} e o 1k a "
                f"{format_currency(quote['price'], quote['price_currency'])}, você pode comprar "
                f"aproximadamente **{format_robux(quote['robux'])} Robux**. "
                f"Sobra equivalente: **{format_currency(quote['remainder'], quote['price_currency'])}**."
                f"{conversion_text}"
            )

        if settings.ticket_enabled:
            @self.bot.slash_command(
                name="ticket",
                description="Publica o painel de atendimento",
                guild_ids=command_guild_ids,
            )
            async def ticket(ctx: discord.ApplicationContext) -> None:
                if not self._has_ticket_staff_access(ctx.author):
                    await ctx.respond(
                        "Apenas a equipe de suporte pode publicar o painel de tickets.",
                        ephemeral=True,
                    )
                    return
                embed = discord.Embed(
                    title="🎫 CENTRAL DE ATENDIMENTO",
                    description=(
                        "Olá! Seja bem-vindo à central de tickets do RedStore.\n\n"
                        "Se você precisa de ajuda, tem dúvidas ou quer resolver uma situação, "
                        "abra um ticket pelo botão abaixo.\n\n"
                        "📌 **Antes de abrir:** explique o problema com clareza, evite spam e "
                        "aguarde a equipe responder.\n\n"
                        "📂 **Suporte disponível:** suporte geral, denúncias e dúvidas.\n\n"
                        "⚠️ A abertura de tickets sem motivo pode resultar em punição."
                    ),
                    color=discord.Color.dark_blue(),
                )
                embed.set_footer(text="Sistema de Tickets • RedStore")
                await ctx.respond(embed=embed, view=TicketPanelView(self))

        @self.bot.slash_command(
            name="prova",
            description="Publica a prova de uma entrega",
            guild_ids=command_guild_ids,
        )
        async def prova_slash(
            ctx: discord.ApplicationContext,
            cliente: discord.Member = discord.Option(discord.Member, "Cliente"),
            produto: str = discord.Option(str, "Produto"),
            imagem: discord.Attachment = discord.Option(
                discord.Attachment,
                "Imagem da prova entregue",
            ),
            imagem_2: discord.Attachment | None = discord.Option(
                discord.Attachment,
                "Imagem adicional 2",
                required=False,
                default=None,
            ),
            imagem_3: discord.Attachment | None = discord.Option(
                discord.Attachment,
                "Imagem adicional 3",
                required=False,
                default=None,
            ),
            imagem_4: discord.Attachment | None = discord.Option(
                discord.Attachment,
                "Imagem adicional 4",
                required=False,
                default=None,
            ),
            imagem_5: discord.Attachment | None = discord.Option(
                discord.Attachment,
                "Imagem adicional 5",
                required=False,
                default=None,
            ),
            imagem_6: discord.Attachment | None = discord.Option(
                discord.Attachment,
                "Imagem adicional 6",
                required=False,
                default=None,
            ),
            imagem_7: discord.Attachment | None = discord.Option(
                discord.Attachment,
                "Imagem adicional 7",
                required=False,
                default=None,
            ),
            imagem_8: discord.Attachment | None = discord.Option(
                discord.Attachment,
                "Imagem adicional 8",
                required=False,
                default=None,
            ),
            imagem_9: discord.Attachment | None = discord.Option(
                discord.Attachment,
                "Imagem adicional 9",
                required=False,
                default=None,
            ),
            imagem_10: discord.Attachment | None = discord.Option(
                discord.Attachment,
                "Imagem adicional 10",
                required=False,
                default=None,
            ),
        ) -> None:
            if not isinstance(ctx.author, discord.Member) or not self._has_deliverer_access(ctx.author):
                await ctx.respond(
                    "Apenas usuários com o cargo Entregador podem usar este comando.",
                    ephemeral=True,
                )
                return
            if ctx.guild is None:
                await ctx.respond("Este comando só pode ser usado dentro do servidor.", ephemeral=True)
                return
            imagens = [
                attachment
                for attachment in (
                    imagem,
                    imagem_2,
                    imagem_3,
                    imagem_4,
                    imagem_5,
                    imagem_6,
                    imagem_7,
                    imagem_8,
                    imagem_9,
                    imagem_10,
                )
                if attachment is not None
            ]
            if any(not self._is_image_attachment(attachment) for attachment in imagens):
                await ctx.respond("Uma das imagens informadas não é válida.", ephemeral=True)
                return
            destination = self._proof_destination(ctx.guild, ctx.channel)
            if destination is None:
                await ctx.respond(
                    "O canal configurado para provas não foi encontrado neste servidor.",
                    ephemeral=True,
                )
                return
            await ctx.respond("Publicando prova...", ephemeral=True)
            await self.publish_proof(destination, cliente, produto, imagens)

        @self.bot.command(name="prova")
        async def prova(ctx: commands.Context, *, argumentos: str = "") -> None:
            if not isinstance(ctx.author, discord.Member) or not self._has_deliverer_access(ctx.author):
                await ctx.reply("Apenas usuários com o cargo Entregador podem usar este comando.")
                return
            if ctx.guild is None:
                await ctx.reply("Este comando só pode ser usado dentro do servidor.")
                return
            if not ctx.message.mentions:
                await ctx.reply(
                    "Uso: `!prova @cliente produto` com uma ou mais imagens anexadas. "
                    "O número da venda é automático."
                )
                return

            cliente = ctx.message.mentions[0]
            produto = re.sub(rf"<@!?{cliente.id}>", "", argumentos, count=1).strip()
            if not produto:
                await ctx.reply(
                    "Informe o produto. Exemplo: `!prova @cliente 600 Gamepass`."
                )
                return

            imagens = [
                attachment
                for attachment in ctx.message.attachments
                if self._is_image_attachment(attachment)
            ]
            if not imagens:
                await ctx.reply("Anexe uma ou mais imagens da prova entregue junto com o comando.")
                return

            destination = self._proof_destination(ctx.guild, ctx.channel)
            if destination is None:
                await ctx.reply("O canal configurado para provas não foi encontrado neste servidor.")
                return
            await self.publish_proof(destination, cliente, produto, imagens)

    @staticmethod
    def _is_image_attachment(attachment: discord.Attachment) -> bool:
        return (
            (attachment.content_type or "").startswith("image/")
            or attachment.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
        )

    @staticmethod
    def _proof_destination(
        guild: discord.Guild,
        fallback: discord.abc.Messageable,
    ) -> discord.abc.Messageable | None:
        if not settings.proof_channel_id:
            return fallback
        channel = guild.get_channel(settings.proof_channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    @staticmethod
    async def _latest_sale_number(destination: discord.abc.Messageable) -> int:
        """Find the highest proof number already published in the destination."""
        highest = 0
        if not hasattr(destination, "history"):
            return highest

        async for message in destination.history(limit=None):
            for embed in message.embeds:
                match = re.search(r"Venda\s+#(\d+)", embed.title or "", re.IGNORECASE)
                if match:
                    highest = max(highest, int(match.group(1)))
        return highest

    async def _next_sale_number(self, destination: discord.abc.Messageable) -> int:
        """Reserve a number and recover the sequence after ephemeral restarts."""
        async with self._proof_number_lock:
            local_number = proofs.next_sale_number()
            published_number = await self._latest_sale_number(destination)
            return max(local_number, published_number + 1)

    async def publish_proof(
        self,
        destination: discord.abc.Messageable,
        cliente: discord.Member,
        produto: str,
        imagens: Sequence[discord.Attachment],
    ) -> None:
        if not imagens:
            raise ValueError("A prova precisa ter pelo menos uma imagem.")

        numero = await self._next_sale_number(destination)
        horario = datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%H:%M")
        rotulo_imagens = "imagem" if len(imagens) == 1 else "imagens"
        embed_principal = discord.Embed(
            title=f"🔒 Venda #{numero}",
            description="Obrigado pela preferência! 🏅",
            color=discord.Color.gold(),
        )
        embed_principal.add_field(name="Cliente", value=cliente.mention, inline=False)
        embed_principal.add_field(name="Produto", value=produto[:1024], inline=False)
        embed_principal.set_image(url=imagens[0].url)
        embed_principal.set_footer(text=f"Hoje às {horario} • {len(imagens)} {rotulo_imagens}")

        embeds = [embed_principal]
        for imagem in imagens[1:]:
            embed = discord.Embed(color=discord.Color.gold())
            embed.set_image(url=imagem.url)
            embeds.append(embed)

        # O Discord limita cada mensagem a 10 embeds; pedidos maiores seguem
        # em mensagens de continuação sem repetir a menção do cliente.
        for indice in range(0, len(embeds), 10):
            lote = embeds[indice:indice + 10]
            primeira_mensagem = indice == 0
            await destination.send(
                content=(
                    cliente.mention
                    if primeira_mensagem
                    else f"Continuação da prova da Venda #{numero}"
                ),
                embeds=lote,
                allowed_mentions=(
                    discord.AllowedMentions(users=[cliente])
                    if primeira_mensagem
                    else discord.AllowedMentions.none()
                ),
            )

    @staticmethod
    def _has_deliverer_access(member: discord.Member) -> bool:
        if settings.deliverer_role_id:
            return any(role.id == settings.deliverer_role_id for role in member.roles)
        expected_name = settings.deliverer_role_name.strip().casefold()
        return any(role.name.strip().casefold() == expected_name for role in member.roles)

    @staticmethod
    def _legacy_ticket_data_from_channel(channel: discord.TextChannel) -> dict[str, Any] | None:
        topic = channel.topic or ""
        if not topic.startswith("redstore-ticket|"):
            return None
        values: dict[str, str] = {}
        for item in topic.split("|")[1:]:
            key, separator, value = item.partition("=")
            if separator:
                values[key] = value
        try:
            user_id = int(values["user"])
        except (KeyError, ValueError):
            return None
        claimed_by = values.get("claimed", "0")
        try:
            claimed_id = int(claimed_by) if claimed_by != "0" else None
        except ValueError:
            claimed_id = None
        try:
            mentions = max(0, int(values.get("mentions", "0")))
        except ValueError:
            mentions = 0
        return {
            "user_id": user_id,
            "opened_at": values.get("opened", "não informado"),
            "claimed_by": claimed_id,
            "claimed_at": values.get("claimed_at") or None,
            "mentions": mentions,
            "state": values.get("state", "open"),
        }

    def _ticket_data_from_channel(self, channel: discord.TextChannel) -> dict[str, Any] | None:
        data = tickets.get(channel.id)
        if data:
            return data
        # Migra tickets antigos que usavam o tópico, sem manter esse formato nos novos.
        legacy_data = self._legacy_ticket_data_from_channel(channel)
        if legacy_data:
            tickets.save(channel.id, legacy_data)
        return legacy_data

    async def _migrate_legacy_ticket_topics(self) -> None:
        guild = self.bot.get_guild(settings.discord_guild_id) if settings.discord_guild_id else None
        if guild is None:
            return
        for channel in guild.text_channels:
            if not (channel.topic or "").startswith("redstore-ticket|"):
                continue
            legacy_data = self._legacy_ticket_data_from_channel(channel)
            if legacy_data:
                tickets.save(channel.id, legacy_data)
            try:
                await channel.edit(topic=None, reason="Migrar metadados do ticket para o SQLite")
            except discord.Forbidden:
                logger.warning("Sem permissão para limpar o tópico do ticket %s", channel.id)
            except discord.HTTPException:
                logger.warning("Não foi possível limpar o tópico do ticket %s", channel.id)

    def _has_ticket_staff_access(self, member: discord.Member | discord.User) -> bool:
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.manage_channels:
            return True
        role_ids = set(settings.ticket_support_role_ids)
        if settings.ticket_master_role_id:
            role_ids.add(settings.ticket_master_role_id)
        return any(role.id in role_ids for role in member.roles)

    def _is_ticket_owner_or_staff(
        self,
        member: discord.Member | discord.User,
        data: dict[str, Any],
    ) -> bool:
        return member.id == data["user_id"] or self._has_ticket_staff_access(member)

    def _find_open_ticket(
        self,
        guild: discord.Guild,
        user_id: int,
    ) -> discord.TextChannel | None:
        for channel in guild.text_channels:
            if settings.ticket_category_id and channel.category_id != settings.ticket_category_id:
                continue
            data = self._ticket_data_from_channel(channel)
            if data and data["user_id"] == user_id and data["state"] == "open":
                return channel
        return None

    async def open_ticket(self, interaction: discord.Interaction) -> None:
        if not settings.ticket_enabled:
            await interaction.response.send_message("O sistema de tickets está desativado.", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Este botão só pode ser usado dentro do servidor do RedStore.",
                ephemeral=True,
            )
            return
        if settings.discord_guild_id and guild.id != settings.discord_guild_id:
            await interaction.response.send_message(
                "Este painel pertence ao servidor oficial do RedStore.",
                ephemeral=True,
            )
            return
        if not settings.ticket_category_id:
            await interaction.response.send_message(
                "O sistema de tickets ainda não foi configurado pela equipe.",
                ephemeral=True,
            )
            return
        category = guild.get_channel(settings.ticket_category_id)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "A categoria de tickets configurada não foi encontrada.",
                ephemeral=True,
            )
            return

        existing = self._find_open_ticket(guild, interaction.user.id)
        if existing:
            await interaction.response.send_message(
                f"Você já possui um ticket aberto: {existing.mention}",
                ephemeral=True,
            )
            return

        support_role_ids = set(settings.ticket_support_role_ids)
        if settings.ticket_master_role_id:
            support_role_ids.add(settings.ticket_master_role_id)
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
            ),
        }
        for role_id in support_role_ids:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                )

        data = {
            "user_id": interaction.user.id,
            "opened_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "claimed_by": None,
            "claimed_at": None,
            "mentions": 0,
            "state": "open",
        }
        try:
            channel = await guild.create_text_channel(
                name=f"ticket-{interaction.user.id}",
                category=category,
                overwrites=overwrites,
                reason=f"Ticket aberto por {interaction.user}",
            )
            tickets.save(channel.id, data)
            embed = discord.Embed(
                title="📂 Ticket aberto",
                description=(
                    f"{interaction.user.mention}, seu ticket foi aberto com sucesso.\n\n"
                    "Aguarde um membro da equipe assumir o atendimento."
                ),
                color=discord.Color.blue(),
            )
            await channel.send(embed=embed, view=TicketView(self))
        except discord.Forbidden:
            logger.exception("Discord recusou a criação do ticket")
            await interaction.response.send_message(
                "Não foi possível criar o ticket. Verifique as permissões do bot.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            logger.exception("Discord falhou ao criar o ticket")
            await interaction.response.send_message(
                "Não foi possível criar o ticket agora. Tente novamente.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Ticket criado com sucesso: {channel.mention}",
            ephemeral=True,
        )

    async def claim_ticket(self, interaction: discord.Interaction) -> None:
        if not self._has_ticket_staff_access(interaction.user):
            await interaction.response.send_message("Sem permissão para assumir tickets.", ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Este botão só funciona em tickets.", ephemeral=True)
            return
        data = self._ticket_data_from_channel(interaction.channel)
        if not data:
            await interaction.response.send_message("Este canal não é um ticket válido.", ephemeral=True)
            return
        if data["claimed_by"] and data["claimed_by"] != interaction.user.id:
            await interaction.response.send_message("Esse ticket já foi assumido por outro atendente.", ephemeral=True)
            return
        data["claimed_by"] = interaction.user.id
        data["claimed_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        tickets.save(interaction.channel.id, data)
        await interaction.channel.edit(
            name=f"ticket-assumido-{data['user_id']}",
        )
        await interaction.channel.send(
            f"<@{data['user_id']}> seu ticket foi assumido por {interaction.user.mention}."
        )
        await interaction.response.send_message("Ticket assumido.", ephemeral=True)

    async def mention_ticket(self, interaction: discord.Interaction) -> None:
        if not self._has_ticket_staff_access(interaction.user):
            await interaction.response.send_message("Sem permissão para mencionar o usuário.", ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Este botão só funciona em tickets.", ephemeral=True)
            return
        data = self._ticket_data_from_channel(interaction.channel)
        if not data:
            await interaction.response.send_message("Este canal não é um ticket válido.", ephemeral=True)
            return
        if data["mentions"] >= 3:
            await interaction.response.send_message("O limite de 3 notificações já foi atingido.", ephemeral=True)
            return
        member = interaction.guild.get_member(data["user_id"]) if interaction.guild else None
        if member is None:
            try:
                member = await interaction.guild.fetch_member(data["user_id"]) if interaction.guild else None
            except discord.HTTPException:
                member = None
        if member is None:
            await interaction.response.send_message("Não foi possível encontrar o dono do ticket.", ephemeral=True)
            return
        embed = discord.Embed(
            title="📢 Ticket aguardando atendimento",
            description=f"Seu ticket ainda está aberto: {interaction.channel.mention}",
            color=discord.Color.orange(),
        )
        try:
            await member.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                "Não foi possível enviar DM. As mensagens privadas do usuário estão bloqueadas.",
                ephemeral=True,
            )
            return
        data["mentions"] += 1
        tickets.save(interaction.channel.id, data)
        await interaction.response.send_message(
            f"Usuário notificado ({data['mentions']}/3).", ephemeral=True
        )

    async def show_rename_ticket_modal(self, interaction: discord.Interaction) -> None:
        if not self._has_ticket_staff_access(interaction.user):
            await interaction.response.send_message("Sem permissão para renomear tickets.", ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.TextChannel) or not self._ticket_data_from_channel(interaction.channel):
            await interaction.response.send_message("Este canal não é um ticket válido.", ephemeral=True)
            return
        await interaction.response.send_modal(TicketRenameModal(self))

    async def rename_ticket(self, interaction: discord.Interaction, requested_name: str) -> None:
        if not self._has_ticket_staff_access(interaction.user):
            await interaction.response.send_message("Sem permissão para renomear tickets.", ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.TextChannel) or not self._ticket_data_from_channel(interaction.channel):
            await interaction.response.send_message("Este canal não é um ticket válido.", ephemeral=True)
            return
        slug = re.sub(r"[^a-z0-9-]+", "-", requested_name.lower()).strip("-")
        if not slug:
            await interaction.response.send_message("Informe um nome válido.", ephemeral=True)
            return
        try:
            await interaction.channel.edit(name=f"ticket-{slug[:88]}")
        except discord.HTTPException:
            await interaction.response.send_message("Não foi possível renomear o ticket.", ephemeral=True)
            return
        await interaction.response.send_message("Ticket renomeado.", ephemeral=True)

    async def close_ticket(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Este botão só funciona em tickets.", ephemeral=True)
            return
        data = self._ticket_data_from_channel(interaction.channel)
        if not data:
            await interaction.response.send_message("Este canal não é um ticket válido.", ephemeral=True)
            return
        if not self._is_ticket_owner_or_staff(interaction.user, data):
            await interaction.response.send_message("Sem permissão para fechar este ticket.", ephemeral=True)
            return
        if interaction.channel.id in self._ticket_closing:
            await interaction.response.send_message("Este ticket já está sendo fechado.", ephemeral=True)
            return
        self._ticket_closing.add(interaction.channel.id)
        await interaction.response.send_message(
            f"Ticket será fechado em {settings.ticket_close_delay_seconds} segundos."
        )
        try:
            await asyncio.sleep(settings.ticket_close_delay_seconds)
            guild = interaction.guild
            if guild is None:
                return
            log_channel = guild.get_channel(settings.ticket_log_channel_id) if settings.ticket_log_channel_id else None
            if log_channel:
                log_embed = discord.Embed(title="📁 LOG DE TICKET", color=discord.Color.red())
                log_embed.add_field(name="👤 Usuário", value=f"<@{data['user_id']}>")
                log_embed.add_field(name="🆔 ID", value=str(data["user_id"]))
                log_embed.add_field(name="⏰ Aberto em", value=str(data["opened_at"]), inline=False)
                log_embed.add_field(
                    name="👮 Assumido por",
                    value=f"<@{data['claimed_by']}>" if data["claimed_by"] else "Não assumido",
                )
                log_embed.add_field(name="⏱ Assumido em", value=str(data["claimed_at"] or "Não assumido"))
                log_embed.add_field(name="🔒 Fechado por", value=interaction.user.mention)
                log_embed.add_field(
                    name="⏳ Fechado em",
                    value=datetime.now(UTC).isoformat(timespec="seconds"),
                    inline=False,
                )
                await log_channel.send(embed=log_embed)
            await interaction.channel.delete(reason=f"Ticket fechado por {interaction.user}")
            tickets.delete(interaction.channel.id)
        except discord.NotFound:
            logger.info("Ticket %s já havia sido removido", interaction.channel.id)
        except discord.Forbidden:
            logger.exception("Discord recusou o fechamento do ticket %s", interaction.channel.id)
        except discord.HTTPException:
            logger.exception("Discord falhou ao fechar o ticket %s", interaction.channel.id)
        finally:
            self._ticket_closing.discard(interaction.channel.id)

    async def start(self) -> None:
        if not settings.discord_bot_token:
            logger.warning("DISCORD_BOT_TOKEN não configurado; bot desativado")
            return
        try:
            await self.bot.start(settings.discord_bot_token)
        except discord.LoginFailure:
            logger.exception("Token do bot Discord inválido")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Bot Discord encerrou inesperadamente")

    async def close(self) -> None:
        if not self.bot.is_closed():
            await self.bot.close()

    def guild(self) -> discord.Guild:
        if not settings.discord_guild_id:
            raise HTTPException(status_code=503, detail="DISCORD_GUILD_ID não configurado")
        configured_guild = self.bot.get_guild(settings.discord_guild_id)
        if configured_guild is None:
            raise HTTPException(status_code=503, detail="Bot ainda não está conectado ao servidor")
        return configured_guild

    async def member_snapshot(self, discord_id: str) -> dict[str, Any]:
        guild = self.guild()
        try:
            member = await guild.fetch_member(int(discord_id))
        except (discord.NotFound, ValueError):
            return {"is_member": False, "roles": []}
        except discord.HTTPException as exc:
            raise HTTPException(status_code=502, detail="Discord não respondeu ao consultar o membro") from exc
        return {
            "is_member": True,
            "username": str(member),
            "roles": [
                {"id": str(role.id), "name": role.name, "position": role.position}
                for role in member.roles
                if role.name != "@everyone"
            ],
        }

    async def change_role(
        self,
        discord_id: str,
        role_id: int,
        action: Literal["add", "remove"],
    ) -> dict[str, Any]:
        try:
            numeric_discord_id = int(discord_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="discord_id precisa ser numérico") from exc
        if numeric_discord_id <= 0:
            raise HTTPException(status_code=400, detail="discord_id inválido")
        guild = self.guild()
        role = guild.get_role(role_id)
        if role is None:
            raise HTTPException(status_code=404, detail="Cargo não encontrado")
        if not guild.me or role >= guild.me.top_role:
            raise HTTPException(status_code=400, detail="O bot não pode gerenciar este cargo")
        try:
            member = await guild.fetch_member(numeric_discord_id)
            if action == "add":
                await member.add_roles(role, reason="Sincronização solicitada pelo RedStore")
            else:
                await member.remove_roles(role, reason="Sincronização solicitada pelo RedStore")
        except discord.NotFound as exc:
            raise HTTPException(status_code=404, detail="Usuário não está no servidor") from exc
        except discord.Forbidden as exc:
            raise HTTPException(status_code=403, detail="Discord recusou a alteração do cargo") from exc
        except discord.HTTPException as exc:
            raise HTTPException(status_code=502, detail="Discord não respondeu à alteração do cargo") from exc
        return await self.member_snapshot(discord_id)

    def health(self) -> dict[str, Any]:
        guild = self.bot.get_guild(settings.discord_guild_id) if settings.discord_guild_id else None
        return {
            "bot_configured": bool(settings.discord_bot_token),
            "bot_ready": self.ready.is_set(),
            "guild_connected": guild is not None,
            "guild_id": str(settings.discord_guild_id) if settings.discord_guild_id else None,
        }


    async def start(self) -> None:
        if not settings.discord_bot_token:
            logger.warning("DISCORD_BOT_TOKEN nao configurado; bot desativado")
            return

        self._bot_started.clear()
        self._bot_stopped.clear()
        self._bot_thread = threading.Thread(
            target=self._run_bot_loop,
            name="discord-bot",
            daemon=True,
        )
        self._bot_thread.start()
        await asyncio.to_thread(self._bot_started.wait, 15)
        await asyncio.to_thread(self._bot_stopped.wait)

    def _run_bot_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._bot_loop = loop
        self._bot_started.set()
        try:
            loop.run_until_complete(self.bot.start(settings.discord_bot_token))
        except discord.LoginFailure:
            logger.exception("Token do bot Discord invalido")
        except Exception:
            logger.exception("Bot Discord encerrou inesperadamente")
        finally:
            self.ready.clear()
            self._connected_guild_id = None
            self._bot_loop = None
            self._bot_stopped.set()
            loop.close()

    async def _stop_deposit_role_sync_on_bot_loop(self) -> None:
        task = self._deposit_role_sync_task
        self._deposit_role_sync_task = None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        loop = self._bot_loop
        if loop and loop.is_running():
            stop_sync_future = asyncio.run_coroutine_threadsafe(
                self._stop_deposit_role_sync_on_bot_loop(), loop
            )
            await asyncio.wrap_future(stop_sync_future)
            close_future = asyncio.run_coroutine_threadsafe(self.bot.close(), loop)
            await asyncio.wrap_future(close_future)
            await asyncio.to_thread(self._bot_stopped.wait, 15)

    def _guild_on_bot_loop(self) -> discord.Guild:
        if not settings.discord_guild_id:
            raise HTTPException(status_code=503, detail="DISCORD_GUILD_ID nao configurado")
        configured_guild = self.bot.get_guild(settings.discord_guild_id)
        if configured_guild is None:
            raise HTTPException(status_code=503, detail="Bot ainda nao esta conectado ao servidor")
        return configured_guild

    async def _run_on_bot_loop(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        loop = self._bot_loop
        if loop is None or not loop.is_running() or not self.ready.is_set():
            raise HTTPException(status_code=503, detail="Bot ainda nao esta conectado ao Discord")
        future = asyncio.run_coroutine_threadsafe(operation(), loop)
        return await asyncio.wrap_future(future)

    async def _member_snapshot_on_bot_loop(self, discord_id: str) -> dict[str, Any]:
        guild = self._guild_on_bot_loop()
        try:
            member = await guild.fetch_member(int(discord_id))
        except (discord.NotFound, ValueError):
            return {"is_member": False, "roles": []}
        except discord.HTTPException as exc:
            raise HTTPException(status_code=502, detail="Discord nao respondeu ao consultar o membro") from exc
        return {
            "is_member": True,
            "username": str(member),
            "roles": [
                {"id": str(role.id), "name": role.name, "position": role.position}
                for role in member.roles
                if role.name != "@everyone"
            ],
        }

    async def member_snapshot(self, discord_id: str) -> dict[str, Any]:
        return await self._run_on_bot_loop(
            lambda: self._member_snapshot_on_bot_loop(discord_id)
        )

    async def _change_role_on_bot_loop(
        self,
        discord_id: str,
        role_id: int,
        action: Literal["add", "remove"],
    ) -> dict[str, Any]:
        guild = self._guild_on_bot_loop()
        role = guild.get_role(role_id)
        if role is None:
            raise HTTPException(status_code=404, detail="Cargo nao encontrado")
        if not guild.me or role >= guild.me.top_role:
            raise HTTPException(status_code=400, detail="O bot nao pode gerenciar este cargo")
        try:
            member = await guild.fetch_member(int(discord_id))
            if action == "add":
                await member.add_roles(role, reason="Sincronizacao solicitada pelo RedStore")
            else:
                await member.remove_roles(role, reason="Sincronizacao solicitada pelo RedStore")
        except discord.NotFound as exc:
            raise HTTPException(status_code=404, detail="Usuario nao esta no servidor") from exc
        except discord.Forbidden as exc:
            raise HTTPException(status_code=403, detail="Discord recusou a alteracao do cargo") from exc
        except discord.HTTPException as exc:
            raise HTTPException(status_code=502, detail="Discord nao respondeu a alteracao do cargo") from exc
        return await self._member_snapshot_on_bot_loop(discord_id)

    async def change_role(
        self,
        discord_id: str,
        role_id: int,
        action: Literal["add", "remove"],
    ) -> dict[str, Any]:
        try:
            numeric_discord_id = int(discord_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="discord_id precisa ser numerico") from exc
        if numeric_discord_id <= 0:
            raise HTTPException(status_code=400, detail="discord_id invalido")
        return await self._run_on_bot_loop(
            lambda: self._change_role_on_bot_loop(str(numeric_discord_id), role_id, action)
        )

    def health(self) -> dict[str, Any]:
        return {
            "bot_configured": bool(settings.discord_bot_token),
            "bot_ready": self.ready.is_set(),
            "guild_connected": self._connected_guild_id is not None,
            "guild_id": str(settings.discord_guild_id) if settings.discord_guild_id else None,
        }

    async def guild_details(self) -> dict[str, Any]:
        return await self._run_on_bot_loop(self._guild_details_on_bot_loop)

    async def _guild_details_on_bot_loop(self) -> dict[str, Any]:
        guild = self._guild_on_bot_loop()
        return {
            "id": str(guild.id),
            "name": guild.name,
            "roles": [
                {"id": str(role.id), "name": role.name, "position": role.position, "managed": role.managed}
                for role in guild.roles
                if not role.managed and role.name != "@everyone"
            ],
        }

    async def _send_deposit_notification_on_bot_loop(
        self,
        deposit_id: int,
        transaction_code: str,
        username: str,
        amount: Decimal,
        payment_amount: Decimal | None = None,
        discount_amount: Decimal | None = None,
        discount_code: str | None = None,
        used_discount_code: bool = False,
    ) -> dict[str, Any]:
        if not settings.deposit_notification_discord_id:
            logger.warning("DEPOSIT_NOTIFICATION_DISCORD_ID não está configurado")
            return {"notified": False, "reason": "admin_not_configured"}

        try:
            admin = self.bot.get_user(settings.deposit_notification_discord_id)
            if admin is None:
                admin = await self.bot.fetch_user(settings.deposit_notification_discord_id)
            amount_text = f"R$ {amount:.2f}".replace(".", ",")
            payment_text = f"R$ {(payment_amount if payment_amount is not None else amount):.2f}".replace(".", ",")
            discount_text = f"R$ {(discount_amount or Decimal('0')):.2f}".replace(".", ",")
            if used_discount_code:
                promotion_text = f"Sim — **{discount_code}**" if discount_code else "Sim"
            else:
                promotion_text = "Não"
            discount_line = f"Desconto aplicado: **{discount_text}**\n" if used_discount_code else ""
            await admin.send(
                "🔔 **Novo depósito pendente**\n"
                f"Código: **{transaction_code}** (ID interno: `{deposit_id}`)\n"
                f"Usuário: **{username}**\n"
                f"Valor creditado: **{amount_text}**\n"
                f"Valor pago: **{payment_text}**\n"
                f"Código promocional usado: **{promotion_text}**\n"
                f"{discount_line}"
                "O usuário informou que já fez o pagamento. Confira o comprovante antes de decidir.",
                view=DepositReviewView(self, deposit_id),
            )
        except discord.Forbidden:
            logger.warning(
                "Não foi possível enviar DM ao administrador %s; verifique as mensagens privadas",
                settings.deposit_notification_discord_id,
            )
            return {"notified": False, "reason": "dm_forbidden"}
        except discord.HTTPException:
            logger.exception("Discord recusou o envio da notificação de depósito")
            return {"notified": False, "reason": "discord_error"}
        return {"notified": True}

    def _deliverer_role_on_bot_loop(self, guild: discord.Guild) -> discord.Role | None:
        if settings.deliverer_role_id:
            return guild.get_role(settings.deliverer_role_id)
        expected_name = settings.deliverer_role_name.strip().casefold()
        return next(
            (role for role in guild.roles if role.name.strip().casefold() == expected_name),
            None,
        )

    def _order_notification_channel_on_bot_loop(
        self,
        guild: discord.Guild,
    ) -> discord.TextChannel | discord.Thread | None:
        if not settings.order_notification_channel_id:
            return None
        channel = guild.get_channel(settings.order_notification_channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    async def _send_order_notification_on_bot_loop(
        self,
        order_id: int,
        order_code: str,
        username: str,
        roblox_nick: str | None,
        total_amount: Decimal,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        guild = self._guild_on_bot_loop()
        role = self._deliverer_role_on_bot_loop(guild)
        if role is None:
            logger.warning("Cargo de entregador não foi encontrado para avisos de pedidos")
            return {"notified": False, "reason": "deliverer_role_not_found"}

        channel = self._order_notification_channel_on_bot_loop(guild)
        if channel is None:
            logger.warning(
                "Canal de aviso de pedidos não foi encontrado; configure ORDER_NOTIFICATION_CHANNEL_ID"
            )
            return {"notified": False, "reason": "notification_channel_not_found"}

        item_lines = []
        for item in items:
            title = str(item.get("product_title") or "Produto")
            game = str(item.get("game") or "Jogo")
            product_type = str(item.get("type") or "")
            quantity = int(item.get("quantity") or 1)
            suffix = f" • {product_type}" if product_type else ""
            item_lines.append(f"{quantity}x **{title}** — {game}{suffix}")

        embed = discord.Embed(
            title="📦 Novo pedido para entrega",
            description="Um novo pedido foi aprovado no site e está aguardando entrega.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Pedido", value=f"`{order_code}` (ID `{order_id}`)", inline=True)
        embed.add_field(name="Cliente", value=username, inline=True)
        embed.add_field(
            name="Nick Roblox",
            value=roblox_nick or "Não informado",
            inline=True,
        )
        embed.add_field(
            name="Itens",
            value="\n".join(item_lines)[:1024] or "Nenhum item informado",
            inline=False,
        )
        embed.add_field(
            name="Total",
            value=format_currency(total_amount, "BRL"),
            inline=True,
        )
        embed.add_field(
            name="Próximo passo",
            value=f"Acesse {settings.site_url} e realize a entrega.",
            inline=False,
        )
        embed.set_footer(text="RedStore • fila de entregas")

        try:
            await channel.send(
                content=role.mention,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=[role]),
            )
        except discord.Forbidden:
            logger.warning("Discord recusou o aviso do pedido %s no canal %s", order_code, channel.id)
            return {"notified": False, "reason": "send_forbidden"}
        except discord.HTTPException:
            logger.exception("Falha ao publicar o aviso do pedido %s", order_code)
            return {"notified": False, "reason": "discord_error"}
        return {"notified": True, "channel_id": str(channel.id), "role_id": str(role.id)}

    async def notify_order_created(
        self,
        order_id: int,
        order_code: str,
        username: str,
        roblox_nick: str | None,
        total_amount: Decimal,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return await self._run_on_bot_loop(
            lambda: self._send_order_notification_on_bot_loop(
                order_id,
                order_code,
                username,
                roblox_nick,
                total_amount,
                items,
            )
        )

    async def notify_deposit_pending(
        self,
        deposit_id: int,
        transaction_code: str,
        username: str,
        amount: Decimal,
        payment_amount: Decimal | None = None,
        discount_amount: Decimal | None = None,
        discount_code: str | None = None,
        used_discount_code: bool = False,
    ) -> dict[str, Any]:
        return await self._run_on_bot_loop(
            lambda: self._send_deposit_notification_on_bot_loop(
                deposit_id,
                transaction_code,
                username,
                amount,
                payment_amount,
                discount_amount,
                discount_code,
                used_discount_code,
            )
        )

    async def review_deposit(
        self,
        interaction: discord.Interaction,
        deposit_id: int,
        action: Literal["approve", "reject"],
        view: DepositReviewView,
    ) -> None:
        if interaction.user.id != settings.deposit_notification_discord_id:
            await interaction.response.send_message("Apenas o administrador configurado pode revisar depósitos.")
            return
        if not isinstance(interaction.channel, discord.DMChannel):
            await interaction.response.send_message("Esta ação só pode ser usada na DM de revisão.")
            return
        if deposit_id in self._deposit_review_in_flight:
            await interaction.response.send_message("Este depósito já está sendo processado. Aguarde o resultado.")
            return

        self._deposit_review_in_flight.add(deposit_id)
        for child in view.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.defer()
        await interaction.message.edit(view=view)
        try:
            endpoint = f"/api/internal/admin/deposits/{deposit_id}/{action}"
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    f"{settings.redstore_api_url.rstrip('/')}{endpoint}",
                    headers={"X-Discord-Bridge-Key": settings.redstore_bridge_api_key},
                )

            if response.is_success:
                result = response.json()
                status_text = "aprovado e creditado" if action == "approve" else "rejeitado sem alterar o saldo"
                content = (
                    f"✅ Depósito **{result.get('transactionCode', deposit_id)}** {status_text}. "
                    f"Valor: R$ {Decimal(str(result.get('amount', '0'))):.2f}."
                ).replace(".", ",")
                if action == "approve":
                    discord_id, confirmed_amount = await self._fetch_deposit_summary(deposit_id)
                    if discord_id:
                        try:
                            role_result = await self._sync_deposit_roles_on_bot_loop(
                                str(discord_id), confirmed_amount
                            )
                            tier_name = role_result.get("tier") or "sem cargo"
                            content += f" Cargo de depósito atualizado: **{tier_name}**."
                        except (RuntimeError, httpx.HTTPError) as exc:
                            logger.warning(
                                "Depósito %s aprovado, mas o cargo não foi sincronizado: %s",
                                deposit_id,
                                exc,
                            )
                            content += " O cargo será sincronizado automaticamente em instantes."
            else:
                detail = "Não foi possível concluir a revisão."
                try:
                    body = response.json()
                    detail = body.get("message") or body.get("detail") or detail
                except (ValueError, TypeError):
                    pass
                content = f"⚠️ Revisão não concluída: {detail} O botão foi desativado para evitar duplicidade."
            await interaction.edit_original_response(content=content, view=view)
        except (httpx.RequestError, discord.HTTPException):
            logger.exception("Falha ao processar depósito %s via Discord", deposit_id)
            await interaction.edit_original_response(
                content="⚠️ Não foi possível contactar o backend. Tente novamente em alguns instantes.",
                view=view,
            )
        finally:
            self._deposit_review_in_flight.discard(deposit_id)


class RoleChange(BaseModel):
    role_id: int = Field(gt=0)
    action: Literal["add", "remove"]


class DepositPendingNotification(BaseModel):
    deposit_id: int = Field(gt=0)
    transaction_code: str = Field(min_length=1, max_length=50)
    username: str = Field(min_length=1, max_length=100)
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    payment_amount: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    discount_amount: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    discount_code: str | None = Field(default=None, max_length=50)
    used_discount_code: bool = False


class OrderItemNotification(BaseModel):
    product_title: str = Field(min_length=1, max_length=200)
    game: str = Field(min_length=1, max_length=100)
    type: str = Field(default="", max_length=100)
    quantity: int = Field(gt=0, le=100)


class OrderCreatedNotification(BaseModel):
    order_id: int = Field(gt=0)
    order_code: str = Field(min_length=1, max_length=50)
    username: str = Field(min_length=1, max_length=100)
    roblox_nick: str | None = Field(default=None, max_length=100)
    total_amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    items: list[OrderItemNotification] = Field(min_length=1, max_length=50)


class OAuthExchangeRequest(BaseModel):
    code: str = Field(min_length=20, max_length=200)


class OAuthExchangeStore:
    def __init__(self) -> None:
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}

    def issue(self, payload: dict[str, Any]) -> str:
        self._purge()
        code = secrets.token_urlsafe(48)
        self._items[code] = (time.monotonic() + 60, payload)
        return code

    def consume(self, code: str) -> dict[str, Any]:
        self._purge()
        item = self._items.pop(code, None)
        if not item or item[0] < time.monotonic():
            raise HTTPException(status_code=400, detail="Código de troca OAuth inválido ou expirado")
        return item[1]

    def _purge(self) -> None:
        now = time.monotonic()
        self._items = {code: item for code, item in self._items.items() if item[0] >= now}


store = UserStore(settings.database_path)
tickets = TicketStore(settings.database_path)
proofs = ProofStore(settings.database_path)
oauth = OAuthClient()
bridge = DiscordBridge()
oauth_exchanges = OAuthExchangeStore()
state_signer = TimestampSigner(settings.session_secret, salt="oauth-state")
session_signer = TimestampSigner(settings.session_secret, salt="redstore-session")
bot_task: asyncio.Task[None] | None = None


def sign_value(signer: TimestampSigner, value: str) -> str:
    return signer.sign(value).decode("utf-8")


def read_signed_cookie(
    request: Request,
    name: str,
    signer: TimestampSigner,
    max_age: int,
) -> str | None:
    value = request.cookies.get(name)
    if not value:
        return None
    try:
        return signer.unsign(value, max_age=max_age).decode("utf-8")
    except (BadSignature, SignatureExpired):
        return None


def current_user(request: Request) -> dict[str, Any]:
    discord_id = read_signed_cookie(request, "redstore_session", session_signer, 60 * 60 * 24 * 7)
    if not discord_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Faça login com o Discord")
    user = store.get(discord_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessão inválida")
    return user


def require_api_key(x_redstore_api_key: str | None = Header(default=None)) -> None:
    if not settings.internal_api_key or settings.internal_api_key == "dev-only-change-me":
        raise HTTPException(status_code=503, detail="INTERNAL_API_KEY precisa ser configurada")
    provided = (x_redstore_api_key or "").encode()
    expected = settings.internal_api_key.encode()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key inválida")


def require_bridge_api_key(x_discord_bridge_key: str | None = Header(default=None)) -> None:
    if not settings.redstore_bridge_api_key:
        raise HTTPException(status_code=503, detail="REDSTORE_BRIDGE_API_KEY precisa ser configurada")
    provided = (x_discord_bridge_key or "").encode()
    expected = settings.redstore_bridge_api_key.encode()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key inválida")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global bot_task
    validate_configuration()
    bot_task = asyncio.create_task(bridge.start())
    yield
    await bridge.close()
    if bot_task and not bot_task.done():
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)
    store.close()
    tickets.close()
    proofs.close()


app = FastAPI(title="RedStore Discord Bridge", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-RedStore-Api-Key", "X-Discord-Bridge-Key"],
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", **bridge.health()}


@app.post(
    "/api/v1/notifications/deposit-pending",
    dependencies=[Depends(require_bridge_api_key)],
)
async def notify_deposit_pending(payload: DepositPendingNotification) -> dict[str, Any]:
    return await bridge.notify_deposit_pending(
        payload.deposit_id,
        payload.transaction_code,
        payload.username,
        payload.amount,
        payload.payment_amount,
        payload.discount_amount,
        payload.discount_code,
        payload.used_discount_code,
    )


@app.post(
    "/api/v1/notifications/order-created",
    dependencies=[Depends(require_bridge_api_key)],
)
async def notify_order_created(payload: OrderCreatedNotification) -> dict[str, Any]:
    return await bridge.notify_order_created(
        payload.order_id,
        payload.order_code,
        payload.username,
        payload.roblox_nick,
        payload.total_amount,
        [item.model_dump() for item in payload.items],
    )


@app.get("/auth/discord/login")
async def discord_login(
    flow: Literal["login", "register", "link"] = "login",
    link_token: str | None = None,
    privacy_policy_version: str | None = None,
    privacy_notice_acknowledged: bool = False,
    age_confirmed: bool = False,
    terms_version: str | None = None,
    terms_accepted: bool = False,
) -> RedirectResponse:
    if not settings.discord_client_id or not settings.discord_client_secret:
        raise HTTPException(status_code=503, detail="Credenciais OAuth do Discord não configuradas")
    if flow not in {"login", "register", "link"}:
        raise HTTPException(status_code=400, detail="Fluxo OAuth inválido")
    if flow == "link" and not link_token:
        raise HTTPException(status_code=400, detail="Token para vincular o Discord não informado")
    if flow == "register" and (
        not privacy_policy_version
        or not privacy_notice_acknowledged
        or not age_confirmed
        or not terms_version
        or not terms_accepted
    ):
        raise HTTPException(status_code=400, detail="Aceite os Termos, o Aviso de Privacidade e confirme sua idade")
    state = secrets.token_urlsafe(32)
    state_payload = json.dumps(
        {
            "state": state,
            "flow": flow,
            "linkToken": link_token,
            "privacyPolicyVersion": privacy_policy_version,
            "privacyNoticeAcknowledged": privacy_notice_acknowledged,
            "ageConfirmed": age_confirmed,
            "termsVersion": terms_version,
            "termsAccepted": terms_accepted,
        },
        separators=(",", ":"),
    )
    response = RedirectResponse(oauth.authorization_url(state), status_code=307)
    response.set_cookie(
        "redstore_oauth_state",
        sign_value(state_signer, state_payload),
        max_age=600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    return response


@app.get("/auth/discord/callback")
async def discord_callback(request: Request, code: str | None = None, state: str | None = None) -> RedirectResponse:
    signed_state = read_signed_cookie(request, "redstore_oauth_state", state_signer, 600)
    try:
        state_data = json.loads(signed_state or "")
    except json.JSONDecodeError:
        state_data = {}
    expected_state = state_data.get("state")
    if not code or not state or not expected_state or not hmac.compare_digest(state, expected_state):
        raise HTTPException(status_code=400, detail="State OAuth inválido ou expirado")
    discord_user = await oauth.exchange_code(code)
    if settings.require_guild_membership:
        membership = await bridge.member_snapshot(str(discord_user["id"]))
        if not membership["is_member"]:
            raise HTTPException(status_code=403, detail="Você precisa estar no servidor do RedStore para continuar")
    user = store.upsert(discord_user)
    provision_data: dict[str, Any] = {"flow": state_data.get("flow", "login")}
    if provision_data["flow"] == "register":
        provision_data.update(
            {
                "privacyPolicyVersion": state_data["privacyPolicyVersion"],
                "privacyNoticeAcknowledged": state_data["privacyNoticeAcknowledged"],
                "ageConfirmed": state_data["ageConfirmed"],
                "termsVersion": state_data["termsVersion"],
                "termsAccepted": state_data["termsAccepted"],
            }
        )
    elif provision_data["flow"] == "link":
        provision_data["linkToken"] = state_data.get("linkToken")
    try:
        provisioned_user = await oauth.provision_redstore_user(discord_user, provision_data)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "Não foi possível concluir a autenticação com Discord."
        error_url = f"{settings.site_url.rstrip('/')}/auth/discord/success?{urlencode({'error': detail, 'flow': provision_data['flow']})}"
        response = RedirectResponse(error_url, status_code=303)
        response.delete_cookie("redstore_oauth_state")
        return response
    exchange_code = oauth_exchanges.issue(provisioned_user)
    success_url = f"{settings.site_url.rstrip('/')}/auth/discord/success?{urlencode({'code': exchange_code, 'flow': provision_data['flow']})}"
    response = RedirectResponse(success_url, status_code=303)
    if provision_data["flow"] != "link":
        response.set_cookie(
            "redstore_session",
            sign_value(session_signer, str(user["discord_id"])),
            max_age=60 * 60 * 24 * 7,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
        )
    response.delete_cookie("redstore_oauth_state")
    return response


@app.post("/auth/discord/exchange")
async def exchange_discord_session(payload: OAuthExchangeRequest) -> dict[str, Any]:
    return oauth_exchanges.consume(payload.code)


@app.post("/auth/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie("redstore_session")
    return response


@app.get("/api/v1/me")
async def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    snapshot = await bridge.member_snapshot(user["discord_id"])
    avatar = (
        f"https://cdn.discordapp.com/avatars/{user['discord_id']}/{user['avatar_hash']}.png?size=256"
        if user.get("avatar_hash")
        else None
    )
    return {**user, "avatar_url": avatar, **snapshot}


@app.post("/api/v1/users/{discord_id}/sync", dependencies=[Depends(require_api_key)])
async def sync_user(discord_id: str) -> dict[str, Any]:
    user = store.get(discord_id)
    snapshot = await bridge.member_snapshot(discord_id)
    return {"user": user, **snapshot}


@app.post("/api/v1/users/{discord_id}/roles", dependencies=[Depends(require_api_key)])
async def update_user_role(discord_id: str, change: RoleChange) -> dict[str, Any]:
    return await bridge.change_role(discord_id, change.role_id, change.action)


@app.get("/api/v1/guild", dependencies=[Depends(require_api_key)])
async def guild_info() -> dict[str, Any]:
    return await bridge.guild_details()


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Erro não tratado", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Erro interno do servidor"})


if __name__ == "__main__":
    # pyrefly: ignore [missing-import]
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "80")), reload=False)
