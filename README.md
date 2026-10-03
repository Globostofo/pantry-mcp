# pantry-mcp

Remote MCP server exposing Grocy (the [pantry](https://github.com/Globostofo/pantry) service: food stock, recipes, meal plan, shopping list) to claude.ai (web, desktop, mobile) as a custom connector.

- Read tools: `stock`, `stock_alerts`, `product`, `recipes`, `recipe`, `meal_plan`, `shopping_list`, `query_objects` (raw rows of any Grocy entity except API keys and sessions), `system_info`.
- Write tools, taking names and units instead of ids (amounts are converted to the product's stock unit, the unit Grocy stores them in; writes only accept exact names):
  - master data: `create_product`, `create_recipe`;
  - stock: `add_to_stock`, `consume`, `open_product`, `transfer`, `inventory`, `undo` (reverts a stock change by its transaction id);
  - recipes, shopping list, meal plan: `cook_recipe`, `add_recipe_missing_to_shopping_list`, `shopping_list_add`, `shopping_list_remove`, `shopping_list_add_missing`, `shopping_list_clear`, `plan_meal`;
  - generic, for the rest of the API: `create_object`, `update_object`, `delete_object` (any editable Grocy entity except API keys).
- Talks to Grocy through the `heracles` Docker network (`http://pantry`), with a Grocy API key dedicated to this server. Grocy itself stays off the internet.
- Auth: the server is its own OAuth authorization server for claude.ai (dynamic client registration, same domain as the MCP endpoint) and delegates the actual login to the SSO (`auth.globostofo.net`) through a pre-registered OIDC client. It uses the SSO account, group and login flow shared by all of Romain's assistant connectors (`assistant`, see the heracles-brain README), plus a username check in the server.
- Publicly exposed on `pantry-mcp.globostofo.net`: an exception to the VPN-only rule, since claude.ai's servers are the ones calling the connector. A dedicated hostname (not `pantry.globostofo.net/mcp`) leaves `pantry.globostofo.net` free for Grocy's web UI.

## Prerequisites

- Docker + Docker Compose
- `heracles` Docker network (created by heracles-proxy)
- heracles-proxy and heracles-auth running
- pantry running and attached to the `heracles` network (see its README)
- The `assistant` account, group and `assistant-authentication` flow in the SSO (heracles-brain README, "Isolated access in the SSO")

## Setup

### 1. Grocy API key

**[dev — browser, on the LAN or through the VPN]** In Grocy (`http://192.168.1.123:9283`): user menu → Manage API keys → Add, description `pantry-mcp`. Keep the key for `.env`.

### 2. SSO provider and application

**[dev — browser, via VPN]** In the admin panel (`http://192.168.1.123:9000/if/admin/`):

1. **Signing key**: System → Certificates → Generate: common name `pantry-mcp-signing`, validity `3650` days, private key algorithm **RSA** (the server verifies RS256 by default).
2. **Provider**: Applications → Providers → Create → OAuth2/OpenID Provider
   - Name: `pantry-mcp`
   - Authentication flow: **`assistant-authentication`**
   - Authorization flow: `default-provider-authorization-explicit-consent`
   - Client type: **Confidential**
   - Redirect URIs: Strict, `https://pantry-mcp.globostofo.net/auth/callback`
   - Signing key: **`pantry-mcp-signing`**; Encryption key: empty
   - Scopes (Advanced protocol settings): `openid`, `email`, `profile` **and `offline_access`** (without it no refresh token is issued, and claude.ai is logged out after 5 minutes)
   - Refresh token validity: `days=90`
   - Note the Client ID and Client Secret.
3. **Application**: Applications → Applications → Create, name `pantry-mcp`, slug **`pantry-mcp`** (part of `OIDC_CONFIG_URL`), provider `pantry-mcp`. Then Policy / Group / User Bindings → Bind existing → Group → **`assistant`**. Without a binding, the application would be open to every SSO user.

### 3. Environment

**[prod — RPi]**

```bash
git clone git@github.com:Globostofo/pantry-mcp.git ~/pantry-mcp
cd ~/pantry-mcp
cp .env.example .env

# JWT_SIGNING_KEY
openssl rand -base64 36

# Fill in OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, JWT_SIGNING_KEY and GROCY_API_KEY
nano .env
```

## Deploy

**[prod — RPi]**

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose logs -f pantry-mcp
```

OAuth state (registered clients, tokens) lives in the `oauth` volume, encrypted with `JWT_SIGNING_KEY`.

Update (back up Grocy first when the update changes write tools, see the pantry README):

```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

In claude.ai, tools marked destructive (`consume`, `inventory`, `undo`, `cook_recipe`, `shopping_list_remove`, `shopping_list_clear`, `update_object`, `delete_object`) can be set to ask for approval in the connector settings.

## Check

**[prod — RPi]** Grocy is reachable from the container (must print the Grocy version):

```bash
docker compose exec pantry-mcp python -c "import os, httpx; print(httpx.get('http://pantry/api/system/info', headers={'GROCY-API-KEY': os.environ['GROCY_API_KEY']}).json())"
```

**[dev]**

```bash
# Both must point to https://pantry-mcp.globostofo.net (not to the SSO domain)
curl -s https://pantry-mcp.globostofo.net/.well-known/oauth-protected-resource/mcp
curl -s https://pantry-mcp.globostofo.net/.well-known/oauth-authorization-server

# Must return 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://pantry-mcp.globostofo.net/mcp
```

## Add the connector in claude.ai

**[claude.ai — browser]** Settings → Connectors → Add custom connector:

- Name: `Pantry`
- URL: `https://pantry-mcp.globostofo.net/mcp`
- Leave the advanced OAuth settings empty (the server supports dynamic client registration)

Then Connect, log in with the `assistant` account (password + second factor) and approve. If the browser already has an SSO session with another account, log out of `auth.globostofo.net` first, or connect from a private window.

## Development

**[dev]**

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Tests run against an in-memory fake Grocy (`httpx.MockTransport`): no network access.

Run locally without auth, against the real Grocy (on the LAN or through the VPN; refused when `BASE_URL` is https):

```bash
PANTRY_NO_AUTH=1 BASE_URL=http://localhost:8000 \
  GROCY_URL=http://192.168.1.123:9283 GROCY_API_KEY=<key> .venv/bin/pantry-mcp

npx @modelcontextprotocol/inspector   # connect to http://localhost:8000/mcp
```
