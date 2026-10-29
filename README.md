# IA Local sob Pressão — código do livro

Código de exemplo do livro *IA Local sob Pressão* (Arquiteturas de Backend em Escala, Vol. 06),
de Eduardo Vieira. Versão `0.7.0`, a que acompanha esta edição.

A Prumo Contabilidade, o ERP Vértice e todos os dados são fictícios.

## O que tem aqui

- **Serviços que sobem** (`compose.yml`): o Broker de IA (API e vagas de GPU), a Central de
  Eventos, o consumidor do Parcelamentos, o Prumo ID (Keycloak), o n8n e o Ollama, real ou
  simulado (`scripts/fake_ollama.py`).
- **Bibliotecas com testes**: o motor de cálculo, a leitura de documentos, a memória com RLS e
  os agentes (laço, skills, planilha, agendador, n8n, chaves MCP, gatilhos).
- **Não tem**: as telas da Prumo. O livro as descreve; o repositório traz o mecanismo.

O apêndice A do livro explica a ordem de subida e as variáveis.

## Testes

```bash
pip install -e ".[dev]"
docker run -d --name prumo-teste -p 127.0.0.1:5432:5432 \
       -e POSTGRES_PASSWORD=teste pgvector/pgvector:pg16
PRUMO_TESTE_PG=postgresql://postgres:teste@localhost/postgres pytest -rs
```

O banco é descartável, mas escuta só em `127.0.0.1` e tem senha. Sem `PRUMO_TESTE_PG`, os testes
que precisam do banco são pulados; a primeira linha da saída do `pytest` diz qual dos dois
casos está valendo, e o `-rs` lista os pulados. Nenhum teste usa GPU. A
leitura de PDF precisa do Tesseract com o idioma português instalado no sistema.

## Medições e avaliação

- `scripts/medicoes/`: um script por quadro "NÚMERO MEDIDO" do livro (apêndice D).
- `scripts/avaliar_modelo.py`: a avaliação do modelo real com gabarito, pelo Broker (cap. 18).

## Licença

O código deste repositório está sob a licença MIT (arquivo `LICENSE`): pode ser copiado,
adaptado e usado em projetos comerciais, mantendo o aviso de copyright. A licença vale só
para o código; o texto do livro tem todos os direitos reservados.
