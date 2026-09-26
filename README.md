# Assistente do Residencial Aurora

API em Python 3.12+ com Google ADK **2.9.1**, Gemini e SQLite. O modelo interpreta os pedidos; as tools e o banco aplicam as regras de identidade, aprovação e reserva.

## Arquitetura

```text
Cliente → FastAPI → Runner / App → aurora (agente principal)
                                  ├─ reservas → tools → SQLite do condomínio
                                  ├─ portaria → tools → SQLite do condomínio
                                  └─ regulamento → busca local por capítulo/artigo
                   └─ DatabaseSessionService → SQLite de sessões e eventos
```

Os agentes são definidos em [aurora/agents.py](aurora/agents.py), na função `build_app`. Todos usam Gemini; o padrão é `gemini-2.5-flash`, configurável por `GEMINI_MODEL`.

| Agente | Responsabilidade e acionamento |
| --- | --- |
| `aurora` | Recebe a conversa e encaminha com `transfer_to_agent`. Não recebe o regulamento nem os dados dos moradores nas instruções. |
| `reservas` | Consulta áreas, disponibilidade e reservas; cria e cancela reservas por tools. Também consulta visitantes quando o pedido combina as duas listas. |
| `portaria` | Lista visitantes e solicita autorização de entrada por tools. |
| `regulamento` | Consulta os artigos pertinentes pela tool `consultar_regulamento` e responde com a referência do artigo. |

A separação limita as ferramentas de cada especialista. Após uma transferência, o especialista pode continuar a conversa e encaminhar um novo assunto ao principal ou a outro especialista. As transferências para o agente pai ficam habilitadas: essa configuração permite que o Runner retome a confirmação no autor correto, inclusive ao carregar a sessão do SQLite. `App` usa `ResumabilityConfig(is_resumable=True)`; a resposta de confirmação informa o `invocation_id` original. Os testes cobrem essa combinação com a versão fixada.

Em [aurora/runtime.py](aurora/runtime.py), o `Runner` recebe mensagens e respostas nativas de confirmação. `DatabaseSessionService` persiste estado e eventos completos em `storage/sessoes.sqlite3`. Reservas, visitantes, vínculo entre sessão e apartamento e recibos de confirmação ficam em `storage/condominio.sqlite3`. Os dois bancos são locais e dispensam Docker ou outro serviço.

Os arquivos de `dados/` são o estado inicial e a fonte do regulamento. Nenhuma operação os modifica. O banco é inicializado uma única vez; subir a API novamente preserva as alterações. A restauração é um comando separado.

## Garantias

### 1. Cobrança e acesso exigem confirmação do sistema

Em [aurora/tools.py](aurora/tools.py), `reservar_area` lê a taxa do catálogo e chama `tool_context.request_confirmation(...)` quando ela é positiva. `autorizar_visitante` sempre solicita confirmação. Ambas devolvem `aguardando_confirmacao` antes de qualquer gravação. Reservar a quadra e cancelar uma reserva própria seguem diretamente para o banco.

O fluxo em [aurora/runtime.py](aurora/runtime.py) é:

1. `sync_confirmations` extrai `adk_request_confirmation` dos eventos persistidos, com o ID da chamada original, o ID da execução e os detalhes da ação.
2. A API devolve todas as pendências da sessão em `confirmacoes_pendentes`.
3. `confirm` aceita somente um ID pendente nessa sessão e monta um `FunctionResponse(name="adk_request_confirmation", id=..., response=...)`.
4. O `Runner` retoma a execução original e reexecuta a tool com a decisão do morador.

Em [aurora/store.py](aurora/store.py), `answer` usa `BEGIN IMMEDIATE` e consulta `WHERE id=? AND session_id=? AND respondida=0`. Um ID inexistente, de outra sessão ou já respondido gera `ConfirmationConflict`, convertido em **409** na API.

`execute` exige a decisão registrada pela rota e a confirmação nativa. A permissão fica vinculada à sessão, à chamada original, à ação e aos detalhes exatos. O efeito e o recibo são gravados **na mesma transação**. Uma execução repetida devolve o recibo, sem repetir o efeito. Negar grava somente o recibo da recusa. Texto como "já confirmei" não cria essa permissão. Enquanto há pendência, novas mensagens devolvem a lista existente e orientam o uso da rota de confirmações.

### 2. O apartamento é imutável e vem da sessão

`Runtime.create` define `state={"apartamento": apartment}` e registra o vínculo persistente. `CondoTools.identity`, em [aurora/tools.py](aurora/tools.py), obtém a identidade de `tool_context.state` e a compara com o vínculo pelo ID da sessão. Uma divergência impede a operação. O trigger `immutable_apartment` impede alterar esse vínculo no banco.

Nenhuma tool expõe um parâmetro `apartamento` ao modelo. Em [aurora/store.py](aurora/store.py), `reservations`, `visitors` e `cancel` filtram pelo apartamento validado. A consulta de disponibilidade usa apenas `SELECT 1` e retorna livre/ocupada, sem nome, apartamento ou código do ocupante. `execute` confere o vínculo novamente na transação de gravação.

As rotas `/apartamentos/...` são as rotas administrativas de verificação exigidas pelo desafio. Elas não são tools dos agentes. A autenticação está fora do escopo: na API deste exercício, criar uma sessão representa a autenticação do apartamento.

### 3. Conversas e dados sobrevivem ao reinício

[aurora/runtime.py](aurora/runtime.py) configura `DatabaseSessionService` com `sqlite+aiosqlite` em arquivo. `session` carrega os mesmos eventos e estado após reiniciar. A rota de eventos retorna `event.model_dump(mode="json")`, preservando conteúdo completo, chamadas, respostas, ações e IDs.

[aurora/store.py](aurora/store.py) mantém as alterações no SQLite em disco. O marcador `seeded` impede reinicializar os dados na subida. Cancelamentos marcam `ativa=0`, preservando o código na chave primária. Novos códigos usam UUID e têm unicidade garantida pelo banco, inclusive contra reservas canceladas. `sync_confirmations` pode reconstruir pendências a partir dos eventos.

Os testes reiniciam o runtime com os mesmos bancos, verificam igualdade dos eventos, enviam uma nova mensagem e aprovam reservas e visitantes pendentes.

### 4. Só os artigos pertinentes entram no contexto

[aurora/regulation.py](aurora/regulation.py), classe `Regulation`, divide `dados/regulamento.md` por capítulo e artigo em memória local. `search` identifica o assunto e devolve **no máximo dois artigos de um único capítulo**. Sem assunto identificado, pede esclarecimento; nunca usa o documento inteiro como fallback.

`consultar_regulamento`, em [aurora/tools.py](aurora/tools.py), usa a última pergunta textual do morador nos eventos. O modelo não pode fornecer uma consulta alternativa nem solicitar todos os capítulos por um parâmetro da tool. A pergunta sobre piscina aos domingos recupera o capítulo IV, incluindo o art. 22 e o fechamento às **20h**. Capítulos de outros assuntos não entram no resultado.

A busca é lexical e local, dispensando um serviço de embeddings. Perguntas sem assunto explícito podem exigir esclarecimento. Para perguntas com vários assuntos, o atendimento trata um capítulo por consulta.

### 5. Só uma reserva ativa por área e data

Em [aurora/store.py](aurora/store.py), a restrição é aplicada na gravação:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uma_reserva_ativa
    ON reservas(area, data) WHERE ativa = 1;
```

`execute` abre uma transação `BEGIN IMMEDIATE`; `_insert` tenta gravar a reserva. Se outra reserva ganhou a disputa, o SQLite recusa a segunda inserção e a tool devolve `status="ocupada"`. O fluxo termina com **200**, sem revelar a identidade do outro morador. A checagem antecipada serve para informar disponibilidade; a proteção efetiva é o índice único, que vale também entre processos distintos.

O teste de concorrência dispara duas aprovações em sessões de apartamentos diferentes; outro teste disputa uma reserva em dois processos independentes.

## Como rodar

### Pré-requisitos e configuração

- [uv](https://docs.astral.sh/uv/getting-started/installation/) instalado.
- Python 3.12 ou superior. `.python-version` seleciona 3.12; o uv pode instalá-lo.
- Chave do [Google AI Studio](https://aistudio.google.com/apikey) com acesso ao Gemini.

No clone do repositório:

```bash
cp .env.example .env
uv sync
```

Preencha `GOOGLE_API_KEY` no `.env`. O arquivo é ignorado pelo Git.

| Variável | Uso |
| --- | --- |
| `GOOGLE_API_KEY` | Obrigatória para conversar com o Gemini. Dispensável para restauração, consultas de verificação e testes locais. |
| `GEMINI_MODEL` | Opcional. Vazio usa `gemini-2.5-flash` em todos os agentes. |
| `AURORA_STORAGE_DIR` | Opcional. Vazio usa `storage/` na raiz. Permite separar ambientes e testes. |

Confira o [catálogo oficial de modelos](https://ai.google.dev/gemini-api/docs/models) e os [limites do projeto](https://aistudio.google.com/usage?tab=rate-limit) antes da avaliação. Disponibilidade e cotas dependem do projeto. Use a chave do Google AI Studio e execute sem configuração de Vertex AI no ambiente.

### Restaurar e subir

Com a API **parada**, restaure os dados:

```bash
uv run aurora-reset
```

Esse comando restaura reservas e visitantes a partir de `dados/` e **apaga sessões e confirmações** do diretório configurado. Não altera os arquivos originais.

Suba a API:

```bash
uv run aurora-api
```

A API responde em **http://localhost:8000**, com documentação interativa em http://localhost:8000/docs. Para reiniciar, use Ctrl+C e repita somente `uv run aurora-api`, mantendo o mesmo diretório de armazenamento.

O comando usa um worker. Requisições de uma mesma sessão são serializadas para preservar a ordem do histórico; sessões distintas podem conversar simultaneamente. A restrição de reservas fica no banco e independe desse bloqueio por sessão.

### Contrato e exemplo

| Método | Rota | Resultado |
| --- | --- | --- |
| POST | `/sessoes` | `201`, com `session_id` |
| POST | `/sessoes/{session_id}/mensagens` | `200`, com `resposta` e `confirmacoes_pendentes` |
| POST | `/sessoes/{session_id}/confirmacoes` | `200` no mesmo formato; `409` se o ID não estiver pendente nessa sessão |
| GET | `/sessoes/{session_id}/eventos` | `200`, lista completa de eventos em ordem |
| GET | `/apartamentos/{apartamento}/reservas` | `200`, reservas ativas com `codigo`, `area` e `data` |
| GET | `/apartamentos/{apartamento}/visitantes` | `200`, visitantes com `nome` e `data` |

Sessões inexistentes recebem `404` em todas as rotas que usam `session_id`. Entradas inválidas recebem `422`. Apartamento desconhecido na criação recebe `422`; suas rotas de verificação devolvem lista vazia. Indisponibilidade do modelo recebe `503`. Se o modelo falhar após o efeito confirmado ter sido gravado, a API devolve `200` com o resultado do recibo persistido.

Exemplo em Python, usando `httpx` já instalado por `uv sync`:

```python
import httpx

api = httpx.Client(base_url="http://localhost:8000", timeout=120)
session_id = api.post("/sessoes", json={"apartamento": "101"}).json()["session_id"]
reply = api.post(f"/sessoes/{session_id}/mensagens", json={
    "texto": "Reserve o salão de festas para 2030-04-20."
}).json()
print(reply)
id = reply["confirmacoes_pendentes"][0]["id"]
print(api.post(f"/sessoes/{session_id}/confirmacoes", json={
    "id": id, "confirmado": True
}).json())
print(api.get("/apartamentos/101/reservas").json())
```

### Verificação

```bash
uv run pytest -q
uv run ruff check aurora tests scripts
uv run ruff format --check aurora tests scripts
```

Os testes em [tests/](tests/) usam SQLite temporário e o Runner real do ADK. Somente o modelo é substituído por respostas programadas; não precisam de chave nem consomem tokens. Cobrem regras, contrato, retomada nativa de confirmações, reinício, isolamento, recibos, concorrência e recuperação de artigos. Eles não atestam a interpretação de linguagem natural de um modelo remoto.

Para testar conversas com o Gemini real, com `.env` preenchido:

```bash
uv run python scripts/smoke_gemini.py
```

O script sobe uma API em porta local temporária, usa bancos separados e encerra o processo ao terminar. Consome a cota da chave configurada e verifica pedidos, confirmações, acesso, regulamento, reinício e concorrência. A execução pode falhar por cota ou disponibilidade; o erro é informado, sem substituir o Gemini por uma resposta simulada.

Referências: [confirmação de ações no ADK](https://adk.dev/tools-custom/confirmation/) e [código do Runner na versão fixada](https://github.com/google/adk-python/blob/v2.9.1/src/google/adk/runners.py). A documentação alerta para limitações de confirmação com `DatabaseSessionService`; por isso os testes exercitam a topologia, o serviço persistente e a retomada juntos.
