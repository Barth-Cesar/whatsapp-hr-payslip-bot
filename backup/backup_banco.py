# -*- coding: utf-8 -*-
"""
BACKUP DIÁRIO DO BANCO - Chatbot RH (holerites via WhatsApp)
Autor: César Barth · 2026 · linkedin.com/in/barthcesar07

O que faz (uma vez por dia, pelo Agendador de Tarefas):
  1. Exporta o banco rh_database com o pg_dump (inclui cadastro, holerites registrados,
     auditoria e também o próprio n8n: fluxo, usuários e credenciais).
  2. CRIPTOGRAFA o arquivo com uma senha (AES, via biblioteca cryptography).
     Sem a senha ninguém consegue abrir o backup - nem no Google Drive.
  3. Guarda no disco local com o nome do dia da semana (rh_database_seg ... _dom).
     Assim existem sempre 7 cópias: a de cada dia substitui a da semana anterior.
  4. Envia a mesma cópia para o Google Drive (via rclone), substituindo a do mesmo dia.
  5. Registra o resultado em backup.log.

Uso:
  python backup_banco.py                    -> faz o backup
  python backup_banco.py --restaurar ARQ    -> descriptografa ARQ e gera um .backup para o pg_restore
"""
import base64, configparser, datetime, os, subprocess, sys

PASTA = os.path.dirname(os.path.abspath(__file__))
DIAS = ['seg', 'ter', 'qua', 'qui', 'sex', 'sab', 'dom']
MAGICO = b'CRH1'  # identifica o formato do arquivo criptografado


def carregar_config():
    cfg = configparser.RawConfigParser()  # Raw: aceita % e # na senha
    caminho = os.path.join(PASTA, 'backup.ini')
    if not cfg.read(caminho, encoding='utf-8'):
        sys.exit(f'ERRO: não encontrei {caminho}')
    return cfg


def log(cfg, texto):
    linha = f"{datetime.datetime.now():%d/%m/%Y %H:%M:%S}  {texto}"
    print(linha)
    with open(os.path.join(cfg['backup']['pasta_local'], 'backup.log'), 'a', encoding='utf-8') as f:
        f.write(linha + '\n')


def chave(senha, sal):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    k = Scrypt(salt=sal, length=32, n=2 ** 15, r=8, p=1).derive(senha.encode('utf-8'))
    return base64.urlsafe_b64encode(k)


def criptografar(dados, senha):
    from cryptography.fernet import Fernet
    sal = os.urandom(16)
    return MAGICO + sal + Fernet(chave(senha, sal)).encrypt(dados)


def descriptografar(dados, senha):
    from cryptography.fernet import Fernet, InvalidToken
    if not dados.startswith(MAGICO):
        sys.exit('ERRO: este arquivo não é um backup do Chatbot RH.')
    sal, token = dados[4:20], dados[20:]
    try:
        return Fernet(chave(senha, sal)).decrypt(token)
    except InvalidToken:
        sys.exit('ERRO: senha do backup incorreta (ou arquivo corrompido).')


def fazer_backup(cfg):
    b, k = cfg['banco'], cfg['backup']
    os.makedirs(k['pasta_local'], exist_ok=True)
    dia = DIAS[datetime.date.today().weekday()]
    nome = f"rh_database_{dia}.backup.cripto"
    destino = os.path.join(k['pasta_local'], nome)

    # 1) pg_dump direto para a memória (o banco do chatbot é pequeno)
    env = dict(os.environ, PGPASSWORD=b['senha'])
    r = subprocess.run([b.get('pg_dump', 'pg_dump'), '-h', b['host'], '-p', b['porta'], '-U', b['usuario'],
                        '-d', b['banco'], '-F', 'c'], capture_output=True, env=env)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError('pg_dump falhou: ' + r.stderr.decode('utf-8', 'replace').strip())
    bruto = r.stdout

    # 2) criptografa e grava (primeiro num temporário, para nunca deixar arquivo pela metade)
    cifrado = criptografar(bruto, k['senha_backup'])
    tmp = destino + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(cifrado)
    os.replace(tmp, destino)
    log(cfg, f"OK local: {nome} ({len(bruto) / 1024:.0f} KB de banco, {len(cifrado) / 1024:.0f} KB criptografado)")

    # 3) envia ao Google Drive (substitui o arquivo do mesmo dia)
    remoto = k.get('remoto', '').strip()
    if not remoto:
        log(cfg, 'AVISO: envio ao Google Drive desativado (campo "remoto" vazio no backup.ini).')
        return
    cmd = [k.get('rclone', 'rclone'), '--config', k['rclone_config'], 'copyto', destino, f"{remoto}/{nome}"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode != 0:
        raise RuntimeError('envio ao Google Drive falhou: ' + (r.stderr or r.stdout).strip()[-500:])
    log(cfg, f"OK Google Drive: {remoto}/{nome}")


def restaurar(cfg, arquivo):
    with open(arquivo, 'rb') as f:
        dados = descriptografar(f.read(), cfg['backup']['senha_backup'])
    saida = os.path.splitext(arquivo)[0]  # tira o ".cripto" -> fica ".backup"
    with open(saida, 'wb') as f:
        f.write(dados)
    print(f'Arquivo descriptografado: {saida}')
    print('Para restaurar num banco de teste:')
    print('  psql -U postgres -h localhost -c "CREATE DATABASE rh_database_teste;"')
    print(f'  pg_restore -U postgres -h localhost -d rh_database_teste --no-owner "{saida}"')
    print('Apague o arquivo descriptografado depois do uso.')


def main():
    cfg = carregar_config()
    if '--restaurar' in sys.argv:
        i = sys.argv.index('--restaurar')
        if i + 1 >= len(sys.argv):
            sys.exit('Uso: python backup_banco.py --restaurar CAMINHO_DO_ARQUIVO.cripto')
        restaurar(cfg, sys.argv[i + 1])
        return
    try:
        fazer_backup(cfg)
    except Exception as e:
        os.makedirs(cfg['backup']['pasta_local'], exist_ok=True)
        log(cfg, f'ERRO: {e}')
        sys.exit(1)  # o Agendador mostra o resultado diferente de 0


if __name__ == '__main__':
    main()
