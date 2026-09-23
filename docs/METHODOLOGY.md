# Protocolo experimental

## Pergunta e unidade de análise

A implementação apoia a comparação entre uma pipeline de correção de apontamentos de manutenibilidade com filtragem prévia por LLM (`filtered`) e a mesma pipeline sem essa etapa (`unfiltered`). O filtro pode reduzir tentativas desnecessárias, mas também descartar problemas pertinentes. Ambos os efeitos devem ser medidos.

A unidade principal de comparação é o par projeto/commit/repetição, com uma execução por condição. Tentativas do mesmo projeto e repetições do mesmo apontamento não devem ser tratadas automaticamente como amostras independentes.

## Preparação do corpus

1. Escolher repositórios e registrar licença, linguagem, versão, tamanho, critérios de inclusão e exclusão e origem.
2. Fixar `project.ref` em um SHA. Mudanças no corpus, testes e dependências devem preceder essa fixação.
3. Verificar que os comandos terminam sem interação e detectam falhas por código de saída. Não usar testes em modo watch.
4. Fixar versão e digest das imagens, versão do scanner, perfil de qualidade, regras, parâmetros, modelo e prompts. Manter o mesmo ambiente em ambas as condições.
5. Realizar um piloto para verificar a capacidade do ambiente; separar seus artefatos da coleta principal.
6. Definir antes da coleta o número de repetições, limites, regras elegíveis, tratamento de falhas e análise estatística.

Os exemplos de configuração são modelos operacionais, não um corpus científico pronto. Maven, scripts npm, extras Python e relatórios de cobertura precisam corresponder ao projeto real. Para Java, projetos com módulos ou classpath externo podem exigir propriedades adicionais do analisador.

## Procedimento de cada par

Cada execução reconstrói a referência, roda testes e faz análise inicial. Uma referência funcional inválida encerra a execução como falha. As duas condições usam o mesmo commit e configuração; a ordem é embaralhada com a semente registrada.

Na condição filtrada, o modelo classifica o apontamento. `false_positive` é registrado e evita a correção daquele apontamento no estado de código observado; `pertinent` e `inconclusive` seguem para proposta. Na condição sem filtro, todos os apontamentos elegíveis seguem diretamente para proposta. Ambas usam o mesmo contrato de correção e a mesma política de aceitação.

Os limites de chamadas e de custo são aplicados por execução e incluem a classificação. Isso mede o efeito da filtragem sob orçamento total equivalente; não equivale a conceder o mesmo número de propostas de correção a cada condição. Documente essa escolha no TCC. A igualdade de valores máximos não implica igualdade do consumo efetivo.

O processo para por limites, ausência de candidatos elegíveis ou ausência de progresso. `completed` significa encerramento controlado, que pode ocorrer com apontamentos restantes. Sempre apresentar `stop_reason` junto aos resultados.

## Verdade de referência e cegamento

Exporte os apontamentos iniciais:

```powershell
.\.venv\Scripts\mimir-pipeline.exe labels-export artifacts/python/exp-ID --output reference_labels/python.csv
```

A planilha contém identidade, regra, caminho, linha original, âncora, mensagem, `manual_label` e `notes`; não contém condição, previsão ou justificativa do modelo. Mantenha rótulos fora do repositório avaliado e do contexto enviado ao LLM. Preencha exatamente `pertinent`, `false_positive` ou `inconclusive`; ausência de rótulo não é considerada resposta correta ou incorreta.

O avaliador deve inspecionar o commit de referência, a regra e o comportamento do programa sem acessar as previsões. Defina um manual de anotação e registre justificativas em `notes`. Se houver múltiplos avaliadores, guarde anotações independentes e a adjudicação. A pipeline não calcula concordância entre anotadores automaticamente.

```powershell
.\.venv\Scripts\mimir-pipeline.exe labels-evaluate artifacts/python/exp-ID --labels reference_labels/python.csv --output artifacts/python/exp-ID/evaluation
```

Esse comando produz `evaluation.json` e `predictions.csv`. Usa a primeira classificação de cada identidade por execução, evitando contar todas as retentativas como novas classificações. Rótulos conhecidos são os do conjunto inicial; classificações sem rótulo permanecem explicitamente desconhecidas. A identidade inicial diferencia ocorrências por posição, mensagem e âncora; durante o ciclo, a chave do scanner mantém a identidade mesmo com deslocamentos de linha.

## Métricas produzidas

| Medida | Definição e interpretação |
|---|---|
| Apontamentos iniciais e finais | Contagens dos snapshots, preservando multiplicidade. |
| Resolvidos | Multiplicidades presentes inicialmente e ausentes ao final. |
| Novos | Multiplicidades adicionais no snapshot final. |
| Redução líquida | Contagem inicial menos contagem final. |
| Aceitos/rejeitados/filtrados | Estados registrados nas tentativas; filtragem não reduz a contagem do SonarQube. |
| Falhas de validação | Propostas cujo build ou testes falharam; separadas de erros de infraestrutura e LLM. |
| Tempo | Duração registrada da execução; inclui overhead de ambiente e análise. |
| Tokens/chamadas/custo | Consumo informado pelo provedor e estimativa pelos preços configurados. Dados indisponíveis permanecem nulos. |
| Esforço de remediação | Estimativa do SonarQube em minutos; não mede tempo humano economizado. |
| Precisão do filtro | Entre as classificações `false_positive` com rótulo humano conclusivo, fração realmente rotulada `false_positive`. |
| Revocação do filtro | Fração dos falsos positivos humanos classificados como tal; abstenções do modelo permanecem no denominador. |
| Descarte de problemas pertinentes | Fração dos problemas humanos pertinentes classificados para descarte, mais contagem dos efetivamente filtrados. |
| Cobertura | Proporção com rótulo humano conclusivo e proporção de decisões conclusivas do modelo. |

A matriz de confusão apresenta três classes. As métricas binárias excluem rótulos humanos inconclusivos e ausentes. A acurácia apresentada considera apenas previsões decisivas sobre rótulos humanos conclusivos; interprete-a junto à cobertura, pois abstenções podem elevar a acurácia desse subconjunto.

## Comparação e exclusões

O relatório calcula `filtered − unfiltered` apenas quando há exatamente uma execução de cada condição no mesmo experimento, projeto, repetição e commit, ambas completas, com snapshots e mesma modalidade real/simulada. Exige igualdade do multiconjunto inicial de identidade, severidade e esforço, além das métricas iniciais. Pares incompletos ou incompatíveis ficam em `comparison.json` com motivo da exclusão.

Diferença positiva na redução líquida favorece a filtragem; diferenças negativas em custo, tempo e falhas favorecem a filtragem. O relatório é descritivo e não executa testes de significância. Defina agregação por projeto, intervalos de confiança e eventual teste pareado antes de examinar os resultados. Preserve também falhas e exclusões; não selecionar apenas pares favoráveis.

## Limites de interpretação

- Testes aprovados fornecem evidência limitada à suíte; não comprovam equivalência semântica. Revisão manual das correções e, quando cabível, testes adicionais independentes fortalecem a avaliação.
- Redução de manutenibilidade reportada não representa toda a dívida técnica e não avalia automaticamente novas vulnerabilidades.
- Rótulos e identidade dependem do contexto da regra e do código. Se o scanner atribuir uma chave inédita a um alerta inalterado, a política o considera novo conservadoramente; casos de rejeição por esse motivo exigem inspeção.
- Versões do servidor, plugins, perfis e resposta do provedor podem variar. Revise os manifests; a semente embaralha a ordem, mas não fixa o comportamento remoto.
- Build, cache de dependências, disponibilidade de rede e desempenho da máquina influenciam o tempo. O custo é uma estimativa, sem reconstrução completa do faturamento.
- A demonstração usa regras e respostas determinísticas feitas para verificar o fluxo. Seus dados nunca devem entrar nas tabelas de eficácia do TCC.

O [documento do TCC](https://docs.google.com/document/d/1Jic8G9G6ERIs0A_Zf7d8kSdqttlXZe8LdfLWmmBSdnk/edit) é a referência do projeto; o protocolo final e suas alterações devem ser registrados pelo pesquisador.
