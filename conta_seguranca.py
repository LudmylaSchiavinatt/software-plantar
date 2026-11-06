"""Minha conta: código próprio da cooperativa, sem guardar o segredo em texto."""
import hmac
import re
import secrets
from flask import abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash


def validar_codigo(codigo, confirmacao, senha=None):
    if not isinstance(codigo, str) or not 8 <= len(codigo) <= 64 or any(c.isspace() for c in codigo):
        return 'Use um código de 8 a 64 caracteres, sem espaços.'
    if not isinstance(confirmacao, str) or not hmac.compare_digest(codigo.encode(), confirmacao.encode()):
        return 'Os códigos informados não coincidem.'
    if senha is not None and hmac.compare_digest(codigo.encode(), senha.encode()):
        return 'Escolha um código diferente da sua senha de acesso.'
    return None


def token_conta():
    if 'csrf_conta' not in session:
        session['csrf_conta'] = secrets.token_urlsafe(32)
    return session['csrf_conta']


def conferir_csrf_conta():
    esperado = session.get('csrf_conta', '')
    informado = request.form.get('csrf_token', '')
    if not esperado or not hmac.compare_digest(esperado.encode(), informado.encode()):
        abort(400, description='Formulário expirado. Atualize a página e tente novamente.')


def instalar_minha_conta(app, get_db, cooperativa_required):
    app.jinja_env.globals['token_conta'] = token_conta
    conn = get_db()
    try:
        conn.execute('ALTER TABLE cooperativas ADD COLUMN IF NOT EXISTS codigo_seguranca_hash TEXT')
        conn.execute('ALTER TABLE cooperativas ADD COLUMN IF NOT EXISTS codigo_seguranca_atualizado_em TIMESTAMPTZ')
        conn.execute('ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS conta_tentativas INTEGER NOT NULL DEFAULT 0')
        conn.execute('ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS conta_bloqueada_ate TIMESTAMPTZ')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    @app.route('/minha-conta', methods=['GET', 'POST'])
    @cooperativa_required
    def minha_conta():
        if request.method == 'POST':
            conferir_csrf_conta()
        conn = get_db()
        try:
            # Não aceita ID de cooperativa ou usuário vindo do formulário.
            conta = conn.execute('''SELECT c.id, c.nome, c.cnpj, c.codigo_seguranca_hash,
                u.id AS usuario_id, u.email, u.senha_hash, u.ativo, u.aprovado,
                COALESCE(u.conta_bloqueada_ate > CURRENT_TIMESTAMP, FALSE) AS bloqueada
                FROM cooperativas c JOIN usuarios u ON u.cooperativa_id = c.id
                WHERE c.id = ? AND u.id = ? AND u.tipo = 'admin' AND c.ativo = TRUE
                FOR UPDATE OF c, u''', (current_user.cooperativa_id, current_user.id)).fetchone()
            if not conta or not conta['ativo'] or not conta['aprovado']:
                abort(403)
            if request.method == 'GET':
                # Nunca passa hashes, contadores ou senhas para o template.
                publico = {k: conta[k] for k in ('nome', 'cnpj', 'email')}
                return render_template('minha_conta.html', conta=publico,
                                       codigo_configurado=bool(conta['codigo_seguranca_hash']))
            if conta['bloqueada']:
                flash('Muitas tentativas incorretas. Aguarde 5 minutos para tentar novamente.', 'warning')
                return redirect(url_for('minha_conta'))

            acao = request.form.get('acao')
            if acao not in ('codigo', 'dados', 'senha'):
                abort(400)

            def recusar_segredo(mensagem):
                conn.execute('''UPDATE usuarios SET
                    conta_tentativas = CASE WHEN conta_bloqueada_ate <= CURRENT_TIMESTAMP THEN 1 ELSE conta_tentativas + 1 END,
                    conta_bloqueada_ate = CASE
                        WHEN conta_bloqueada_ate <= CURRENT_TIMESTAMP THEN NULL
                        WHEN conta_tentativas + 1 >= 5 THEN CURRENT_TIMESTAMP + INTERVAL '5 minutes'
                        ELSE NULL END
                    WHERE id = ? AND cooperativa_id = ?''', (conta['usuario_id'], current_user.cooperativa_id))
                conn.commit()
                flash(mensagem, 'danger')
                return redirect(url_for('minha_conta'))

            senha_atual = request.form.get('senha_atual', '')
            if acao in ('codigo', 'senha'):
                if len(senha_atual) > 256 or not check_password_hash(conta['senha_hash'], senha_atual):
                    return recusar_segredo('Senha atual incorreta.')
            if acao in ('dados', 'senha'):
                if not conta['codigo_seguranca_hash']:
                    flash('Defina primeiro seu código na seção Segurança, confirmando sua senha atual.', 'warning')
                    return redirect(url_for('minha_conta'))
                codigo = request.form.get('codigo_atual', '')
                if len(codigo) > 64 or not check_password_hash(conta['codigo_seguranca_hash'], codigo):
                    return recusar_segredo('Código de segurança incorreto.')

            if acao == 'codigo':
                codigo = request.form.get('novo_codigo', '')
                erro = validar_codigo(codigo, request.form.get('confirma_codigo', ''), senha_atual)
                if erro:
                    flash(erro, 'danger')
                    return redirect(url_for('minha_conta'))
                conn.execute('''UPDATE cooperativas SET codigo_seguranca_hash = ?,
                    codigo_seguranca_atualizado_em = CURRENT_TIMESTAMP WHERE id = ?''',
                    (generate_password_hash(codigo), current_user.cooperativa_id))
                mensagem = 'Código de segurança salvo. O código anterior, se existia, deixou de funcionar.'
            elif acao == 'dados':
                nome = request.form.get('nome', '').strip()
                cnpj = request.form.get('cnpj', '').strip()
                email = request.form.get('email', '').strip()
                if not nome or len(nome) > 150 or len(cnpj) > 20 or len(re.sub(r'\D', '', cnpj)) != 14:
                    flash('Confira o nome da cooperativa e o CNPJ com 14 dígitos.', 'danger')
                    return redirect(url_for('minha_conta'))
                if len(email) > 120 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
                    flash('Informe um e-mail válido.', 'danger')
                    return redirect(url_for('minha_conta'))
                if conn.execute("SELECT id FROM cooperativas WHERE regexp_replace(cnpj, '[^0-9]', '', 'g') = ? AND id <> ?", (re.sub(r'\D', '', cnpj), current_user.cooperativa_id)).fetchone():
                    flash('Esse CNPJ já pertence a outra cooperativa.', 'danger')
                    return redirect(url_for('minha_conta'))
                if conn.execute('SELECT id FROM usuarios WHERE cooperativa_id = ? AND lower(email) = lower(?) AND id <> ?', (current_user.cooperativa_id, email, conta['usuario_id'])).fetchone():
                    flash('Esse e-mail já está cadastrado nesta cooperativa.', 'danger')
                    return redirect(url_for('minha_conta'))
                conn.execute('UPDATE cooperativas SET nome = ?, cnpj = ? WHERE id = ?', (nome, cnpj, current_user.cooperativa_id))
                conn.execute('UPDATE usuarios SET email = ? WHERE id = ? AND cooperativa_id = ?', (email, conta['usuario_id'], current_user.cooperativa_id))
                mensagem = 'Dados da conta atualizados.'
            else:
                nova = request.form.get('nova_senha', '')
                if not 8 <= len(nova) <= 128 or nova != request.form.get('confirma_senha', ''):
                    flash('Informe e confirme uma nova senha de 8 a 128 caracteres.', 'danger')
                    return redirect(url_for('minha_conta'))
                if check_password_hash(conta['codigo_seguranca_hash'], nova):
                    flash('A senha deve ser diferente do código de segurança.', 'danger')
                    return redirect(url_for('minha_conta'))
                conn.execute('UPDATE usuarios SET senha_hash = ? WHERE id = ? AND cooperativa_id = ?', (generate_password_hash(nova), conta['usuario_id'], current_user.cooperativa_id))
                mensagem = 'Senha alterada. Entre novamente com a nova senha.'
            conn.execute('UPDATE usuarios SET conta_tentativas = 0, conta_bloqueada_ate = NULL WHERE id = ? AND cooperativa_id = ?', (conta['usuario_id'], current_user.cooperativa_id))
            conn.commit()
            if acao == 'senha':
                logout_user()
                session.pop('csrf_conta', None)
            flash(mensagem, 'success')
            return redirect(url_for('landing' if acao == 'senha' else 'minha_conta'))
        except Exception as exc:
            conn.rollback()
            # Erros HTTP permanecem com seu status original; detalhes do banco não são exibidos.
            from werkzeug.exceptions import HTTPException
            if isinstance(exc, HTTPException):
                raise
            current_app.logger.exception('Falha ao atualizar a conta da cooperativa %s', current_user.cooperativa_id)
            if request.method == 'GET':
                return 'Não foi possível carregar Minha conta. Volte ao painel e tente novamente.', 503
            flash('Não foi possível salvar. Nenhuma alteração desta operação foi aplicada.', 'danger')
            return redirect(url_for('minha_conta'))
        finally:
            conn.close()
