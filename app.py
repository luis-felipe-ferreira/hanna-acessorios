import hmac
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import cloudinary
import cloudinary.uploader
import psycopg2
import requests
from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, UserMixin, login_required, login_user, logout_user

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")
ASAAS_TIMEOUT = 10
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("FLASK_ENV") == "production",
)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)


def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url)

    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASS")
    )


@contextmanager
def db_cursor(commit=False):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        yield conn, cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def get_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def validate_csrf_token():
    session_token = session.get("_csrf_token")
    submitted_token = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
    if not session_token or not submitted_token or not hmac.compare_digest(session_token, submitted_token):
        abort(400)


@app.context_processor
def inject_csrf_token():
    return {
        "csrf_token": get_csrf_token,
        "whatsapp_number": os.getenv("WHATSAPP_NUMBER", "5586999999999"),
        "store_pickup_address": os.getenv("STORE_PICKUP_ADDRESS", "Ponto marcado no mapa — Chapadinha/MA"),
    }


@app.before_request
def protect_forms_from_csrf():
    if request.method == "POST" and request.endpoint != "webhook_asaas":
        validate_csrf_token()


@app.template_filter("brl")
def format_brl(value):
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError):
        return value

    formatted = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


@app.template_filter("whatsapp")
def format_whatsapp(value):
    digits = "".join(char for char in str(value or "") if char.isdigit())
    if digits and not digits.startswith("55"):
        digits = f"55{digits}"
    return digits


def send_owner_notification(message):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        print("Telegram não configurado; notificação do pedido não enviada.")
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=ASAAS_TIMEOUT,
        ).raise_for_status()
    except requests.RequestException as exc:
        print(f"Erro ao enviar notificação no Telegram: {exc}")


def build_order_notification(cur, payment_id, status):
    cur.execute(
        """
        SELECT
            v.id,
            v.cliente_nome,
            v.cliente_telefone,
            v.valor,
            v.entrega_tipo,
            v.entrega_cep,
            v.entrega_rua,
            v.entrega_numero,
            v.entrega_complemento,
            v.entrega_bairro,
            v.entrega_cidade,
            v.entrega_estado,
            p.nome
        FROM vendas v
        LEFT JOIN produtos p ON p.id = v.produto_id
        WHERE v.asaas_id = %s
        """,
        (payment_id,)
    )
    pedido = cur.fetchone()
    if not pedido:
        return None

    (
        venda_id,
        cliente_nome,
        cliente_telefone,
        valor,
        entrega_tipo,
        entrega_cep,
        entrega_rua,
        entrega_numero,
        entrega_complemento,
        entrega_bairro,
        entrega_cidade,
        entrega_estado,
        produto_nome,
    ) = pedido

    if entrega_tipo == "retirada":
        recebimento = f"Retirada na loja\nEndereço: {os.getenv('STORE_PICKUP_ADDRESS', 'Endereço da loja não configurado')}"
    else:
        complemento = f" - {entrega_complemento}" if entrega_complemento else ""
        recebimento = (
            "Entrega\n"
            f"Endereço: {entrega_rua}, {entrega_numero}{complemento}\n"
            f"Bairro: {entrega_bairro}\n"
            f"Cidade/UF: {entrega_cidade}/{entrega_estado}\n"
            f"CEP: {entrega_cep}"
        )

    titulo = "Pagamento confirmado" if status == "pago" else "Pagamento recebido sem estoque"
    return (
        f"<b>{titulo}</b>\n"
        f"Pedido: #{venda_id}\n"
        f"Produto: {produto_nome or 'Produto removido'}\n"
        f"Valor: {format_brl(valor)}\n"
        f"Cliente: {cliente_nome}\n"
        f"WhatsApp: {cliente_telefone or 'Não informado'}\n"
        f"Recebimento: {recebimento}\n"
        f"Asaas: {payment_id}"
    )


def asaas_headers():
    return {
        "access_token": os.getenv("ASAAS_API_KEY"),
        "Content-Type": "application/json"
    }


def asaas_request(method, path, **kwargs):
    base_url = os.getenv("ASAAS_API_URL", "").rstrip("/")
    response = requests.request(
        method,
        f"{base_url}{path}",
        headers=asaas_headers(),
        timeout=ASAAS_TIMEOUT,
        **kwargs
    )
    response.raise_for_status()
    return response.json()


def validate_asaas_webhook():
    expected_token = os.getenv("ASAAS_WEBHOOK_TOKEN")
    if not expected_token:
        print("ASAAS_WEBHOOK_TOKEN não configurado; webhook recusado por segurança.")
        return False

    candidates = [
        request.headers.get("asaas-access-token"),
        request.headers.get("access_token"),
        request.headers.get("Authorization", "").removeprefix("Bearer ").strip(),
    ]
    return any(token and hmac.compare_digest(token, expected_token) for token in candidates)


class User(UserMixin):
    def __init__(self, id):
        self.id = id


@login_manager.user_loader
def load_user(user_id):
    if user_id == os.getenv("ADMIN_USER"):
        return User(user_id)
    return None


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/")
def vitrine():
    cat_id = request.args.get("categoria")
    busca = request.args.get("busca")

    with db_cursor() as (_, cur):
        cur.execute("SELECT * FROM categorias ORDER BY nome ASC")
        categorias = cur.fetchall()

        query = "SELECT id, nome, preco, categoria_id, foto_url, quantidade FROM produtos WHERE 1=1"
        params = []

        if cat_id:
            query += " AND categoria_id = %s"
            params.append(cat_id)
        if busca:
            query += " AND nome ILIKE %s"
            params.append(f"%{busca}%")

        query += " ORDER BY id DESC"
        cur.execute(query, tuple(params))
        produtos = cur.fetchall()

    return render_template("loja.html", produtos=produtos, categorias=categorias)


@app.route("/checkout/<int:id>")
def checkout(id):
    with db_cursor() as (_, cur):
        cur.execute("SELECT id, nome, preco, foto_url, quantidade FROM produtos WHERE id = %s", (id,))
        produto = cur.fetchone()

    if not produto or produto[4] <= 0:
        flash("Este produto esgotou ou está indisponível no momento.")
        return redirect(url_for("vitrine"))

    return render_template("checkout.html", produto=produto)


@app.route("/processar_pagamento/<int:id>", methods=["POST"])
def processar_pagamento(id):
    nome = request.form.get("nome")
    cpf = request.form.get("cpf")
    email = request.form.get("email")
    telefone = request.form.get("telefone")
    entrega_tipo = request.form.get("entrega_tipo", "entrega")
    entrega_cep = request.form.get("entrega_cep")
    entrega_rua = request.form.get("entrega_rua")
    entrega_numero = request.form.get("entrega_numero")
    entrega_complemento = request.form.get("entrega_complemento")
    entrega_bairro = request.form.get("entrega_bairro")
    entrega_cidade = request.form.get("entrega_cidade")
    entrega_estado = request.form.get("entrega_estado")

    campos_obrigatorios = [nome, cpf, email, telefone]
    if entrega_tipo == "entrega":
        campos_obrigatorios.extend([
            entrega_cep,
            entrega_rua,
            entrega_numero,
            entrega_bairro,
            entrega_cidade,
            entrega_estado,
        ])

    if not all(campos_obrigatorios):
        flash("Preencha todos os dados obrigatórios para finalizar a compra.")
        return redirect(url_for("checkout", id=id))

    with db_cursor() as (_, cur):
        cur.execute("SELECT id, nome, preco, foto_url, quantidade FROM produtos WHERE id = %s", (id,))
        produto = cur.fetchone()

    if not produto or produto[4] <= 0:
        flash("Este produto esgotou ou está indisponível no momento.")
        return redirect(url_for("vitrine"))

    try:
        cliente_payload = {"name": nome, "cpfCnpj": cpf, "email": email}
        c_resp = asaas_request("post", "/customers", json=cliente_payload)

        cobranca_payload = {
            "customer": c_resp["id"],
            "billingType": "PIX",
            "value": float(produto[2]),
            "dueDate": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
            "description": f"Hanna Acessórios - {produto[1]}"
        }
        p_resp = asaas_request("post", "/payments", json=cobranca_payload)
        pagamento_id = p_resp["id"]

        qr_resp = asaas_request("get", f"/payments/{pagamento_id}/pixQrCode")
    except (requests.RequestException, KeyError, ValueError) as exc:
        print(f"Erro ao gerar cobrança Asaas: {exc}")
        flash("Erro ao gerar o pagamento PIX. Tente novamente em alguns instantes.")
        return redirect(url_for("checkout", id=id))

    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            INSERT INTO vendas (
                asaas_id, produto_id, cliente_nome, cliente_cpf, cliente_email, cliente_telefone,
                valor, status, entrega_tipo, entrega_cep, entrega_rua, entrega_numero,
                entrega_complemento, entrega_bairro, entrega_cidade, entrega_estado
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pendente', %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                pagamento_id, id, nome, cpf, email, telefone, produto[2], entrega_tipo,
                entrega_cep, entrega_rua, entrega_numero, entrega_complemento,
                entrega_bairro, entrega_cidade, entrega_estado
            )
        )

    return render_template(
        "pix.html",
        qr_image=qr_resp["encodedImage"],
        copia_cola=qr_resp["payload"],
        produto=(produto[1], produto[2]),
        asaas_id=pagamento_id
    )


@app.route("/verificar_status/<asaas_id>")
def verificar_status(asaas_id):
    with db_cursor() as (_, cur):
        cur.execute("SELECT status FROM vendas WHERE asaas_id = %s", (asaas_id,))
        venda = cur.fetchone()

    if venda:
        return jsonify({"status": venda[0]})
    return jsonify({"status": "ERRO"})


@app.route("/webhook/asaas", methods=["POST"])
def webhook_asaas():
    if not validate_asaas_webhook():
        return jsonify({"status": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "ignored"}), 200

    if data.get("event") in ["PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"]:
        payment_id = data.get("payment", {}).get("id")
        if not payment_id:
            return jsonify({"status": "ignored"}), 200

        print(f"Pagamento confirmado recebido: {payment_id}")

        try:
            notification_status = None
            notification_message = None

            with db_cursor(commit=True) as (_, cur):
                cur.execute(
                    """
                    SELECT produto_id
                    FROM vendas
                    WHERE asaas_id = %s
                      AND COALESCE(status, '') NOT IN ('pago', 'estoque_indisponivel')
                    FOR UPDATE
                    """,
                    (payment_id,)
                )
                venda = cur.fetchone()

                if venda:
                    cur.execute(
                        """
                        UPDATE produtos
                        SET quantidade = quantidade - 1
                        WHERE id = %s
                          AND quantidade > 0
                        RETURNING quantidade
                        """,
                        (venda[0],)
                    )
                    estoque = cur.fetchone()

                    if estoque:
                        cur.execute(
                            "UPDATE vendas SET status = 'pago' WHERE asaas_id = %s",
                            (payment_id,)
                        )
                        notification_status = "pago"
                    else:
                        cur.execute(
                            "UPDATE vendas SET status = 'estoque_indisponivel' WHERE asaas_id = %s",
                            (payment_id,)
                        )
                        notification_status = "estoque_indisponivel"
                        print(f"Pagamento {payment_id} recebido, mas o produto está sem estoque.")

                    notification_message = build_order_notification(cur, payment_id, notification_status)

            print(f"Pedido {payment_id} atualizado com sucesso no banco!")
            if notification_message:
                send_owner_notification(notification_message)
        except Exception as e:
            print(f"Erro ao atualizar banco de dados: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "success"}), 200


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form["username"]
        pw = request.form["password"]

        if user == os.getenv("ADMIN_USER") and pw == os.getenv("ADMIN_PASS"):
            login_user(User(user))
            return redirect(url_for("admin_panel"))

        flash("Usuário ou senha incorretos.")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("vitrine"))


@app.route("/admin")
@login_required
def admin_panel():
    busca = request.args.get("busca")

    with db_cursor() as (_, cur):
        cur.execute("SELECT * FROM categorias ORDER BY nome ASC")
        categorias = cur.fetchall()

        query = """
            SELECT p.id, p.nome, p.preco, p.categoria_id, p.foto_url, p.quantidade, c.nome
            FROM produtos p
            LEFT JOIN categorias c ON p.categoria_id = c.id
            WHERE 1=1
        """
        params = []

        if busca:
            query += " AND p.nome ILIKE %s"
            params.append(f"%{busca}%")

        query += " ORDER BY p.id DESC"

        cur.execute(query, tuple(params))
        produtos = cur.fetchall()

    return render_template("index.html", produtos=produtos, categorias=categorias)


@app.route("/pedidos")
@login_required
def pedidos():
    with db_cursor() as (_, cur):
        cur.execute(
            """
            SELECT
                v.id,
                v.asaas_id,
                v.cliente_nome,
                v.cliente_telefone,
                v.valor,
                v.status,
                v.entrega_tipo,
                v.entrega_cep,
                v.entrega_rua,
                v.entrega_numero,
                v.entrega_complemento,
                v.entrega_bairro,
                v.entrega_cidade,
                v.entrega_estado,
                v.created_at,
                p.nome
            FROM vendas v
            LEFT JOIN produtos p ON p.id = v.produto_id
            ORDER BY v.created_at DESC, v.id DESC
            LIMIT 200
            """
        )
        pedidos_lista = cur.fetchall()

    return render_template("pedidos.html", pedidos=pedidos_lista)


@app.route("/cadastrar_categoria", methods=["POST"])
@login_required
def add_categoria():
    nome = request.form.get("nome_categoria")
    if nome:
        try:
            with db_cursor(commit=True) as (_, cur):
                cur.execute("INSERT INTO categorias (nome) VALUES (%s)", (nome,))
        except Exception as e:
            print(f"Erro ao cadastrar categoria: {e}")
            flash("Erro ao cadastrar categoria.")
    return redirect(url_for("admin_panel"))


@app.route("/cadastrar_produto", methods=["POST"])
@login_required
def add_produto():
    nome = request.form.get("nome")
    preco = request.form.get("preco")
    quantidade = request.form.get("quantidade")
    categoria_id = request.form.get("categoria_id")
    foto = request.files.get("foto")

    if foto and nome and preco and quantidade:
        try:
            upload = cloudinary.uploader.upload(foto)

            with db_cursor(commit=True) as (_, cur):
                cur.execute(
                    "INSERT INTO produtos (nome, preco, categoria_id, foto_url, quantidade) VALUES (%s, %s, %s, %s, %s)",
                    (nome, preco, categoria_id, upload["secure_url"], quantidade)
                )
        except Exception as e:
            print(f"Erro ao cadastrar produto: {e}")
            flash("Erro ao cadastrar produto.")

    return redirect(url_for("admin_panel"))


@app.route("/editar_item/<int:id>", methods=["POST"])
@login_required
def editar_item(id):
    novo_preco = request.form.get("novo_preco")
    nova_qtd = request.form.get("nova_qtd")

    with db_cursor(commit=True) as (_, cur):
        cur.execute("UPDATE produtos SET preco = %s, quantidade = %s WHERE id = %s", (novo_preco, nova_qtd, id))
    return redirect(url_for("admin_panel"))


@app.route("/excluir_produto/<int:id>", methods=["POST"])
@login_required
def excluir_produto(id):
    with db_cursor() as (_, cur):
        cur.execute("SELECT foto_url FROM produtos WHERE id = %s", (id,))
        produto = cur.fetchone()

    if produto and produto[0]:
        public_id = produto[0].split("/")[-1].split(".")[0]
        try:
            cloudinary.uploader.destroy(public_id)
        except Exception as e:
            print(f"Erro ao remover imagem do Cloudinary: {e}")

    with db_cursor(commit=True) as (_, cur):
        # 1. Desvincula o produto do histórico de vendas para evitar erro de chave estrangeira
        cur.execute("UPDATE vendas SET produto_id = NULL WHERE produto_id = %s", (id,))
        
        # 2. Deleta o produto do estoque
        cur.execute("DELETE FROM produtos WHERE id = %s", (id,))
        
    return redirect(url_for("admin_panel"))


if __name__ == "__main__":
    app.run(debug=True)
