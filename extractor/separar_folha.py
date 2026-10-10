# -*- coding: utf-8 -*-
"""
SEPARADOR DE HOLERITES - Chatbot RH (holerites via WhatsApp)
Autor: César Barth · 2026 · linkedin.com/in/barthcesar07

O RH coloca o PDF único da folha (todas as páginas juntas) na pasta de ENTRADA.
Este script:
  1. Lê cada página e cada via pelo CAMPO "Código" do cabeçalho (não busca números soltos no texto).
  2. Valida cada via: código numérico, código existe no cadastro, nome do PDF = nome do cadastro,
     competência e tipo de folha encontrados, sem duplicidade.
  3. Guarda UMA via por documento (se as duas vias da página forem iguais, guarda só a de cima;
     se forem documentos diferentes, ex.: Mensal e Adiantamento, guarda cada uma).
  4. Salva em  pdfs\\holerites\\AAAA\\AAAA-MM\\<codigo>_<tipo>.pdf  e registra no banco.
  5. Qualquer via que falhe em alguma validação vai para PENDÊNCIAS e NÃO fica disponível no bot.
  6. Gera um relatório e move o PDF original para PROCESSADOS (a pasta de entrada fica vazia).

Pode rodar agendado a cada 10 minutos: se a pasta de entrada estiver vazia, encerra em menos de 1 segundo
(nem abre o banco nem carrega as bibliotecas de PDF).

Uso:
  python separar_folha.py            -> processa de verdade
  python separar_folha.py --simular  -> só lê, valida e gera o relatório (não grava nada)
  python separar_folha.py --reprocessar-pendencias [--simular]
        -> confere de novo a pasta de pendências contra o cadastro atual e publica o que agora confere
"""
import configparser, csv, io, json, os, re, shutil, subprocess, sys, time, unicodedata
from datetime import datetime

PASTA = os.path.dirname(os.path.abspath(__file__))
MESES = {'janeiro': 1, 'fevereiro': 2, 'marco': 3, 'abril': 4, 'maio': 5, 'junho': 6, 'julho': 7,
         'agosto': 8, 'setembro': 9, 'outubro': 10, 'novembro': 11, 'dezembro': 12}


# ----------------------------------------------------------------- configuração
def carregar_config():
    cfg = configparser.RawConfigParser()  # Raw: aceita % e # na senha
    caminho = os.path.join(PASTA, 'config.ini')
    if not cfg.read(caminho, encoding='utf-8'):
        sys.exit(f'ERRO: não encontrei {caminho}')
    return cfg


# ----------------------------------------------------------------- banco (via psql, já instalado no servidor)
def psql(cfg, sql, saida=True):
    b = cfg['banco']
    env = dict(os.environ, PGPASSWORD=b.get('senha', os.environ.get('PGPASSWORD', '')),
               PGCLIENTENCODING='UTF8')
    cmd = [b.get('psql', 'psql'), '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-h', b['host'], '-p', b['porta'],
           '-U', b['usuario'], '-d', b['banco']]
    if saida:
        cmd.append('--csv')
    r = subprocess.run(cmd, input=sql, capture_output=True, text=True, encoding='utf-8', env=env)
    if r.returncode != 0:
        raise RuntimeError(f'Erro no banco: {r.stderr.strip()}')
    return list(csv.DictReader(io.StringIO(r.stdout))) if saida else None


def lit(v):
    """Literal SQL seguro (aspas simples duplicadas)."""
    return "'" + str(v).replace("'", "''") + "'"


# ----------------------------------------------------------------- leitura do PDF
def norm(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode()
    return re.sub(r'\s+', ' ', s).strip().upper()


def ler_vias(page):
    """Encontra cada via pelo cabeçalho: rótulo 'Código' com 'Nome' na mesma linha.
    O valor do código é a palavra logo ABAIXO do rótulo (mesma coluna)."""
    words = page.extract_words()
    vias = []
    for w in words:
        if w['text'] != 'Código':
            continue
        mesma_linha = [o for o in words if abs(o['top'] - w['top']) < 2 and o['x0'] > w['x1']]
        if not any(o['text'] == 'Nome' for o in mesma_linha):
            continue  # é o 'Código' da tabela de eventos, não o do cabeçalho
        abaixo = sorted([o for o in words if 3 < o['top'] - w['top'] < 14
                         and o['x0'] < w['x1'] + 2 and o['x1'] > w['x0'] - 2], key=lambda o: o['top'])
        codigo = abaixo[0]['text'] if abaixo else ''
        linha_valor = sorted([o for o in words if abaixo and abs(o['top'] - abaixo[0]['top']) < 2],
                             key=lambda o: o['x0'])
        x_nome = next(o['x0'] for o in mesma_linha if o['text'] == 'Nome')
        x_cbo = next((o['x0'] for o in mesma_linha if o['text'] == 'CBO'), 9999)
        nome = ' '.join(o['text'] for o in linha_valor
                        if x_nome - 2 <= o['x0'] < x_cbo - 10 and not o['text'].isdigit())
        topo = sorted([o for o in words if 0 < w['top'] - o['top'] < 40], key=lambda o: (round(o['top']), o['x0']))
        texto_topo = ' '.join(o['text'] for o in topo)
        m = re.search(r'(janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)'
                      r'\s+de\s+(\d{4})', norm(texto_topo).lower())
        competencia = f'{m.group(2)}-{MESES[m.group(1)]:02d}' if m else ''
        t = re.search(r'Folha\s+([A-Za-zÀ-ú]+)', texto_topo)
        tipo = t.group(1).capitalize() if t else ''
        vias.append({'codigo': codigo, 'nome': nome, 'competencia': competencia, 'tipo': tipo,
                     'y_cabecalho': w['top']})
    vias.sort(key=lambda v: v['y_cabecalho'])
    # limites verticais de cada via: do topo da via até o início da próxima
    for i, v in enumerate(vias):
        v['y_ini'] = max(0, v['y_cabecalho'] - 46)
        v['y_fim'] = vias[i + 1]['y_cabecalho'] - 46 if i + 1 < len(vias) else page.height
    return vias


def salvar_via(reader, n_pagina, via, destino, altura):
    """Grava só a faixa da página que corresponde à via (recorte), num PDF novo."""
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_page(reader.pages[n_pagina])
    p = w.pages[0]
    x0, _, x1, _ = [float(x) for x in p.mediabox]
    topo_pdf, base_pdf = altura - via['y_ini'], altura - via['y_fim']
    for caixa in (p.mediabox, p.cropbox):
        caixa.lower_left = (x0, base_pdf)
        caixa.upper_right = (x1, topo_pdf)
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with open(destino, 'wb') as f:
        w.write(f)


# ----------------------------------------------------------------- processamento
def validar(v, cadastro, por_nome):
    """Devolve o motivo da pendência, ou None se o holerite pode ser publicado."""
    colab = cadastro.get(v['codigo'])
    if not v['codigo'].isdigit():
        return f"Código ilegível ('{v['codigo']}')"
    if not v['competencia']:
        return 'Competência (mês/ano) não encontrada'
    if not v['tipo']:
        return 'Tipo de folha não encontrado'
    if not colab:
        motivo = 'Código não existe no cadastro de colaboradores'
        if norm(v['nome']) in por_nome:
            motivo += (f" - ATENÇÃO: no cadastro, {v['nome']} está com a matrícula {por_nome[norm(v['nome'])]}"
                       f" (corrigir a matrícula na base do RH)")
        return motivo
    if norm(colab['nome']) != norm(v['nome']):
        motivo = f"Nome diferente do cadastro (cadastro: {colab['nome']})"
        if norm(v['nome']) in por_nome:
            motivo += f" - e {v['nome']} está no cadastro com a matrícula {por_nome[norm(v['nome'])]}"
        return motivo
    if colab['ativo'] != 't':
        return 'Colaborador inativo no cadastro'
    return None


def registrar_no_banco(cfg, ok):
    """Grava/atualiza os holerites publicados (uma transação só)."""
    if not ok:
        return
    valores = ',\n'.join(
        f"({int(o['id_colaborador'])}, {lit(o['competencia'] + '-01')}::date, "
        f"{lit(o['destino'].replace(os.sep, '/'))}, {lit(o['tipo'])})" for o in ok)
    psql(cfg, f"""BEGIN;
INSERT INTO folhas_pagamento (id_colaborador, mes_referencia, caminho_arquivo, tipo_folha)
VALUES {valores}
ON CONFLICT (id_colaborador, mes_referencia, tipo_folha)
DO UPDATE SET caminho_arquivo = EXCLUDED.caminho_arquivo;
COMMIT;""", saida=False)


def destino_holerite(pastas, v):
    return os.path.join(pastas['holerites'], v['competencia'][:4], v['competencia'],
                        f"{v['codigo']}_{v['tipo'].lower()}.pdf")


def processar(arquivo, cfg, cadastro, simular, log):
    import pdfplumber                     # bibliotecas pesadas: só carregam quando há arquivo
    from pypdf import PdfReader
    pastas = cfg['pastas']
    reader = PdfReader(arquivo)
    por_nome = {norm(c['nome']): m for m, c in cadastro.items()}   # para sugerir a matrícula certa
    ok, pend, vistos = [], [], {}
    with pdfplumber.open(arquivo) as pdf:
        for n, page in enumerate(pdf.pages):
            vias = ler_vias(page)
            if not vias:
                pend.append({'pagina': n + 1, 'via': '-', 'codigo': '', 'nome': '', 'motivo': 'Nenhum cabeçalho de holerite encontrado na página'})
                continue
            # uma via por documento: descarta vias repetidas (mesmo código, competência e tipo)
            unicas, chaves = [], set()
            for v in vias:
                k = (v['codigo'], v['competencia'], v['tipo'])
                if k not in chaves:
                    chaves.add(k)
                    unicas.append(v)
            for i, v in enumerate(unicas, 1):
                motivo = validar(v, cadastro, por_nome)
                colab = cadastro.get(v['codigo'])
                chave = (v['codigo'], v['competencia'], v['tipo'])
                if not motivo and chave in vistos:
                    motivo = f"Duplicado: o mesmo holerite já apareceu na página {vistos[chave]}"
                info = {'pagina': n + 1, 'via': i, **v}
                if motivo:
                    info['motivo'] = motivo
                    pend.append(info)
                    if not simular:
                        destino = os.path.join(pastas['pendencias'], v['competencia'] or 'sem-competencia',
                                               f"pagina{n + 1:03d}_via{i}_codigo{v['codigo'] or 'x'}.pdf")
                        salvar_via(reader, n, v, destino, page.height)
                        with open(destino[:-4] + '.json', 'w', encoding='utf-8') as f:
                            json.dump({k: v[k] for k in ('codigo', 'nome', 'competencia', 'tipo')} |
                                      {'pagina': n + 1, 'via': i, 'motivo': motivo,
                                       'arquivo_original': os.path.basename(arquivo)}, f, ensure_ascii=False)
                    continue
                vistos[chave] = n + 1
                destino = destino_holerite(pastas, v)
                info.update({'id_colaborador': colab['id'], 'destino': destino})
                ok.append(info)
                if not simular:
                    salvar_via(reader, n, v, destino, page.height)

    if not simular:
        registrar_no_banco(cfg, ok)
    return ok, pend


def relatorio(arquivo, ok, pend, cadastro, simular):
    linhas = []
    linhas.append('=' * 70)
    linhas.append(f"RELATÓRIO DO SEPARADOR DE HOLERITES {'(SIMULAÇÃO - nada foi gravado)' if simular else ''}")
    linhas.append(f"Arquivo: {os.path.basename(arquivo)}")
    linhas.append(f"Data: {datetime.now():%d/%m/%Y %H:%M}")
    linhas.append('=' * 70)
    comps = sorted({o['competencia'] for o in ok} | {p.get('competencia', '') for p in pend if p.get('competencia')})
    tipos = sorted({o['tipo'] for o in ok})
    linhas.append(f"Competência(s): {', '.join(comps) or '-'}   Tipo(s): {', '.join(tipos) or '-'}")
    linhas.append(f"Holerites separados com sucesso: {len(ok)}")
    linhas.append(f"Pendências (NÃO publicadas no bot): {len(pend)}")
    if len(comps) > 1:
        linhas.append('ATENÇÃO: o arquivo tem mais de uma competência. Confira se é o arquivo certo.')
    if pend:
        linhas.append('')
        linhas.append('PENDÊNCIAS - corrigir o cadastro ou o PDF e rodar de novo:')
        for p in pend:
            linhas.append(f"  - Página {p['pagina']}, via {p['via']}, código {p.get('codigo') or '?'} "
                          f"({p.get('nome') or '?'}): {p['motivo']}")
    # colaboradores ativos sem holerite na(s) competência(s) processada(s)
    if ok:
        com_holerite = {o['codigo'] for o in ok}
        sem = sorted((c for c, d in cadastro.items() if d['ativo'] == 't' and c not in com_holerite),
                     key=lambda c: int(c) if c.isdigit() else 0)
        if sem:
            linhas.append('')
            linhas.append(f"Colaboradores ATIVOS no cadastro sem holerite neste arquivo ({len(sem)}):")
            for c in sem:
                linhas.append(f"  - {c} {cadastro[c]['nome']}")
    linhas.append('')
    return '\n'.join(linhas)


IGNORAR = {'desktop.ini', 'thumbs.db'}


def pronto(caminho):
    """True se o arquivo terminou de ser copiado: tamanho parou de mudar e ninguém está com ele aberto.
    (Não dá para usar a data do arquivo: o Windows mantém a data original ao copiar.)"""
    try:
        t1 = os.path.getsize(caminho)
        time.sleep(5)
        t2 = os.path.getsize(caminho)
        if t1 != t2 or t2 == 0:
            return False
        with open(caminho, 'r+b'):          # falha se ainda estiver sendo gravado/aberto por outro programa
            pass
        return True
    except OSError:
        return False


def gravar_relatorio(pastas, nome_arq, texto, sufixo=''):
    carimbo = f"{datetime.now():%Y-%m-%d_%H%M%S}"
    rel = os.path.join(pastas['relatorios'], f"{carimbo}{sufixo}_{os.path.splitext(nome_arq)[0]}.txt")
    with open(rel, 'w', encoding='utf-8') as f:
        f.write(texto)
    return carimbo, rel


# ----------------------------------------------------------------- reprocessamento das pendências
def dados_da_pendencia(caminho):
    """Lê os dados do holerite pendente: do .json ao lado; se não houver (pendências antigas), relê o PDF."""
    lado = caminho[:-4] + '.json'
    if os.path.exists(lado):
        with open(lado, encoding='utf-8') as f:
            return json.load(f)
    import pdfplumber
    m = re.search(r'pagina(\d+)_via(\d+)_codigo(\w+)\.pdf$', os.path.basename(caminho), re.I)
    if not m:
        return None
    with pdfplumber.open(caminho) as pdf:
        vias = ler_vias(pdf.pages[0])
    unicas, chaves = [], set()
    for v in vias:
        k = (v['codigo'], v['competencia'], v['tipo'])
        if k not in chaves:
            chaves.add(k)
            unicas.append(v)
    idx = int(m.group(2)) - 1
    if 0 <= idx < len(unicas) and unicas[idx]['codigo'] == m.group(3):
        v = unicas[idx]
    else:
        v = next((x for x in unicas if x['codigo'] == m.group(3)), None)
    if not v:
        return None
    return {k: v[k] for k in ('codigo', 'nome', 'competencia', 'tipo')} | {'pagina': int(m.group(1)), 'via': int(m.group(2))}


def reprocessar_pendencias(cfg, simular):
    """Confere de novo cada holerite da pasta de pendências contra o cadastro ATUAL.
    O que agora passa em TODAS as validações é publicado (mesmas regras da separação normal);
    o resto continua na pasta de pendências."""
    pastas = cfg['pastas']
    arquivos = sorted(os.path.join(r, f) for r, _, fs in os.walk(pastas['pendencias'])
                      for f in fs if f.lower().endswith('.pdf'))
    if not arquivos:
        print('Nenhuma pendência para reprocessar.')
        return
    cadastro = {r['matricula']: r for r in psql(cfg,
                "SELECT id, matricula, nome, ativo FROM colaboradores WHERE matricula IS NOT NULL;")}
    por_nome = {norm(c['nome']): m for m, c in cadastro.items()}
    publicados, continuam, nesta_rodada = [], [], {}
    for caminho in arquivos:
        nome_arq = os.path.relpath(caminho, pastas['pendencias'])
        v = dados_da_pendencia(caminho)
        if not v:
            continuam.append(f"{nome_arq}: não consegui ler o cabeçalho do holerite")
            continue
        motivo = validar(v, cadastro, por_nome)
        destino = destino_holerite(pastas, v) if not motivo else None
        if not motivo and os.path.exists(destino):
            motivo = 'já existe holerite publicado para este código/mês/tipo (provável duplicado - conferir e apagar)'
        if not motivo and destino in nesta_rodada:
            motivo = f'duplicado: o mesmo holerite também está em {nesta_rodada[destino]} (conferir e apagar)'
        if motivo:
            continuam.append(f"{nome_arq} - código {v['codigo']} ({v['nome']}): {motivo}")
            continue
        nesta_rodada[destino] = nome_arq
        publicados.append({**v, 'id_colaborador': cadastro[v['codigo']]['id'], 'destino': destino, 'origem': caminho})
    if publicados and not simular:
        # 1) copia para a pasta do bot  2) grava no banco  3) só então apaga da pasta de pendências.
        #    Se o banco falhar, desfaz as cópias: nada fica pela metade.
        copiados = []
        try:
            for o in publicados:
                os.makedirs(os.path.dirname(o['destino']), exist_ok=True)
                shutil.copy2(o['origem'], o['destino'])
                copiados.append(o['destino'])
            registrar_no_banco(cfg, publicados)
        except Exception:
            for c in copiados:
                if os.path.exists(c):
                    os.remove(c)
            raise
        for o in publicados:
            os.remove(o['origem'])
            if os.path.exists(o['origem'][:-4] + '.json'):
                os.remove(o['origem'][:-4] + '.json')
    L = ['=' * 70, f"REPROCESSAMENTO DE PENDÊNCIAS {'(SIMULAÇÃO - nada foi gravado)' if simular else ''}",
         f"Data: {datetime.now():%d/%m/%Y %H:%M}", '=' * 70,
         f"Publicados agora no bot: {len(publicados)}", f"Continuam pendentes   : {len(continuam)}"]
    if publicados:
        L += ['', 'PUBLICADOS (o cadastro foi corrigido e agora conferem):'] + \
             [f"  - {o['codigo']} {o['nome']} - {o['tipo']} {o['competencia']}" for o in publicados]
    if continuam:
        L += ['', 'CONTINUAM PENDENTES:'] + [f'  - {c}' for c in continuam]
    texto = '\n'.join(L) + '\n'
    print(texto)
    if publicados or simular or '--relatorio' in sys.argv:   # só gera arquivo quando algo mudou
        gravar_relatorio(pastas, 'pendencias', texto, '_REPROCESSAMENTO' + ('_SIMULACAO' if simular else ''))


def main():
    simular = '--simular' in sys.argv
    cfg = carregar_config()
    pastas = cfg['pastas']
    for p in ('entrada', 'processados', 'holerites', 'pendencias', 'relatorios'):
        os.makedirs(pastas[p], exist_ok=True)

    if '--reprocessar-pendencias' in sys.argv:
        reprocessar_pendencias(cfg, simular)
        return

    todos = sorted(f for f in os.listdir(pastas['entrada'])
                   if os.path.isfile(os.path.join(pastas['entrada'], f))
                   and f.lower() not in IGNORAR and not f.startswith('~$'))
    if not todos:
        print('Pasta de entrada vazia. Nada a fazer.')
        return

    # arquivos que não são PDF: tira da entrada e avisa no relatório
    for nome_arq in [f for f in todos if not f.lower().endswith('.pdf')]:
        arquivo = os.path.join(pastas['entrada'], nome_arq)
        if simular or not pronto(arquivo):
            continue
        texto = (f"ARQUIVO IGNORADO: {nome_arq}\nSó são processados arquivos PDF da folha de pagamento.\n"
                 f"O arquivo foi retirado da pasta de entrada.\n")
        carimbo, rel = gravar_relatorio(pastas, nome_arq, texto, '_NAO_E_PDF')
        shutil.move(arquivo, os.path.join(pastas['processados'], f"{carimbo}_NAO_E_PDF_{nome_arq}"))
        print(texto)

    arquivos = [f for f in todos if f.lower().endswith('.pdf') and pronto(os.path.join(pastas['entrada'], f))]
    if not arquivos:
        print('Nenhum PDF pronto na pasta de entrada (pode estar sendo copiado). Tento de novo na próxima rodada.')
        return

    cadastro = {r['matricula']: r for r in psql(cfg,
                "SELECT id, matricula, nome, ativo FROM colaboradores WHERE matricula IS NOT NULL;")}

    for nome_arq in arquivos:
        arquivo = os.path.join(pastas['entrada'], nome_arq)
        try:
            ok, pend = processar(arquivo, cfg, cadastro, simular, print)
            texto = relatorio(arquivo, ok, pend, cadastro, simular)
        except Exception as e:
            texto = (f"ERRO ao processar {nome_arq}: {e}\n"
                     f"Nenhum holerite deste arquivo foi publicado no bot.\n"
                     f"O arquivo foi retirado da pasta de entrada (guardado em processados com _ERRO no nome).\n"
                     f"Confira se é o PDF correto da folha e coloque-o de novo na pasta, ou avise o desenvolvedor.\n")
            print(texto)
            carimbo, _ = gravar_relatorio(pastas, nome_arq, texto, '_ERRO')
            if not simular:
                shutil.move(arquivo, os.path.join(pastas['processados'], f"{carimbo}_ERRO_{nome_arq}"))
            continue
        print(texto)
        carimbo, rel = gravar_relatorio(pastas, nome_arq, texto, '_SIMULACAO' if simular else '')
        if not simular:
            shutil.move(arquivo, os.path.join(pastas['processados'], f"{carimbo}_{nome_arq}"))
        print(f'Relatório salvo em: {rel}')


if __name__ == '__main__':
    main()
