# Pipeline experimental do TCC

Aplicação Python para comparar correções de apontamentos de manutenibilidade do SonarQube com e sem filtragem por LLM. Cada condição parte do mesmo commit, propõe alterações em cópias Git isoladas, executa build e testes e aceita apenas propostas aprovadas pela política de reanálise.

## Início rápido no Windows

Pré-requisitos da demonstração: Python 3.11 ou superior e Git. Execute no PowerShell:

```powershell
Set-Location C:/Dev/tcc-pipeline
python -m pip install uv
python -m uv sync --frozen
.\.venv\Scripts\tcc-pipeline.exe doctor
.\.venv\Scripts\tcc-pipeline.exe demo
```

O comando `demo` cria um repositório de exemplo, executa as duas condições e gera um relatório HTML com CSV, JSON e gráfico. Os backends são simulados; a demonstração verifica o fluxo e **não constitui resultado científico do TCC**. O caminho do relatório é exibido ao final.

Para experimentos reais, configure Docker, SonarQube, o repositório do corpus e as credenciais conforme [Operação](docs/OPERATIONS.md) e [Infraestrutura](infra/README.md). Os exemplos de configuração cobrem Python, Java e TypeScript.

## Documentação

- [Arquitetura e contratos](docs/ARCHITECTURE.md)
- [Protocolo e interpretação experimental](docs/METHODOLOGY.md)
- [Instalação, comandos, configurações e diagnóstico](docs/OPERATIONS.md)
- [SonarQube e PostgreSQL com Docker Compose](infra/README.md)
- [Validação da implementação e limites do ambiente](docs/VERIFICATION.md)

## Desenvolvimento

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
```

As dependências estão fixadas em `uv.lock`; os exemplos fixam imagens Docker por digest. A integração real depende das ferramentas e credenciais do ambiente. Testes automatizados de adaptadores utilizam respostas controladas, e não substituem uma execução com o SonarQube e o provedor contratados.
