# Customer Shopping Assistant

A Google Gemini-backed terminal chatbot that answers questions using the customer shopping CSV in a local SQLite database. It includes mock login, customer-scoped data retrieval, prompt-injection screening, request validation, and response leakage checks.

## Data and database

The source dataset is `data/customer_shopping_data.csv/customer_shopping_data.csv`. On startup, the app imports the fields needed for shopping questions into `data/customer_shopping.sqlite3`; it refreshes the local database when the CSV changes. SQLite is included with Python, so no database package or service is required. Invoice numbers, dates, age, and gender are not retained in the local database.

Every purchase query is parameterized and filtered by the authenticated customer ID. The assistant receives only aggregated purchase totals grouped by product category, payment method, and shopping mall. Customer IDs, invoice IDs, age, and gender are not sent to Gemini. Each valid question and that customer's summary are sent to the configured Gemini API; do not use data unless you are authorized to share it with that service.

## Configure and run

Add your Google Gemini API key to the root `.env` file:

```dotenv
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_TIMEOUT_SECONDS=120
DEMO_PIN=1234
```

Then run from this folder:

```powershell
python app.py
```

Values already set in the environment take precedence over `.env`. `.env` is excluded from Git; `.env.example` is a safe template.

At the prompt, enter a customer ID present in the CSV and the shared demo PIN (`1234` by default, configurable with `DEMO_PIN`). The PIN is entered visibly using the standard terminal prompt (it is not masked). Use `/logout` to end the session and sign in as another customer, `/help` for supported topics, or `/quit` to exit.

## Supported questions

Ask about your own:

- Purchase total, average purchase value, number of purchases, or units purchased.
- Spend by category or products purchased.
- Payment methods.
- Shopping malls.

For example: `How much did I spend?`, `Which category did I spend the most on?`, or `Which mall did I shop at?`. Questions outside these dataset topics are declined.

## Guardrails and limitations

The app requires a customer ID and mock PIN, validates questions against supported shopping topics, blocks common prompt-injection and cross-customer requests, and checks model responses for customer/invoice identifiers and other sensitive data before displaying them. The database queries are constructed by the application; user input is never executed as SQL. Audit events record outcomes without logging prompts or purchase summaries.

The mock PIN is shared and is not real authentication. Pattern-based injection detection can miss attacks and cause false positives, and response checks cannot guarantee that a language model will always be correct. This is an educational prototype, not production security. Use synthetic data only.

## Troubleshooting Gemini API errors

Check that `GEMINI_API_KEY` is a valid Google AI Studio API key and that `GEMINI_MODEL` is available to it. The request timeout defaults to 120 seconds; increase `GEMINI_TIMEOUT_SECONDS` in `.env` if the API regularly takes longer. If it still times out, retry, check your internet/VPN/proxy connection and Gemini service status, and verify that the configured model is available. Never paste your API key into a chat, issue, or log; provider errors are sanitized before display.

## Tests

```powershell
python -m unittest test_injection.py
```
