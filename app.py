import os
import psycopg2
import cloudinary
import cloudinary.uploader
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from dotenv import load_dotenv
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user

# ==========================================
# 1. CONFIGURAÇÕES INICIAIS
# ==========================================
load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")

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
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASS")
    )

class User(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    if user_id == os.getenv("ADMIN_USER"):
        return User(user_id)
    return None

# ==========================================
# 2. ROTAS PÚBLICAS (VITRINE E CHECKOUT)
# ==========================================
@app.route('/')
def vitrine():
    cat_id = request.args.get('categoria')
    busca = request.args.get('busca')
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT * FROM categorias ORDER BY nome ASC")
    categorias = cur.fetchall()
    
    query = "SELECT id, nome, preco, categoria_id, foto_url, quantidade FROM produtos WHERE 1=1"
    params = []
    
    if cat_id:
        query += " AND categoria_id = %s"
        params.append(cat_id)
    if busca:
        query += " AND nome ILIKE %s"
        params.append(f'%{busca}%')
        
    query += " ORDER BY id DESC"
    
    cur.execute(query, tuple(params))
    produtos = cur.fetchall()
    
    cur.close()
    conn.close()
    return render_template('loja.html', produtos=produtos, categorias=categorias)

@app.route('/checkout/<int:id>')
def checkout(id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, nome, preco, foto_url, quantidade FROM produtos WHERE id = %s", (id,))
    produto = cur.fetchone()
    cur.close()
    conn.close()
    
    if not produto or produto[4] <= 0:
        flash("Este produto esgotou ou está indisponível no momento.")
        return redirect(url_for('vitrine'))
        
    return render_template('checkout.html', produto=produto)

@app.route('/processar_pagamento/<int:id>', methods=['POST'])
def processar_pagamento(id):
    nome = request.form.get('nome')
    cpf = request.form.get('cpf')
    email = request.form.get('email')
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT nome, preco FROM produtos WHERE id = %s", (id,))
    prod = cur.fetchone()
    
    headers = {
        "access_token": os.getenv("ASAAS_API_KEY"),
        "Content-Type": "application/json"
    }
    
    # A. Criar Cliente no Asaas
    cliente_payload = {"name": nome, "cpfCnpj": cpf, "email": email}
    c_resp = requests.post(f"{os.getenv('ASAAS_API_URL')}/customers", json=cliente_payload, headers=headers).json()
    
    if 'id' not in c_resp:
        flash("Erro ao validar seus dados. Verifique o CPF e tente novamente.")
        return redirect(url_for('checkout', id=id))
    
    # B. Criar Cobrança no Asaas (APENAS PIX)
    cobranca_payload = {
        "customer": c_resp['id'],
        "billingType": "PIX", 
        "value": float(prod[1]),
        "dueDate": (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d'),
        "description": f"Hanna Acessórios - {prod[0]}"
    }
    p_resp = requests.post(f"{os.getenv('ASAAS_API_URL')}/payments", json=cobranca_payload, headers=headers).json()
    
    if 'id' not in p_resp:
        flash("Erro ao gerar link de pagamento PIX.")
        return redirect(url_for('checkout', id=id))

    pagamento_id = p_resp['id']

    # B.2 Pedir o QR Code e o Copia-e-Cola
    qr_resp = requests.get(f"{os.getenv('ASAAS_API_URL')}/payments/{pagamento_id}/pixQrCode", headers=headers).json()

    # C. Salvar a Venda no Banco de Dados
    cur.execute(
        "INSERT INTO vendas (asaas_id, produto_id, cliente_nome, valor) VALUES (%s, %s, %s, %s)",
        (pagamento_id, id, nome, prod[1])
    )
    conn.commit()
    cur.close()
    conn.close()
    
    # D. Renderizar nossa tela passando os dados e o ID para o Polling
    return render_template('pix.html', 
                           qr_image=qr_resp['encodedImage'], 
                           copia_cola=qr_resp['payload'], 
                           produto=prod,
                           asaas_id=pagamento_id)

# ==========================================
# 3. ROTAS DE VERIFICAÇÃO E WEBHOOK
# ==========================================

# --- NOVA ROTA: O Javascript chama aqui a cada 3 segundos ---
@app.route('/verificar_status/<asaas_id>')
def verificar_status(asaas_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT status FROM vendas WHERE asaas_id = %s", (asaas_id,))
    venda = cur.fetchone()
    cur.close()
    conn.close()
    
    if venda:
        return jsonify({"status": venda[0]})
    return jsonify({"status": "ERRO"})

# --- O Asaas chama aqui quando o PIX é pago ---
from flask import request, jsonify

@app.route('/webhook/asaas', methods=['POST'])
def webhook_asaas():
    # 1. Recebe os dados enviados pelo Asaas
    data = request.get_json()

    # 2. Verifica se o evento é de pagamento confirmado ou recebido
    if data.get('event') in ['PAYMENT_CONFIRMED', 'PAYMENT_RECEIVED']:
        payment_id = data['payment']['id']
        
        # Log para você acompanhar no painel do Render
        print(f"Pagamento confirmado recebido: {payment_id}")

        try:
            # 3. Conecta ao seu banco Supabase
            conn = get_db_connection()
            cur = conn.cursor()
            
            cur.execute("""
                UPDATE vendas 
                SET status = 'pago' 
                WHERE asaas_id = %s
            """, (payment_id,))

            conn.commit()
            cur.close()
            conn.close()
            
            print(f"Pedido {payment_id} atualizado com sucesso no banco!")
            
        except Exception as e:
            print(f"Erro ao atualizar banco de dados: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

    # 5. Retorna 200 para o Asaas parar de tentar enviar esse mesmo evento
    return jsonify({"status": "success"}), 200

# ==========================================
# 4. ROTAS DE AUTENTICAÇÃO (LOGIN/LOGOUT)
# ==========================================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = request.form['username']
        pw = request.form['password']
        
        if user == os.getenv("ADMIN_USER") and pw == os.getenv("ADMIN_PASS"):
            login_user(User(user))
            return redirect(url_for('admin_panel'))
            
        flash('Usuário ou senha incorretos.')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('vitrine'))

# ==========================================
# 5. ROTAS ADMINISTRATIVAS (PROTEGIDAS)
# ==========================================
@app.route('/admin')
@login_required
def admin_panel():
    busca = request.args.get('busca')
    
    conn = get_db_connection()
    cur = conn.cursor()
    
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
        params.append(f'%{busca}%')
        
    query += " ORDER BY p.id DESC"
    
    cur.execute(query, tuple(params))
    produtos = cur.fetchall()
    
    cur.close()
    conn.close()
    return render_template('index.html', produtos=produtos, categorias=categorias)

@app.route('/cadastrar_categoria', methods=['POST'])
@login_required
def add_categoria():
    nome = request.form.get('nome_categoria')
    if nome:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO categorias (nome) VALUES (%s)", (nome,))
            conn.commit()
        except Exception as e:
            conn.rollback()
        finally:
            cur.close()
            conn.close()
    return redirect(url_for('admin_panel'))

@app.route('/cadastrar_produto', methods=['POST'])
@login_required
def add_produto():
    nome = request.form.get('nome')
    preco = request.form.get('preco')
    quantidade = request.form.get('quantidade')
    categoria_id = request.form.get('categoria_id')
    foto = request.files.get('foto')

    if foto and nome and preco and quantidade:
        upload = cloudinary.uploader.upload(foto)
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO produtos (nome, preco, categoria_id, foto_url, quantidade) VALUES (%s, %s, %s, %s, %s)", 
            (nome, preco, categoria_id, upload['secure_url'], quantidade)
        )
        conn.commit()
        cur.close()
        conn.close()
        
    return redirect(url_for('admin_panel'))

@app.route('/editar_item/<int:id>', methods=['POST'])
@login_required
def editar_item(id):
    novo_preco = request.form.get('novo_preco')
    nova_qtd = request.form.get('nova_qtd')
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE produtos SET preco = %s, quantidade = %s WHERE id = %s", (novo_preco, nova_qtd, id))
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('admin_panel'))

@app.route('/excluir_produto/<int:id>')
@login_required
def excluir_produto(id):
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT foto_url FROM produtos WHERE id = %s", (id,))
    produto = cur.fetchone()
    if produto:
        public_id = produto[0].split('/')[-1].split('.')[0]
        try:
            cloudinary.uploader.destroy(public_id)
        except:
            pass
            
    cur.execute("DELETE FROM produtos WHERE id = %s", (id,))
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('admin_panel'))

if __name__ == '__main__':
    app.run(debug=True)