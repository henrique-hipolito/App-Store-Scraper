# 🎯 Polymarket App Store Monitor

Bot que monitora o ranking de apps gratuitos da App Store e envia alertas via Telegram quando detecta mudanças significativas - perfeito para operar no mercado "Most Downloaded App" do Polymarket.

## 📊 Como Funciona

1. **Scraper**: Consulta o RSS Feed oficial da Apple a cada X segundos
2. **Detector**: Compara o ranking atual com o anterior
3. **Alertas**: Envia notificação no Telegram quando:
   - Um app sobe/desce 3+ posições de uma vez
   - Um novo app entra no TOP 10

## 🚀 Deploy no EasyPanel

### Passo 1: Criar o Serviço

1. Acesse seu EasyPanel
2. Clique em **"Create Service"** → **"App"**
3. Escolha **"GitHub"** ou **"Docker Image"**

### Passo 2: Se usar GitHub

1. Crie um repositório no GitHub com estes arquivos
2. Conecte o repositório no EasyPanel
3. Configure o build:
   - **Dockerfile Path**: `Dockerfile`
   - **Build Context**: `.`

### Passo 3: Se usar Docker direto

1. No EasyPanel, escolha **"Docker Image"**
2. Aponte para onde você hospedar a imagem

### Passo 4: Configurar Variáveis de Ambiente

No EasyPanel, vá em **Environment** e adicione:

```
TELEGRAM_BOT_TOKEN=8548140243:AAGuAeBCXsxqE0xWAG869fRUlhcdEs-O8io
TELEGRAM_CHAT_ID=637550079
CHECK_INTERVAL=60
TOP_APPS_LIMIT=10
POSITION_CHANGE_THRESHOLD=3
```

### Passo 5: Deploy

1. Clique em **Deploy**
2. Verifique os logs para confirmar que está funcionando
3. Você deve receber uma mensagem de "Bot iniciado" no Telegram

## 🛠️ Desenvolvimento Local

```bash
# Clonar e entrar no diretório
cd polymarket-bot

# Criar ambiente virtual (opcional)
python -m venv venv
source venv/bin/activate  # Linux/Mac
# ou
venv\Scripts\activate  # Windows

# Instalar dependências
pip install -r requirements.txt

# Configurar variáveis (ou criar .env)
export TELEGRAM_BOT_TOKEN="seu_token"
export TELEGRAM_CHAT_ID="seu_chat_id"

# Executar
python main.py
```

Ou com Docker:

```bash
docker-compose up -d
docker-compose logs -f
```

## ⚙️ Configurações

| Variável | Padrão | Descrição |
|----------|--------|-----------|
| `CHECK_INTERVAL` | 60 | Segundos entre cada verificação |
| `TOP_APPS_LIMIT` | 10 | Quantos apps do top monitorar |
| `POSITION_CHANGE_THRESHOLD` | 3 | Mínimo de posições para alertar |

## 📱 Exemplo de Alerta

```
🚨 ALERTA DE RANKING - APP STORE 🚨

🚀 UpScrolled subiu 5 posições (#8 → #3)

📊 TOP 5 ATUAL:
  #1 - ChatGPT
  #2 - TikTok  
  #3 - UpScrolled
  #4 - Instagram
  #5 - WhatsApp

🔗 Polymarket
⏰ 2026-01-30 15:30:00
```

## 🎲 Estratégia de Trading

Baseado no post original:

1. **Monitore**: Deixe o bot rodando 24/7
2. **Aja rápido**: Quando receber alerta de app subindo, vá ao Polymarket
3. **Compre "Yes"** no app que está subindo rapidamente
4. **Seja conservador**: Venda com 2-3x de lucro, não espere o máximo
5. **Não seja ganancioso**: O mercado pode virar a qualquer momento

## ⚠️ Avisos

- Este bot monitora dados públicos da Apple
- Não é conselho financeiro
- Faça sua própria análise antes de operar
- O mercado do Polymarket pode ter riscos

## 🔧 Melhorias Futuras

- [ ] Integrar com API do Polymarket para ver odds em tempo real
- [ ] Alertas mais inteligentes baseados em velocidade de mudança
- [ ] Dashboard web para visualização
- [ ] Suporte a múltiplos países

---

Feito para ajudar a detectar oportunidades no Polymarket. Boa sorte! 🍀
