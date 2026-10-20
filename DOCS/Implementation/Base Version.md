# Base Version

## Purpose and scope

This document records the current customer-shopping chatbot implementation: its dataset and storage, functions and their roles, implemented guardrails, user-facing features, configuration, and validation. The application is a terminal-based demonstration that answers questions using shopping records belonging to the mock-authenticated customer.

## Dataset and storage

### Source dataset

The application uses:

`data/customer_shopping_data.csv/customer_shopping_data.csv`

The CSV contains **99,457 purchase records** and these columns:

| Column | Meaning |
| --- | --- |
| `invoice_no` | Source invoice identifier |
| `customer_id` | Customer key used to associate purchases |
| `gender` | Demographic field in the source |
| `age` | Demographic field in the source |
| `category` | Product category |
| `quantity` | Units in the purchase |
| `price` | Purchase amount |
| `payment_method` | Payment method |
| `invoice_date` | Source invoice date |
| `shopping_mall` | Shopping mall |

The data was checked during implementation: it contains 99,457 distinct customer IDs in the `C` followed by six digits format.

### SQLite database

At application startup, `ShoppingDatabase` imports the fields required for supported shopping questions into `data/customer_shopping.sqlite3`. It records the CSV file's modification time and size and reloads the purchase table when either changes. SQLite is part of Python's standard library; no separate database service is required.

The local `purchases` table keeps only `customer_id`, `category`, `quantity`, `price`, `payment_method`, and `shopping_mall`. Invoice numbers, invoice dates, age, and gender are not stored in that table. A customer ID index supports customer-scoped lookups. SQL values, including the customer ID, are passed as query parameters.

The database constructs a permitted summary for one customer: purchase count, total units, total spend, average purchase value, and spend/count/unit aggregates by category, payment method, and mall. This summary—not raw purchase rows—is passed to Gemini.

## Configuration and running

The app reads `.env` without an additional package. Existing operating-system environment variables take precedence over `.env`.

| Setting | Purpose | Application fallback |
| --- | --- | --- |
| `GEMINI_API_KEY` | Google Gemini API key | Required; app exits with setup guidance if missing |
| `GEMINI_MODEL` | Gemini model name | `gemini-3.5-flash-lite` |
| `GEMINI_TIMEOUT_SECONDS` | Gemini request timeout in seconds | `120` |
| `DEMO_PIN` | Shared demo login PIN | `1234` |

Run from the repository root:

```powershell
python app.py
```

The terminal asks for a customer ID present in the CSV, then visibly prompts for the demo PIN. The PIN is shared across customers; it is a demonstration gate and not production authentication. `/logout` invalidates the current session and returns to login; `/help` describes supported topics; `/quit` or `/exit` exits.

**Configuration note:** The current application fallback and README specify `gemini-3.5-flash-lite`. The checked-in `.env.example` currently specifies `gemini-3.5-flash-lite`; if that template is copied to `.env`, its `GEMINI_MODEL` value overrides the application fallback. Check that setting against the model intended for the API account.

## Current user features

- Mock login requires an existing CSV customer ID and the configured shared PIN.
- The chatbot can answer questions about the authenticated customer's purchase totals, average purchase value, purchase count, units, category, payment methods, and shopping malls.
- Requests outside the dataset's supported shopping topics are declined before reaching Gemini.
- Responses are cleaned into readable multiline plain text. Markdown heading, bullet, quote, bold, italic, and inline-code decorations are removed.
- Gemini is instructed to use a short heading and one fact per line, not invent facts, and avoid naming a currency because the CSV does not define one.
- A rate limiter allows up to 8 requests per customer during a rolling 60-second window.
- Audit events are appended to `logs/guardrail_audit.jsonl`. They contain a timestamp, customer ID, event, outcome, and reason; prompts and purchase summaries are not logged.
- Gemini calls use the REST `generateContent` endpoint through Python's `urllib`, send the API key in the `x-goog-api-key` header, and use the configured timeout.

## Guardrails implemented

### 1. Mock authentication and session authorization

`ShoppingChatbot.login` normalizes the customer ID, verifies that it exists in the imported dataset, and compares the supplied ASCII PIN with the configured PIN using `hmac.compare_digest`. A successful login creates a random session token stored in an in-memory token-to-customer mapping. Each answer request checks that the token is active and bound to the same customer ID. Logout removes the token.

This prevents normal chatbot calls without a valid in-process session from retrieving a customer's summary. Since the PIN is shared and sessions exist only in process memory, this is mock authentication, not a production identity system.

### 2. Request screening, scope enforcement, and topic validation

`inspect_request` normalizes Unicode, removes invisible control characters, enforces a 500-character input limit, and checks for common prompt-injection patterns. It blocks patterns asking to override instructions, expose hidden prompts, disable protections, perform bulk or database extraction, request sensitive fields, or run SQL-like queries. Customer IDs in either `CUST_###` or `C######` form are checked against the authenticated ID; a different ID blocks the request.

`is_supported_question` separately requires at least one recognized shopping topic (purchases/spend, products/categories, payment, or malls/stores). Unsupported questions never reach Gemini. This topic check is keyword-based and is not a full natural-language intent classifier.

### 3. Customer-scoped data retrieval and minimization

Database queries always filter by the customer ID derived from the authenticated session, and use parameterized SQL values. User text is never executed as SQL. The chatbot builds only aggregate facts for that customer and sends those to Gemini; it does not send invoice IDs, dates, or demographic fields.

### 4. Model instruction and output checks

Gemini receives a system instruction to treat the question and supplied data as untrusted, follow the authorized summary only, avoid cross-customer or protected information, and report when the data cannot answer. `clean_response` normalizes output, removes control characters and Markdown decoration, and preserves useful line breaks. `inspect_response` blocks empty or over-2,000-character answers and answers matching configured patterns for customer/invoice identifiers, long numbers, email addresses, and protected terms. A blocked response is replaced with a generic message.

These are defense-in-depth checks, not a guarantee that all prompt injection or leakage can be detected. A supported-topic question and the permitted customer aggregate are sent to the configured Gemini service.

### 5. Rate limiting and audit

`RateLimiter` limits requests independently per customer to 8 per 60-second rolling window. `AuditLogger` records decision metadata without recording user prompts or purchase summaries.

## Source files and functions

### `app.py`

Contains configuration, data access, authentication/session handling, Gemini integration, audit, rate limiting, and the terminal entry point.

| Function or method | Purpose and how it is used |
| --- | --- |
| `load_environment(path)` | Reads simple `KEY=value` entries from `.env`; uses `os.environ.setdefault` so existing environment settings win. |
| `prompt_demo_pin()` | Reads the PIN using visible standard `input`, rather than a hidden-password prompt that may not work in some VS Code terminals. |
| `ShoppingDatabase.__init__(csv_path, database_path)` | Opens SQLite, configures row access, initializes or refreshes the database, and closes the connection if initialization fails. |
| `ShoppingDatabase._refresh_from_csv()` | Checks that the CSV exists and has the expected columns; compares source metadata; imports the required fields transactionally if the file changed. Rolls back and reports malformed data or database errors. |
| `ShoppingDatabase.customer_exists(customer_id)` | Uses a parameterized exact-match lookup to establish whether a customer has a purchase record. |
| `ShoppingDatabase.permitted_context(customer_id)` | Queries totals and grouped aggregates scoped to one customer; returns only the allowlisted summary used as Gemini context. |
| `ShoppingDatabase.close()` | Closes the SQLite connection during app shutdown. |
| `AuditLogger.log(customer_id, event, outcome, reason)` | Appends one JSONL audit event with timestamp and decision metadata. |
| `RateLimiter.__init__()` | Initializes per-customer request timestamp queues. |
| `RateLimiter.check(customer_id)` | Removes timestamps older than 60 seconds and either blocks or counts the new request under the limit of 8. |
| `GeminiChatClient.__init__(api_key, model, timeout_seconds)` | Stores API settings and rejects non-positive timeout values. |
| `GeminiChatClient.answer(prompt, context)` | Builds a Gemini `generateContent` request with system guidance and the customer's summary; sends it; parses text; and reports provider, network, timeout, invalid JSON, and empty-response errors explicitly. |
| `GeminiChatClient._timeout_message()` | Produces the timeout guidance, including retry, connection checks, and `GEMINI_TIMEOUT_SECONDS`. |
| `ShoppingChatbot.__init__(...)` | Creates the SQLite-backed data layer, PIN configuration, audit logger, limiter, Gemini client, and empty session registry. |
| `ShoppingChatbot.login(customer_id, pin)` | Validates customer and PIN, then creates and registers a random authenticated session token. |
| `ShoppingChatbot.logout(session)` | Removes a session token to invalidate it. |
| `ShoppingChatbot.answer(session, prompt)` | Checks session authorization, screens input, validates topic, enforces rate limits, retrieves scoped aggregates, calls Gemini, cleans and checks output, and audits outcomes. |
| `main()` | Loads configuration, validates the Gemini key and timeout, creates the chatbot, runs the login/chat command loop, reports errors, and closes the database on exit. |

### `injection.py`

Contains reusable request and response validation functions and the `GuardrailDecision` result type.

| Function or type | Purpose and how it is used |
| --- | --- |
| `GuardrailDecision` | Immutable result carrying `ALLOW`, `SANITIZE`, or `BLOCK`, normalized text, and a reason code. Shared by request screening and the chatbot flow. |
| `inspect_request(text, authenticated_customer_id)` | Normalizes user text, enforces input length, applies injection patterns, and rejects other-customer IDs. Returns a decision instead of passing rejected text to the model. |
| `is_supported_question(text)` | Checks for supported shopping-domain keywords before the chatbot spends a model request. |
| `clean_response(text)` | Normalizes model output, removes control characters and common Markdown decoration, and keeps multiline structure. |
| `inspect_response(text)` | Returns whether output is empty, too long, or contains configured sensitive identifier/field patterns. Used to decide whether an answer can be displayed. |

The module constants `MAX_INPUT_CHARS`, `MAX_RESPONSE_CHARS`, `INJECTION_PATTERNS`, and `SUPPORTED_TOPICS` configure the screening boundaries and patterns.

### `test_injection.py`

Uses Python `unittest`; test fixtures create small temporary CSV and SQLite files, so database behavior can be verified without editing the project's real dataset.

| Test fixture or test group | Coverage |
| --- | --- |
| `TestDataMixin.setUp()` | Creates a temporary CSV containing a few customers and purchases and assigns a temporary SQLite path. |
| `InspectRequestTests` | Verifies safe input, hidden-character normalization, instruction overrides, cross-customer and bulk extraction attempts, SQL requests, sensitive-field requests, excessive length, and allowed versus unsupported topics. |
| `InspectResponseTests` | Verifies detection of PAN-like values, customer/invoice IDs and email, acceptance of ordinary amounts, Markdown cleanup with line preservation, empty output, and excessive output length. |
| `ShoppingDatabaseTests` | Verifies CSV import, aggregate accuracy, customer isolation, omission of unused identifiers/demographics, exact parameterized customer lookup, refresh after CSV changes, and explicit failure for an invalid schema. |
| `EnvironmentTests.test_env_file_loads_values_without_overriding_environment()` | Verifies `.env` loading and environment-variable precedence. |
| `EnvironmentTests.test_pin_prompt_uses_visible_standard_input()` | Verifies the PIN prompt uses standard visible terminal input and strips surrounding whitespace. |
| `GeminiIntegrationTests.test_client_uses_gemini_generate_content_endpoint_and_key()` | Verifies Gemini URL, API-key header, request shape, model content parsing, and timeout value. |
| `GeminiIntegrationTests.test_http_error_includes_provider_message_and_redacts_key()` | Verifies provider errors are surfaced without exposing the API key. |
| `GeminiIntegrationTests.test_response_without_text_is_reported_explicitly()` | Verifies an empty model response produces a clear error. |
| `GeminiIntegrationTests.test_timeout_error_explains_how_to_retry_or_configure_timeout()` | Verifies direct and URL-wrapped timeout errors include recovery guidance. |
| `GeminiIntegrationTests.test_non_positive_timeout_is_rejected()` | Verifies invalid timeout settings fail early. |
| `ShoppingChatbotTests.setUp()` | Creates an isolated chatbot, substitutes a mock Gemini client, and suppresses writes to the real audit log for tests. |
| `ShoppingChatbotTests.test_login_requires_existing_customer_and_correct_pin()` | Verifies correct, incorrect, non-ASCII PINs and nonexistent customers. |
| `ShoppingChatbotTests.test_unauthenticated_session_cannot_query_data()` | Verifies a forged session cannot call the model. |
| `ShoppingChatbotTests.test_authenticated_question_sends_only_customer_aggregates()` | Verifies an authenticated question reaches the model with scoped summary data only. |
| `ShoppingChatbotTests.test_unsupported_question_never_reaches_model()` | Verifies request-topic validation runs before model access. |
| `ShoppingChatbotTests.test_cross_customer_prompt_never_reaches_model()` | Verifies cross-customer access attempts are blocked before model access. |
| `ShoppingChatbotTests.test_prompt_injection_never_reaches_model()` | Verifies an instruction override/bulk extraction prompt is blocked. |
| `ShoppingChatbotTests.test_leaky_model_response_is_not_displayed()` | Verifies a response containing another customer ID is replaced by the generic block message. |
| `ShoppingChatbotTests.test_model_response_is_cleaned_before_display()` | Verifies model formatting decorations are removed while line structure remains. |
| `ShoppingChatbotTests.test_logout_invalidates_session()` | Verifies a logged-out token cannot query the model. |

Run the tests from the repository root:

```powershell
python -m unittest test_injection.py
```

The most recent recorded validation for this implementation ran **34 tests successfully**. Python syntax and Pylance diagnostics were also checked with no reported issues.

## Limitations and follow-up considerations

- The mock PIN is shared, visible when entered, and not a real account credential.
- Session tokens are held in process memory and do not persist across restarts.
- Input screening and topic recognition use patterns/keywords; they may miss paraphrased attacks or reject legitimate wording.
- Output screening uses patterns and cannot guarantee that model output is free of every possible disclosure or error.
- The chatbot answers only about fields and aggregates included in its context; it does not provide invoice-level lookup or personal demographics.
- Gemini availability, model access, network conditions, and latency depend on the external API and connection.
- The CSV does not define a currency; the assistant is instructed not to invent one.
