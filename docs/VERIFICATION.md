# Verificação da implementação

Esta página registra a validação local realizada na entrega inicial. Os resultados demonstrativos não são resultados científicos do TCC.

| Verificação | Resultado observado |
|---|---|
| Suíte no Windows / Python 3.14.2 | 143 aprovados, 2 ignorados |
| Suíte no Windows / Python 3.11.16 | 143 aprovados, 2 ignorados |
| Ruff: análise estática e formatação | Aprovado |
| Construção de wheel e distribuição fonte | Aprovada |
| Instalação do wheel em ambiente isolado | Aprovada |
| Demonstração pelo pacote instalado | Ambas as condições concluídas; relatórios gerados |
| Docker / SonarQube reais | Não executados: Docker ausente na máquina |
| Provedor LLM real | Não executado: credencial e modelo ainda precisam ser configurados |
| CI Linux/Windows | Workflow entregue; execução remota não realizada |

Os dois testes ignorados verificam links simbólicos, cuja criação não estava disponível neste Windows. Os testes cobrem contratos HTTP com respostas controladas, falhas de validação, aplicação atômica de patches, limites, isolamento Git, caminhos Unicode, geração de relatórios e avaliação de rótulos independentes. Os comandos Docker são exercitados com respostas controladas, sem iniciar contêineres reais.

A demonstração contém dois apontamentos simulados. Ambas as condições aceitam a remoção de uma atribuição não utilizada. Sem filtragem, remover a outra atribuição quebra um teste real; a proposta é rejeitada. Com filtragem, essa proposta é evitada. O alerta filtrado permanece no snapshot final, sem ser contado como correção.

Evidências locais da entrega:

- `artifacts/verification/python314.xml` e `python311.xml`: relatórios JUnit da suíte.
- `artifacts/package-demo/exp-7c1bb8ee035f/report.html`: relatório da demonstração executada pelo wheel instalado.
- A mesma pasta contém `summary.csv`, `attempts.csv`, `comparison.json`, gráficos, logs, patches e snapshots.
- `dist/mimir_pipeline-0.1.0-py3-none-any.whl` e `dist/mimir_pipeline-0.1.0.tar.gz`: pacotes construídos.

Os artefatos locais e pacotes estão ignorados pelo Git; uma nova execução produz outros identificadores. Para coleta científica, escolha o corpus e os commits, prepare Docker/SonarQube, configure o modelo e as credenciais, execute um piloto e siga [o protocolo metodológico](METHODOLOGY.md).
