# AutoCare Motors support and visit booking

A Telegram bot for a fictional Bengaluru dealership. It answers product questions from the dealership catalog, searches the web through Serper, and lets customers review and approve a showroom visit.

## Run locally

Use Python 3.11 or newer. Run the setup commands from the project directory on macOS or Linux.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n .env.example .env
```

Set `GROQ_API_KEY` in `.env` and keep `GROQ_MODEL=openai/gpt-oss-120b` for the demo. Add `SERPER_API_KEY` for web search. For email, set `SMTP_HOST`, `SMTP_PORT`, and `SMTP_FROM_EMAIL`, plus `SMTP_USER` and `SMTP_PASSWORD` if the server requires login. The app uses implicit TLS on port 465 and STARTTLS on other ports, including the default 587. If email is unconfigured or delivery fails, the confirmed booking still appears in chat and in the booking database.

Start the API in one terminal.

```sh
source .venv/bin/activate
uvicorn backend.api:app --host 127.0.0.1 --port 8000
```

Create a bot in [@BotFather](https://t.me/BotFather) with `/newbot`, then put the token in `TELEGRAM_BOT_TOKEN` in `.env`. Run the bot in a second terminal.

```sh
source .venv/bin/activate
python -m frontend.telegram_bot
```

Open your bot in Telegram and send `/start`. The bot publishes a five-item capability list for empty chats at startup. Use `/start` or `/help` for the welcome card in an existing chat, `/status` for the latest reply or pending approval, and `/new` to start a fresh conversation. The bot uses long polling, so FastAPI can stay on localhost. Run one polling process per bot token.

Telegram conversations have their own persistent thread IDs in `data/telegram_sessions.sqlite`. Existing Streamlit conversation histories remain in the checkpoint database. Customers can manage existing bookings in Telegram using the booking ID and original email.

## Admin dashboard

Start the separate Streamlit process from the project directory.

```sh
source .venv/bin/activate
python -m streamlit run frontend/admin_app.py --server.address 127.0.0.1 --server.port 8502
```

Open [the admin dashboard](http://127.0.0.1:8502). The Bookings and Customers tabs show the records in the shared SQLite database. Use the Cars tab to add a car or select an existing car to edit or remove. The dashboard connects directly to SQLite and can run independently of FastAPI. Keep it on localhost because it contains customer details and has no login.

## Try the demo

1. Ask “Which cars do you have?” then “Which is best for a family?”
2. Ask “Search online for some reviews.” The AutoCare models are fictional. The agent can search for reviews of comparable real cars.
3. Ask “I'd like to see the Trailview tomorrow at 3 PM.” If the showroom is closed or the time is taken, ask for a date and time the agent finds available.
4. Give your name and email. Review the visit card and choose **Confirm**, **Reject**, or **Modify**. For **Modify**, describe your change in chat and review the new card.
5. To try recovery, restart the API while the approval card is visible. Send `/status` in the same Telegram chat and confirm the visit.
6. Start a new chat and ask “Find my booking.” Provide the confirmation ID and the original booking email. Then ask “Move it to 5 PM” or “Cancel my booking” and confirm the action on the review card.

The showroom uses Asia/Kolkata time, operates Monday to Saturday, and offers 45-minute visits starting at 11:00, 13:00, 15:00, and 17:00.

Rescheduling keeps the same booking ID and releases the original slot. Cancellation releases the slot and keeps the booking available for status lookup. Customers can change future bookings. Confirm saves the booking and replies immediately after the database commit. The API processes email and Google Calendar delivery in the background and records delivery status in the admin Bookings tab. SMTP confirmation is for new bookings. Reschedules and cancellations update the same Calendar event.

For a financing estimate, try “What’s the EMI for ₹5 lakh over five years?” The EMI server uses 8.5% annual interest. Customers asking for a discount are invited to discuss it at the showroom.

## Google Calendar invitations

Enable the Google Calendar API in your Google Cloud project and create an OAuth client. Authorize it as the showroom calendar owner using the `https://www.googleapis.com/auth/calendar.events` scope and offline access. Put the resulting OAuth client ID, client secret, and refresh token into `GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_CALENDAR_CLIENT_SECRET`, and `GOOGLE_CALENDAR_REFRESH_TOKEN` in `.env`. Set `GOOGLE_CALENDAR_ID` to the showroom calendar ID or use `primary` for the authorized account's main calendar. [Google OAuth setup](https://developers.google.com/identity/protocols/oauth2/web-server)

The integration creates the event on the showroom calendar and invites the email address on the booking. The recipient's Google Calendar invitation settings determine whether it appears automatically or after they respond. [Google Calendar invitation behavior](https://developers.google.com/workspace/calendar/api/concepts/inviting-attendees-to-events)

## Project layout

| Directory | Responsibility |
|---|---|
| `backend/` | FastAPI endpoints, booking contracts, and MCP tools |
| `ai/` | Agent graph, tool adapters, conversation policy, and prompt |
| `database/` | SQLite connections, schema, and queries |
| `frontend/` | Telegram customer bot and Streamlit admin dashboard |
| `core/` | Shared configuration, showroom information, and search policy |
| `data/` | Showroom knowledge JSON and local SQLite files |
| `tests/` | Integration and regression tests |

## How it works

- `ai/agent.py` runs a LangGraph loop that feeds tool results back to ChatGroq. The model can read the catalog, check availability, search through MCP, and propose a visit. The graph stops after six rounds of tool execution if the model keeps requesting tools.
- `ai/tools.py` uses `langchain-mcp-adapters` to discover tools from four FastMCP stdio servers. `backend/mcp_server.py` runs the dealership server by default and the Serper search server with the `serper` argument. `backend/calendar_mcp.py` delivers Calendar invitations and updates. `backend/emi_mcp.py` calculates financing estimates.
- A validated proposal pauses at `interrupt()`. Only a confirmed `Command(resume=...)` reaches the MCP write tools. The booking server rechecks availability and updates reservations in one SQLite transaction. Repeated writes are safe to retry, and pending edits become stale if another action changes the booking. Each new booking gets at most one SMTP attempt. If the server stops during the SMTP attempt, the app reports that delivery could not be verified.
- SQLite checkpoints live in `data/checkpoints.sqlite`. Booking data lives in `data/bookings.sqlite`, and car details come from its `cars` table. Dealership hours, services, and policies come from `data/knowledge.json`. Optional `CHECKPOINT_PATH` and `BOOKINGS_DB_PATH` overrides can point to files in an existing writable directory. Run one API worker. Set `API_URL` if the Telegram bot needs a different API address.

## Customer conversation and search limits

Edit `ai/prompts/customer.md` to adjust the bot's tone and conversation flow, then restart FastAPI. The bot checks availability before asking for booking contact details. General browsing and availability questions do not need customer information.

Qualifying automotive questions trigger a Serper search before the agent answers. Try “Hyundai Creta vs Kia Seltos”, “What's the latest Tata Nexon EV recall?”, “Search online for EV charging and range”, or “Look up tyre maintenance for cars.” The search supports reviews, comparisons, launches, recalls, safety, specifications, market prices, and ownership topics. Specific model searches use a curated list of public model names. AutoCare prices and inventory come from SQLite.

Serper receives a fixed query assembled from approved terms. It permits at most one search attempt per message. The default daily caps are 20 across the bot and 3 per Telegram customer, counted in UTC. Starting a new Telegram conversation does not reset the allowance. For API clients without a Telegram session, the cap applies per thread. Set `SERPER_DAILY_LIMIT` or `SERPER_THREAD_DAILY_LIMIT` in `.env` to change the caps. Setting either to zero disables paid searches.

## Booking storage

The `bookings` table references `customers` by customer ID. A case-insensitive email match links repeat bookings to one customer. The customer record reflects the latest name and email supplied, and each booking keeps the contact details used when it was made.

SQLite is the source of truth for bookings and customers. A new database starts with empty booking and customer tables and a seeded car catalog. The agent retrieves a booking using the customer's booking ID and original email, and checks availability against current reservations.

The SQLite connection, migrations, and booking queries live in `database/bookings.py`. The `cars` schema and its ten fictional sample cars live in `database/cars.py`. Edits made in the admin inventory are available to the agent on its next catalog lookup.

To browse the seeded cars from the project folder, run:

```sh
sqlite3 -readonly -header -column data/bookings.sqlite 'SELECT * FROM cars;'
```

## API

- `POST /chat` accepts a UUIDv4 `thread_id` and a `message`. Omit the message to retrieve an existing transcript and any pending approval. An optional `message_id` deduplicates redelivered messages within a thread.
- `POST /resume` accepts the same `thread_id`, the pending card's `approval_id` (or `booking_id` for a new booking), and a `decision` of `confirm`, `reject`, or `modify`. An invalid or superseded approval ID returns HTTP 409.

Both endpoints return `status` (`response` or `waiting_for_approval`), `message`, displayable `messages`, and the pending `approval` or null. FastAPI serves interactive documentation at `http://127.0.0.1:8000/docs`.

## LangSmith tracing

To enable tracing, set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` in `.env`. In an Agents and Environments workspace, set `LANGSMITH_AGENT_ID` to the ID shown in the console and `LANGSMITH_AGENT_ENVIRONMENT=local` for the Local environment. Set `LANGSMITH_ENDPOINT` to the endpoint shown by the console. Agent routing takes precedence over `LANGSMITH_PROJECT`. In a Tracing Projects workspace, leave the Agent ID blank and use `LANGSMITH_PROJECT`, which defaults to `autocare-motors`.

Restart FastAPI and send a chat message. Open the project's or agent environment's Tracing tab in [LangSmith](https://smith.langchain.com/) to see graph duration, individual Groq calls, input and output token counts, tool calls, and errors. The `chat`, `approval`, and `recovery` tags identify each workflow. [LangSmith tracing guide](https://docs.langchain.com/langsmith/trace-with-langchain)

Tracing covers backend graph execution. Telegram queue time and background email and calendar delivery are outside these traces. Chat text, tool inputs and outputs, customer identifiers, and raw exception details are redacted before upload.

## LangSmith evaluators

Run the synthetic regression scenarios locally or publish them as a LangSmith experiment.

```sh
python -m evaluations.run --case confirmed-booking --checks-only
python -m evaluations.run --publish
```

The runner uses `GROQ_API_KEY` for the agent. The publishing command uses `LANGSMITH_API_KEY` and creates a versioned synthetic dataset and an experiment. Open **Datasets & Experiments** in LangSmith to see one score per metric. Use `--case` to run one scenario. Use `--checks-only` to skip additional judge calls. Set `AUTOCARE_EVAL_JUDGE_MODEL` to select a separate Groq judge model. [LangSmith evaluation types](https://docs.langchain.com/langsmith/evaluation-types)

Rule-based scores check synthetic PII leakage, prompt and code injection, conciseness, task completion, plan adherence, tool selection, and trajectory accuracy. The judge scores safety, toxicity, bias and fairness, quality, hallucination, correctness, perceived error, estimated user satisfaction, tone, and knowledge retention on multi-turn cases. A score of 1 means the check passed. Satisfaction is a judge estimate. PII and injection checks cover the supplied scenarios and patterns.

The scenarios use temporary SQLite databases and the real agent and MCP tool paths. Paid search and external notification delivery are disabled during evaluations. Production LangSmith traces remain redacted, so content-based evaluation runs on these synthetic conversations.

## Verify

```sh
python -m unittest -v
```

The tests use real MCP stdio, LangGraph, and SQLite with temporary data, plus mocked Telegram, Groq, Serper, SMTP, and Google Calendar responses. Re-run the demo with credentials to verify live model behavior, web results, and notification delivery.
