"""
Polymarket App Store Monitor Bot
Monitora o ranking da App Store e envia alertas no Telegram quando detecta mudanças significativas
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

# Intervalo de checagem em segundos (recomendado: 60-120 para não sobrecarregar)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

# Limite de apps para monitorar (top N)
TOP_APPS_LIMIT = int(os.getenv("TOP_APPS_LIMIT", "10"))

# Alertar quando app subir X posições de uma vez (para apps fora do top 3)
POSITION_CHANGE_THRESHOLD = int(os.getenv("POSITION_CHANGE_THRESHOLD", "3"))

# Intervalo do heartbeat em segundos (padrão: 6 horas = 21600)
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "21600"))

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
    change: int  # positivo = subiu, negativo = caiu
    is_new_entry: bool = False
    is_new_leader: bool = False
    entered_top3: bool = False

# ============== POLYMARKET LINK ==============
class PolymarketLink:
    """Gera os links dinâmicos do Polymarket baseado na próxima sexta-feira"""
    
    BASE_URL_1 = "https://polymarket.com/event/1-free-app-in-the-us-apple-app-store-on-{month}-{day}"
    BASE_URL_2 = "https://polymarket.com/event/2-free-app-in-the-us-apple-app-store-on-{month}-{day}"
    ET_TIMEZONE = ZoneInfo("America/New_York")
    
    MONTHS = {
        1: "january",
        2: "february", 
        3: "march",
        4: "april",
        5: "may",
        6: "june",
        7: "july",
        8: "august",
        9: "september",
        10: "october",
        11: "november",
        12: "december"
    }
    
    @classmethod
    def _get_target_friday(cls) -> datetime:
        """Retorna a sexta-feira relevante para o mercado atual"""
        now_et = datetime.now(cls.ET_TIMEZONE)
        
        # Encontrar a sexta-feira relevante
        days_until_friday = (4 - now_et.weekday()) % 7  # 4 = sexta-feira
        
        if days_until_friday == 0:
            # Hoje é sexta
            if now_et.hour < 12:
                # Antes do meio-dia, mercado ainda está ativo
                return now_et
            else:
                # Depois do meio-dia, mercado já resolveu, mostrar próxima sexta
                return now_et + timedelta(days=7)
        else:
            # Não é sexta, pegar a próxima
            return now_et + timedelta(days=days_until_friday)
    
    @classmethod
    def _build_link(cls, base_url: str) -> str:
        target = cls._get_target_friday()
        month_name = cls.MONTHS[target.month]
        day = target.day
        return base_url.format(month=month_name, day=day)
    
    @classmethod
    def get_top1_link(cls) -> str:
        """Link do mercado #1 Free App"""
        return cls._build_link(cls.BASE_URL_1)
    
    @classmethod
    def get_top2_link(cls) -> str:
        """Link do mercado #2 Free App"""
        return cls._build_link(cls.BASE_URL_2)
    
    @classmethod
    def get_market_date_str(cls) -> str:
        """Retorna a data do mercado formatada"""
        target = cls._get_target_friday()
        return target.date().strftime("%d/%m/%Y")


# ============== SCRAPER DA APP STORE ==============
class AppStoreScraper:
    """Scraper usando o RSS Feed oficial da Apple"""
    
    # URLs do RSS Feed da Apple (gratuito e oficial)
    RSS_URL_TEMPLATE = "https://rss.applemarketingtools.com/api/v2/{country}/apps/top-free/{limit}/apps.json"
    
    # URL alternativa (legado)
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
        """Busca o ranking atual de apps gratuitos"""
        session = await self.get_session()
        
        # Tentar URL principal primeiro
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
        
        # Fallback para URL legada
        return await self._fetch_legacy()
    
    async def _fetch_legacy(self) -> list[AppRanking]:
        """Fallback usando URL legada do iTunes"""
        session = await self.get_session()
        url = self.LEGACY_RSS_URL.format(country=self.country, limit=self.limit)
        
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    # Apple retorna content-type text/javascript, então lemos como texto e parseamos
                    text = await response.text()
                    data = json.loads(text)
                    return self._parse_legacy_response(data)
                else:
                    logger.error(f"Fallback retornou status {response.status}")
        except Exception as e:
            logger.error(f"Erro no fallback: {e}")
        
        return []
    
    def _parse_v2_response(self, data: dict) -> list[AppRanking]:
        """Parse da resposta do RSS v2"""
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
        """Parse da resposta legada do iTunes"""
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
    """Detecta mudanças significativas no ranking"""
    
    def __init__(self, threshold: int = 3):
        self.threshold = threshold
        self.previous_rankings: dict[str, AppRanking] = {}
        self.previous_leader_id: Optional[str] = None
        self.previous_top3_ids: set[str] = set()
        self.first_run = True
    
    def detect_changes(self, current_rankings: list[AppRanking]) -> list[RankingChange]:
        """Compara ranking atual com o anterior e detecta mudanças"""
        changes = []
        
        # Criar dict do ranking atual
        current_dict = {app.app_id: app for app in current_rankings}
        
        # Identificar líder e top 3 atuais
        current_leader_id = current_rankings[0].app_id if current_rankings else None
        current_top3_ids = {app.app_id for app in current_rankings[:3]}
        
        if self.first_run:
            self.previous_rankings = current_dict
            self.previous_leader_id = current_leader_id
            self.previous_top3_ids = current_top3_ids
            self.first_run = False
            logger.info(f"Primeira execução - baseline estabelecido com {len(current_rankings)} apps")
            return []
        
        # Verificar cada app no ranking atual
        for app_id, app in current_dict.items():
            change_obj = None
            
            # Verificar se é novo líder
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
            
            # Verificar se entrou no top 3 (e não era do top 3 antes)
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
            
            # Verificar subida significativa (para apps fora do top 3)
            elif app_id in self.previous_rankings and app.position > 3:
                old_pos = self.previous_rankings[app_id].position
                new_pos = app.position
                change = old_pos - new_pos  # positivo = subiu
                
                if change >= self.threshold:
                    change_obj = RankingChange(
                        app_name=app.name,
                        old_position=old_pos,
                        new_position=new_pos,
                        change=change,
                    )
            
            # Verificar novo app que já entrou bem posicionado
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
        
        # Atualizar estado anterior
        self.previous_rankings = current_dict
        self.previous_leader_id = current_leader_id
        self.previous_top3_ids = current_top3_ids
        
        return changes


# ============== BOT DO TELEGRAM ==============
class TelegramBot:
    """Envia alertas via Telegram"""
    
    API_URL = "https://api.telegram.org/bot{token}/sendMessage"
    
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Envia mensagem para o chat configurado"""
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
        """Monta os links do Polymarket formatados"""
        market_date = PolymarketLink.get_market_date_str()
        top1_link = PolymarketLink.get_top1_link()
        top2_link = PolymarketLink.get_top2_link()
        return (
            f"🔗 <a href='{top1_link}'>#1 Free App ({market_date})</a>\n"
            f"🔗 <a href='{top2_link}'>#2 Free App ({market_date})</a>"
        )
    
    def _build_top5(self, top_apps: list[AppRanking]) -> str:
        """Monta o top 5 formatado"""
        lines = ["📊 <b>TOP 5 ATUAL:</b>"]
        for app in top_apps[:5]:
            prefix = "👑" if app.position == 1 else f"  #{app.position}"
            lines.append(f"{prefix} - {app.name}")
        return "\n".join(lines)
    
    async def send_alert(self, changes: list[RankingChange], top_apps: list[AppRanking]):
        """Formata e envia alerta de mudanças"""
        
        CROWN = "👑"
        PODIUM = "🏆"
        UP = "🚀"
        NEW = "🆕"
        ALERT = "🚨"
        
        lines = [f"{ALERT} <b>ALERTA - APP STORE</b> {ALERT}\n"]
        
        for change in changes:
            if change.is_new_leader:
                if change.is_new_entry:
                    lines.append(
                        f"{CROWN} <b>NOVO LÍDER!</b> {change.app_name} entrou direto no #1!"
                    )
                else:
                    lines.append(
                        f"{CROWN} <b>NOVO LÍDER!</b> {change.app_name} "
                        f"(#{change.old_position} → #1)"
                    )
            elif change.entered_top3:
                if change.is_new_entry:
                    lines.append(
                        f"{PODIUM} <b>{change.app_name}</b> entrou direto no TOP 3! (#{change.new_position})"
                    )
                else:
                    lines.append(
                        f"{PODIUM} <b>{change.app_name}</b> entrou no TOP 3 "
                        f"(#{change.old_position} → #{change.new_position})"
                    )
            elif change.is_new_entry:
                lines.append(
                    f"{NEW} <b>{change.app_name}</b> entrou no ranking (#{change.new_position})"
                )
            else:
                lines.append(
                    f"{UP} <b>{change.app_name}</b> subiu {change.change} posições "
                    f"(#{change.old_position} → #{change.new_position})"
                )
        
        lines.append(f"\n{self._build_top5(top_apps)}")
        lines.append(f"\n{self._build_market_links()}")
        lines.append(f"\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        message = "\n".join(lines)
        await self.send_message(message)
    
    async def send_startup_message(self, top_apps: list[AppRanking]):
        """Envia mensagem de inicialização"""
        lines = [
            "🤖 <b>Bot iniciado com sucesso!</b>\n",
            f"📊 Monitorando top {len(top_apps)} apps gratuitos",
            f"⏱ Intervalo de checagem: {CHECK_INTERVAL}s",
            f"💓 Heartbeat a cada {HEARTBEAT_INTERVAL // 3600}h",
            f"📈 Alertas: mudança de líder, entrada no top 3, subida de {POSITION_CHANGE_THRESHOLD}+ posições\n",
            self._build_top5(top_apps),
            f"\n{self._build_market_links()}",
            f"\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        
        message = "\n".join(lines)
        await self.send_message(message)
    
    async def send_heartbeat(self, top_apps: list[AppRanking], uptime_hours: float, checks_count: int, alerts_count: int):
        """Envia heartbeat confirmando que o bot está vivo"""
        lines = [
            "💓 <b>HEARTBEAT - Bot ativo</b>\n",
            self._build_top5(top_apps),
            f"\n📈 Verificações: {checks_count}",
            f"🚨 Alertas enviados: {alerts_count}",
            f"⏱ Uptime: {uptime_hours:.1f}h",
            f"\n{self._build_market_links()}",
            f"\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        
        message = "\n".join(lines)
        await self.send_message(message)


# ============== MONITOR PRINCIPAL ==============
class PolymarketMonitor:
    """Monitor principal que orquestra tudo"""
    
    def __init__(self):
        self.scraper = AppStoreScraper(country="us", limit=TOP_APPS_LIMIT)
        self.detector = ChangeDetector(threshold=POSITION_CHANGE_THRESHOLD)
        self.bot = TelegramBot(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.running = False
        self.start_time: Optional[datetime] = None
        self.checks_count = 0
        self.alerts_count = 0
        self.last_successful_rankings: list[AppRanking] = []
    
    async def start(self):
        """Inicia o monitoramento"""
        logger.info("Iniciando monitor...")
        self.running = True
        self.start_time = datetime.now()
        
        # Primeira execução
        try:
            rankings = await self.scraper.fetch_top_free_apps()
            if rankings:
                self.last_successful_rankings = rankings
                await self.bot.send_startup_message(rankings)
                self.detector.detect_changes(rankings)  # Estabelece baseline
            else:
                logger.error("Não foi possível obter rankings iniciais")
                return
        except Exception as e:
            logger.error(f"Erro na inicialização: {e}")
            return
        
        # Rodar loop de checagem e heartbeat em paralelo
        await asyncio.gather(
            self._check_loop(),
            self._heartbeat_loop(),
        )
        
        await self.cleanup()
    
    async def _check_loop(self):
        """Loop principal de verificação de ranking"""
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
        """Loop do heartbeat"""
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
        """Executa uma verificação única"""
        logger.info("Verificando ranking...")
        self.checks_count += 1
        
        rankings = await self.scraper.fetch_top_free_apps()
        if not rankings:
            logger.warning("Nenhum ranking obtido")
            return
        
        self.last_successful_rankings = rankings
        changes = self.detector.detect_changes(rankings)
        
        if changes:
            self.alerts_count += 1
            logger.info(f"Detectadas {len(changes)} mudanças significativas!")
            await self.bot.send_alert(changes, rankings)
        else:
            logger.info(f"Nenhuma mudança significativa. Líder atual: {rankings[0].name}")
    
    async def send_heartbeat(self):
        """Envia heartbeat"""
        uptime_hours = (datetime.now() - self.start_time).total_seconds() / 3600 if self.start_time else 0
        rankings = self.last_successful_rankings
        
        if rankings:
            await self.bot.send_heartbeat(rankings, uptime_hours, self.checks_count, self.alerts_count)
            logger.info("Heartbeat enviado")
        else:
            logger.warning("Sem rankings para heartbeat")
    
    async def cleanup(self):
        """Limpa recursos"""
        await self.scraper.close()
        await self.bot.close()
        logger.info("Recursos liberados")
    
    def stop(self):
        """Para o monitoramento"""
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
