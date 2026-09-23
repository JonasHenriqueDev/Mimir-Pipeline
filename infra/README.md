# SonarQube e PostgreSQL

O arquivo `compose.yaml` define SonarQube Community Build e PostgreSQL com imagens fixadas por digest, volumes persistentes e a rede `mimir-pipeline-network`. A porta HTTP é publicada somente em `127.0.0.1:9000`. O banco não publica porta para a máquina.

## Subir o ambiente

É necessário Docker com suporte a Compose e contêineres Linux. No Windows, use Docker Desktop já instalado e iniciado. A instalação/configuração do Docker é uma dependência externa ao projeto.

```powershell
Set-Location 'C:/Dev/Mimir Pipeline'
Copy-Item .env.example .env
```

Edite `.env` e preencha `POSTGRES_PASSWORD` com uma senha exclusiva. As linhas de tokens são apenas lembretes; a CLI não carrega esse arquivo. Não versione o `.env`.

```powershell
docker compose --env-file .env -f infra/compose.yaml config --quiet
docker compose --env-file .env -f infra/compose.yaml up -d
docker compose --env-file .env -f infra/compose.yaml ps
docker compose --env-file .env -f infra/compose.yaml logs --tail 100 sonarqube
```

O primeiro início pode levar alguns minutos. Verifique o estado:

```powershell
Invoke-RestMethod http://localhost:9000/api/system/status
```

Aguarde `UP`, abra [SonarQube local](http://localhost:9000), conclua a configuração inicial da conta, prepare os perfis de qualidade e gere um token com as permissões exigidas pelo fluxo. O token precisa analisar e criar os projetos específicos dos experimentos.

No PowerShell que executará a pipeline:

```powershell
$env:SONAR_TOKEN = 'SEU_TOKEN'
$env:OPENAI_API_KEY = 'SUA_CHAVE'
.\.venv\Scripts\mimir-pipeline.exe doctor --config configs/python.local.yaml
```

## Rede e scanner

O processo Python acessa `http://localhost:9000`. O scanner Docker acessa `http://sonarqube:9000` na rede `mimir-pipeline-network`. Por isso os exemplos têm `sonar.url` e `sonar.scanner_url` distintos.

O Compose fixa `SONAR_MULTI_QUALITY_MODE_ENABLED: 'false'`, correspondente a `sonar.mode: standard` nos YAML. Para estudar o modo MQR, altere ambos antes da coleta e registre a mudança. As propriedades do servidor são descritas na [documentação oficial do SonarQube](https://docs.sonarsource.com/sonarqube-server/server-installation/system-properties/common-properties).

Cada scanner monta a worktree em `/usr/src`, recebe `SONAR_TOKEN` pelo ambiente e escreve `.scannerwork/report-task.txt`. A pipeline acrescenta a chave de projeto, URL e caminho de metadados. Não adicione `sonar.token`, `sonar.login`, `sonar.projectKey` nem `sonar.host.url` manualmente às propriedades.

Os contêineres de build/testes pertencem ao runner Python, não ao Compose. Usam a rede de setup para instalar dependências e ficam sem rede durante build/testes nos exemplos. Não compartilham as credenciais do provedor LLM.

Na configuração Java, o scanner recebe os `.class` e os JARs persistidos em `target/`, pois o contêiner Maven já terminou. Adapte o classpath e o nível Java ao corpus conforme a [documentação do analisador Java](https://docs.sonarsource.com/sonarqube-community-build/analyzing-source-code/languages/java).

## Manter e parar

```powershell
docker compose --env-file .env -f infra/compose.yaml stop
docker compose --env-file .env -f infra/compose.yaml start
```

Para remover os contêineres mantendo os volumes:

```powershell
docker compose --env-file .env -f infra/compose.yaml down
```

Não acrescente `--volumes`/`-v` quando precisar preservar análises e configuração. Para arquivamento do estudo, preserve também backup do banco e os artefatos da pipeline; eles são armazenamentos diferentes. Mudanças em `POSTGRES_PASSWORD` não atualizam automaticamente a senha de um banco já inicializado em volume existente.

## Diagnóstico

Se o SonarQube reiniciar ou não ficar saudável, examine os logs de `sonarqube` e `db`, recursos disponíveis e os parâmetros do kernel Linux utilizados pelo mecanismo de busca. Em Docker Desktop, esses requisitos pertencem à VM/WSL Linux, não ao kernel do PowerShell. A pipeline não altera automaticamente configurações do host.

Se uma imagem não puder ser baixada, verifique conexão, registry e arquitetura da máquina; os digests fixados fazem parte da configuração experimental. Uma troca de imagem deve ser registrada e aplicada a ambos os grupos antes da coleta. Se o scanner não puder escrever na worktree montada, ajuste a permissão do diretório/usuário do contêiner para o ambiente utilizado e registre a alteração.

Os manifests e testes de integração com respostas controladas verificam a lógica do adaptador. A execução com Docker, este servidor e um provedor real precisa ser validada no ambiente de coleta antes de considerar o sistema operacional para o estudo.
