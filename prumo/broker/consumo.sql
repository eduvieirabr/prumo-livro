-- Consumo do mês por integração e tarefa (capítulo 5)
SELECT integracao, tarefa,
       count(*)                                         AS pedidos,
       count(*) FILTER (WHERE status = 'reaproveitado') AS reaproveitados,
       sum(tokens_entrada)                              AS tokens_entrada,
       sum(tokens_saida)                                AS tokens_saida,
       round(sum(duracao_ms) / 60000.0, 1)              AS minutos_de_vaga,
       round(sum(ms_descartados) / 60000.0, 1)          AS minutos_descartados
  FROM pedido
 WHERE criado_em >= date_trunc('month', now())
 GROUP BY integracao, tarefa
 ORDER BY minutos_de_vaga DESC NULLS LAST;
