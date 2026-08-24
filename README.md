# RedStore Discord Bridge

Backend que conecta o site RedStore ao servidor Discord. Ele executa a API web/OAuth2 e o bot Discord no mesmo processo.

## O que já está incluído

- Login e registro no site usando Discord OAuth2 (`identify` e `email`).
- Por padrão, somente membros do servidor configurado podem concluir o cadastro.
- Proteção contra CSRF no login com `state` assinado e expirável.
- Sessão HTTP em cookie `HttpOnly`, `SameSite=Lax` e opção `Secure`.
- Registro local da conta Discord e do e-mail vinculado em SQLite.
- Provisionamento da conta principal do RedStore com JWT, e-mail e avatar do Discord.
- Consulta de presença e cargos no servidor configurado.
- API interna protegida pelo header `X-RedStore-Api-Key`.
- API para adicionar/remover cargos, com checagem da hierarquia do bot.
- Comandos `/ping`, `/site`, `/verificar`, `/rank`, `/ranking` e `/robux` (também com prefixo quando aplicável).
- Sistema de tickets com painel, abertura de canal privado, assumir, notificar, renomear, fechar e logs.
- Comandos `!prova` e `/prova` para entregadores publicarem a prova da entrega.
- Endpoint `/health` para monitoramento.

## Configuração

1. Instale Python 3.11 ou superior.
2. Crie e ative um ambiente virtual:

   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

3. Copie `.env.example` para `.env` e preencha os valores.
4. No Discord Developer Portal:
   - crie uma aplicação;
   - copie o Client ID e Client Secret;
   - registre exatamente o `OAUTH_REDIRECT_URI` em OAuth2 > Redirects;
   - crie um bot e copie o token;
   - convide o bot para o servidor com os escopos `bot` e `applications.commands` e as permissões necessárias;
   - habilite o Server Members Intent e o Message Content Intent em Bot > Privileged Gateway Intents.
5. Configure `REDSTORE_API_URL` para a API Java principal e configure a mesma chave longa em
   `REDSTORE_BRIDGE_API_KEY` no bridge e `DISCORD_BRIDGE_API_KEY` na API principal.
6. Para usar `!prova` e `/prova`, configure `DELIVERER_ROLE_ID` com o ID do cargo Entregador. O bot também
   aceita `DELIVERER_ROLE_NAME=Entregador` quando o ID não for informado. Configure `PROOF_CHANNEL_ID`
   com o ID do canal onde as provas devem ser publicadas; se ficar `0`, a prova será publicada no canal
   em que o comando foi executado.
7. Execute:

   ```powershell
   python main.py
   ```

Novos pedidos pagos no site geram automaticamente um aviso no canal configurado em
`ORDER_NOTIFICATION_CHANNEL_ID`, mencionando o cargo `Entregador`. Se essa variável
ficar vazia, o bot usa `PROOF_CHANNEL_ID` como fallback. O backend precisa estar
configurado com `DISCORD_BRIDGE_API_KEY` e `DISCORD_BRIDGE_URL` apontando para este bridge.

Para receber uma DM quando um usuário clicar em “Já fiz o pagamento”, configure
`DEPOSIT_NOTIFICATION_DISCORD_ID` com o ID Discord do administrador responsável
por conferir os depósitos. A notificação informa o usuário e o valor solicitado.

O comando `/rank` (e `!rank`) mostra os depósitos confirmados do usuário e
sincroniza automaticamente seu cargo conforme a progressão abaixo. O comando
`/ranking` (e `!ranking`) publica uma única mensagem com o top 10 de usuários com
maior gasto, exibindo a menção do usuário, o cargo de depósito em texto e o total
gasto, sem ID do Discord ou quantidade de compras.

| Cargo | A partir de | ID |
| --- | ---: | --- |
| Plebeu | R$ 1 | `1540196431875276820` |
| Camponês | R$ 20 | `1540196669801635910` |
| Artesão | R$ 50 | `1540196877193060372` |
| Mercador | R$ 80 | `1540349040246525962` |
| Nobre | R$ 120 | `1540348193076682802` |
| Escudeiro | R$ 160 | `1540351414377779260` |
| Cavaleiro | R$ 210 | `1540351342512705587` |
| Barão | R$ 260 | `1540351462918463508` |
| Visconde | R$ 320 | `1540353613145051187` |
| Conde | R$ 380 | `1540353652827357264` |
| Marquês | R$ 450 | `1540353694707744848` |
| Duque | R$ 550 | `1540353747107188756` |
| Grão-Duque | R$ 650 | `1540353832179990649` |
| Príncipe | R$ 800 | `1540196429950091396` |
| Rei | R$ 1.000 | `1540353925302059028` |
| Arquiduque | R$ 1.250 | `1540353961863811184` |
| Imperador | R$ 1.500 | `1540354008919703613` |
| Soberano Imperial | R$ 2.000 | `1540354059830042625` |
| Imperador Supremo | R$ 2.500 | `1540354059838431242` |
| Lenda da Coroa | R$ 3.500 | `1540354156785700975` |
| Monarca Eterno | R$ 5.000 | `1540354251199479839` |

Os IDs podem ser sobrescritos no ambiente usando variáveis no formato
`DEPOSIT_<NOME>_ROLE_ID` (sem acentos). As variáveis antigas dos cinco primeiros
cargos continuam como fallback para não quebrar configurações existentes.

### Sistema de tickets

Para ativar os tickets, configure no `.env` os IDs de `TICKET_CATEGORY_ID`,
`TICKET_SUPPORT_ROLE_IDS`, `TICKET_MASTER_ROLE_ID` e `TICKET_LOG_CHANNEL_ID`.
Os IDs de cargos podem ser informados separados por vírgula. Depois de reiniciar
o bot, um membro autorizado publica o painel usando `/ticket`.

O bot precisa ter as permissões `Manage Channels`, `View Channel`, `Send Messages`,
`Read Message History`, `Manage Messages` (se necessário para a moderação) e
`Embed Links` na categoria/canal configurados. Os botões são persistentes e os
metadados do ticket ficam no SQLite, para que continuem funcionando após
reinicializações sem aparecerem no canal.

### Comandos `!prova` e `/prova`

Um entregador deve mencionar o cliente, informar o produto e anexar uma ou mais imagens da prova:

```text
!prova @cliente 600 Gamepass
```

O bot gera automaticamente `Venda #1`, `Venda #2` e assim por diante, mantendo a sequência no SQLite
mesmo após reinicializações. No formato slash, use `/prova`, preencha `cliente`, `produto` e `imagem`,
e use os campos opcionais `imagem_2` até `imagem_10` quando necessário. No formato `!prova`, basta
anexar várias imagens à mesma mensagem. O bot publica no canal configurado um embed com a menção do
cliente, produto, todas as imagens anexadas e horário da entrega. Comandos prefixados exigem que o
`Message Content Intent` esteja ativado no Discord Developer Portal.

Durante o desenvolvimento, defina `DISCORD_COMMAND_GUILD_ID` com o ID do servidor de testes. Assim, os slash commands são registrados diretamente nele e aparecem rapidamente. Se ficar vazio, eles serão globais e podem demorar para aparecer.

### Calculadora de Robux

Use `/robux` informando o preço de 1.000 Robux, o dinheiro disponível e a moeda de cada valor. O bot aceita ponto ou vírgula como separador decimal:

```text
/robux valor_k:5,00 dinheiro:20,00 moeda_k:real moeda_dinheiro:real
/robux valor_k:2,00 dinheiro:10,00 moeda_k:dolar moeda_dinheiro:dolar
/robux valor_k:2,00 dinheiro:50,00 moeda_k:dolar moeda_dinheiro:real
```

Também é possível usar `!robux 5,00 20,00 real real` ou `!robux 2,00 50,00 dolar real`. Quando as moedas forem diferentes, o bot consulta a PTAX do Banco Central, converte o orçamento e informa a cotação utilizada. O resultado é arredondado para baixo para não ultrapassar o orçamento.

## Fluxo do site

O botão de login deve apontar para:

```text
GET http://localhost:8000/auth/discord/login
```

Após a autenticação, o usuário retorna para:

```text
{SITE_URL}/auth/discord/success
```

O frontend pode consultar o usuário autenticado com `GET /api/v1/me`, enviando os cookies. A resposta inclui `is_member` e os cargos atuais.

## API interna

As chamadas do backend do RedStore devem enviar:

```http
X-RedStore-Api-Key: valor-de-INTERNAL_API_KEY
```

Essa chave é exclusiva para chamadas servidor-a-servidor. Nunca a coloque no frontend, em JavaScript público ou em requisições feitas diretamente pelo navegador do usuário.

Endpoints disponíveis:

- `GET /api/v1/guild` — lista o servidor e cargos gerenciáveis.
- `POST /api/v1/users/{discord_id}/sync` — consulta vínculo e cargos.
- `POST /api/v1/users/{discord_id}/roles` — altera cargo.

Exemplo de alteração de cargo:

```json
{
  "role_id": 123456789012345678,
  "action": "add"
}
```

## Produção

Defina `ENVIRONMENT=production`. Nesse modo, a aplicação recusa iniciar com credenciais ausentes, segredos padrão/fracos ou cookies sem `COOKIE_SECURE=true`. Use HTTPS, segredos aleatórios e uma URL pública de callback. Restrinja `CORS_ORIGINS` ao domínio real do RedStore e mantenha `.env` fora do controle de versão.

Execute somente um worker/processo desta aplicação em produção, porque cada processo inicia uma conexão própria com o Discord. Se precisar escalar a API, separe o bot em um serviço dedicado antes de usar múltiplos workers.
