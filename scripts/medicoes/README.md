# Medições do livro

Um script por quadro **NÚMERO MEDIDO** medido no contêiner, capítulos 5 a 17. Cada script
apaga e recria só as tabelas que usa, num banco separado, e imprime os mesmos números que o
quadro do livro traz.

```bash
export PRUMO_MEDIR_PG=postgresql://postgres:teste@127.0.0.1/prumo_medir   # banco só para isto
python -m scripts.medicoes.cap06_fila                               # da raiz do repositório
```

O banco precisa das extensões `vector` (pgvector) e `unaccent` para o capítulo 10, e o papel
`postgres` precisa poder criar o papel `mem_app`; o script do capítulo 10 dá a ele a senha
`mem_app` e se conecta como ele, com senha ou sem.
Os capítulos 9 e 13 não usam banco.

Ficaram de fora os números que não saíram do contêiner: a comparação com os DARFs reais
(cap. 1), o teste de carga e o OCR (caps. 3 e 4), feitos na plataforma real, e o teste contra
um n8n de verdade (cap. 15). O sabotador do capítulo 18 é medido pela própria suíte de testes.

## Ambiente da rodada abaixo

2 vCPU Intel Xeon 2,30 GHz, 7 GB de RAM, disco virtual, Ubuntu 24.04, Python 3.13,
PostgreSQL 16.15 com a configuração padrão do pacote (`shared_buffers` 128 MB, `fsync` e
`synchronous_commit` ligados), pgvector 0.6.0, banco e scripts na mesma máquina. Uma rodada
de cada script, sem aquecimento além do que cada script faz.

## Scripts e números

Os números da última coluna são os que estão no livro, todos desta máquina e desta versão
do código (v0.7.0, outubro de 2026).

| Script | Cap. | O que mede | No livro |
| --- | --- | --- | --- |
| `cap05_broker` | 5 | criar pedido pela API, com 20 mil antigos; devolver reaproveitado (mediana de 300) | 9,6 ms; 9,8 ms |
| `cap06_fila` | 6 | retirada da fila com 1 consumidor; 10 mil pedidos com 8 consumidores | 3,6 ms; 39,1 s, nenhum repetido |
| `cap07_sse` | 7 | do evento gravado ao cliente SSE, uvicorn real (40 eventos) | 167 ms; pior 300 ms |
| `cap08_reservas` | 8 | 400 reservas de R$ 0,50 contra teto de R$ 100, 8 conexões | 200 aceitas; 1,3 ms |
| `cap09_token` | 9 | conferir token RS256 com a chave em memória (média de 5 mil) | 75 µs |
| `cap10_memoria` | 10 | trechos visíveis da Lívia; busca híbrida com RLS, Lívia e Marta (mediana de 50) | 9.350; 58 ms e 60 ms |
| `cap10_memoria` | 10 | primeira versão, exigindo todas as palavras | 44 ms |
| `cap11_laco` | 11 | gravar estado e registro por passo, com posse e batimento (50 execuções de 10 passos) | 1,1 ms |
| `cap13_planilha` | 13 | tokens do arquivo e do mapa; cálculo (mediana de 7), 90 e 20 mil linhas | ~1.540 / ~146 / 1,7 ms; ~356.500 / ~146 / 415 ms |
| `cap14_agenda` | 14 | próxima vez de "5º dia útil, 8h" (mediana de 2.000) | 0,4 ms |
| `cap14_agenda` | 14 | volta vazia (mediana de 20); volta com 500 vencidos | 0,8 ms; 707 ms |
| `cap14_agenda` | 14 | 40 agentes de 1 s vencidos juntos | 5,1 s |
| `cap15_chaves` | 15 | conferir chave pessoal (mediana de 55); neutralizar 20 mil caracteres | 0,64 ms; 0,58 ms |
| `cap16_central` | 16 | publicar 1 evento; lote de 500 (mediana de 3 rodadas) | 1,3 ms; 221 ms |
| `cap16_central` | 16 | 1 publicador; 4 publicadores, 250 eventos cada | ~820 ev/s; ~715 ev/s |
| `cap16_central` | 16 | publicar pela API HTTP com token (200) | 11,4 ms; p95 14,2 ms |
| `cap17_baixa` | 17 | consumir mil pagamentos em lotes de 100; outbox de mil baixas | 0,84 s; 1,48 s |

Números de milissegundos variam de uma rodada para a outra, numa máquina virtual de dois
núcleos, em uns 10% a 30% (a volta vazia do agendador foi de 0,55 a 0,82 ms em rodadas diferentes); as contagens (200 reservas aceitas, nenhum pedido repetido, 500
ocorrências registradas, 8 agentes por vez) não variam.

## Notas

- **Capítulo 10, "todas as palavras".** Os trechos gerados sorteiam 40 palavras de um
  vocabulário de 25, então quase todo trecho tem as palavras de qualquer pergunta, e a versão
  que exige todas acha trechos. Com este corpus sintético, o script mede só o tempo; o
  problema da primeira versão, que perde o trecho sem uma das palavras, é de mecanismo.
- **Capítulo 11.** O laço atual renova a posse com uma thread de batimento em cada passo e
  confere a posse em cada gravação (o que entrou na revisão de outubro, depois de uma primeira medição de 0,44 ms). O custo por
  passo subiu para perto de 1 ms; continua desprezível perto de qualquer passo que chame o
  modelo.
- **Capítulo 16, lote de 500.** Ficou entre 200 e 220 ms em todas as rodadas desta máquina
  (uma medição anterior, antes da identidade permanente, deu 153 ms).
  A publicação grava, para cada evento, a identidade permanente (`central.evento_id`, com o
  resumo do conteúdo), além do evento e do número do canal.
