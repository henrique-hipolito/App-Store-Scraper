"""
Polymarket App Store Monitor Bot
Monitora o ranking da App Store e odds do Polymarket, grava histórico e envia alertas no Telegram
"""

import asyncio
import aiohttp
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Optional
import logging

# Configuração de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============== CONFIGURAÇÕES ==============
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8548140243:AAGuAeBCXsxqE0xWAG869fRUlhcdEs-O8io")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "637550079")
DATABASE_URL = os.getenv("DATABASE_URL", "postgres://postgres1:abcd1234@ws_postgres-scraper:5432/as-scraper?sslmode=disable")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
TOP_APPS_LIMIT = int(os.getenv("TOP_APPS_LIMIT", "10"))
POSITION_CHANGE_THRESHOLD = int(os.getenv("POSITION_CHANGE_THRESHOLD", "3"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "43200"))
PRICE_CHANGE_THRESHOLD = float(os.getenv("PRICE_CHANGE_THRESHOLD", "0.01"))  # 1 centavo para salvar no banco
ODDS_ALERT_PERCENT = float(os.getenv("ODDS_ALERT_PERCENT", "9999"))  # 5% para alertar
ODDS_ALERT_MIN_CHANGE = float(os.getenv("ODDS_ALERT_MIN_CHANGE", "0.01"))  # mínimo 1 centavo para alertar

# ============== ESTRUTURAS DE DADOS ==============
@dataclass
class AppRanking:
    position: int
    app_id: str
    name: str
    developer: str
    timestamp: datetime = field(default_factory=datetime.now)

@dataclass
class RankingChange:
    app_name: str
    old_position: Optional[int]
    new_position: int
    change: int
    is_new_entry: bool = False
    is_new_leader: bool = False
    entered_top3: bool = False

@dataclass
class MarketOdds:
    app_name: str
    yes_price: float
    no_price: float
    volume_24h: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)

@dataclass
class OddsChange:
    app_name: str
    old_price: float
    new_price: float
    change_cents: float
    change_percent: float

# ============== DATABASE ==============
class Database:
    """Gerencia conexão e operações com PostgreSQL"""
    
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.pool = None
    
    async def connect(self):
        """Conecta ao banco de dados"""
        try:
            import asyncpg
            self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=5)
            await self._create_tables()
            logger.info("Conectado ao PostgreSQL")
        except Exception as e:
            logger.error(f"Erro ao conectar ao PostgreSQL: {e}")
            self.pool = None
    
    async def close(self):
        """Fecha conexão"""
        if self.pool:
            await self.pool.close()
    
    async def _create_tables(self):
        """Cria tabelas se não existirem"""
        if not self.pool:
            return
        
        async with self.pool.acquire() as conn:
            # Tabela de histórico de ranking
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS ranking_history (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ DEFAULT NOW(),
                    app_id VARCHAR(50),
                    app_name VARCHAR(255),
                    position INTEGER,
                    developer VARCHAR(255)
                )
            ''')
            
            # Tabela de histórico de odds do Polymarket
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS odds_history (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ DEFAULT NOW(),
                    market_type VARCHAR(50),
                    app_name VARCHAR(255),
                    yes_price DECIMAL(5,4),
                    no_price DECIMAL(5,4),
                    volume_24h DECIMAL(15,2),
                    market_date DATE
                )
            ''')
            
            # Tabela de alertas enviados
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS alerts_log (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ DEFAULT NOW(),
                    alert_type VARCHAR(50),
                    app_name VARCHAR(255),
                    details JSONB
                )
            ''')
            
            # Índices
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_ranking_timestamp ON ranking_history(timestamp)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_odds_timestamp ON odds_history(timestamp)')
    
    async def save_ranking(self, rankings: list[AppRanking]):
        """Salva ranking atual se houver mudança"""
        if not self.pool:
            return
        
        async with self.pool.acquire() as conn:
            for app in rankings:
                await conn.execute('''
                    INSERT INTO ranking_history (app_id, app_name, position, developer)
                    VALUES ($1, $2, $3, $4)
                ''', app.app_id, app.name, app.position, app.developer)
    
    async def save_odds(self, market_type: str, odds_list: list[MarketOdds], market_date: datetime) -> list[OddsChange]:
        """Salva odds se houver mudança e retorna mudanças significativas para alerta"""
        changes = []
        
        if not self.pool:
            return changes
        
        async with self.pool.acquire() as conn:
            for odds in odds_list:
                # Verificar último registro
                last = await conn.fetchrow('''
                    SELECT yes_price FROM odds_history 
                    WHERE market_type = $1 AND app_name = $2 
                    ORDER BY timestamp DESC LIMIT 1
                ''', market_type, odds.app_name)
                
                old_price = float(last['yes_price']) if last else None
                
                # Salvar só se mudou mais que o threshold (1¢)
                if not last or abs(old_price - odds.yes_price) >= PRICE_CHANGE_THRESHOLD:
                    await conn.execute('''
                        INSERT INTO odds_history (market_type, app_name, yes_price, no_price, volume_24h, market_date)
                        VALUES ($1, $2, $3, $4, $5, $6)
                    ''', market_type, odds.app_name, odds.yes_price, odds.no_price, odds.volume_24h, market_date.date())
                
                # Verificar se deve alertar (5% E mínimo 1¢)
                if old_price and old_price > 0:
                    change_cents = odds.yes_price - old_price
                    change_percent = change_cents / old_price
                    
                    if abs(change_percent) >= ODDS_ALERT_PERCENT and abs(change_cents) >= ODDS_ALERT_MIN_CHANGE:
                        changes.append(OddsChange(
                            app_name=odds.app_name,
                            old_price=old_price,
                            new_price=odds.yes_price,
                            change_cents=change_cents,
                            change_percent=change_percent
                        ))
        
        return changes
    
    async def save_alert(self, alert_type: str, app_name: str, details: dict):
        """Salva log de alerta"""
        if not self.pool:
            return
        
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO alerts_log (alert_type, app_name, details)
                VALUES ($1, $2, $3)
            ''', alert_type, app_name, json.dumps(details))
    
    async def should_save_ranking(self, rankings: list[AppRanking]) -> bool:
        """Verifica se deve salvar ranking (só se mudou)"""
        if not self.pool:
            logger.debug("Pool não disponível, salvando ranking")
            return True
        
        async with self.pool.acquire() as conn:
            # Pegar último ranking salvo
            rows = await conn.fetch('''
                SELECT DISTINCT ON (app_id) app_id, position 
                FROM ranking_history 
                ORDER BY app_id, timestamp DESC
            ''')
            
            if not rows:
                logger.info("Nenhum ranking anterior, salvando baseline")
                return True
            
            # Criar dict com app_id como string para garantir comparação correta
            last_ranking = {str(row['app_id']): row['position'] for row in rows}
            current_ranking = {str(app.app_id): app.position for app in rankings}
            
            # Verificar se mudou
            changed = False
            for app_id, position in current_ranking.items():
                old_position = last_ranking.get(app_id)
                if old_position != position:
                    logger.info(f"Mudança detectada: {app_id} era #{old_position}, agora #{position}")
                    changed = True
            
            # Verificar apps novos
            new_apps = set(current_ranking.keys()) - set(last_ranking.keys())
            if new_apps:
                logger.info(f"Novos apps detectados: {new_apps}")
                changed = True
            
            if not changed:
                logger.debug("Ranking não mudou, não salvando")
            
            return changed


# ============== POLYMARKET API ==============
class PolymarketAPI:
    """Busca dados do Polymarket via Gamma API"""
    
    BASE_URL = "https://gamma-api.polymarket.com"
    ET_TIMEZONE = ZoneInfo("America/New_York")
    
    MONTHS = {
        1: "january", 2: "february", 3: "march", 4: "april",
        5: "may", 6: "june", 7: "july", 8: "august",
        9: "september", 10: "october", 11: "november", 12: "december"
    }
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
    
    def _get_target_friday(self) -> datetime:
        """Retorna a sexta-feira relevante"""
        now_et = datetime.now(self.ET_TIMEZONE)
        days_until_friday = (4 - now_et.weekday()) % 7
        
        if days_until_friday == 0:
            if now_et.hour < 12:
                return now_et
            else:
                return now_et + timedelta(days=7)
        else:
            return now_et + timedelta(days=days_until_friday)
    
    def _build_slug(self, position: int) -> str:
        """Constrói o slug do evento"""
        target = self._get_target_friday()
        month = self.MONTHS[target.month]
        day = target.day
        return f"{position}-free-app-in-the-us-apple-app-store-on-{month}-{day}"
    
    def get_market_link(self, position: int) -> str:
        """Retorna link do mercado"""
        slug = self._build_slug(position)
        return f"https://polymarket.com/event/{slug}"
    
    def get_market_date(self) -> datetime:
        """Retorna data do mercado"""
        return self._get_target_friday()
    
    def get_market_date_str(self) -> str:
        """Retorna data formatada"""
        return self._get_target_friday().strftime("%d/%m/%Y")
    
    async def fetch_market_odds(self, position: int) -> list[MarketOdds]:
        """Busca odds do mercado #1 ou #2"""
        session = await self.get_session()
        slug = self._build_slug(position)
        
        try:
            url = f"{self.BASE_URL}/events?slug={slug}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    data = await response.json()
                    return self._parse_odds(data)
                else:
                    logger.warning(f"Polymarket API retornou status {response.status}")
        except Exception as e:
            logger.warning(f"Erro ao buscar odds do Polymarket: {e}")
        
        return []
    
    def _parse_odds(self, data: list) -> list[MarketOdds]:
        """Parse das odds do evento"""
        odds_list = []
        
        if not data or len(data) == 0:
            return odds_list
        
        event = data[0]
        markets = event.get("markets", [])
        
        for market in markets:
            # Só processar markets ativos (ignorar placeholders como "App B", "App C")
            if not market.get("active", False):
                continue
            
            # Nome do app vem limpo no groupItemTitle
            app_name = market.get("groupItemTitle", "")
            if not app_name or app_name.startswith("App ") or app_name == "Other":
                continue
            
            outcomes_prices = market.get("outcomePrices", "[]")
            volume_24h = market.get("volume24hr", 0) or 0
            
            # Parse dos preços (vem como string JSON)
            try:
                if isinstance(outcomes_prices, str):
                    prices = json.loads(outcomes_prices)
                else:
                    prices = outcomes_prices
                
                if len(prices) >= 2:
                    yes_price = float(prices[0])
                    no_price = float(prices[1])
                    
                    odds_list.append(MarketOdds(
                        app_name=app_name,
                        yes_price=yes_price,
                        no_price=no_price,
                        volume_24h=float(volume_24h),
                    ))
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Erro ao parsear odds para {app_name}: {e}")
        
        return odds_list


# ============== SCRAPER DA APP STORE ==============
class AppStoreScraper:
    """Scraper usando o RSS Feed oficial da Apple"""
    
    RSS_URL_TEMPLATE = "https://rss.applemarketingtools.com/api/v2/{country}/apps/top-free/{limit}/apps.json"
    LEGACY_RSS_URL = "https://itunes.apple.com/{country}/rss/topfreeapplications/limit={limit}/json"
    
    def __init__(self, country: str = "us", limit: int = 25):
        self.country = country
        self.limit = limit
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    "Accept": "application/json",
                }
            )
        return self.session
    
    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def fetch_top_free_apps(self) -> list[AppRanking]:
        session = await self.get_session()
        url = self.RSS_URL_TEMPLATE.format(country=self.country, limit=self.limit)
        
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    data = await response.json()
                    return self._parse_v2_response(data)
                else:
                    logger.warning(f"RSS v2 retornou status {response.status}, tentando fallback...")
        except Exception as e:
            logger.warning(f"Erro no RSS v2: {e}, tentando fallback...")
        
        return await self._fetch_legacy()
    
    async def _fetch_legacy(self) -> list[AppRanking]:
        session = await self.get_session()
        url = self.LEGACY_RSS_URL.format(country=self.country, limit=self.limit)
        
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    text = await response.text()
                    data = json.loads(text)
                    return self._parse_legacy_response(data)
                else:
                    logger.error(f"Fallback retornou status {response.status}")
        except Exception as e:
            logger.error(f"Erro no fallback: {e}")
        
        return []
    
    def _parse_v2_response(self, data: dict) -> list[AppRanking]:
        rankings = []
        results = data.get("feed", {}).get("results", [])
        
        for idx, app in enumerate(results, start=1):
            rankings.append(AppRanking(
                position=idx,
                app_id=app.get("id", ""),
                name=app.get("name", "Unknown"),
                developer=app.get("artistName", "Unknown"),
            ))
        
        return rankings
    
    def _parse_legacy_response(self, data: dict) -> list[AppRanking]:
        rankings = []
        entries = data.get("feed", {}).get("entry", [])
        
        for idx, app in enumerate(entries, start=1):
            app_id = ""
            if "id" in app and "attributes" in app["id"]:
                app_id = app["id"]["attributes"].get("im:id", "")
            
            name = app.get("im:name", {}).get("label", "Unknown")
            developer = app.get("im:artist", {}).get("label", "Unknown")
            
            rankings.append(AppRanking(
                position=idx,
                app_id=app_id,
                name=name,
                developer=developer,
            ))
        
        return rankings


# ============== DETECTOR DE MUDANÇAS ==============
class ChangeDetector:
    def __init__(self, threshold: int = 3):
        self.threshold = threshold
        self.previous_rankings: dict[str, AppRanking] = {}
        self.previous_leader_id: Optional[str] = None
        self.previous_top3_ids: set[str] = set()
        self.first_run = True
    
    def detect_changes(self, current_rankings: list[AppRanking]) -> list[RankingChange]:
        changes = []
        current_dict = {app.app_id: app for app in current_rankings}
        current_leader_id = current_rankings[0].app_id if current_rankings else None
        current_top3_ids = {app.app_id for app in current_rankings[:3]}
        
        if self.first_run:
            self.previous_rankings = current_dict
            self.previous_leader_id = current_leader_id
            self.previous_top3_ids = current_top3_ids
            self.first_run = False
            logger.info(f"Primeira execução - baseline estabelecido com {len(current_rankings)} apps")
            return []
        
        for app_id, app in current_dict.items():
            change_obj = None
            
            if app.position == 1 and app_id != self.previous_leader_id:
                old_pos = self.previous_rankings[app_id].position if app_id in self.previous_rankings else None
                change_obj = RankingChange(
                    app_name=app.name,
                    old_position=old_pos,
                    new_position=1,
                    change=old_pos - 1 if old_pos else 0,
                    is_new_entry=app_id not in self.previous_rankings,
                    is_new_leader=True,
                )
            elif app_id in current_top3_ids and app_id not in self.previous_top3_ids:
                old_pos = self.previous_rankings[app_id].position if app_id in self.previous_rankings else None
                change_obj = RankingChange(
                    app_name=app.name,
                    old_position=old_pos,
                    new_position=app.position,
                    change=old_pos - app.position if old_pos else 0,
                    is_new_entry=app_id not in self.previous_rankings,
                    entered_top3=True,
                )
            elif app_id in self.previous_rankings and app.position > 3:
                old_pos = self.previous_rankings[app_id].position
                new_pos = app.position
                change = old_pos - new_pos
                
                if change >= self.threshold:
                    change_obj = RankingChange(
                        app_name=app.name,
                        old_position=old_pos,
                        new_position=new_pos,
                        change=change,
                    )
            elif app_id not in self.previous_rankings and app.position <= 5:
                change_obj = RankingChange(
                    app_name=app.name,
                    old_position=None,
                    new_position=app.position,
                    change=0,
                    is_new_entry=True,
                    is_new_leader=app.position == 1,
                    entered_top3=app.position <= 3,
                )
            
            if change_obj:
                changes.append(change_obj)
        
        self.previous_rankings = current_dict
        self.previous_leader_id = current_leader_id
        self.previous_top3_ids = current_top3_ids
        
        return changes


# ============== BOT DO TELEGRAM ==============
class TelegramBot:
    API_URL = "https://api.telegram.org/bot{token}/sendMessage"
    
    def __init__(self, token: str, chat_id: str, polymarket: PolymarketAPI):
        self.token = token
        self.chat_id = chat_id
        self.polymarket = polymarket
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        session = await self.get_session()
        url = self.API_URL.format(token=self.token)
        
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        
        try:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    logger.info("Mensagem enviada com sucesso")
                    return True
                else:
                    error = await response.text()
                    logger.error(f"Erro ao enviar mensagem: {error}")
                    return False
        except Exception as e:
            logger.error(f"Exceção ao enviar mensagem: {e}")
            return False
    
    def _build_market_links(self) -> str:
        market_date = self.polymarket.get_market_date_str()
        link1 = self.polymarket.get_market_link(1)
        link2 = self.polymarket.get_market_link(2)
        return (
            f"🔗 <a href='{link1}'>#1 Free App ({market_date})</a>\n"
            f"🔗 <a href='{link2}'>#2 Free App ({market_date})</a>"
        )
    
    def _build_top5(self, top_apps: list[AppRanking]) -> str:
        lines = ["📊 <b>TOP 5 ATUAL:</b>"]
        for app in top_apps[:5]:
            prefix = "👑" if app.position == 1 else f"  #{app.position}"
            lines.append(f"{prefix} - {app.name}")
        return "\n".join(lines)
    
    def _build_odds_section(self, odds_list: list[MarketOdds]) -> str:
        if not odds_list:
            return ""
        
        lines = ["\n💰 <b>ODDS POLYMARKET:</b>"]
        for odds in sorted(odds_list, key=lambda x: x.yes_price, reverse=True)[:5]:
            price_cents = int(odds.yes_price * 100)
            volume_str = f"${odds.volume_24h:,.0f}" if odds.volume_24h >= 1 else ""
            if volume_str:
                lines.append(f"  {odds.app_name}: {price_cents}¢ (vol: {volume_str})")
            else:
                lines.append(f"  {odds.app_name}: {price_cents}¢")
        return "\n".join(lines)
    
    async def send_alert(self, changes: list[RankingChange], top_apps: list[AppRanking], odds_list: list[MarketOdds] = None):
        CROWN = "👑"
        PODIUM = "🏆"
        UP = "🚀"
        NEW = "🆕"
        ALERT = "🚨"
        
        lines = [f"{ALERT} <b>ALERTA - APP STORE</b> {ALERT}\n"]
        
        for change in changes:
            if change.is_new_leader:
                if change.is_new_entry:
                    lines.append(f"{CROWN} <b>NOVO LÍDER!</b> {change.app_name} entrou direto no #1!")
                else:
                    lines.append(f"{CROWN} <b>NOVO LÍDER!</b> {change.app_name} (#{change.old_position} → #1)")
            elif change.entered_top3:
                if change.is_new_entry:
                    lines.append(f"{PODIUM} <b>{change.app_name}</b> entrou direto no TOP 3! (#{change.new_position})")
                else:
                    lines.append(f"{PODIUM} <b>{change.app_name}</b> entrou no TOP 3 (#{change.old_position} → #{change.new_position})")
            elif change.is_new_entry:
                lines.append(f"{NEW} <b>{change.app_name}</b> entrou no ranking (#{change.new_position})")
            else:
                lines.append(f"{UP} <b>{change.app_name}</b> subiu {change.change} posições (#{change.old_position} → #{change.new_position})")
        
        lines.append(f"\n{self._build_top5(top_apps)}")
        
        if odds_list:
            lines.append(self._build_odds_section(odds_list))
        
        lines.append(f"\n{self._build_market_links()}")
        lines.append(f"\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        message = "\n".join(lines)
        await self.send_message(message)
    
    async def send_odds_alert(self, odds_changes: list[OddsChange], current_odds: list[MarketOdds] = None):
        """Envia alerta quando odds mudam significativamente"""
        CHART = "📈"
        DOWN = "📉"
        ALERT = "💰"
        
        lines = [f"{ALERT} <b>ALERTA - ODDS POLYMARKET</b> {ALERT}\n"]
        
        for change in odds_changes:
            old_cents = int(change.old_price * 100)
            new_cents = int(change.new_price * 100)
            pct = change.change_percent * 100
            
            if change.change_cents > 0:
                emoji = CHART
                direction = "subiu"
            else:
                emoji = DOWN
                direction = "caiu"
            
            lines.append(f"{emoji} <b>{change.app_name}</b> {direction} {abs(pct):.1f}% ({old_cents}¢ → {new_cents}¢)")
        
        if current_odds:
            lines.append(self._build_odds_section(current_odds))
        
        lines.append(f"\n{self._build_market_links()}")
        lines.append(f"\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        message = "\n".join(lines)
        await self.send_message(message)
    
    async def send_startup_message(self, top_apps: list[AppRanking], odds_list: list[MarketOdds] = None):
        lines = [
            "🤖 <b>Bot iniciado com sucesso!</b>\n",
            f"📊 Monitorando top {len(top_apps)} apps gratuitos",
            f"⏱ Intervalo de checagem: {CHECK_INTERVAL}s",
            f"💓 Heartbeat a cada {HEARTBEAT_INTERVAL // 3600}h",
            f"📈 Alertas: líder, top 3, subida de {POSITION_CHANGE_THRESHOLD}+ posições, odds ±5%",
            f"🗄️ Salvando histórico no PostgreSQL\n",
            self._build_top5(top_apps),
        ]
        
        if odds_list:
            lines.append(self._build_odds_section(odds_list))
        
        lines.append(f"\n{self._build_market_links()}")
        lines.append(f"\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        message = "\n".join(lines)
        await self.send_message(message)
    
    async def send_heartbeat(self, top_apps: list[AppRanking], uptime_hours: float, checks_count: int, alerts_count: int, odds_list: list[MarketOdds] = None):
        lines = [
            "💓 <b>HEARTBEAT - Bot ativo</b>\n",
            self._build_top5(top_apps),
        ]
        
        if odds_list:
            lines.append(self._build_odds_section(odds_list))
        
        lines.append(f"\n📈 Verificações: {checks_count}")
        lines.append(f"🚨 Alertas enviados: {alerts_count}")
        lines.append(f"⏱ Uptime: {uptime_hours:.1f}h")
        lines.append(f"\n{self._build_market_links()}")
        lines.append(f"\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        message = "\n".join(lines)
        await self.send_message(message)


# ============== MONITOR PRINCIPAL ==============
class PolymarketMonitor:
    def __init__(self):
        self.scraper = AppStoreScraper(country="us", limit=TOP_APPS_LIMIT)
        self.detector = ChangeDetector(threshold=POSITION_CHANGE_THRESHOLD)
        self.polymarket = PolymarketAPI()
        self.bot = TelegramBot(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, self.polymarket)
        self.db = Database(DATABASE_URL)
        self.running = False
        self.start_time: Optional[datetime] = None
        self.checks_count = 0
        self.alerts_count = 0
        self.last_successful_rankings: list[AppRanking] = []
        self.last_odds: list[MarketOdds] = []
    
    async def start(self):
        logger.info("Iniciando monitor...")
        self.running = True
        self.start_time = datetime.now()
        
        # Conectar ao banco
        await self.db.connect()
        
        # Primeira execução
        try:
            rankings = await self.scraper.fetch_top_free_apps()
            odds = await self.polymarket.fetch_market_odds(1)
            
            if rankings:
                self.last_successful_rankings = rankings
                self.last_odds = odds
                await self.bot.send_startup_message(rankings, odds)
                self.detector.detect_changes(rankings)
                
                # Salvar no banco
                if await self.db.should_save_ranking(rankings):
                    await self.db.save_ranking(rankings)
                if odds:
                    await self.db.save_odds("#1", odds, self.polymarket.get_market_date())
            else:
                logger.error("Não foi possível obter rankings iniciais")
                return
        except Exception as e:
            logger.error(f"Erro na inicialização: {e}")
            return
        
        await asyncio.gather(
            self._check_loop(),
            self._heartbeat_loop(),
        )
        
        await self.cleanup()
    
    async def _check_loop(self):
        while self.running:
            try:
                await asyncio.sleep(CHECK_INTERVAL)
                await self.check_once()
            except asyncio.CancelledError:
                logger.info("Check loop cancelado")
                break
            except Exception as e:
                logger.error(f"Erro no check loop: {e}")
                await asyncio.sleep(30)
    
    async def _heartbeat_loop(self):
        while self.running:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await self.send_heartbeat()
            except asyncio.CancelledError:
                logger.info("Heartbeat loop cancelado")
                break
            except Exception as e:
                logger.error(f"Erro no heartbeat: {e}")
                await asyncio.sleep(60)
    
    async def check_once(self):
        logger.info("Verificando ranking e odds...")
        self.checks_count += 1
        
        rankings = await self.scraper.fetch_top_free_apps()
        if not rankings:
            logger.warning("Nenhum ranking obtido")
            return
        
        self.last_successful_rankings = rankings
        ranking_changes = self.detector.detect_changes(rankings)
        
        # Salvar ranking no banco se mudou
        if await self.db.should_save_ranking(rankings):
            await self.db.save_ranking(rankings)
            logger.info("Ranking salvo no banco")
        
        # Buscar odds a cada check
        odds = await self.polymarket.fetch_market_odds(1)
        odds_changes = []
        if odds:
            self.last_odds = odds
            odds_changes = await self.db.save_odds("#1", odds, self.polymarket.get_market_date())
            if odds_changes:
                logger.info(f"Detectadas {len(odds_changes)} mudanças significativas nas odds!")
        
        # Alertar mudanças no ranking
        if ranking_changes:
            self.alerts_count += 1
            logger.info(f"Detectadas {len(ranking_changes)} mudanças significativas no ranking!")
            
            # Salvar alerta no banco
            for change in ranking_changes:
                await self.db.save_alert(
                    "ranking_change",
                    change.app_name,
                    {
                        "old_position": change.old_position,
                        "new_position": change.new_position,
                        "is_new_leader": change.is_new_leader,
                        "entered_top3": change.entered_top3
                    }
                )
            
            await self.bot.send_alert(ranking_changes, rankings, odds)
        
        # Alertar mudanças nas odds (separado do ranking)
        if odds_changes:
            self.alerts_count += 1
            
            # Salvar alerta no banco
            for change in odds_changes:
                await self.db.save_alert(
                    "odds_change",
                    change.app_name,
                    {
                        "old_price": change.old_price,
                        "new_price": change.new_price,
                        "change_cents": change.change_cents,
                        "change_percent": change.change_percent
                    }
                )
            
            await self.bot.send_odds_alert(odds_changes, odds)
        
        if not ranking_changes and not odds_changes:
            logger.info(f"Nenhuma mudança significativa. Líder atual: {rankings[0].name}")
    
    async def send_heartbeat(self):
        uptime_hours = (datetime.now() - self.start_time).total_seconds() / 3600 if self.start_time else 0
        rankings = self.last_successful_rankings
        
        if rankings:
            # Buscar odds atualizadas
            odds = await self.polymarket.fetch_market_odds(1)
            if odds:
                self.last_odds = odds
                await self.db.save_odds("#1", odds, self.polymarket.get_market_date())
            
            await self.bot.send_heartbeat(rankings, uptime_hours, self.checks_count, self.alerts_count, odds)
            logger.info("Heartbeat enviado")
        else:
            logger.warning("Sem rankings para heartbeat")
    
    async def cleanup(self):
        await self.scraper.close()
        await self.bot.close()
        await self.polymarket.close()
        await self.db.close()
        logger.info("Recursos liberados")
    
    def stop(self):
        self.running = False


# ============== ENTRY POINT ==============
async def main():
    monitor = PolymarketMonitor()
    
    try:
        await monitor.start()
    except KeyboardInterrupt:
        logger.info("Interrompido pelo usuário")
        monitor.stop()


if __name__ == "__main__":
    asyncio.run(main())