# Deploy gratuito: Hanna Acessórios

Este guia usa Render para hospedar o Flask, Neon para PostgreSQL gratuito e UptimeRobot para chamar uma rota leve a cada 5 minutos.

## 1. Antes de subir para o GitHub

Não suba estes arquivos/pastas:

- `.env`
- `venv/`
- `env/`
- `.venv/`
- `__pycache__/`
- `ngrok.exe`
- arquivos `*.log`

Esses itens já estão no `.gitignore`. O arquivo `.env.example` pode subir, porque não tem segredos.

## 2. Banco gratuito no Neon

1. Crie uma conta em `https://neon.com`.
2. Crie um projeto PostgreSQL.
3. Copie a connection string do banco. Ela costuma parecer com:

```text
postgresql://usuario:senha@host.neon.tech/dbname?sslmode=require
```

4. Abra o SQL Editor do Neon e rode as migrações nesta ordem:

```text
migrations/000_initial_schema.sql
migrations/001_security_stock.sql
migrations/002_delivery_orders.sql
```

Se o banco estiver vazio, a `000_initial_schema.sql` cria tudo do zero. As outras completam/garantem os ajustes de segurança, estoque e entrega.

## 3. Deploy no Render

1. Suba o projeto para o GitHub.
2. No Render, clique em New e escolha Web Service.
3. Conecte o repositório.
4. Configure:

Build Command:

```bash
pip install -r requirements.txt
```

Start Command:

```bash
gunicorn app:app
```

5. Em Environment Variables, preencha:

```env
SECRET_KEY=gere_uma_chave_grande
ADMIN_USER=seu_usuario_admin
ADMIN_PASS=sua_senha_admin

CLOUDINARY_CLOUD_NAME=...
CLOUDINARY_API_KEY=...
CLOUDINARY_API_SECRET=...

ASAAS_API_URL=https://sandbox.asaas.com/api/v3
ASAAS_API_KEY=...
ASAAS_WEBHOOK_TOKEN=gere_um_token_grande

WHATSAPP_NUMBER=5586SEUNUMERO
STORE_PICKUP_ADDRESS=Rua Exemplo, 123 - Bairro - Cidade/UF
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DATABASE_URL=postgresql://usuario:senha@host.neon.tech/dbname?sslmode=require
FLASK_ENV=production
```

Use `DATABASE_URL` no Render. As variáveis `DB_HOST`, `DB_NAME`, `DB_USER` e `DB_PASS` podem ficar vazias quando `DATABASE_URL` estiver definida.

## 4. Webhook do Asaas

Depois do deploy, copie a URL do Render, por exemplo:

```text
https://hanna-acessorios.onrender.com
```

No Asaas, configure o webhook:

```text
https://hanna-acessorios.onrender.com/webhook/asaas
```

Use no Asaas o mesmo token que você colocou em:

```env
ASAAS_WEBHOOK_TOKEN=...
```

## 5. UptimeRobot para manter acordado

1. Crie conta em `https://uptimerobot.com`.
2. Clique em New Monitor.
3. Tipo: HTTP(s).
4. URL:

```text
https://hanna-acessorios.onrender.com/health
```

5. Intervalo: 5 minutes.
6. Salve.

A rota `/health` não acessa banco nem imagens, então é leve para manter o serviço ativo.

## 6. Notificação gratuita por Telegram

O sistema pode avisar o dono da loja automaticamente quando o Pix for confirmado.

1. Abra o Telegram e fale com `@BotFather`.
2. Envie `/newbot` e siga os passos.
3. Copie o token do bot e coloque no Render:

```env
TELEGRAM_BOT_TOKEN=token_do_bot
```

4. Envie uma mensagem qualquer para o seu bot.
5. Acesse no navegador, trocando pelo token real:

```text
https://api.telegram.org/botSEU_TOKEN/getUpdates
```

6. Procure o número em `chat.id` e coloque no Render:

```env
TELEGRAM_CHAT_ID=seu_chat_id
```

Quando o pagamento for confirmado, o Telegram recebe produto, valor, cliente, WhatsApp e entrega. Se for retirada, recebe o endereço definido em:

```env
STORE_PICKUP_ADDRESS=...
```

## 7. Observações importantes

- Render Free pode dormir após inatividade e pode reiniciar.
- O filesystem do Render Free é temporário; não salve imagens localmente. Este projeto usa Cloudinary, então está correto.
- Para uma loja com vendas reais, considere migrar para um plano pago barato quando puder.
