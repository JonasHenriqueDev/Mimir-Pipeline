# Operação

## Instalação e verificação local

Use Python 3.11 ou superior e Git. No PowerShell:

```powershell
Set-Location 'C:/Dev/Mimir Pipeline'
python -m pip install uv
python -m uv sync --frozen
.\.venv\Scripts\mimir-pipeline.exe --help
.\.venv\Scripts\mimir-pipeline.exe doctor
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
```

`python -m uv` funciona mesmo quando o executável `uv` não está no PATH. A instalação precisa de acesso aos pacotes; após instalada, a demonstração não usa Docker, SonarQube nem API LLM. Em Linux/macOS, substitua `.\.venv\Scripts\mimir-pipeline.exe` por `.venv/bin/mimir-pipeline`.

```powershell
.\.venv\Scripts\mimir-pipeline.exe demo --output artifacts/demo
```

A demonstração cria seu próprio repositório em `artifacts/demo/demo-sources/`, roda testes reais e usa scanner e respostas LLM simulados. Ela contém uma atribuição removível e outra acessada dinamicamente; o segundo caso permite verificar a preservação do comportamento e a rejeição de uma proposta inadequada. O HTML identifica os dados simulados.

## Preparar uma execução real

1. Instalar e iniciar Docker com contêineres Linux; preparar o SonarQube conforme [infra/README.md](../infra/README.md).
2. Clonar localmente o projeto a avaliar e escolher um commit. O `repo` da configuração não aceita URL remota.
3. Copiar o exemplo correspondente e adaptar caminhos, comandos, fontes e propriedades do scanner.
4. Definir um modelo disponível na conta e compatível com saída estruturada, além das credenciais no ambiente do processo.
5. Executar `doctor --config`, um piloto e, após validá-lo, a coleta principal.

```powershell
Copy-Item configs/python.example.yaml configs/python.local.yaml
# Edite configs/python.local.yaml antes de executar os comandos seguintes.
$env:SONAR_TOKEN = 'SEU_TOKEN_DO_SONARQUBE'
$env:OPENAI_API_KEY = 'SUA_CHAVE_DO_PROVEDOR'
.\.venv\Scripts\mimir-pipeline.exe doctor --config configs/python.local.yaml
.\.venv\Scripts\mimir-pipeline.exe experiment --config configs/python.local.yaml
```

Os valores acima são marcadores; não os copie como credenciais reais. Em terminais compartilhados, injete as variáveis por seu gerenciador de segredos em vez de registrar valores no histórico. A CLI **não carrega `.env` automaticamente**. `--env-file .env` pertence ao Docker Compose e não exporta variáveis para o processo Python.

O SonarQube precisa permitir que o token execute análises e crie os projetos com chaves atribuídas pela pipeline. As chaves são específicas por experimento, condição e repetição. Um token restrito a um projeto preexistente pode não servir para esse fluxo. Configure o perfil padrão das linguagens antes do piloto e mantenha-o fixo durante a coleta.

`doctor` verifica ferramentas, existência das variáveis, repositório e estado do servidor quando há configuração. Não consome chamadas LLM e não garante validade da chave, autorização do token ou compatibilidade do modelo. Uma execução real é necessária para validar essas integrações.

## Comandos

| Comando | Resultado |
|---|---|
| `doctor [--config arquivo.yaml]` | Diagnóstico sem chamada ao LLM. |
| `demo [--output diretório]` | Demonstração completa nas duas condições e relatório. |
| `experiment --config arquivo.yaml` | Todas as repetições das duas condições e relatório. |
| `run --config arquivo.yaml --condition filtered` | Somente a condição escolhida; também aceita `unfiltered`. |
| `report diretório-do-experimento` | Regenera HTML, CSV, PNG e comparação JSON. |
| `labels-export diretório-do-experimento --output rótulos.csv` | Exporta CSV sem previsões do modelo; recusa sobrescrever arquivo existente. |
| `labels-evaluate diretório-do-experimento --labels rótulos.csv [--output diretório]` | Avalia os rótulos; saída padrão em `evaluation/` no experimento. |

`experiment`, `run` e `demo` imprimem a localização do relatório. Falhas retornam código de saída diferente de zero. Uma condição isolada não produz um par comparável; use `experiment` para a comparação principal. Passe a pasta `exp-...` aos comandos de relatório e rótulos, não a pasta que contém vários experimentos.

## Configuração

Os exemplos estão em `configs/python.example.yaml`, `configs/java.example.yaml` e `configs/typescript.example.yaml`. Campos desconhecidos são rejeitados. `project.repo` e `output_dir` relativos são resolvidos em relação à pasta do YAML, não ao terminal. Por exemplo, `repo: ../../python-project` em `C:/Dev/Mimir Pipeline/configs/` aponta para `C:/Dev/python-project`.

| Grupo | Campos essenciais e comportamento |
|---|---|
| `project` | `name`, `repo`, `ref`, `language`, `source_globs`, `context_files`, `allowed_edit_globs`, `protected_globs` e comandos. `test_commands` deve ter ao menos um comando. |
| `runner` | `mode: docker` ou `local`, `image`, `timeout_seconds`, redes `setup_network`/`network`, `memory` e `cpus`. |
| `sonar` | `url` acessível pelo Python, `scanner_url` acessível pelo scanner, `scanner_command`, `properties`, `rules`, `mode` e prazos. |
| `llm` | `provider`, `base_url`, `api_key_env`, `model`, `max_calls`, `max_output_tokens`, prazos, retentativas e contexto máximo. |
| `experiment` | Limites de iterações, tentativas, tentativas por identidade, tempo, tamanho/arquivos de proposta, repetições, semente e política de novos apontamentos. |
| `output_dir` | Pasta fora do repositório avaliado que receberá o experimento e seu catálogo. |

Os comandos são listas de argumentos, executadas sem shell implícito:

```yaml
test_commands:
  - [python, -m, pytest, -q]
```

Não coloque `&&`, pipes ou redirecionamentos como se fossem um comando de terminal. No runner, `{python}` é substituído pelo Python da pipeline no modo local e por `python` no Docker; `{repo}` aponta para a worktree local ou `/workspace` no contêiner. O scanner admite `{repo}`, `{project_key}` e `{sonar_url}`.

`source_globs` define o escopo de fontes do scanner simulado; o escopo real do scanner é definido por `sonar.properties`, como `sonar.sources` e `sonar.exclusions`. Mantenha esses campos coerentes. O contexto usa o arquivo alvo, `context_files` e testes próximos. `sonar.rules`, `allowed_edit_globs` e `protected_globs` restringem candidatos antes de consumir chamadas LLM; a política de novos apontamentos continua considerando o snapshot de manutenibilidade completo.

O modo Sonar `standard` consulta `CODE_SMELL`; `mqr` consulta impactos de `MAINTAINABILITY`. O valor deve corresponder ao modo do servidor. As credenciais não podem ser incluídas em `scanner_command` ou `properties`: use `token_env`.

Para OpenAI, mantenha `provider: openai`. Um serviço com protocolo Chat Completions e JSON Schema pode usar:

```yaml
llm:
  provider: openai_compatible
  base_url: https://ENDPOINT-DO-SEU-PROVEDOR/v1
  api_key_env: MODEL_API_KEY
  model: IDENTIFICADOR-DO-MODELO
  max_calls: 30
  max_output_tokens: 4096
```

O suporte não é presumido para qualquer servidor dito compatível: o endpoint precisa aceitar os parâmetros e contratos implementados. `temperature` é omitida por padrão; configure-a apenas quando o modelo a aceitar. Não há cache automático de respostas.

Para habilitar limite de custo, preencha `llm.max_cost_usd`, `input_price_per_million` e `output_price_per_million` com valores conferidos para o modelo e a data de coleta. Sem preços, o custo fica indisponível. O limite usa reserva conservadora antes de cada chamada e não substitui o controle de gastos da conta. Retentativas contam no limite de chamadas; falhas sem informação de consumo mantêm a incerteza no relatório.

## Adaptar os três exemplos

- **Python:** o exemplo pressupõe `src/`, `tests/` e extra de instalação `.[test]`. Ajuste a instalação ao manifesto real. O build executa `compileall`; os testes usam pytest.
- **Java:** o exemplo pressupõe Maven em módulo único, Java 21, `src/main/java`, `src/test/java` e saídas `target/classes` e `target/test-classes`. O setup baixa dependências e copia JARs de compilação/teste para `target/sonar-libraries` e `target/sonar-test-libraries`, acessíveis ao scanner separado. Build e testes usam Maven offline. Ajuste também `sonar.java.source`. Projetos Gradle ou multi-módulo exigem configuração própria.
- **TypeScript:** o exemplo pressupõe `package-lock.json`, `npm ci`, script `build` e script `test:ci` que termina após uma execução. Ajuste a imagem Node e globs de acordo com o projeto.

Os exemplos não geram relatórios de cobertura automaticamente. Caso a cobertura faça parte das métricas do estudo, configure sua produção nos testes e as propriedades correspondentes do SonarQube. Ausência de cobertura deve permanecer indisponível, não ser interpretada como zero.

## Ler os artefatos

Abra `report.html` localmente. O gráfico é incorporado e o relatório não precisa de rede; CSV e JSON ficam ao lado. `experiment.json` registra SHA, configuração, ambiente, ordem e modalidade. Cada execução tem `result.json` e `events.jsonl`; cada tentativa mantém contexto, proposta, diff, comandos/logs e snapshot quando disponíveis. A pasta `llm/` registra as requisições e respostas com remoção dos segredos reconhecidos.

O contexto do código é enviado ao provedor real configurado. A exclusão de nomes sensíveis e a remoção por padrões são medidas auxiliares e não garantem encontrar todo segredo embutido no código. Prepare o corpus antes de executá-lo e mantenha os artefatos sob o mesmo controle de acesso do código estudado.

Worktrees e commits são preservados para inspeção. Para inspecionar a versão aceita, leia o campo `worktree` do `result.json` e use `git log`/`git diff` nesse diretório. Nenhum comando da pipeline publica ou envia os commits a um servidor Git.

## Diagnóstico de problemas

| Sintoma | Verificação |
|---|---|
| Docker ausente ou daemon indisponível | Inicie Docker Desktop com contêineres Linux; `docker info` deve funcionar. A demo continua disponível sem Docker. |
| SonarQube não chega a `UP` | Consulte os logs do Compose e a configuração da VM Linux descrita em `infra/README.md`. |
| Scanner sem `report-task.txt` | Consulte `scanner.log`; verifique volume, permissão de escrita, token e rede. A pipeline recusa reaproveitar tarefa antiga. |
| HTTP 401/403 no Sonar | Verifique token, permissão de análise/criação e URL do servidor. |
| Scanner não alcança `localhost:9000` | Dentro do contêiner do exemplo use `scanner_url: http://sonarqube:9000`; o Python usa `url: http://localhost:9000`. |
| HTTP 400 ou JSON inválido do LLM | Consulte o artefato da chamada e confirme suporte a schema, modelo e parâmetros. A proposta não é aplicada. |
| Trecho antigo não corresponde | O modelo precisa copiar exatamente um trecho disponível. A tentativa rejeitada mantém evidências; não relaxe automaticamente a correspondência. |
| Referência funcional inválida | Reproduza os comandos do baseline; corrija o corpus/configuração e faça nova execução. |
| Dependência faltando no build offline | Ajuste o setup para obter todas as dependências necessárias, mantendo as mesmas condições para os dois grupos. |
| Arquivos alterados por build/testes | O projeto modifica fontes ou arquivos versionados durante validação. Corrija o processo do corpus antes da coleta. |
| `no_progress` ou `max_*` | Consulte os limites e tentativas; encerramento controlado não significa que todo apontamento foi corrigido. |
| Mais de 10.000 apontamentos | Reduza o escopo planejado do corpus; a integração recusa a consulta incompleta da API. |

Não existe retomada automática de um experimento interrompido. Preserve-o, regenere seu relatório quando possível e inicie outro experimento. Interrupções podem deixar contêineres ou worktrees que precisam de inspeção; não remova evidências da coleta antes de arquivá-las.
