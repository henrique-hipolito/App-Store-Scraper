FROM python:3.12-slim

WORKDIR /app

# Copiar requirements e instalar dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código da aplicação
COPY main.py .

# Variáveis de ambiente (podem ser sobrescritas no EasyPanel)
ENV TELEGRAM_BOT_TOKEN=""
ENV TELEGRAM_CHAT_ID=""
ENV CHECK_INTERVAL="60"
ENV TOP_APPS_LIMIT="10"
ENV POSITION_CHANGE_THRESHOLD="3"

# Executar o bot
CMD ["python", "-u", "main.py"]
